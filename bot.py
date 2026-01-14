#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin

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

# =========================================================
# ENV (Railway -> Variables)
# =========================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# =========================================================
# Settings
# =========================================================
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "state.json"
SEEN_FILE = DATA_DIR / "seen.json"

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))  # 5 min
MAX_SEND_PER_RUN = int(os.getenv("MAX_SEND_PER_RUN", "6"))

LIST_TIMEOUT = int(os.getenv("LIST_TIMEOUT", "20"))
DETAIL_TIMEOUT = int(os.getenv("DETAIL_TIMEOUT", "20"))

# =========================================================
# HTTP session with retries
# =========================================================
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
    backoff_factor=0.8,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
SESSION.mount("https://", HTTPAdapter(max_retries=retry))
SESSION.mount("http://", HTTPAdapter(max_retries=retry))


# =========================================================
# Storage helpers
# =========================================================
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


# =========================================================
# Parsing / Blocking detection
# =========================================================
PRICE_RE = re.compile(r"(\d[\d\.\s]*)(?:€|EUR)", re.IGNORECASE)
YEAR_RE = re.compile(r"(19\d{2}|20\d{2})")


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


def parse_price_eur(text: str) -> Optional[int]:
    t = text.replace("\xa0", " ")
    m = PRICE_RE.search(t)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def parse_first_registration_year(text: str) -> Optional[int]:
    t = text.replace("\xa0", " ")
    # gezielt um "Erstzulassung/EZ"
    for key in ["Erstzulassung", "EZ", "Erst-Zulassung", "First registration"]:
        idx = t.lower().find(key.lower())
        if idx != -1:
            window = t[idx : idx + 260]
            ym = YEAR_RE.search(window)
            if ym:
                return int(ym.group(1))
    # fallback
    ym = YEAR_RE.search(t)
    return int(ym.group(1)) if ym else None


def normalize_url(url: str) -> str:
    url = url.strip().split("#", 1)[0]
    return url


def dedupe_keep_order(urls: List[str]) -> List[str]:
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# =========================================================
# Fetch (async wrapper around requests)
# =========================================================
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


# =========================================================
# Search URL builders
# =========================================================
def mobile_search_url(q: str, plz: str, radius: int, max_price: int) -> str:
    base = "https://suchen.mobile.de/fahrzeuge/search.html"
    params = {
        "isSearchRequest": "true",
        "s": "Car",
        "vc": "Car",
        "dam": "0",
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
    # kleinanzeigen ist zickig -> distance UND radius setzen
    base = "https://www.kleinanzeigen.de/s-autos/k0"
    params = {
        "keywords": q,
        "locationStr": plz,
        "distance": str(radius),
        "radius": str(radius),
        "priceTo": str(max_price),
        "sortingField": "SORTING_DATE",
        "pageNum": "1",
    }
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())
    return f"{base}?{qs}"


# =========================================================
# Extract listing URLs
# =========================================================
def extract_mobile_links(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href:
            continue

        # mobile detail links
        if "details.html?id=" in href or "/fahrzeuge/details.html" in href:
            u = urljoin("https://suchen.mobile.de", href)
            urls.append(normalize_url(u))

        # manchmal kommen auch auto-inserat urls
        if "mobile.de/auto-inserat/" in href:
            u = urljoin("https://www.mobile.de", href)
            urls.append(normalize_url(u))

    return dedupe_keep_order(urls)


def extract_kleinanzeigen_links(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href:
            continue
        if "/s-anzeige/" in href:
            u = urljoin("https://www.kleinanzeigen.de", href)
            urls.append(normalize_url(u))
    return dedupe_keep_order(urls)


# =========================================================
# Command parsing
# =========================================================
def parse_set(args: List[str]) -> Optional[Dict[str, Any]]:
    """
    /set <query...> <PLZ> <UMKREIS> <MAX_PREIS> <MIN_BJ>
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
        "active": True,
        "sources": ["mobile", "kleinanzeigen"],
        "created_at": time.time(),
    }


# =========================================================
# Seen logic
# =========================================================
def should_send(chat_seen: Dict[str, float], url: str) -> bool:
    return url not in chat_seen


def mark_seen(chat_seen: Dict[str, float], url: str) -> None:
    chat_seen[url] = time.time()


# =========================================================
# Telegram text
# =========================================================
def fmt_search(s: Dict[str, Any], idx: int) -> str:
    src = ",".join(s.get("sources", []))
    return (
        f"*{idx}.* `{s.get('q','')}` | PLZ `{s.get('plz','')}` | Radius `{s.get('radius','')}`km | "
        f"Max `{s.get('max_price','')}`€ | MinBJ `{s.get('min_year','')}` | Quellen `{src}` | "
        f"{'✅' if s.get('active') else '⛔'}"
    )


# =========================================================
# Telegram handlers
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🛠️ AutoSuchBot läuft.\n\n"
        "Befehle:\n"
        "/set <query...> <PLZ> <UMKREIS> <MAXPREIS> <MINBJ>\n"
        "/list\n"
        "/del <index>\n"
        "/stop\n"
        "/status\n"
        "/run  (manueller Sofort-Check + Diagnose)\n\n"
        "Beispiel:\n"
        "`/set bmw 320 d touring 10115 50 12000 2013`",
        parse_mode=ParseMode.MARKDOWN,
    )


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
        f"⏱️ Intervall: {CHECK_INTERVAL_SECONDS}s\n"
        f"📤 Max/Run: {MAX_SEND_PER_RUN}"
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
    parsed = parse_set(context.args or [])
    if not parsed:
        await update.message.reply_text(
            "❌ Falsches Format.\n"
            "/set <query...> <PLZ> <UMKREIS> <MAXPREIS> <MINBJ>\n\n"
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
        await update.message.reply_text("❌ Index muss Zahl sein. (siehe /list)")
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

    await update.message.reply_text("🗑️ Gelöscht:\n" + fmt_search(removed, idx), parse_mode=ParseMode.MARKDOWN)


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

    await update.message.reply_text("⛔ Alle Suchen in diesem Chat deaktiviert.")


# =========================================================
# Core checking
# =========================================================
async def run_checks_for_chat(chat_id: str, bot, verbose: bool = False) -> str:
    state = load_state()
    seen = load_seen()

    searches: List[Dict[str, Any]] = state.get("chats", {}).get(chat_id, {}).get("searches", [])
    if not searches:
        return "ℹ️ Keine Suchen gesetzt."

    active_searches = [s for s in searches if s.get("active")]
    if not active_searches:
        return "ℹ️ Keine aktiven Suchen."

    seen.setdefault("chats", {}).setdefault(chat_id, {})
    chat_seen: Dict[str, float] = seen["chats"][chat_id]

    lines: List[str] = []
    total_sent = 0

    for s in active_searches:
        q = s["q"]
        plz = s["plz"]
        radius = int(s["radius"])
        max_price = int(s["max_price"])
        min_year = int(s["min_year"])
        sources = s.get("sources", ["mobile", "kleinanzeigen"])

        urls: List[str] = []
        diag_parts: List[str] = []

        # mobile
        if "mobile" in sources:
            url = mobile_search_url(q=q, plz=plz, radius=radius, max_price=max_price)
            code, html = await fetch(url, timeout=LIST_TIMEOUT)
            blocked = (code == 200 and html and looks_like_blocked(html))
            extracted = extract_mobile_links(html) if (code == 200 and html and not blocked) else []
            urls.extend(extracted)
            diag_parts.append(f"mobile http={code} blocked={blocked} links={len(extracted)}")

        # kleinanzeigen
        if "kleinanzeigen" in sources:
            url = kleinanzeigen_search_url(q=q, plz=plz, radius=radius, max_price=max_price)
            code, html = await fetch(url, timeout=LIST_TIMEOUT)
            blocked = (code == 200 and html and looks_like_blocked(html))
            extracted = extract_kleinanzeigen_links(html) if (code == 200 and html and not blocked) else []
            urls.extend(extracted)
            diag_parts.append(f"kleinanzeigen http={code} blocked={blocked} links={len(extracted)}")

        urls = dedupe_keep_order(urls)

        checked = 0
        passed = 0
        to_send: List[str] = []

        for u in urls:
            if not should_send(chat_seen, u):
                continue

            checked += 1
            ok = await validate_listing_by_details(u, max_price=max_price, min_year=min_year)

            # IMMER markieren (sonst hängt er an denselben Dingern für immer)
            mark_seen(chat_seen, u)

            if not ok:
                continue

            passed += 1
            to_send.append(u)
            if len(to_send) >= MAX_SEND_PER_RUN:
                break

        if to_send:
            header = (
                f"🔎 *Neue Treffer* für: `{q}`\n"
                f"PLZ `{plz}` | Radius `{radius}`km | Max `{max_price}`€ | MinBJ `{min_year}`\n\n"
            )
            await bot.send_message(chat_id=int(chat_id), text=header + "\n".join(to_send), parse_mode=ParseMode.MARKDOWN)
            total_sent += len(to_send)

        save_seen(seen)

        if verbose:
            lines.append(
                f"🧾 `{q}` → urls={len(urls)} checked={checked} passed={passed} sent={len(to_send)}\n"
                f"   " + " | ".join(diag_parts)
            )

        await asyncio.sleep(1.0)

    if verbose:
        return "✅ Check fertig.\n\n" + ("\n\n".join(lines) if lines else "ℹ️ Kein Output.") + f"\n\n📤 Gesamt gesendet: {total_sent}"
    return "ok"


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = context.bot
    state = load_state()
    chat_ids = list((state.get("chats") or {}).keys())

    for chat_id in chat_ids:
        try:
            await run_checks_for_chat(chat_id, bot, verbose=False)
        except Exception as e:
            print(f"[scheduled_job] chat {chat_id} error: {e}")


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text("🔧 Starte manuellen Check…")
    diag = await run_checks_for_chat(chat_id, context.bot, verbose=True)
    await update.message.reply_text(diag)


# =========================================================
# Entrypoint
# =========================================================
def main() -> None:
    ensure_data_dir()

    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN fehlt (Railway Variables).")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("run", run_cmd))

    # Scheduler (wenn JobQueue vorhanden)
    if app.job_queue is None:
        print("⚠️ JobQueue fehlt. Installiere python-telegram-bot[job-queue]. Scheduler aus.")
    else:
        app.job_queue.run_repeating(scheduled_job, interval=CHECK_INTERVAL_SECONDS, first=8)

    print("✅ Bot startet polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
