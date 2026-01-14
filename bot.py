#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AutoSuchBot (Kleinanzeigen + mobile.de) – robust & crash-sicher

ENV Variablen (Railway -> Variables):
- TELEGRAM_BOT_TOKEN   = dein Telegram Token
- OPENAI_API_KEY       = optional (wird hier NICHT benötigt)

WICHTIG requirements.txt:
python-telegram-bot[job-queue]==21.6
requests
beautifulsoup4
lxml
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# Config
# =========================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()  # optional, not used here

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "state.json"
SEEN_FILE = DATA_DIR / "seen.json"

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))  # 5 min default
MAX_SEND_PER_RUN = int(os.getenv("MAX_SEND_PER_RUN", "6"))  # max links per chat per check
DETAIL_TIMEOUT = int(os.getenv("DETAIL_TIMEOUT", "20"))
LIST_TIMEOUT = int(os.getenv("LIST_TIMEOUT", "20"))

# =========================
# HTTP Session (retries)
# =========================

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }
)

retry = Retry(
    total=4,
    backoff_factor=0.9,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
SESSION.mount("https://", HTTPAdapter(max_retries=retry))
SESSION.mount("http://", HTTPAdapter(max_retries=retry))

# =========================
# Helpers: Storage
# =========================

def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps({"chats": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    if not SEEN_FILE.exists():
        SEEN_FILE.write_text(json.dumps({"chats": {}}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state() -> Dict[str, Any]:
    ensure_data_dir()
    return load_json(STATE_FILE, {"chats": {}})


def save_state(state: Dict[str, Any]) -> None:
    save_json(STATE_FILE, state)


def load_seen() -> Dict[str, Any]:
    ensure_data_dir()
    return load_json(SEEN_FILE, {"chats": {}})


def save_seen(seen: Dict[str, Any]) -> None:
    save_json(SEEN_FILE, seen)


# =========================
# Parsing / Validation
# =========================

PRICE_RE = re.compile(r"(\d[\d\.\s]*)(?:€|EUR)", re.IGNORECASE)
YEAR_RE = re.compile(r"(19\d{2}|20\d{2})")


def parse_price_eur(text: str) -> Optional[int]:
    t = text.replace("\xa0", " ")
    m = PRICE_RE.search(t)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def parse_first_registration_year(text: str) -> Optional[int]:
    t = text.replace("\xa0", " ")
    keys = ["Erstzulassung", "EZ", "First registration", "Erst-Zulassung"]
    for key in keys:
        idx = t.lower().find(key.lower())
        if idx != -1:
            window = t[idx : idx + 260]
            ym = YEAR_RE.search(window)
            if ym:
                return int(ym.group(1))
    # fallback (nicht perfekt, aber besser als nix)
    ym = YEAR_RE.search(t)
    return int(ym.group(1)) if ym else None


def looks_like_blocked(html: str) -> bool:
    h = html.lower()
    needles = [
        "captcha",
        "access denied",
        "forbidden",
        "cloudflare",
        "consent",
        "cookie",
        "bitte bestätige",
        "unusual traffic",
        "automated",
        "datenschutz",
    ]
    return any(n in h for n in needles)


def normalize_url(url: str) -> str:
    url = url.strip()
    # remove tracking fragments
    url = url.split("#", 1)[0]
    return url


def dedupe_keep_order(urls: List[str]) -> List[str]:
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def fetch_sync(url: str, timeout: int) -> Tuple[int, str]:
    try:
        r = SESSION.get(url, timeout=timeout)
        return r.status_code, r.text or ""
    except Exception:
        return 0, ""


async def fetch(url: str, timeout: int) -> Tuple[int, str]:
    return await asyncio.to_thread(fetch_sync, url, timeout)


async def validate_listing_by_details(url: str, max_price: int, min_year: int) -> bool:
    code, html = await fetch(url, timeout=DETAIL_TIMEOUT)
    if code != 200 or not html:
        return False
    if looks_like_blocked(html):
        return False

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    price = parse_price_eur(text)
    year = parse_first_registration_year(text)

    if price is not None and price > max_price:
        return False
    if year is not None and year < min_year:
        return False

    return True


# =========================
# Search URL builders
# =========================

def mobile_search_url(q: str, plz: str, radius: int, max_price: int) -> str:
    """
    mobile.de: wir nutzen eine einfache Such-URL.
    Mindestjahr wird über Detailseiten-Validierung geprüft.
    """
    base = "https://suchen.mobile.de/fahrzeuge/search.html"
    params = {
        "isSearchRequest": "true",
        "s": "Car",
        "dam": "0",
        "vc": "Car",
        "sfmr": "1",
        "ref": "srp",
        "lang": "de",
        "zipcode": plz,
        "radius": str(radius),
        "maxPrice": str(max_price),
        "q": q,
    }
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())
    return f"{base}?{qs}"


def kleinanzeigen_search_url(q: str, plz: str, radius: int, max_price: int) -> str:
    """
    Kleinanzeigen: Such-URL + Preisfilter.
    Mindestjahr wird über Detailseiten-Validierung geprüft.
    """
    # Kleinanzeigen nutzt "k0l<plz>" nicht offiziell dokumentiert;
    # wir nutzen location=<plz> und distance=<km>.
    base = "https://www.kleinanzeigen.de/s-autos/k0"
    params = {
        "keywords": q,
        "locationStr": plz,
        "radius": str(radius),
        "priceTo": str(max_price),
        "sortingField": "SORTING_DATE",
        "pageNum": "1",
    }
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())
    return f"{base}?{qs}"


# =========================
# Extract listing URLs
# =========================

def extract_mobile_links(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href:
            continue
        # typische mobile detail urls:
        # /fahrzeuge/details.html?id=...
        if "details.html?id=" in href or "/fahrzeuge/details.html" in href:
            u = urljoin("https://suchen.mobile.de", href)
            urls.append(normalize_url(u))
    return dedupe_keep_order(urls)


def extract_kleinanzeigen_links(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href:
            continue
        # typische Anzeige-URLs enthalten /s-anzeige/
        if "/s-anzeige/" in href:
            u = urljoin("https://www.kleinanzeigen.de", href)
            urls.append(normalize_url(u))
    return dedupe_keep_order(urls)


# =========================
# Command parsing
# =========================

def parse_set(args: List[str]) -> Optional[Dict[str, Any]]:
    """
    /set <query...> <PLZ> <UMKREIS_KM> <MAX_PREIS> <MIN_BJ>
    """
    if len(args) < 5:
        return None

    plz_raw, radius_raw, max_price_raw, min_year_raw = args[-4], args[-3], args[-2], args[-1]
    query_tokens = args[:-4]
    if not query_tokens:
        return None

    plz = re.sub(r"\D", "", plz_raw.strip())
    if not plz:
        return None

    try:
        radius = int(radius_raw)
        max_price = int(max_price_raw)
        min_year = int(re.sub(r"\D", "", min_year_raw))
    except Exception:
        return None

    q = " ".join(t.strip() for t in query_tokens if t.strip()).strip()
    if not q:
        return None

    # clamp radius to sane values
    if radius < 1:
        radius = 1
    if radius > 500:
        radius = 500

    return {
        "q": q,
        "plz": plz,
        "radius": radius,
        "max_price": max_price,
        "min_year": min_year,
        "created_at": time.time(),
        "active": True,
        "sources": ["mobile", "kleinanzeigen"],
    }


# =========================
# Seen logic
# =========================

def should_send(seen_chat: Dict[str, float], url: str) -> bool:
    return url not in seen_chat


def mark_seen(seen_chat: Dict[str, float], url: str) -> None:
    seen_chat[url] = time.time()


# =========================
# Telegram text helpers
# =========================

def fmt_search(s: Dict[str, Any], idx: int) -> str:
    src = ",".join(s.get("sources", []))
    return (
        f"*{idx}.* `{s.get('q','')}` | PLZ `{s.get('plz','')}` | "
        f"Radius `{s.get('radius','')}`km | Max `{s.get('max_price','')}`€ | "
        f"MinBJ `{s.get('min_year','')}` | Quellen `{src}` | "
        f"{'✅' if s.get('active') else '⛔'}"
    )


# =========================
# Telegram handlers
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🛠️ AutoSuchBot läuft.\n\n"
        "Befehle:\n"
        "/set <query...> <PLZ> <UMKREIS> <MAX_PREIS> <MIN_BJ>\n"
        "/list\n"
        "/del <index>\n"
        "/stop\n"
        "/status\n\n"
        "Beispiel:\n"
        "`/set bmw 320 d touring 10115 50 12000 2013`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    seen = load_seen()
    chat_id = str(update.effective_chat.id)

    searches = state.get("chats", {}).get(chat_id, {}).get("searches", [])
    seen_count = len(seen.get("chats", {}).get(chat_id, {}))

    active_count = sum(1 for s in searches if s.get("active"))
    await update.message.reply_text(
        f"📌 Suchen: {len(searches)} (aktiv: {active_count})\n"
        f"👀 Gesehene Links: {seen_count}\n"
        f"⏱️ Check-Intervall: {CHECK_INTERVAL_SECONDS}s\n"
        f"📤 Max Links pro Run: {MAX_SEND_PER_RUN}"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    chat_id = str(update.effective_chat.id)
    searches = state.get("chats", {}).get(chat_id, {}).get("searches", [])

    if not searches:
        await update.message.reply_text("Keine Suchen gesetzt. Nutze /set ...")
        return

    lines = ["📋 Deine Suchen:"]
    for i, s in enumerate(searches):
        lines.append(fmt_search(s, i))
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    parsed = parse_set(args)

    if not parsed:
        await update.message.reply_text(
            "❌ Falsches Format.\n"
            "Nutze:\n"
            "/set <query...> <PLZ> <UMKREIS> <MAX_PREIS> <MIN_BJ>\n\n"
            "Beispiel:\n"
            "`/set bmw 320 d touring 10115 50 12000 2013`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    state = load_state()
    chat_id = str(update.effective_chat.id)
    state.setdefault("chats", {}).setdefault(chat_id, {}).setdefault("searches", [])
    state["chats"][chat_id]["searches"].append(parsed)
    save_state(state)

    await update.message.reply_text(
        "✅ Suche gespeichert:\n" + fmt_search(parsed, len(state["chats"][chat_id]["searches"]) - 1),
        parse_mode=ParseMode.MARKDOWN,
    )


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text("❌ Nutze: /del <index> (siehe /list)")
        return

    try:
        idx = int(args[0])
    except Exception:
        await update.message.reply_text("❌ Index muss eine Zahl sein. (siehe /list)")
        return

    state = load_state()
    chat_id = str(update.effective_chat.id)
    searches = state.get("chats", {}).get(chat_id, {}).get("searches", [])

    if idx < 0 or idx >= len(searches):
        await update.message.reply_text("❌ Index außerhalb. (siehe /list)")
        return

    removed = searches.pop(idx)
    state["chats"][chat_id]["searches"] = searches
    save_state(state)

    await update.message.reply_text(
        "🗑️ Gelöscht:\n" + fmt_search(removed, idx),
        parse_mode=ParseMode.MARKDOWN,
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    chat_id = str(update.effective_chat.id)
    searches = state.get("chats", {}).get(chat_id, {}).get("searches", [])

    if not searches:
        await update.message.reply_text("Keine Suchen vorhanden.")
        return

    for s in searches:
        s["active"] = False

    state["chats"][chat_id]["searches"] = searches
    save_state(state)

    await update.message.reply_text("⛔ Alle Suchen für diesen Chat wurden deaktiviert.")


# =========================
# Scheduled job
# =========================

async def run_checks_for_chat(chat_id: str, bot) -> None:
    state = load_state()
    seen = load_seen()

    chat_state = state.get("chats", {}).get(chat_id, {})
    searches: List[Dict[str, Any]] = chat_state.get("searches", [])
    if not searches:
        return

    seen.setdefault("chats", {}).setdefault(chat_id, {})
    chat_seen: Dict[str, float] = seen["chats"][chat_id]

    # nur aktive
    active_searches = [s for s in searches if s.get("active")]
    if not active_searches:
        return

    for s_idx, s in enumerate(active_searches):
        q = s["q"]
        plz = s["plz"]
        radius = int(s["radius"])
        max_price = int(s["max_price"])
        min_year = int(s["min_year"])
        sources = s.get("sources", ["mobile", "kleinanzeigen"])

        urls: List[str] = []

        # mobile
        if "mobile" in sources:
            url = mobile_search_url(q=q, plz=plz, radius=radius, max_price=max_price)
            code, html = await fetch(url, timeout=LIST_TIMEOUT)
            if code == 200 and html and not looks_like_blocked(html):
                urls.extend(extract_mobile_links(html))

        # kleinanzeigen
        if "kleinanzeigen" in sources:
            url = kleinanzeigen_search_url(q=q, plz=plz, radius=radius, max_price=max_price)
            code, html = await fetch(url, timeout=LIST_TIMEOUT)
            if code == 200 and html and not looks_like_blocked(html):
                urls.extend(extract_kleinanzeigen_links(html))

        urls = dedupe_keep_order(urls)

        to_send: List[str] = []
        for u in urls:
            if not should_send(chat_seen, u):
                continue

            ok = await validate_listing_by_details(u, max_price=max_price, min_year=min_year)
            if not ok:
                mark_seen(chat_seen, u)  # damit er nicht immer wieder den gleichen Müll prüft
                continue

            to_send.append(u)
            mark_seen(chat_seen, u)

            if len(to_send) >= MAX_SEND_PER_RUN:
                break

        if to_send:
            header = (
                f"🔎 *Neue Treffer* für: `{q}`\n"
                f"PLZ `{plz}` | Radius `{radius}`km | Max `{max_price}`€ | MinBJ `{min_year}`\n\n"
            )
            msg = header + "\n".join(to_send)
            await bot.send_message(chat_id=int(chat_id), text=msg, parse_mode=ParseMode.MARKDOWN)

        # seen speichern nach jeder Suche
        save_seen(seen)

        # kleine Pause zwischen Suchen, damit du nicht sofort geblockt wirst
        await asyncio.sleep(1.0)


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = context.bot
    state = load_state()

    chats = list((state.get("chats") or {}).keys())
    if not chats:
        return

    # nacheinander, stabiler (parallel kann dir block/429 bringen)
    for chat_id in chats:
        try:
            await run_checks_for_chat(chat_id, bot)
        except Exception as e:
            # niemals crashen wegen einem Chat
            print(f"[scheduled_job] chat {chat_id} error: {e}")


# =========================
# Entrypoint
# =========================

def main() -> None:
    ensure_data_dir()

    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN fehlt in ENV (Railway Variables).")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    # Scheduler starten (crash-sicher)
    if app.job_queue is None:
        print("⚠️ JobQueue fehlt – installiere python-telegram-bot[job-queue]. Starte ohne Scheduler.")
    else:
        app.job_queue.run_repeating(
            scheduled_job,
            interval=CHECK_INTERVAL_SECONDS,
            first=5,
        )

    app.run_polling()


if __name__ == "__main__":
    main()
