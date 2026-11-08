#!/usr/bin/env python3
"""Alertes Vinted UK -> Telegram : notifie chaque nouvel article publié."""

import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode

import requests

# ==================== CONFIGURATION ====================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_URL = "https://www.vinted.co.uk"                      # vitrine : session/cookies
API_URL = "https://api.vinted.co.uk/svc-catalogue/items"   # API catalogue
LOCALE = "en-GB"
CHECK_INTERVAL = 60
STATE_FILE = Path("vinted_seen_items.json")

# Tailles acceptées -> vérification LOCALE avant chaque alerte (infaillible,
# même si l'API ignore le filtre taille). Matching tolérant : "L" accepte
# "L" et "L / UK 16-18" mais ni "XL" ni "Large". Vide set() = tout accepter.
ALLOWED_SIZES = {"L"}

# Paramètre du filtre taille côté API :
#   "auto"  -> teste les variantes et retient celle qui filtre RÉELLEMENT
#   "size_ids" ou "attribute_ids[size]" -> force une variante
SIZE_PARAM = "auto"

# Colle ici tes URLs de recherche Vinted (page catalog), telles quelles
SEARCH_URLS = [
    "https://www.vinted.co.uk/catalog?search_text=Colt&brand_ids[]=368277&size_ids[]=209&size_ids[]=1438&size_ids[]=1638&size_ids[]=1640&search_id=4252287858&order=newest_first",
]

SKIP_PROMOTED = True  # ignore les articles sponsorisés (souvent hors-sujet)

# ========================================================

JUNK_PARAMS = {"page", "time", "search_id", "order", "per_page"}

# Renommage des filtres dont l'API n'accepte pas le nom d'origine.
# size_ids est volontairement ABSENT : ses 2 variantes sont testées.
PARAM_MAP = {
    "brand_ids": "attribute_ids[brand]",
    "catalog_ids": "attribute_ids[catalog]",
    "status_ids": "attribute_ids[status]",
}

SIZE_VARIANTS = [
    ("size_ids[]", "size_ids"),                        # syntaxe du site web
    ("attribute_ids[size][]", "attribute_ids[size]"),  # famille attribute_ids
]
SIZE_KEYS = {key for _, key in SIZE_VARIANTS}

PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "Locale": LOCALE,
    "Platform": "web",
    "X-Next-App": "marketplace-web",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

CSRF_PATTERNS = [
    re.compile(r'name="csrf-token"\s+content="([^"]+)"'),
    re.compile(r'"csrf[_-]?token"\s*[:=]\s*"([^"]+)"'),
]

CURRENCY_SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}


def url_to_params(url):
    """URL de recherche Vinted -> paramètres API (avec renommage des filtres)."""
    params = {}
    for key, values in parse_qs(urlparse(url).query).items():
        base = key[:-2] if key.endswith("[]") else key    # retire les [] finaux
        new_key = PARAM_MAP.get(base, base)
        params[new_key] = values if len(values) > 1 else values[0]
    for junk in JUNK_PARAMS:
        params.pop(junk, None)
    return params


def encode_query(params):
    """Query string ; les listes sont encodées en clé[] répétée (style du site web)."""
    pairs = []
    for key, val in params.items():
        if isinstance(val, list):
            pairs += [(f"{key}[]", v) for v in val]
        else:
            pairs.append((key, val))
    return urlencode(pairs)


def new_session():
    """Session anonyme amorcée sur la vitrine : cookie access_token_web + X-Anon-Id (+ CSRF)."""
    s = requests.Session()
    s.headers.update(PAGE_HEADERS)
    r = s.get(BASE_URL, timeout=30)
    if "access_token_web" not in s.cookies:
        r = s.post(BASE_URL, timeout=30)  # fallback
    anon_id = r.headers.get("X-Anon-Id")
    if anon_id:
        s.headers["X-Anon-Id"] = anon_id
    for pattern in CSRF_PATTERNS:
        m = pattern.search(r.text or "")
        if m:
            s.headers["X-Csrf-Token"] = m.group(1)
            break
    s.headers.update(API_HEADERS)
    return s


def fetch_query(session, query):
    r = session.get(f"{API_URL}?{query}", timeout=30)
    if r.status_code == 404:
        raise RuntimeError("404 : endpoint introuvable — l'API Vinted a encore changé")
    if r.status_code in (403, 429):
        raise RuntimeError(f"bloqué par Vinted (HTTP {r.status_code}) — espace les scans")
    r.raise_for_status()
    data = r.json()
    items = data.get("items")
    if not isinstance(items, list):
        raise RuntimeError(f"réponse inattendue: {list(data)[:5]}")
    return items, r.url


# ---------- Extraction des champs (API sept. 2026) ----------

def get_item_box(item):
    box = item.get("item_box")
    return box if isinstance(box, dict) else {}


def extract_size(item):
    """Taille de l'article.

    Depuis sept. 2026 l'API catalogue renvoie size_title=null : la taille
    se trouve dans item_box.accessibility_label ("..., Size: L / UK 16-18, ...")
    ou item_box.second_line ("L / UK 16-18 · Very good").
    """
    size = item.get("size_title")
    if size:
        return str(size).strip()

    box = get_item_box(item)
    label = str(box.get("accessibility_label") or "")
    m = re.search(r"(?:Size|Taille)\s*:\s*([^,]+)", label)
    if m:
        return m.group(1).strip()

    second = str(box.get("second_line") or "")
    if "·" in second:                       # "L / UK 16-18 · Very good"
        return second.split("·", 1)[0].strip()
    return ""                               # pas de "·" = probablement la condition seule


def extract_brand(item):
    brand = item.get("brand_title")
    if brand:
        return str(brand)
    return str(get_item_box(item).get("first_line") or "")


def size_matches(size_title, allowed):
    """True si la taille correspond à l'une des tailles autorisées.
    'L' matche 'L' et 'L / UK 16-18', mais ni 'XL' ni 'Large'."""
    s = str(size_title or "").strip().lower()
    if not s or s == "?":
        return False
    for a in allowed:
        a = str(a).strip().lower()
        if not a:
            continue
        if s == a or re.match(rf"^{re.escape(a)}\b", s):
            return True
    return False


def choose_syntax(session, params, name):
    """Trouve la variante qui filtre RÉELLEMENT par taille.

    Vinted répond HTTP 200 même quand il ignore un filtre inconnu : on
    compare donc les tailles renvoyées avec filtre à une requête de
    référence SANS filtre taille.
    """
    print(f"[?] Détection du filtre taille pour '{name}' :")
    allowed = list(ALLOWED_SIZES)

    base_sizes = set()
    base = {k: v for k, v in params.items() if k != "size_ids"}
    try:
        base_items, _ = fetch_query(session, encode_query(base))
        base_sizes = {(extract_size(i) or "?").strip().lower() for i in base_items}
        print(f"    {'sans filtre':23} : {len(base_items):3} articles | "
              f"{len(base_sizes)} tailles distinctes")
    except Exception as e:
        print(f"    {'sans filtre':23} : échec ({e})")
    time.sleep(1)

    results = []  # (clé, nb_articles, filtre_effectif, tailles, hors_sélection)
    for label, key in SIZE_VARIANTS:
        p = dict(params)
        p[key] = p.pop("size_ids")
        try:
            items, _ = fetch_query(session, encode_query(p))
        except Exception as e:
            print(f"    {label:23} : échec — {e}")
            time.sleep(1)
            continue
        sizes = {(extract_size(i) or "?").strip().lower() for i in items}
        if allowed:
            hors = sorted(s for s in sizes if not size_matches(s, allowed))
            effectif = bool(sizes) and not hors
        else:
            hors = []
            effectif = bool(base_sizes) and sizes < base_sizes
        verdict = "✔ filtre effectif" if effectif else "✘ filtre ignoré"
        print(f"    {label:23} : {len(items):3} articles | "
              f"tailles : {', '.join(sorted(sizes)) or 'aucune'} | {verdict}")
        results.append((key, len(items), effectif, sizes, hors))
        time.sleep(1)

    if not results:
        raise RuntimeError("aucune variante du filtre taille n'a fonctionné (API bloquée ?)")

    bonnes = [r for r in results if r[2]]
    if bonnes:
        best = max(bonnes, key=lambda r: r[1])
        print(f"[✓] Filtre taille opérationnel via « {best[0]} »")
    else:
        avec_items = [r for r in results if r[1] > 0] or results
        best = min(avec_items, key=lambda r: (len(r[4]), -r[1]))
        print("[!] Aucune variante ne filtre réellement — le filtrage local "
              "(ALLOWED_SIZES) prend le relais")
    return best[0]


def build_item_url(item):
    """L'API renvoie un chemin relatif (/items/123...) -> URL complète."""
    url = item.get("url") or ""
    return url if url.startswith("http") else BASE_URL + url


def build_price(item):
    p = item.get("price") or {}
    amount = p.get("amount")
    if amount is None:
        return "N/D"
    sym = CURRENCY_SYMBOLS.get(p.get("currency_code", ""), p.get("currency_code", ""))
    return f"{sym}{float(amount):.2f}"


def extract_photo(item):
    photo = item.get("photo") or {}
    return photo.get("full_size_url") or photo.get("url")


# ---------- État ----------

def load_state():
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        if isinstance(data, list):                     # très ancien format
            return set(data), None
        return set(data.get("seen", [])), data.get("size_param")
    return set(), None


def save_state(seen, size_param=None):
    STATE_FILE.write_text(json.dumps({"seen": sorted(seen), "size_param": size_param}))


# ---------- Telegram ----------

def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    if not r.ok:
        print(f"[Telegram {r.status_code}] {r.text}")
    return r.ok


def send_alert(item, search_name):
    title = html.escape(item.get("title") or "Article")
    brand = html.escape(extract_brand(item) or "Sans marque")
    size = extract_size(item)
    price = build_price(item)
    url = build_item_url(item)
    photo = extract_photo(item)

    size_txt = f"\n📏 Taille : {html.escape(size)}" if size else ""
    text = (
        f"🔔 <b>Nouvelle annonce</b> — {html.escape(search_name)}\n\n"
        f"👕 {title}\n"
        f"🏷️ {brand}{size_txt}\n"
        f"💰 <b>{price}</b>\n"
        f"🔗 {url}"
    )

    if photo:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            json={"chat_id": TELEGRAM_CHAT_ID, "photo": photo,
                  "caption": text[:1024], "parse_mode": "HTML"},
            timeout=15,
        )
        if r.ok:
            return True
        print(f"[Telegram photo {r.status_code}] {r.text} -> fallback texte")
    return send_telegram(text)


# ---------- Scan ----------

def run_once(seen, alert=True, syntax=None):
    session = new_session()
    for url in SEARCH_URLS:
        params = url_to_params(url)
        name = params.get("search_text", "recherche")
        params["order"] = "newest_first"
        params["per_page"] = 96
        has_size = "size_ids" in params

        try:
            if has_size:
                if SIZE_PARAM in SIZE_KEYS:               # variante imposée
                    key = SIZE_PARAM
                else:                                     # auto
                    if not syntax or syntax not in SIZE_KEYS:
                        syntax = choose_syntax(session, params, name)
                    key = syntax
                p = dict(params)
                p[key] = p.pop("size_ids")
                try:
                    items, _ = fetch_query(session, encode_query(p))
                except Exception:
                    if SIZE_PARAM in SIZE_KEYS:
                        raise
                    print("[!] Variante en cache échouée -> nouvelle détection")
                    syntax = choose_syntax(session, params, name)
                    p = dict(params)
                    p[syntax] = p.pop("size_ids")
                    items, _ = fetch_query(session, encode_query(p))
            else:
                items, _ = fetch_query(session, encode_query(params))
        except Exception as e:
            print(f"[!] Erreur sur '{name}' : {e}")
            continue

        size_list = ", ".join(sorted({(extract_size(i) or "?").strip() for i in items}))
        print(f"[i] {name}: {len(items)} articles | tailles : {size_list or 'aucune'}")

        # Auto-diagnostic : si la taille reste introuvable, on dump la structure
        if items and all(not extract_size(i) for i in items[:10]):
            box = items[0].get("item_box") or {}
            print(f"[!] Taille introuvable — item_box du 1er article : "
                  f"{json.dumps(box, ensure_ascii=False)[:300]}")

        new_count = 0
        for item in items:
            if SKIP_PROMOTED and item.get("content_source") == "search_promoted_items":
                continue
            item_id = str(item.get("id", ""))
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)

            # Filet de sécurité : la taille doit correspondre à ALLOWED_SIZES
            size_title = extract_size(item)
            if ALLOWED_SIZES and not size_matches(size_title, ALLOWED_SIZES):
                print(f"  [skip] taille « {size_title or '?'} » : "
                      f"{str(item.get('title') or '')[:45]}")
                continue

            new_count += 1
            if alert:
                ok = send_alert(item, name)
                print(f"  -> {'OK' if ok else 'ÉCHEC'} : {item.get('title')}")
                time.sleep(1)

        print(f"[{'+' if new_count else '-'}] {new_count} nouveau(x) — {name}")
        time.sleep(2)
    save_state(seen, syntax)
    return syntax


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        sys.exit("Définis d'abord les variables d'environnement :\n"
                 "  export TELEGRAM_BOT_TOKEN='...' && export TELEGRAM_CHAT_ID='...'")

    if "--test" in sys.argv:
        ok = send_telegram("✅ Test : bot Vinted UK → Telegram opérationnel !")
        print("Message de test envoyé ✔️" if ok else "Échec ✖️")
        return

    seen, syntax = load_state()
    first = not seen

    if "--once" in sys.argv:              # mode GitHub Actions : une passe puis exit
        run_once(seen, alert=not first, syntax=syntax)
        return

    if first:
        print("Premier lancement : baseline des articles existants (pas d'alertes).")
        run_once(seen, alert=False, syntax=syntax)

    print(f"Surveillance active, scan toutes les {CHECK_INTERVAL}s...")
    while True:
        time.sleep(CHECK_INTERVAL)
        syntax = run_once(seen, alert=True, syntax=syntax)


if __name__ == "__main__":
    main()
