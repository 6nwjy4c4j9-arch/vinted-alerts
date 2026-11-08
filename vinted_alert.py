#!/usr/bin/env python3
"""Alertes Vinted UK -> Telegram : notifie chaque nouvel article publié."""

import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

# ==================== CONFIGURATION ====================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_URL = "https://www.vinted.co.uk"                     # vitrine : session/cookies
API_URL = "https://api.vinted.co.uk/svc-catalogue/items"   # API : nouvel hôte depuis sept. 2026
LOCALE = "en-GB"
CHECK_INTERVAL = 60
STATE_FILE = Path("vinted_seen_items.json")

# Colle ici tes URLs de recherche Vinted (page catalog)
SEARCH_URLS = [
    "https://www.vinted.co.uk/catalog?search_text=colt&search_id=4680351837&size_ids[]=208&page=1&time=1792332757&brand_ids[]=368277",
]

SKIP_PROMOTED = True  # ignore les articles sponsorisés (souvent hors-sujet)

# ========================================================

JUNK_PARAMS = {"page", "time", "search_id", "order", "per_page"}

# L'API ignore les anciens noms -> renommage obligatoire
PARAM_MAP = {
    "brand_ids": "attribute_ids[brand]",
    "size_ids": "attribute_ids[size]",
    "catalog_ids": "attribute_ids[catalog]",
    "status_ids": "attribute_ids[status]",
}

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
        base = key[:-2] if key.endswith("[]") else key   # brand_ids[] -> brand_ids
        new_key = PARAM_MAP.get(base, base)              # -> attribute_ids[brand]
        params[new_key] = values if len(values) > 1 else values[0]
    for junk in JUNK_PARAMS:
        params.pop(junk, None)
    return params


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


def fetch_items(session, params):
    r = session.get(API_URL, params=params, timeout=30)
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
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen)))


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
    brand = html.escape(item.get("brand_title") or "Sans marque")
    size = item.get("size_title") or ""
    price = build_price(item)
    url = build_item_url(item)
    photo = extract_photo(item)

    size_txt = f"\n📏 Taille : {html.escape(str(size))}" if size else ""
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

def run_once(seen, alert=True):
    session = new_session()
    for url in SEARCH_URLS:
        params = url_to_params(url)
        name = params.get("search_text", "recherche")
        params["order"] = "newest_first"
        params["per_page"] = 96

        try:
            items, req_url = fetch_items(session, params)
        except Exception as e:
            print(f"[!] Erreur sur '{name}' : {e}")
            continue

        print(f"[i] {name}: {len(items)} articles | {req_url}")

        new_count = 0
        for item in items:
            if SKIP_PROMOTED and item.get("content_source") == "search_promoted_items":
                continue
            item_id = str(item.get("id", ""))
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            new_count += 1
            if alert:
                ok = send_alert(item, name)
                print(f"  -> {'OK' if ok else 'ÉCHEC'} : {item.get('title')}")
                time.sleep(1)

        print(f"[{'+' if new_count else '-'}] {new_count} nouveau(x) — {name}")
        time.sleep(2)
    save_state(seen)


def main():
    if "--test" in sys.argv:
        ok = send_telegram("✅ Test : bot Vinted UK → Telegram opérationnel !")
        print("Message de test envoyé ✔️" if ok else "Échec ✖️")
        return

    seen = load_state()
    if not seen:
        print("Premier lancement : baseline des articles existants (pas d'alertes).")
        run_once(seen, alert=False)

    if "--once" in sys.argv:  # mode GitHub Actions : une passe puis exit
        return

    print(f"Surveillance active, scan toutes les {CHECK_INTERVAL}s...")
    while True:
        time.sleep(CHECK_INTERVAL)
        run_once(seen, alert=True)


if __name__ == "__main__":
    main()
