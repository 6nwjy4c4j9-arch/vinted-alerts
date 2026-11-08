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

# Tailles acceptées -> vérification LOCALE avant chaque alerte (infaillible).
# Matching tolérant : "L" accepte "L" et "L / UK 16-18", mais ni "XL" ni "Large".
# Plusieurs tailles : {"L", "W32", "36 in | 90 cm"} — vide set() = tout accepter.
ALLOWED_SIZES = {"L","W30","W32"}

# Paramètre du filtre taille côté API :
#   "auto"  -> teste les variantes et retient celle qui filtre RÉELLEMENT
#   "attribute_ids[size]" ou "size_ids" -> force une variante
SIZE_PARAM = "auto"

# Colle ici tes URLs de recherche Vinted (page catalog), telles quelles.
# Pas besoin de trier les size_ids : le script apprend lequel correspond
# à quelle taille et n'utilise que ceux qui matchent ALLOWED_SIZES.
SEARCH_URLS = [
    "https://www.vinted.co.uk/catalog?search_text=Colt&brand_ids[]=368277&size_ids[]=209&size_ids[]=1438&size_ids[]=1638&size_ids[]=1640&search_id=4252287858&order=newest_first",
]

SKIP_PROMOTED = True  # ignore les articles sponsorisés (souvent hors-sujet)

# ========================================================

JUNK_PARAMS = {"page", "time", "search_id", "order", "per_page"}

# Renommage des filtres dont l'API n'accepte pas le nom d'origine.
# size_ids est volontairement ABSENT : ses variantes sont testées.
PARAM_MAP = {
    "brand_ids": "attribute_ids[brand]",
    "catalog_ids": "attribute_ids[catalog]",
    "status_ids": "attribute_ids[status]",
}

# Variantes pour le filtre taille, par ordre de préférence.
# Format confirmé de la nouvelle API : CSV dans une seule valeur
# (attribute_ids[size]=209,1438) ; les formes répétées clé[]=v sont ignorées.
SIZE_VARIANTS = [
    ("attribute_ids[size] (csv)", "attribute_ids[size]"),
    ("size_ids[] (répété)", "size_ids"),
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


def encode_query(params, repeat_size=False):
    """Query string ; filtres multi-valeurs en CSV dans une seule valeur
    (format de la nouvelle API). repeat_size=True -> size_ids en clé[] répétée."""
    pairs = []
    for key, val in params.items():
        if isinstance(val, list):
            if repeat_size and key == "size_ids":
                pairs += [(f"{key}[]", v) for v in val]
            else:
                pairs.append((key, ",".join(str(v) for v in val)))
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
    """Taille : size_title (si présent), sinon item_box.accessibility_label
    ("..., Size: L / UK 16-18, ...") ou item_box.second_line ("L / UK 16-18 · Very good")."""
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
    return ""


def extract_brand(item):
    brand = item.get("brand_title")
    if brand:
        return str(brand)
    return str(get_item_box(item).get("first_line") or "")


def organic(items):
    """Retire les articles sponsorisés (ils ignorent les filtres)."""
    if not SKIP_PROMOTED:
        return items
    return [i for i in items if i.get("content_source") != "search_promoted_items"]


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


def normalize_ids(v):
    """size_ids peut être une string (1 ID) ou une liste -> liste de strings."""
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def choose_syntax(session, params, name, raw_ids):
    """Trouve la variante d'encodage qui filtre RÉELLEMENT par taille.

    Vinted répond HTTP 200 même en ignorant un filtre : on compare donc les
    tailles renvoyées avec filtre à une référence SANS filtre taille. Un
    filtre effectif réduit le nombre de tailles distinctes (les sponsorisés
    sont exclus de l'analyse : ils ignorent les filtres par conception).
    """
    print(f"[?] Détection du format du filtre taille pour '{name}' :")
    allowed = list(ALLOWED_SIZES)

    base_sizes, base_count = set(), 0
    try:
        base_items, _ = fetch_query(session, encode_query(params))
        org = organic(base_items)
        base_count = len(org)
        base_sizes = {(extract_size(i) or "?").strip().lower() for i in org}
        print(f"    {'sans filtre':26} : {base_count:3} articles | "
              f"{len(base_sizes)} tailles distinctes")
    except Exception as e:
        print(f"    {'sans filtre':26} : échec ({e})")
    time.sleep(1)

    results = []  # (clé, nb_articles, filtre_effectif)
    for label, key in SIZE_VARIANTS:
        p = dict(params)
        p[key] = raw_ids
        try:
            items, _ = fetch_query(session, encode_query(p, repeat_size=(key == "size_ids")))
        except Exception as e:
            print(f"    {label:26} : échec — {e}")
            time.sleep(1)
            continue
        org = organic(items)
        sizes = {(extract_size(i) or "?").strip().lower() for i in org}
        if base_sizes:
            effectif = bool(sizes) and sizes < base_sizes
            detail = f"({len(base_sizes)} -> {len(sizes)} tailles)" if effectif else ""
        elif allowed:
            effectif = bool(sizes) and all(size_matches(s, allowed) for s in sizes)
            detail = ""
        else:
            effectif, detail = False, ""
        verdict = f"✔ filtre effectif {detail}" if effectif else "✘ filtre ignoré"
        print(f"    {label:26} : {len(org):3} articles | "
              f"tailles : {', '.join(sorted(sizes)) or 'aucune'} | {verdict}")
        results.append((key, len(org), effectif))
        time.sleep(1)

    if not results:
        raise RuntimeError("aucune variante du filtre taille n'a fonctionné (API bloquée ?)")
    bonnes = [r for r in results if r[2]]
    if bonnes:
        best = max(bonnes, key=lambda r: r[1])
        print(f"[✓] Format retenu : « {best[0]} »")
    else:
        best = max(results, key=lambda r: r[1])
        print("[!] Aucun format ne filtre réellement — le filtrage local "
              "(ALLOWED_SIZES) prend le relais")
    return best[0]


def learn_size_ids(session, params, name, key, ids):
    """Sonde chaque size_id individuellement pour déduire sa taille réelle.

    Les IDs de l'URL ne correspondent pas toujours aux tailles voulues
    (ex. W30/W32 mélangés à des L) : on lit la taille des résultats de
    chaque ID pris seul, et pick_size_ids ne gardera que les utiles.
    """
    print(f"[?] Apprentissage des IDs de taille pour '{name}' :")
    mapping = {}
    for sid in ids:
        p = dict(params)
        p[key] = [sid]
        count, sizes = 0, []
        try:
            items, _ = fetch_query(session, encode_query(p, repeat_size=(key == "size_ids")))
            org = organic(items)
            count = len(org)
            sizes = sorted({(extract_size(i) or "?").strip().lower() for i in org})
        except Exception as e:
            print(f"    ID {sid:6} : échec — {e}")
        mapping[sid] = sizes
        keep = not sizes or any(size_matches(s, ALLOWED_SIZES) for s in sizes)
        print(f"    ID {sid:6} : {count:3} articles | "
              f"tailles : {', '.join(sizes) or 'aucune'} | "
              f"{'✔ gardé' if keep else '✘ écarté'}")
        time.sleep(1)
    return mapping


def pick_size_ids(mapping, raw_ids):
    """Ne garde que les IDs dont la taille observée correspond à ALLOWED_SIZES
    (ou est inconnue/vide : le garde-fou local couvrira)."""
    good = []
    for sid in raw_ids:
        sizes = mapping.get(sid)
        if sizes is None or not sizes or any(size_matches(s, ALLOWED_SIZES) for s in sizes):
            good.append(sid)
    return good


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
            return set(data), None, None
        id_map = data.get("size_id_map")
        id_map = {str(k): list(v) for k, v in id_map.items()} if isinstance(id_map, dict) else None
        return set(data.get("seen", [])), data.get("size_param_v2"), id_map
    return set(), None, None


def save_state(seen, syntax=None, id_map=None):
    STATE_FILE.write_text(json.dumps(
        {"seen": sorted(seen), "size_param_v2": syntax, "size_id_map": id_map or {}}))


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

def run_once(seen, alert=True, syntax=None, id_map=None):
    session = new_session()
    for url in SEARCH_URLS:
        params = url_to_params(url)
        name = params.get("search_text", "recherche")
        params["order"] = "newest_first"
        params["per_page"] = 96
        raw_ids = normalize_ids(params.pop("size_ids", None))

        try:
            if raw_ids:
                # 1) Format d'encodage du filtre taille
                if SIZE_PARAM in SIZE_KEYS:
                    key = SIZE_PARAM
                else:
                    if not syntax or syntax not in SIZE_KEYS:
                        syntax = choose_syntax(session, params, name, raw_ids)
                    key = syntax

                # 2) Apprentissage : quel ID correspond à quelle taille ?
                id_map = id_map or {}
                missing = [sid for sid in raw_ids if sid not in id_map]
                if missing:
                    id_map.update(learn_size_ids(session, params, name, key, missing))
                good = pick_size_ids(id_map, raw_ids)
                use_ids = good or raw_ids
                if good:
                    print(f"[✓] IDs retenus (tailles autorisées) : {', '.join(good)}")
                else:
                    print("[!] Aucun ID ne correspond aux tailles autorisées "
                          "-> tous les IDs + garde-fou local")

                # 3) Requête principale
                p = dict(params)
                p[key] = use_ids
                try:
                    items, _ = fetch_query(session, encode_query(p, repeat_size=(key == "size_ids")))
                except Exception:
                    if SIZE_PARAM in SIZE_KEYS:
                        raise
                    print("[!] Requête échouée -> nouvelle détection du format")
                    syntax = choose_syntax(session, params, name, raw_ids)
                    key = syntax
                    p = dict(params)
                    p[key] = use_ids
                    items, _ = fetch_query(session, encode_query(p, repeat_size=(key == "size_ids")))
            else:
                items, _ = fetch_query(session, encode_query(params))
        except Exception as e:
            print(f"[!] Erreur sur '{name}' : {e}")
            continue

        org = organic(items)
        size_list = ", ".join(sorted({(extract_size(i) or "?").strip() for i in org}))
        print(f"[i] {name}: {len(org)} articles hors sponsorisés | tailles : {size_list or 'aucune'}")

        new_count = 0
        for item in org:
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
    save_state(seen, syntax, id_map)
    return syntax, id_map


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        sys.exit("Définis d'abord les variables d'environnement :\n"
                 "  export TELEGRAM_BOT_TOKEN='...' && export TELEGRAM_CHAT_ID='...'")

    if "--test" in sys.argv:
        ok = send_telegram("✅ Test : bot Vinted UK → Telegram opérationnel !")
        print("Message de test envoyé ✔️" if ok else "Échec ✖️")
        return

    seen, syntax, id_map = load_state()
    first = not seen

    if "--once" in sys.argv:              # mode GitHub Actions : une passe puis exit
        run_once(seen, alert=not first, syntax=syntax, id_map=id_map)
        return

    if first:
        print("Premier lancement : baseline des articles existants (pas d'alertes).")
        run_once(seen, alert=False, syntax=syntax, id_map=id_map)

    print(f"Surveillance active, scan toutes les {CHECK_INTERVAL}s...")
    while True:
        time.sleep(CHECK_INTERVAL)
        syntax, id_map = run_once(seen, alert=True, syntax=syntax, id_map=id_map)


if __name__ == "__main__":
    main()
