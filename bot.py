import os
import re
import json
import time
import sqlite3
import hashlib
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List, Tuple

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# CONFIG – HIER stellst du Punkt 3+4 ein
# =========================

# 3) Blacklist-Wörter (alles mit diesen Wörtern wird verworfen)
BLACKLIST = [
    "unfall",
    "bastler",
    "motorschaden",
    "defekt",
    "export",
    "teileträger",
    "teiletraeger",
    "ohne tüv",
    "ohne tuv",
]

# 4) Nur mit Bildern standardmäßig? (kannst du pro /set überschreiben)
DEFAULT_ONLY_WITH_PHOTOS = True

# Default-Check-Intervall
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "180"))

# Default-Radius
DEFAULT_RADIUS_KM = int(os.getenv("DEFAULT_RADIUS_KM", "300"))

# DB
DB_PATH = "data.db"

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
}

# =========================
# DB
# =========================

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            query TEXT NOT NULL,
            price_to INTEGER,
            radius_km INTEGER,
            location TEXT,
            year_from INTEGER,
            year_to INTEGER,
            only_photos INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            chat_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            item_id TEXT NOT NULL,
            first_seen INTEGER NOT NULL,
            PRIMARY KEY (chat_id, source, item_id)
        );
    """)
    return conn

# =========================
# MODELL
# =========================

@dataclass
class Search:
    id: int
    chat_id: int
    query: str
    price_to: Optional[int]
    radius_km: int
    location: str
    year_from: Optional[int]
    year_to: Optional[int]
    only_photos: bool

# =========================
# HELFER
# =========================

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).lower()

def contains_blacklist(text: str) -> bool:
    t = norm(text)
    return any(w in t for w in BLACKLIST)

def parse_int(val: Optional[str]) -> Optional[int]:
    if not val:
        return None
    val = re.sub(r"[^\d]", "", val)
    return int(val) if val else None

def hash_id(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]

def format_search(s: Search) -> str:
    parts = [f"🚗 *{s.query}*"]
    parts.append(f"💶 bis *{s.price_to}€*" if s.price_to else "💶 *egal*")
    parts.append(f"📍 *{s.location}* ({s.radius_km} km)")
    if s.year_from or s.year_to:
        parts.append(f"🗓 BJ: *{s.year_from or '—'}* bis *{s.year_to or '—'}*")
    else:
        parts.append("🗓 BJ: *egal*")
    parts.append("🖼 nur mit Bildern" if s.only_photos else "🖼 Bilder egal")
    return "\n".join(parts)

# =========================
# URL BUILDER
# =========================

def build_kleinanzeigen_url(query: str, price_to: Optional[int]) -> str:
    # Simple Suchlink: keywords + maxPrice
    q = requests.utils.quote(query)
    url = f"https://www.kleinanzeigen.de/s-autos/k0c216?keywords={q}"
    if price_to:
        url += f"&priceTo={price_to}"
    return url

def build_mobile_url(query: str, price_to: Optional[int], radius_km: int) -> str:
    # Simple Suchlink: query + maxPrice + radius
    q = requests.utils.quote(query)
    url = (
        "https://suchen.mobile.de/fahrzeuge/search.html?"
        f"dam=0&isSearchRequest=true&sfmr=false&vc=Car&"
        f"maxPrice={price_to or ''}&"
        f"radius={radius_km}&"
        f"query={q}"
    )
    return url

# =========================
# SCRAPER (minimal, best effort)
# =========================

def fetch_html(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.text

def parse_kleinanzeigen(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    items = []
    # Kleinanzeigen markup ändert öfter – wir nehmen robuste Heuristik:
    for a in soup.select("a[href*='/s-anzeige/']"):
        href = a.get("href", "")
        if not href.startswith("/"):
            continue
        link = "https://www.kleinanzeigen.de" + href.split("?")[0]
        title = norm(a.get_text(" ", strip=True))
        if not title or len(title) < 4:
            continue
        item_id = hash_id(link)
        items.append({
            "id": item_id,
            "title": title,
            "link": link,
            "source": "kleinanzeigen",
            "has_photo": True,  # KA hat fast immer thumbs; harte Prüfung ist wacklig
            "year": None,
            "price": None
        })
    # dedupe
    uniq = {}
    for it in items:
        uniq[it["id"]] = it
    return list(uniq.values())[:30]

def parse_mobile(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    items = []
    # mobile.de: Artikelkarten enthalten Links auf /fahrzeuge/details.html?id=...
    for a in soup.select("a[href*='details.html?id=']"):
        href = a.get("href", "")
        if "details.html?id=" not in href:
            continue
        link = href if href.startswith("http") else "https://suchen.mobile.de" + href
        title = a.get_text(" ", strip=True)
        title_n = norm(title)
        if not title_n:
            continue
        item_id = None
        m = re.search(r"id=(\d+)", href)
        item_id = m.group(1) if m else hash_id(link)

        items.append({
            "id": str(item_id),
            "title": title.strip(),
            "link": link,
            "source": "mobile",
            "has_photo": True,  # minimal
            "year": None,
            "price": None
        })
    uniq = {}
    for it in items:
        uniq[it["id"]] = it
    return list(uniq.values())[:30]

# =========================
# FILTER
# =========================

def passes_filters(item: Dict[str, Any], s: Search) -> bool:
    text = f"{item.get('title','')} {item.get('link','')}"
    if contains_blacklist(text):
        return False
    if s.only_photos and not item.get("has_photo", False):
        return False

    # Jahr/Preis: wenn wir sie nicht sauber extrahieren können, lassen wir’s durch.
    year = item.get("year")
    price = item.get("price")

    if s.year_from and year and year < s.year_from:
        return False
    if s.year_to and year and year > s.year_to:
        return False
    if s.price_to and price and price > s.price_to:
        return False

    return True

# =========================
# COMMAND PARSER
# =========================

def parse_set_args(text: str) -> Tuple[str, Dict[str, Any]]:
    """
    Unterstützte Formate:
    /set audi a3 price=10000 radius=300 loc="neustadt an der weinstrasse" year_from=2016 year_to=2022 photos=1
    Kurz:
    /set audi a3 10000 neustadt an der weinstrasse 300
    """
    raw = text.strip()

    # Entferne "/set"
    raw = re.sub(r"^/set\s*", "", raw).strip()

    # key=value Tokens rausziehen
    kv = {}
    tokens = []
    # simple tokenizer mit quotes
    parts = re.findall(r'"[^"]+"|\S+', raw)

    for p in parts:
        if "=" in p and not p.startswith('"'):
            k, v = p.split("=", 1)
            kv[k.strip().lower()] = v.strip().strip('"')
        else:
            tokens.append(p.strip().strip('"'))

    # Query: alles bis zu erstem "price/radius/loc/year..." token ist eh schon raus – tokens sind "rest"
    # Wenn key=... benutzt wurde: query = alles was übrig ist bis wir location erkennen? Wir machen’s simpel:
    query = " ".join(tokens).strip()

    # Defaults
    data = {
        "price_to": parse_int(kv.get("price") or kv.get("max") or kv.get("price_to")),
        "radius_km": parse_int(kv.get("radius")) or DEFAULT_RADIUS_KM,
        "location": kv.get("loc") or kv.get("location") or "DE",
        "year_from": parse_int(kv.get("year_from") or kv.get("bj_from") or kv.get("year>= ")),
        "year_to": parse_int(kv.get("year_to") or kv.get("bj_to")),
        "only_photos": (kv.get("photos") or kv.get("pics") or ("1" if DEFAULT_ONLY_WITH_PHOTOS else "0")) in ("1", "true", "yes"),
    }

    # Wenn User "Kurzformat" schreibt: query price loc radius
    # Beispiel: /set audi a3 10000 neustadt an der weinstrasse 300
    # Wir erkennen: wenn query keine Buchstaben? -> skip. Sonst: wenn price_to fehlt und tokens >= 2:
    if not data["price_to"] and len(tokens) >= 2:
        # suche erste reine Zahl als price
        for i, t in enumerate(tokens):
            if re.fullmatch(r"\d{3,8}", t):
                data["price_to"] = int(t)
                # alles nach price bis zur letzten Zahl (radius) = location
                rest = tokens[i+1:]
                if rest and re.fullmatch(r"\d{1,4}", rest[-1]):
                    data["radius_km"] = int(rest[-1])
                    loc_parts = rest[:-1]
                else:
                    loc_parts = rest
                if loc_parts:
                    data["location"] = " ".join(loc_parts)
                # query = tokens vor price
                query = " ".join(tokens[:i]).strip()
                break

    if not query:
        query = "autos"

    return query, data

# =========================
# TELEGRAM HANDLERS
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ *AutoSuchBot online.*\n\n"
        "*Benutzung:*\n"
        "`/set audi a3 price=10000 radius=300 loc=\"Neustadt an der Weinstraße\" year_from=2016 photos=1`\n\n"
        "*Andere:*\n"
        "`/list`\n"
        "`/del <id>`\n"
        "`/stop`\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with db() as conn:
        conn.execute("DELETE FROM searches WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM seen WHERE chat_id=?", (chat_id,))
    await update.message.reply_text("🛑 Alles gelöscht. Keine Alerts mehr.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with db() as conn:
        rows = conn.execute(
            "SELECT id, query, price_to, radius_km, location, year_from, year_to, only_photos "
            "FROM searches WHERE chat_id=? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("Keine gespeicherten Suchen.")
        return

    lines = ["📌 *Gespeicherte Suchen:*"]
    for r in rows:
        s = Search(
            id=r[0], chat_id=chat_id, query=r[1], price_to=r[2], radius_km=r[3],
            location=r[4], year_from=r[5], year_to=r[6], only_photos=bool(r[7]),
        )
        lines.append(f"\n*{s.id}*\n{format_search(s)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Benutzung: /del <id>")
        return
    sid = parse_int(context.args[0])
    if not sid:
        await update.message.reply_text("Gib eine gültige ID an, z.B. /del 1")
        return

    with db() as conn:
        conn.execute("DELETE FROM searches WHERE chat_id=? AND id=?", (chat_id, sid))
    await update.message.reply_text(f"🗑 Suche {sid} gelöscht.")

async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    query, data = parse_set_args(text)

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO searches (chat_id, query, price_to, radius_km, location, year_from, year_to, only_photos, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                query,
                data["price_to"],
                data["radius_km"],
                data["location"],
                data["year_from"],
                data["year_to"],
                1 if data["only_photos"] else 0,
                int(time.time()),
            ),
        )
        sid = cur.lastrowid

    s = Search(
        id=sid,
        chat_id=chat_id,
        query=query,
        price_to=data["price_to"],
        radius_km=data["radius_km"],
        location=data["location"],
        year_from=data["year_from"],
        year_to=data["year_to"],
        only_photos=bool(data["only_photos"]),
    )

    ka = build_kleinanzeigen_url(s.query, s.price_to)
    mo = build_mobile_url(s.query, s.price_to, s.radius_km)

    reply = (
        "✅ *Suche gespeichert!*\n\n"
        f"{format_search(s)}\n\n"
        f"🔎 *Kleinanzeigen:*\n{ka}\n\n"
        f"🔎 *mobile.de:*\n{mo}\n\n"
        "⏱ Alerts laufen jetzt automatisch."
    )
    await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

# =========================
# ALERT LOOP
# =========================

def load_searches(conn: sqlite3.Connection, chat_id: int) -> List[Search]:
    rows = conn.execute(
        "SELECT id, query, price_to, radius_km, location, year_from, year_to, only_photos "
        "FROM searches WHERE chat_id=? ORDER BY id ASC",
        (chat_id,),
    ).fetchall()
    out = []
    for r in rows:
        out.append(Search(
            id=r[0], chat_id=chat_id, query=r[1], price_to=r[2], radius_km=r[3],
            location=r[4], year_from=r[5], year_to=r[6], only_photos=bool(r[7])
        ))
    return out

def is_seen(conn: sqlite3.Connection, chat_id: int, source: str, item_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM seen WHERE chat_id=? AND source=? AND item_id=?",
        (chat_id, source, item_id),
    ).fetchone()
    return bool(row)

def mark_seen(conn: sqlite3.Connection, chat_id: int, source: str, item_id: str):
    conn.execute(
        "INSERT OR IGNORE INTO seen (chat_id, source, item_id, first_seen) VALUES (?, ?, ?, ?)",
        (chat_id, source, item_id, int(time.time())),
    )

async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    # Wir laufen über alle chats, die Suchen haben
    with db() as conn:
        chats = conn.execute("SELECT DISTINCT chat_id FROM searches").fetchall()

    for (chat_id,) in chats:
        with db() as conn:
            searches = load_searches(conn, chat_id)

        for s in searches:
            ka_url = build_kleinanzeigen_url(s.query, s.price_to)
            mo_url = build_mobile_url(s.query, s.price_to, s.radius_km)

            # Kleinanzeigen
            try:
                html = fetch_html(ka_url)
                items = parse_kleinanzeigen(html)
                await process_items(context, s, items)
            except Exception:
                pass

            # mobile.de
            try:
                html = fetch_html(mo_url)
                items = parse_mobile(html)
                await process_items(context, s, items)
            except Exception:
                pass

async def process_items(context: ContextTypes.DEFAULT_TYPE, s: Search, items: List[Dict[str, Any]]):
    new_hits = []
    with db() as conn:
        for it in items:
            if not passes_filters(it, s):
                continue
            if is_seen(conn, s.chat_id, it["source"], it["id"]):
                continue
            mark_seen(conn, s.chat_id, it["source"], it["id"])
            new_hits.append(it)

    # Max 5 Alerts pro Check pro Suche, sonst spammt’s
    for it in new_hits[:5]:
        text = (
            "🚨 *Neuer Treffer!*\n"
            f"🔎 Suche #{s.id}: *{s.query}*\n"
            f"🌐 Quelle: *{it['source']}*\n"
            f"🧾 {it.get('title','')}\n"
            f"🔗 {it.get('link')}\n"
        )
        await context.bot.send_message(chat_id=s.chat_id, text=text, parse_mode=ParseMode.MARKDOWN)

# =========================
# MAIN
# =========================

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("stop", cmd_stop))

    # Job Queue
    app.job_queue.run_repeating(alert_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
