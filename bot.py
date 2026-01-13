import os
import re
import json
import time
import hashlib
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# OpenAI ist optional: wenn Key/Quota spinnt, nutzen wir Fallback-Parsing.
try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except Exception:
    OpenAI = None
    _OPENAI_AVAILABLE = False


# =========================
# CONFIG (via Env)
# =========================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))  # 5 min default
MIN_YEAR = int(os.getenv("MIN_YEAR", "2016"))
REQUIRE_IMAGES = os.getenv("REQUIRE_IMAGES", "true").lower() == "true"

# Blacklist (hart)
BLACKLIST_WORDS = [
    "unfall",
    "bastler",
    "motorschaden",
    "defekt",
    "export",
    "teileträger",
    "ohne tüv",
    "ohne tuv",
]

DATA_FILE = "data.json"

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
}

# =========================
# HELPERS: persistence
# =========================
def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}

def save_data(data: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def sha_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# =========================
# FILTERS
# =========================
def is_blacklisted(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in BLACKLIST_WORDS)

def extract_year(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

def year_ok(year: Optional[int]) -> bool:
    # Wenn kein Jahr erkennbar: raus (sauberer, weniger Müll)
    if year is None:
        return False
    return year >= MIN_YEAR

def images_ok(image_count: Optional[int]) -> bool:
    if not REQUIRE_IMAGES:
        return True
    return (image_count or 0) > 0


# =========================
# BUILD SEARCH URLS
# (Links sind vor allem für User-Klick – Scraping ist best-effort)
# =========================
def build_kleinanzeigen_url(query: str, max_price: int) -> str:
    q = urllib.parse.quote(query)
    return f"https://www.kleinanzeigen.de/s-autos/k0c216?keywords={q}&priceTo={max_price}"

def build_mobile_url(query: str, max_price: int, radius_km: int, location: str) -> str:
    # mobile ist bei make/model IDs nervig; wir nutzen keyword/search-text
    q = urllib.parse.quote(query)
    loc = urllib.parse.quote(location)
    return (
        "https://suchen.mobile.de/fahrzeuge/search.html"
        f"?isSearchRequest=true&vc=Car&dam=0&sb=rel"
        f"&maxPrice={max_price}"
        f"&radius={radius_km}"
        f"&location={loc}"
        f"&q={q}"
    )


# =========================
# SCRAPERS (best-effort)
# =========================
def fetch_html(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None

def parse_kleinanzeigen_listings(search_url: str) -> List[Dict[str, Any]]:
    """
    Best-effort parser: liefert Liste aus dicts:
    {title, url, snippet, image_count, year}
    """
    html = fetch_html(search_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    results: List[Dict[str, Any]] = []

    # Kleinanzeigen: viele Layouts, wir nehmen robuste Heuristiken
    # Suche nach <article> Karten
    cards = soup.find_all(["article", "li"])
    for c in cards:
        a = c.find("a", href=True)
        if not a:
            continue
        href = a["href"]
        if not isinstance(href, str):
            continue
        if "/s-anzeige/" not in href:
            continue

        url = "https://www.kleinanzeigen.de" + href if href.startswith("/") else href

        # title
        title = ""
        h2 = c.find(["h2", "h3"])
        if h2 and h2.get_text(strip=True):
            title = h2.get_text(" ", strip=True)

        # snippet/desc (best-effort)
        snippet = ""
        p = c.find("p")
        if p and p.get_text(strip=True):
            snippet = p.get_text(" ", strip=True)

        # image detection (best-effort): count <img> inside card
        imgs = c.find_all("img")
        image_count = len(imgs)

        year = extract_year(title + " " + snippet)

        if title:
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "image_count": image_count,
                    "year": year,
                    "source": "kleinanzeigen",
                }
            )

        if len(results) >= 20:
            break

    return results

def parse_mobile_listings(search_url: str) -> List[Dict[str, Any]]:
    """
    Best-effort parser für mobile.de Suchseite.
    Kann je nach Änderungen mal leer sein – dann skippen wir.
    """
    html = fetch_html(search_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    results: List[Dict[str, Any]] = []

    # mobile: Links enthalten oft /fahrzeuge/details.html?id=
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not isinstance(href, str):
            continue
        if "fahrzeuge/details.html" not in href:
            continue

        url = href if href.startswith("http") else "https://suchen.mobile.de" + href

        # title near anchor text
        title = a.get_text(" ", strip=True) or ""
        if len(title) < 6:
            continue

        # image: try find img near link
        parent = a.parent
        imgs = parent.find_all("img") if parent else []
        image_count = len(imgs)

        # snippet near title not easy; keep empty
        snippet = ""

        year = extract_year(title)

        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "image_count": image_count,
                "year": year,
                "source": "mobile",
            }
        )

        if len(results) >= 20:
            break

    return results


# =========================
# PARSING /set
# =========================
SYSTEM_PROMPT = f"""
Du bist ein Parser für Telegram-Befehle zur Autosuche.
Gib NUR JSON zurück.

Eingabeformat:
set/<marke+modell> <max_preis> <standort> <umkreis_km>

Beispiel:
set/audi a3 10000 Neustadt an der Weinstraße 300

JSON Format:
{{
  "query": "<marke+modell als string>",
  "max_price": 0,
  "location": "<standort als string>",
  "radius_km": 0
}}
"""

def fallback_parse_set(args: str) -> Optional[Dict[str, Any]]:
    """
    Fallback: erkennt 1. Zahl = max_price, letzte Zahl = radius,
    davor query, dazwischen location.
    """
    parts = args.strip().split()
    if len(parts) < 4:
        return None

    # find first int (price)
    price_idx = None
    for i, p in enumerate(parts):
        if p.isdigit():
            price_idx = i
            break
    if price_idx is None or price_idx == 0:
        return None

    # last token radius
    if not parts[-1].isdigit():
        return None

    max_price = int(parts[price_idx])
    radius_km = int(parts[-1])

    query = " ".join(parts[:price_idx]).strip()
    location = " ".join(parts[price_idx + 1 : -1]).strip()

    if not query or not location:
        return None

    return {
        "query": query.lower(),
        "max_price": max_price,
        "location": location,
        "radius_km": radius_km,
    }

def openai_parse_set(args: str) -> Optional[Dict[str, Any]]:
    if not (_OPENAI_AVAILABLE and OPENAI_API_KEY):
        return None
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        user_input = f"set/{args}"
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_input},
            ],
        )
        content = resp.choices[0].message.content.strip()
        data = json.loads(content)
        # minimal validation
        if not isinstance(data, dict):
            return None
        if not data.get("query") or not data.get("location"):
            return None
        data["max_price"] = int(data["max_price"])
        data["radius_km"] = int(data["radius_km"])
        data["query"] = str(data["query"]).strip().lower()
        data["location"] = str(data["location"]).strip()
        return data
    except Exception:
        return None


# =========================
# TELEGRAM TEXTS
# =========================
START_TEXT_DE = (
    "✅ AutoSuchBot online.\n\n"
    "🔍 Wie funktioniert der Bot?\n"
    "Du speicherst eine Suche mit /set. Dann checkt der Bot regelmäßig Kleinanzeigen & mobile.\n"
    f"Filter: ab Baujahr {MIN_YEAR}, nur mit Bildern, Blacklist.\n\n"
    "Befehle:\n"
    "/set audi a3 10000 Neustadt an der Weinstraße 300\n"
    "/list\n"
    "/del 1\n"
    "/stop\n"
)

START_TEXT_ES = (
    "✅ AutoSuchBot online.\n\n"
    "🔍 ¿Cómo funciona el bot?\n"
    "Guardas una búsqueda con /set. Luego el bot revisa Kleinanzeigen & mobile regularmente.\n"
    f"Filtros: desde año {MIN_YEAR}, fotos obligatorias, blacklist.\n\n"
    "Comandos:\n"
    "/set audi a3 10000 Neustadt an der Weinstraße 300\n"
    "/list\n"
    "/del 1\n"
    "/stop\n"
)


# =========================
# COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT_DE)
    await update.message.reply_text(START_TEXT_ES)

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    searches = data.get("users", {}).get(user_id, {}).get("searches", [])
    if not searches:
        await update.message.reply_text("Keine gespeicherten Suchen.")
        return

    msg = "📌 Deine Suchen:\n\n"
    for i, s in enumerate(searches, start=1):
        msg += f"{i}) {s['query']} bis {s['max_price']}€ | {s['location']} ({s['radius_km']}km)\n"
    await update.message.reply_text(msg)

async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    user = data.setdefault("users", {}).setdefault(user_id, {"searches": [], "seen": []})
    searches = user.get("searches", [])

    parts = (update.message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Nutzung: /del 1")
        return

    idx = int(parts[1]) - 1
    if idx < 0 or idx >= len(searches):
        await update.message.reply_text("Nummer ungültig.")
        return

    removed = searches.pop(idx)
    user["searches"] = searches
    save_data(data)
    await update.message.reply_text(f"✅ Gelöscht: {removed['query']}")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    if user_id in data.get("users", {}):
        data["users"][user_id]["searches"] = []
        save_data(data)
    await update.message.reply_text("🛑 Alle Suchen/Alerts gestoppt.")

async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    raw = (update.message.text or "").strip()

    # Telegram liefert "/set ..." (evtl. auch "/set@BotName ...")
    args = raw.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Format:\n/set <marke+modell> <maxpreis> <ort> <km>")
        return

    rest = args[1].strip()

    parsed = openai_parse_set(rest) or fallback_parse_set(rest)
    if not parsed:
        await update.message.reply_text("❌ Konnte nicht parsen.\nBeispiel:\n/set audi a3 10000 Neustadt an der Weinstraße 300")
        return

    query = parsed["query"]
    max_price = int(parsed["max_price"])
    location = parsed["location"]
    radius_km = int(parsed["radius_km"])

    klein_url = build_kleinanzeigen_url(query, max_price)
    mob_url = build_mobile_url(query, max_price, radius_km, location)

    data = load_data()
    user = data.setdefault("users", {}).setdefault(user_id, {"searches": [], "seen": []})

    user["searches"].append(
        {
            "query": query,
            "max_price": max_price,
            "location": location,
            "radius_km": radius_km,
            "kleinanzeigen_url": klein_url,
            "mobile_url": mob_url,
            "created": int(time.time()),
        }
    )
    save_data(data)

    await update.message.reply_text(
        "✅ Suche gespeichert!\n\n"
        f"🚗 {query}\n"
        f"💶 bis {max_price}€\n"
        f"📍 {location} ({radius_km} km)\n\n"
        f"🔎 Kleinanzeigen:\n{klein_url}\n\n"
        f"🔎 mobile.de:\n{mob_url}\n\n"
        "⏱️ Alerts laufen jetzt automatisch."
    )


# =========================
# ALERT JOB
# =========================
async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    users = data.get("users", {})
    bot = context.bot

    for user_id, udata in users.items():
        searches = udata.get("searches", [])
        seen_ids = set(udata.get("seen", []))

        for s in searches:
            query = s["query"]
            max_price = s["max_price"]

            # Kleinanzeigen listings
            try:
                klein_list = parse_kleinanzeigen_listings(s["kleinanzeigen_url"])
            except Exception:
                klein_list = []

            # mobile listings (best-effort)
            try:
                mob_list = parse_mobile_listings(s["mobile_url"])
            except Exception:
                mob_list = []

            for ad in (klein_list + mob_list):
                title = ad.get("title", "")
                snippet = ad.get("snippet", "")
                url = ad.get("url", "")
                image_count = ad.get("image_count", 0)
                year = ad.get("year")

                # basic match (query words in title/snippet)
                text_blob = f"{title} {snippet}".lower()
                if not all(w in text_blob for w in query.lower().split()):
                    continue

                # price filter is already in URL, but keep safety (we don't parse price reliably here)
                # blacklist
                if is_blacklisted(f"{title} {snippet}"):
                    continue
                # year
                if not year_ok(year):
                    continue
                # images
                if not images_ok(image_count):
                    continue

                ad_id = sha_id(url or (title + snippet))
                if ad_id in seen_ids:
                    continue

                seen_ids.add(ad_id)
                src = ad.get("source", "source")
                year_txt = f"{year}" if year else "unbekannt"

                await bot.send_message(
                    chat_id=int(user_id),
                    text=(
                        f"🆕 Neue Anzeige ({src})\n"
                        f"🚗 {title}\n"
                        f"📅 Baujahr: {year_txt}\n"
                        f"🖼️ Bilder: {image_count}\n\n"
                        f"🔗 {url}"
                    ),
                    disable_web_page_preview=True,
                )

        udata["seen"] = list(seen_ids)

    save_data(data)


# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN fehlt.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    # Auto alerts
    if app.job_queue is None:
        raise RuntimeError("JobQueue fehlt. Installiere python-telegram-bot[job-queue].")

    app.job_queue.run_repeating(alert_job, interval=CHECK_INTERVAL_SECONDS, first=20)

    app.run_polling()

if __name__ == "__main__":
    main()
