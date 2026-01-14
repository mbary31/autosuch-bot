#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram AutoSuchBot (Email-forwarder Edition)

Problem (deine Diagnose):
- mobile.de -> HTTP 403 (Datacenter/Railway IP)
- Kleinanzeigen -> Consent/Anti-Bot HTML (blocked=True)

=> Scraping ist auf Railway unzuverlässig.

Lösung (sauber & stabil):
- Richte bei mobile.de und Kleinanzeigen gespeicherte Suchen mit E-Mail-Benachrichtigung ein.
- Dieser Bot liest die E-Mails aus einem Postfach und postet neue Treffer-Links in Telegram.

ENV (Railway -> Variables):
- TELEGRAM_BOT_TOKEN     (Pflicht)
- IMAP_HOST              (z.B. imap.gmail.com)
- IMAP_PORT              (optional, default 993)
- IMAP_USER              (Mailbox-Adresse)
- IMAP_PASS              (App-Passwort / IMAP-Passwort)
- IMAP_FOLDER            (optional, default INBOX)
- CHECK_INTERVAL_SECONDS (optional, default 180)
- MAX_LINKS_PER_RUN      (optional, default 10)

Commands:
- /start
- /status
- /run    (manueller Check)
"""

from __future__ import annotations

import asyncio
import imaplib
import json
import os
import re
import ssl
from email import message_from_bytes
from email.header import decode_header
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes


# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

IMAP_HOST = os.getenv("IMAP_HOST", "").strip()
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "").strip()
IMAP_PASS = os.getenv("IMAP_PASS", "").strip()
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX").strip()

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "180"))
MAX_LINKS_PER_RUN = int(os.getenv("MAX_LINKS_PER_RUN", "10"))

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
SEEN_FILE = DATA_DIR / "seen.json"
CHATS_FILE = DATA_DIR / "chats.json"

URL_RE = re.compile(r"https?://[^\s<>()\"']+")


# =========================
# Storage
# =========================
def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SEEN_FILE.exists():
        SEEN_FILE.write_text(
            json.dumps({"seen_message_ids": [], "seen_links": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if not CHATS_FILE.exists():
        CHATS_FILE.write_text(json.dumps({"chats": []}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_seen() -> Dict[str, List[str]]:
    ensure_data_dir()
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen_message_ids": [], "seen_links": []}


def save_seen(seen: Dict[str, List[str]]) -> None:
    ensure_data_dir()
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SEEN_FILE)


def load_chats() -> List[int]:
    ensure_data_dir()
    try:
        obj = json.loads(CHATS_FILE.read_text(encoding="utf-8"))
        return [int(x) for x in obj.get("chats", [])]
    except Exception:
        return []


def save_chat(chat_id: int) -> None:
    ensure_data_dir()
    chats = set(load_chats())
    chats.add(int(chat_id))
    CHATS_FILE.write_text(json.dumps({"chats": sorted(chats)}, ensure_ascii=False, indent=2), encoding="utf-8")


# =========================
# Email parsing
# =========================
def _decode_header_value(v: Optional[str]) -> str:
    if not v:
        return ""
    parts = decode_header(v)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def extract_text_from_msg(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp.lower():
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/html" and "attachment" not in disp.lower():
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace")
                html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
                html = re.sub(r"(?is)<.*?>", " ", html)
                return re.sub(r"\s+", " ", html).strip()
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    body = payload.decode(charset, errors="replace")
    if msg.get_content_type() == "text/html":
        body = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", body)
        body = re.sub(r"(?is)<.*?>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
    return body


def extract_links(text: str) -> List[str]:
    links = URL_RE.findall(text or "")
    clean = []
    seen = set()
    for u in links:
        u = u.strip().rstrip(").,;!?\"'")
        if u not in seen:
            seen.add(u)
            clean.append(u)
    return clean


# =========================
# IMAP
# =========================
def imap_fetch_latest(limit: int = 35) -> List[Tuple[str, str, str]]:
    if not (IMAP_HOST and IMAP_USER and IMAP_PASS):
        raise RuntimeError("IMAP env vars fehlen (IMAP_HOST/IMAP_USER/IMAP_PASS).")

    ctx = ssl.create_default_context()
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
    M.login(IMAP_USER, IMAP_PASS)
    M.select(IMAP_FOLDER)

    typ, data = M.uid("search", None, "ALL")
    if typ != "OK":
        M.logout()
        return []

    uids = data[0].split()
    if not uids:
        M.logout()
        return []

    uids = uids[-limit:][::-1]  # newest first

    out: List[Tuple[str, str, str]] = []
    for uid in uids:
        uid_s = uid.decode("utf-8", errors="replace")
        typ, msg_data = M.uid("fetch", uid, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue
        raw = msg_data[0][1]
        msg = message_from_bytes(raw)
        subject = _decode_header_value(msg.get("Subject"))
        body = extract_text_from_msg(msg)
        out.append((uid_s, subject, body))

    M.logout()
    return out


async def fetch_latest_async(limit: int = 35) -> List[Tuple[str, str, str]]:
    return await asyncio.to_thread(imap_fetch_latest, limit)


# =========================
# Bot logic
# =========================
async def check_inbox_and_post(chat_id: int, bot, verbose: bool = False) -> str:
    seen = load_seen()
    seen_ids: Set[str] = set(seen.get("seen_message_ids", []))
    seen_links: Set[str] = set(seen.get("seen_links", []))

    mails = await fetch_latest_async(limit=35)

    new_msgs = 0
    new_links: List[str] = []

    # process oldest -> newest
    for uid, subject, body in reversed(mails):
        if uid in seen_ids:
            continue

        text = f"{subject}\n{body}"
        links = extract_links(text)

        # keep only relevant links
        for u in links:
            if ("mobile.de" in u) or ("kleinanzeigen.de" in u):
                if u not in seen_links:
                    new_links.append(u)
                    seen_links.add(u)

        seen_ids.add(uid)
        new_msgs += 1

    # persist
    seen["seen_message_ids"] = list(seen_ids)[-5000:]
    seen["seen_links"] = list(seen_links)[-20000:]
    save_seen(seen)

    if not new_links:
        return "📭 Keine neuen Links gefunden." if verbose else "ok"

    new_links = new_links[-MAX_LINKS_PER_RUN:]

    msg = "🔔 *Neue Treffer aus E-Mail-Alerts:*

" + "\n".join(new_links)
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

    if verbose:
        return f"✅ Gesendet: {len(new_links)} Link(s) | Neue Mails verarbeitet: {new_msgs}"
    return "ok"


# =========================
# Telegram handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_chat(update.effective_chat.id)
    await update.message.reply_text(
        "🧰 *AutoSuchBot (E-Mail Forwarder) läuft.*

"
        "Ich lese die E-Mail-Benachrichtigungen deiner gespeicherten Suchen (mobile.de & Kleinanzeigen)
"
        "und poste neue Links hier.

"
        "Befehle:
"
        "/status
"
        "/run (manueller Check)

"
        "Wichtig: IMAP-Variablen in Railway setzen (IMAP_HOST/IMAP_USER/IMAP_PASS).",
        parse_mode=ParseMode.MARKDOWN,
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_chat(update.effective_chat.id)
    seen = load_seen()
    await update.message.reply_text(
        "📌 Status
"
        f"IMAP_HOST: {'✅' if IMAP_HOST else '❌'}
"
        f"IMAP_USER: {'✅' if IMAP_USER else '❌'}
"
        f"IMAP_FOLDER: {IMAP_FOLDER}
"
        f"Intervall: {CHECK_INTERVAL_SECONDS}s
"
        f"Gesehene Mails: {len(seen.get('seen_message_ids', []))}
"
        f"Gesehene Links: {len(seen.get('seen_links', []))}"
    )


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_chat(update.effective_chat.id)
    await update.message.reply_text("🔎 Prüfe Postfach…")
    try:
        diag = await check_inbox_and_post(update.effective_chat.id, context.bot, verbose=True)
    except Exception as e:
        diag = f"❌ IMAP Fehler: {e}"
    await update.message.reply_text(diag)


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = context.bot
    chats = load_chats()
    if not chats:
        return
    for cid in chats:
        try:
            await check_inbox_and_post(cid, bot, verbose=False)
        except Exception as e:
            print(f"[scheduled_job] chat {cid} error: {e}")


def main() -> None:
    ensure_data_dir()

    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN fehlt.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("run", run_cmd))

    if app.job_queue is None:
        print("⚠️ JobQueue fehlt. Installiere python-telegram-bot[job-queue]. Scheduler AUS.")
    else:
        app.job_queue.run_repeating(scheduled_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    print("✅ Bot läuft (polling)…")
    app.run_polling()


if __name__ == "__main__":
    main()
