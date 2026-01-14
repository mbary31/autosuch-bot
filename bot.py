import os
import re
import json
import time
import hashlib
import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ---------------------------
# CONFIG
# ---------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing env var TELEGRAM_BOT_TOKEN")

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))  # every minute
MAX_RESULTS_PER_CHECK = int(os.getenv("MAX_RESULTS_PER_CHECK", "10"))    # limit per run
RESEND_AFTER_SECONDS = int(os.getenv("RESEND_AFTER_SECONDS", str(2 * 60 * 60)))  # 2h

# blacklist words (auto-K.O.)
BLACKLIST = {
    "unfall",
    "bastler",
    "motorschaden",
    "defekt",
    "export",
    "teileträger",
    "teiletraeger",
    "ohne tüv",
    "ohne tuv",
    "ohne tüv!",
    "ohne tuv!",
}

# "must have images" filter
REQUIRE_IMAGES = True

# storage
DATA_DIR = os.getenv("DATA_DIR", ".")
SEARCHES_FILE = os.path.join(DATA_DIR, "searches.json")
SENT_FILE = os.path.join(DATA_DIR, "sent_cache.json")

# user-agent (important for scraping)
UA = os.getenv(
    "SCRAPE_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("autosuch-bot")


# ---------------------------
# DATA MODELS
# ---------------------------

@dataclass
class Search:
    make: str
    model: str
    plz: str
    radius_km: int
    max_price: int
    min_year: int

    @property
    def query(self) -> str:
        # for searching
        return f"{self.make} {self.model}".strip()

    @property
    def key(self) -> str:
        # unique identifier for this search
        return f"{self.make.lower()}|{self.model.lower()}|{self.plz}|{self.radius_km}|{self.max_price}|{self.min_year}"


# sent cache structure:
# sent_cache[chat_id][listing_id] = {"ts": last_sent_ts, "hash": content_hash}
SentCache = Dict[str, Dict[str, Dict[str, str]]]
SearchStore = Dict[str, List[Dict]]


# ---------------------------
# UTIL
# ---------------------------

def load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning("Failed to load %s: %s", path, e)
    return default


def save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def contains_blacklist(text: str) -> Optional[str]:
    t = norm(text)
    for w in BLACKLIST:
        if w in t:
            return w
    return None


def extract_year(text: str) -> Optional[int]:
    # tries to find a plausible year in text
    if not text:
        return None
    years = re.findall(r"\b(19[5-9]\d|20[0-2]\d)\b", text)
    if not years:
        return None
    # choose the first plausible year
    try:
        return int(years[0])
    except:
        return None


def hash_listing(title: str, price: str, location: str, extra: str = "") -> str:
    h = hashlib.sha256()
    h.update((title or "").encode("utf-8"))
    h.update(b"|")
    h.update((price or "").encode("utf-8"))
    h.update(b"|")
    h.update((location or "").encode("utf-8"))
    h.update(b"|")
    h.update((extra or "").encode("utf-8"))
    return h.hexdigest()


def now_ts() -> int:
    return int(time.time())


# ---------------------------
# URL BUILDERS
# ---------------------------

def kleinanzeigen_search_url(s: Search) -> str:
    # Important: do NOT prefix with "auto," or "autos," -> keywords must be only query
    # We'll use a safe querystring-based URL.
    #
    # NOTE: Kleinanzeigen URL formats vary; this one works widely:
    # /s-autos/k0c216?keywords=...&locationStr=...&radius=...&priceTo=...
    from urllib.parse import urlencode

    params = {
        "keywords": s.query,           # only query, nothing else!
        "locationStr": s.plz,
        "radius": str(s.radius_km),
        "priceTo": str(s.max_price),
        "sortierung": "date",          # newest first (best effort)
    }
    return "https://www.kleinanzeigen.de/s-autos/k0c216?" + urlencode(params)


def mobile_search_url(s: Search) -> str:
    # mobile.de URL: we’ll use fulltext query BUT we also filter strictly in code
    # so the bot only alerts true matches.
    from urllib.parse import urlencode

    params = {
        "isSearchRequest": "true",
        "q": s.query,                 # query string
        "maxPrice": str(s.max_price),
        "radius": str(s.radius_km),
        "zip": s.plz,
        "dam": "0",                   # no accident-damaged filter parameter used here
        "sb": "rel",                  # relevance
        "vc": "Car",
        "cn": "DE",
    }
    return "https://suchen.mobile.de/fahrzeuge/search.html?" + urlencode(params)


# ---------------------------
# SCRAPERS
# ---------------------------

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})


def scrape_kleinanzeigen(s: Search) -> List[Dict]:
    url = kleinanzeigen_search_url(s)
    r = session.get(url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    results = []
    # Kleinanzeigen listings usually have links containing "/s-anzeige/"
    for a in soup.select("a[href*='/s-anzeige/']"):
        href = a.get("href") or ""
        if not href.startswith("http"):
            href = "https://www.kleinanzeigen.de" + href

        # find container for more info
        card = a
        for _ in range(4):
            if card and card.name != "article":
                card = card.parent
            else:
                break

        title = a.get_text(" ", strip=True)
        if not title or len(title) < 3:
            continue

        # best-effort: locate price & location text near the link
        card_text = card.get_text(" ", strip=True) if card else ""
        price = ""
        m_price = re.search(r"(\d[\d\.\s]*)(\s?€)", card_text)
        if m_price:
            price = (m_price.group(1).replace(".", "").replace(" ", "") + " €").strip()

        location = ""
        # often something like "67157 Wachenheim"
        m_loc = re.search(r"\b(\d{5})\b\s+([A-Za-zÄÖÜäöüß\- ]{2,})", card_text)
        if m_loc:
            location = f"{m_loc.group(1)} {m_loc.group(2).strip()}"

        # image check (best effort)
        has_img = False
        if REQUIRE_IMAGES and card:
            has_img = bool(card.select_one("img"))
        else:
            has_img = True

        results.append({
            "source": "kleinanzeigen",
            "id": href,   # stable enough
            "title": title,
            "url": href,
            "price_text": price,
            "location_text": location,
            "raw_text": card_text,
            "has_img": has_img,
        })

    return results


def scrape_mobile(s: Search) -> List[Dict]:
    url = mobile_search_url(s)
    r = session.get(url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    results = []
    # mobile results typically have anchors to "/auto-inserat/"
    for a in soup.select("a[href*='/auto-inserat/']"):
        href = a.get("href") or ""
        if not href:
            continue
        if href.startswith("/"):
            href = "https://suchen.mobile.de" + href
        if "mobile.de" not in href:
            continue

        # find card container (best effort)
        card = a
        for _ in range(6):
            if card and card.name not in ("article", "li", "div"):
                card = card.parent
            else:
                break

        title = a.get_text(" ", strip=True)
        if not title:
            continue

        card_text = card.get_text(" ", strip=True) if card else ""

        # extract price
        price = ""
        m_price = re.search(r"(\d[\d\.\s]*)\s?€", card_text)
        if m_price:
            price = (m_price.group(1).replace(".", "").replace(" ", "") + " €").strip()

        # image check
        has_img = False
        if REQUIRE_IMAGES and card:
            has_img = bool(card.select_one("img"))
        else:
            has_img = True

        results.append({
            "source": "mobile",
            "id": href,
            "title": title,
            "url": href,
            "price_text": price,
            "location_text": "",
            "raw_text": card_text,
            "has_img": has_img,
        })

    return results


# ---------------------------
# FILTERING
# ---------------------------

def parse_price_eur(price_text: str) -> Optional[int]:
    if not price_text:
        return None
    m = re.search(r"(\d[\d\.\s]*)", price_text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(".", "").replace(" ", ""))
    except:
        return None


def strict_match_make_model(s: Search, title: str, raw: str) -> bool:
    # Make sure mobile doesn’t spam all Audis:
    # require both make and model to appear in title or snippet.
    t = norm(title + " " + raw)
    return (norm(s.make) in t) and (norm(s.model) in t)


def passes_filters(s: Search, item: Dict) -> Tuple[bool, str]:
    title = item.get("title", "")
    raw = item.get("raw_text", "")

    # strict make/model match (especially for mobile)
    if item.get("source") == "mobile":
        if not strict_match_make_model(s, title, raw):
            return False, "no_strict_make_model"

    # blacklist
    bad = contains_blacklist(title + " " + raw)
    if bad:
        return False, f"blacklist:{bad}"

    # images
    if REQUIRE_IMAGES and not item.get("has_img", False):
        return False, "no_image"

    # price
    p = parse_price_eur(item.get("price_text", ""))
    if p is not None and p > s.max_price:
        return False, "over_price"

    # year (best effort)
    y = extract_year(title + " " + raw)
    if y is not None and y < s.min_year:
        return False, "year_too_old"

    return True, "ok"


# ---------------------------
# SENDING / DEDUP
# ---------------------------

def should_send(chat_id: str, item: Dict, sent_cache: SentCache) -> bool:
    listing_id = item["id"]
    title = item.get("title", "")
    price = item.get("price_text", "")
    loc = item.get("location_text", "")
    raw = item.get("raw_text", "")

    content_hash = hash_listing(title, price, loc, raw[:400])

    sent_cache.setdefault(chat_id, {})
    prev = sent_cache[chat_id].get(listing_id)

    ts = now_ts()

    if not prev:
        sent_cache[chat_id][listing_id] = {"ts": str(ts), "hash": content_hash}
        return True

    prev_ts = int(prev.get("ts", "0"))
    prev_hash = prev.get("hash", "")

    changed = (content_hash != prev_hash)
    too_old = (ts - prev_ts) >= RESEND_AFTER_SECONDS

    if changed or too_old:
        sent_cache[chat_id][listing_id] = {"ts": str(ts), "hash": content_hash}
        return True

    return False


def format_alert(s: Search, item: Dict) -> str:
    src = item.get("source", "")
    title = item.get("title", "").strip()
    price = item.get("price_text", "").strip()
    loc = item.get("location_text", "").strip()
    url = item.get("url", "").strip()

    parts = [f"🚗 *{title}*"]
    meta = []
    if price:
        meta.append(f"💶 {price}")
    if loc:
        meta.append(f"📍 {loc}")
    meta.append(f"🔎 {src}")

    parts.append(" | ".join(meta))
    parts.append(f"🔗 {url}")

    return "\n".join(parts)


# ---------------------------
# TELEGRAM COMMANDS
# ---------------------------

def parse_set_args(text: str) -> Search:
    # Expected:
    # /set Auto Model PLZ Umkreis Preis BJ
    #
    # Example:
    # /set audi a6 67157 300 10000 2010
    parts = text.strip().split()
    if len(parts) < 7:
        raise ValueError("Format: /set Auto Model PLZ Umkreis Preis BJ (z.B. /set audi a6 67157 300 10000 2010)")

    # parts[0] is /set
    make = parts[1]
    model = parts[2]
    plz = parts[3]

    try:
        radius = int(re.sub(r"\D", "", parts[4]))
        max_price = int(re.sub(r"\D", "", parts[5]))
        min_year = int(re.sub(r"\D", "", parts[6]))
    except:
        raise ValueError("Umkreis/Preis/BJ müssen Zahlen sein. Beispiel: /set audi a6 67157 300 10000 2010")

    if not re.fullmatch(r"\d{5}", plz):
        raise ValueError("PLZ muss 5-stellig sein (z.B. 67157).")

    return Search(make=make, model=model, plz=plz, radius_km=radius, max_price=max_price, min_year=min_year)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ *AutoSuchBot online.*\n\n"
        "*Benutzung:*\n"
        "`/set Auto Model PLZ Umkreis Preis BJ`\n"
        "Beispiel:\n"
        "`/set audi a6 67157 300 10000 2010`\n\n"
        "*Andere:*\n"
        "`/list`  – gespeicherte Suchen\n"
        "`/del <nr>` – Suche löschen\n"
        "`/stop`  – alle Suchen löschen\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    try:
        s = parse_set_args(update.message.text)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return

    store: SearchStore = load_json(SEARCHES_FILE, {})
    store.setdefault(chat_id, [])
    store[chat_id].append(asdict(s))
    save_json(SEARCHES_FILE, store)

    # confirm + show search links
    ka = kleinanzeigen_search_url(s)
    mo = mobile_search_url(s)

    await update.message.reply_text(
        "✅ *Suche gespeichert!*\n"
        f"🚗 `{s.make} {s.model}`\n"
        f"💶 bis `{s.max_price}€`\n"
        f"📍 PLZ `{s.plz}` + `{s.radius_km} km`\n"
        f"📆 ab BJ `{s.min_year}`\n\n"
        f"🔎 *Kleinanzeigen Suche:*\n{ka}\n\n"
        f"🔎 *mobile.de Suche:*\n{mo}\n\n"
        "⏱️ Alerts laufen jetzt automatisch (jede Minute).",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    store: SearchStore = load_json(SEARCHES_FILE, {})
    items = store.get(chat_id, [])

    if not items:
        await update.message.reply_text("Keine gespeicherten Suchen.")
        return

    lines = ["📌 *Deine Suchen:*"]
    for i, d in enumerate(items, start=1):
        s = Search(**d)
        lines.append(
            f"{i}) 🚗 `{s.make} {s.model}` | 📍 `{s.plz}` + `{s.radius_km}km` | 💶 `{s.max_price}€` | 📆 `{s.min_year}`"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    store: SearchStore = load_json(SEARCHES_FILE, {})
    items = store.get(chat_id, [])

    if not items:
        await update.message.reply_text("Keine gespeicherten Suchen.")
        return

    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Format: /del 1")
        return

    idx = int(parts[1]) - 1
    if idx < 0 or idx >= len(items):
        await update.message.reply_text("❌ Nummer existiert nicht. Nutze /list")
        return

    removed = items.pop(idx)
    store[chat_id] = items
    save_json(SEARCHES_FILE, store)

    s = Search(**removed)
    await update.message.reply_text(f"🗑️ Gelöscht: {s.make} {s.model} ({s.plz})")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    store: SearchStore = load_json(SEARCHES_FILE, {})
    if chat_id in store:
        store.pop(chat_id, None)
        save_json(SEARCHES_FILE, store)

    # also clear cache for this chat
    sent: SentCache = load_json(SENT_FILE, {})
    if chat_id in sent:
        sent.pop(chat_id, None)
        save_json(SENT_FILE, sent)

    await update.message.reply_text("🛑 Alles gelöscht. Keine Alerts mehr.")


# ---------------------------
# JOB LOOP
# ---------------------------

async def check_once(app: Application):
    store: SearchStore = load_json(SEARCHES_FILE, {})
    sent: SentCache = load_json(SENT_FILE, {})

    for chat_id, searches in store.items():
        compiled: List[Search] = []
        for d in searches:
            try:
                compiled.append(Search(**d))
            except:
                continue

        if not compiled:
            continue

        for s in compiled:
            # scrape both sources
            all_items: List[Dict] = []
            try:
                all_items += scrape_kleinanzeigen(s)
            except Exception as e:
                log.warning("Kleinanzeigen scrape failed: %s", e)

            try:
                all_items += scrape_mobile(s)
            except Exception as e:
                log.warning("mobile scrape failed: %s", e)

            # filter + send
            sent_count = 0
            for item in all_items:
                ok, reason = passes_filters(s, item)
                if not ok:
                    continue

                if not should_send(chat_id, item, sent):
                    continue

                text = format_alert(s, item)
                try:
                    await app.bot.send_message(
                        chat_id=int(chat_id),
                        text=text,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=False,
                    )
                    sent_count += 1
                except Exception as e:
                    log.warning("send_message failed: %s", e)

                if sent_count >= MAX_RESULTS_PER_CHECK:
                    break

    save_json(SENT_FILE, sent)


async def job_runner(context: ContextTypes.DEFAULT_TYPE):
    await check_once(context.application)


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("stop", cmd_stop))

    # job queue every minute
    app.job_queue.run_repeating(job_runner, interval=CHECK_INTERVAL_SECONDS, first=5)

    log.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
