#!/usr/bin/env python3
"""
Telegram Group Intelligence Bot
Single-file Render app. Uses a Telegram channel as the ONLY message store.

Required environment variables:
  BOT_TOKEN             Telegram bot token from @BotFather
  TELEGRAM_API_ID       API ID from https://my.telegram.org
  TELEGRAM_API_HASH     API hash from https://my.telegram.org
  TELEGRAM_SESSION      Telethon StringSession (generate with: python app.py --generate-session)
  STORAGE_CHANNEL       Private channel username or numeric ID (e.g. -1001234567890)
  GROQ_API_KEY          Groq API key
  ADMIN_USER_ID         Your personal Telegram numeric user ID

Optional:
  GROQ_MODEL            Default: llama-3.3-70b-versatile
  MAX_STORAGE_SCAN      Default: 2500
  MAX_SYNC_PER_GROUP    Default: 300
  TIMEZONE              Default: Asia/Kolkata

Render:
  Build command: pip install -U telethon python-telegram-bot groq aiohttp
  Start command: python app.py

Create a PRIVATE Telegram channel using your personal account. Your personal account
must be able to post in it. The bot does not need to be an administrator of the channel:
the Telethon user session writes and reads the channel.

Run `python app.py --generate-session` on your own computer ONCE to generate
TELEGRAM_SESSION. Never share that string; it grants access to your Telegram account.
"""

import os
import sys
import json
import asyncio
import logging
import argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def load_local_env():
    """Load local environment settings beside app.py.

    Windows Notepad sometimes saves a file named ".env" as ".env.txt"
    when file extensions are hidden. Support both names. Existing system
    environment variables (such as Render's) always take precedence.
    """
    folder = os.path.dirname(os.path.abspath(__file__))
    candidates = (os.path.join(folder, ".env"), os.path.join(folder, ".env.txt"))
    env_path = next((candidate for candidate in candidates if os.path.isfile(candidate)), None)
    if env_path is None:
        print("No local .env or .env.txt file found beside app.py; using system environment variables.")
        return

    with open(env_path, "r", encoding="utf-8-sig") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)
    print(f"Loaded local settings from {os.path.basename(env_path)}")


# Load laptop .env values before reading configuration below.
load_local_env()

from aiohttp import web
from groq import AsyncGroq
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("telegram-intelligence")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
SESSION_STRING = os.getenv("TELEGRAM_SESSION", "").strip()
STORAGE_CHANNEL = os.getenv("STORAGE_CHANNEL", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip()
MAX_STORAGE_SCAN = int(os.getenv("MAX_STORAGE_SCAN", "2500"))
MAX_SYNC_PER_GROUP = int(os.getenv("MAX_SYNC_PER_GROUP", "300"))

LOCAL_TZ = ZoneInfo(TIMEZONE)
telethon_client = None
storage_entity = None
groq_client = None
known_keys = set()
storage_lock = asyncio.Lock()


def now_local():
    return datetime.now(LOCAL_TZ)


def is_admin(update: Update) -> bool:
    return bool(
        update.effective_user
        and ADMIN_USER_ID
        and str(update.effective_user.id) == ADMIN_USER_ID
    )


async def reply_long(message, text):
    """Telegram messages are limited; split long AI answers safely."""
    text = text or "I couldn't generate an answer."
    limit = 3900
    for start in range(0, len(text), limit):
        await message.reply_text(text[start:start + limit], disable_web_page_preview=True)


def parse_storage_record(raw):
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("kind") in ("group_message", "chat"):
            return data
    except Exception:
        pass
    return None


async def write_record(record):
    """Append one JSON record to the private Telegram storage channel."""
    key = record.get("key")
    if key and key in known_keys:
        return False

    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    # Leave room below Telegram's message size limit.
    if len(payload) > 3900:
        record["text"] = (record.get("text") or "")[:2300]
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))

    async with storage_lock:
        if key and key in known_keys:
            return False
        try:
            await telethon_client.send_message(storage_entity, payload, link_preview=False)
            if key:
                known_keys.add(key)
            return True
        except FloodWaitError as exc:
            log.warning("Telegram rate limit: waiting %s seconds", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
            await telethon_client.send_message(storage_entity, payload, link_preview=False)
            if key:
                known_keys.add(key)
            return True


async def load_known_keys():
    """Load recent record keys from channel to avoid duplicate collection."""
    count = 0
    async for msg in telethon_client.iter_messages(storage_entity, limit=MAX_STORAGE_SCAN):
        record = parse_storage_record(msg.raw_text or "")
        if record and record.get("key"):
            known_keys.add(record["key"])
        count += 1
    log.info("Loaded %s existing channel records; %s unique message keys", count, len(known_keys))


async def collect_group_message(event):
    try:
        if not event.is_group or not event.message:
            return
        chat = await event.get_chat()
        if not chat:
            return
        if getattr(chat, "id", None) == getattr(storage_entity, "id", None):
            return

        msg = event.message
        text = (msg.message or "").strip()
        if not text and not msg.media:
            return

        chat_id = str(event.chat_id)
        message_id = int(msg.id)
        sender_id = str(msg.sender_id or "")
        group_title = getattr(chat, "title", None) or "Telegram group"
        sender = ""
        try:
            sender_obj = await event.get_sender()
            if sender_obj:
                sender = (
                    getattr(sender_obj, "username", None)
                    or " ".join(filter(None, [
                        getattr(sender_obj, "first_name", None),
                        getattr(sender_obj, "last_name", None),
                    ]))
                )
        except Exception:
            pass

        date = msg.date
        if date and date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        date_iso = (date or datetime.now(timezone.utc)).isoformat()

        record = {
            "kind": "group_message",
            "key": f"{chat_id}:{message_id}",
            "group_id": chat_id,
            "group": group_title[:150],
            "message_id": message_id,
            "sender_id": sender_id,
            "sender": (sender or "Unknown")[:100],
            "date": date_iso,
            "text": text[:2500] if text else "[Media attachment; text/caption unavailable]",
            "has_media": bool(msg.media),
        }
        await write_record(record)
    except Exception:
        log.exception("Could not collect a Telegram group message")


async def get_records(limit=None):
    records = []
    scan_limit = limit or MAX_STORAGE_SCAN
    async for msg in telethon_client.iter_messages(storage_entity, limit=scan_limit):
        record = parse_storage_record(msg.raw_text or "")
        if record:
            records.append(record)
    records.reverse()
    return records


def record_local_date(record):
    try:
        dt = datetime.fromisoformat(record["date"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).date()
    except Exception:
        return None


def choose_relevant_records(records, question, max_items=65):
    """Simple local ranking before sending only relevant snippets to the AI."""
    q = question.lower()
    words = {
        w.strip(".,!?():;[]{}'\"")
        for w in q.split()
        if len(w.strip(".,!?():;[]{}'\"")) > 2
    }
    today = now_local().date()
    wants_today = any(x in q for x in ("today", "this morning", "tonight", "aaj"))
    wants_yesterday = "yesterday" in q
    yesterday = today - timedelta(days=1)

    pool = []
    for rec in records:
        if rec.get("kind") != "group_message":
            continue
        d = record_local_date(rec)
        if wants_today and d != today:
            continue
        if wants_yesterday and d != yesterday:
            continue
        body = (rec.get("text") or "").lower()
        group = (rec.get("group") or "").lower()
        overlap = sum(1 for w in words if w in body or w in group)
        score = overlap * 4
        if d == today:
            score += 2
        if not words or q.strip() in ("what happened", "summary", "summarize"):
            score += 1
        pool.append((score, rec))

    if not pool and (wants_today or wants_yesterday):
        return []
    if words:
        matched = [x for x in pool if x[0] > 2]
        if matched:
            pool = matched

    # Keep most recent records while allowing relevance to influence ordering.
    pool.sort(key=lambda x: (x[0], x[1].get("date", "")), reverse=True)
    selected = [r for _, r in pool[:max_items]]
    selected.sort(key=lambda r: r.get("date", ""))
    return selected


def build_context(records):
    chunks = []
    for r in records:
        line = (
            f"[{r.get('date','')}] Group: {r.get('group','?')} | "
            f"Sender: {r.get('sender','?')} | Message: {r.get('text','')}"
        )
        chunks.append(line[:1800])
    context = "\n".join(chunks)
    return context[-26000:]


async def get_chat_history(user_id, limit=8):
    records = await get_records(limit=MAX_STORAGE_SCAN)
    history = [
        r for r in records
        if r.get("kind") == "chat" and str(r.get("user_id")) == str(user_id)
    ]
    return history[-limit:]


async def save_chat_turn(user_id, role, text):
    record = {
        "kind": "chat",
        "user_id": str(user_id),
        "role": role,
        "date": datetime.now(timezone.utc).isoformat(),
        "text": (text or "")[:2500],
    }
    await write_record(record)


async def answer_question(user_id, question):
    records = await get_records(limit=MAX_STORAGE_SCAN)
    relevant = choose_relevant_records(records, question)
    context = build_context(relevant)

    history = await get_chat_history(user_id, limit=8)
    messages = [{
        "role": "system",
        "content": (
            "You are a friendly, helpful Telegram Group Intelligence assistant. "
            "Talk naturally like a human assistant. Answer in simple, clear English. "
            "Use ONLY the supplied Telegram group records for claims about what group "
            "members said or shared. Never invent messages, names, links, dates, or "
            "opportunities. If the stored messages do not contain the answer, say so "
            "clearly. Distinguish facts from your interpretation. When useful, mention "
            "the group name, sender, and message date. Summarize noise and prioritize "
            "announcements, opportunities, deadlines, technical knowledge, decisions, "
            "and safety warnings. If the user asks a general question unrelated to the "
            "groups, answer normally and explain that group records are not needed."
        )
    }]

    for item in history:
        role = item.get("role")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": item.get("text", "")[:1800]})

    messages.append({
        "role": "user",
        "content": (
            f"Question: {question}\n\n"
            f"Relevant stored group messages ({len(relevant)} selected):\n"
            f"{context if context else '[No matching group messages found in storage.]'}"
        )
    })

    completion = await groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.3,
        max_tokens=1400,
    )
    return completion.choices[0].message.content.strip()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Sorry, this is a private personal assistant.")
        return
    await update.message.reply_text(
        "Hey! I'm your Telegram Group Intelligence assistant.\n\n"
        "Ask me naturally, for example:\n"
        "• What happened in my groups today?\n"
        "• Find AWS discussions from yesterday.\n"
        "• Did anyone share an internship?\n"
        "• Explain the most useful technical message.\n\n"
        "Commands:\n"
        "/groups - list groups visible to your account\n"
        "/sync 1 - import the last 1 day of group history\n"
        "/sync 7 - import the last 7 days (more messages)\n"
        "/status - check the system\n"
        "/help - show help\n\n"
        "I collect new group messages while this Render service is running."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    count = len(known_keys)
    await update.message.reply_text(
        f"System is running.\nStored message keys loaded: {count}\n"
        f"Storage: Telegram channel\nAI: Groq ({GROQ_MODEL})"
    )


async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text("Checking groups your Telegram account can access...")
    lines = []
    async for dialog in telethon_client.iter_dialogs():
        if dialog.is_group:
            lines.append(f"• {dialog.name} (ID: {dialog.id})")
    if not lines:
        await update.message.reply_text("I couldn't find any groups.")
        return
    text = "Groups visible to your personal Telegram account:\n\n" + "\n".join(lines[:100])
    await reply_long(update.message, text)


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    days = 1
    if context.args:
        try:
            days = max(1, min(int(context.args[0]), 30))
        except ValueError:
            await update.message.reply_text("Use /sync 1 or /sync 7 (maximum 30 days).")
            return

    await update.message.reply_text(
        f"Starting history import for the last {days} day(s). "
        "This may take a while for large groups."
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    saved = 0
    scanned = 0
    skipped = 0

    try:
        async for dialog in telethon_client.iter_dialogs():
            if not dialog.is_group:
                continue
            if getattr(dialog.entity, "id", None) == getattr(storage_entity, "id", None):
                continue
            per_group = 0
            try:
                async for msg in telethon_client.iter_messages(dialog.entity, limit=MAX_SYNC_PER_GROUP):
                    if msg.date and msg.date.replace(tzinfo=timezone.utc) < cutoff:
                        break
                    scanned += 1
                    text = (msg.message or "").strip()
                    if not text and not msg.media:
                        skipped += 1
                        continue
                    sender = ""
                    if msg.sender:
                        sender = (
                            getattr(msg.sender, "username", None)
                            or " ".join(filter(None, [
                                getattr(msg.sender, "first_name", None),
                                getattr(msg.sender, "last_name", None),
                            ]))
                        )
                    record = {
                        "kind": "group_message",
                        "key": f"{dialog.id}:{msg.id}",
                        "group_id": str(dialog.id),
                        "group": (dialog.name or "Telegram group")[:150],
                        "message_id": int(msg.id),
                        "sender_id": str(msg.sender_id or ""),
                        "sender": (sender or "Unknown")[:100],
                        "date": msg.date.isoformat() if msg.date else datetime.now(timezone.utc).isoformat(),
                        "text": text[:2500] if text else "[Media attachment; text/caption unavailable]",
                        "has_media": bool(msg.media),
                    }
                    if await write_record(record):
                        saved += 1
                    per_group += 1
            except FloodWaitError as exc:
                log.warning("Flood wait during sync: %s seconds", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
            except Exception:
                log.exception("Could not sync group %s", dialog.name)

    except Exception:
        log.exception("History sync failed")

    await update.message.reply_text(
        f"History import finished.\n"
        f"New messages stored: {saved}\n"
        f"Messages scanned: {scanned}\n"
        f"Skipped empty messages: {skipped}\n\n"
        "You can now ask questions about the stored messages."
    )


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("This is a private bot.")
        return
    question = (update.message.text or "").strip()
    if not question:
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )
    try:
        answer = await answer_question(update.effective_user.id, question)
        await save_chat_turn(update.effective_user.id, "user", question)
        await save_chat_turn(update.effective_user.id, "assistant", answer)
        await reply_long(update.message, answer)
    except Exception as exc:
        log.exception("AI answer failed")
        await update.message.reply_text(
            "I hit a problem while answering. Please try again in a moment. "
            f"(Technical detail: {str(exc)[:180]})"
        )


async def generate_session():
    if not API_ID or not API_HASH:
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH first.")
        return
    phone = os.getenv("TELEGRAM_PHONE", "").strip() or input("Your Telegram phone number (with country code): ").strip()
    client = TelegramClient(StringSession(), int(API_ID), API_HASH)
    await client.start(phone=phone)
    print("\nYour TELEGRAM_SESSION string is below. Keep it SECRET.")
    print("Copy it into Render Environment as TELEGRAM_SESSION.\n")
    print(client.session.save())
    await client.disconnect()


async def list_dialogs():
    """Print dialog names and IDs so a private storage channel ID is easy to find."""
    if not (API_ID and API_HASH and SESSION_STRING):
        print("Set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_SESSION first.")
        return
    client = TelegramClient(StringSession(SESSION_STRING), int(API_ID), API_HASH)
    await client.start()
    try:
        async for dialog in client.iter_dialogs():
            kind = "GROUP" if dialog.is_group else ("CHANNEL" if dialog.is_channel else "CHAT")
            print(f"{kind:7} | {dialog.id} | {dialog.name}")
    finally:
        await client.disconnect()


async def resolve_storage_entity(client, configured_value):
    """
    Resolve a private storage channel from the signed-in user's dialogs.

    Telethon needs an access hash for private channels. A bare positive
    channel ID is not enough, and get_entity(positive_number) can be
    interpreted as a user ID. Dialogs provide the cached entity/access hash.
    """
    value = str(configured_value).strip()

    # A username or t.me-style public identifier can be resolved directly.
    if not value.lstrip("-").isdigit():
        try:
            entity = await client.get_entity(value)
        except Exception as exc:
            raise RuntimeError(
                "Could not resolve STORAGE_CHANNEL. Check its @username or "
                "invite/access and make sure the Telethon personal account joined it."
            ) from exc
        if not getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
            raise RuntimeError("STORAGE_CHANNEL resolved, but it is not a Telegram channel or supergroup.")
        return entity

    configured_id = int(value)
    raw_id = abs(configured_id)

    # Search dialogs so Telethon can use the channel's cached access hash.
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not (dialog.is_channel or dialog.is_group):
            continue

        dialog_id = int(dialog.id)
        entity_id = int(getattr(entity, "id", 0) or 0)
        if dialog_id == configured_id or entity_id == raw_id:
            log.info("Resolved storage channel from Telegram dialogs: %s (ID %s)", dialog.name, dialog_id)
            return entity

    raise RuntimeError(
        f"STORAGE_CHANNEL '{value}' was not found in the personal Telegram account's dialogs. "
        "Join the private storage channel with the same personal account used for TELEGRAM_SESSION, "
        "give that account permission to post, then set STORAGE_CHANNEL to the channel ID printed "
        "by 'python app.py --list-dialogs' (usually a -100... ID). Do not use the bot's user ID."
    )


async def run_app():
    global telethon_client, storage_entity, groq_client

    required = {
        "BOT_TOKEN": BOT_TOKEN,
        "TELEGRAM_API_ID": API_ID,
        "TELEGRAM_API_HASH": API_HASH,
        "TELEGRAM_SESSION": SESSION_STRING,
        "STORAGE_CHANNEL": STORAGE_CHANNEL,
        "GROQ_API_KEY": GROQ_API_KEY,
        "ADMIN_USER_ID": ADMIN_USER_ID,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("Missing Render environment variables: " + ", ".join(missing))

    telethon_client = TelegramClient(
        StringSession(SESSION_STRING),
        int(API_ID),
        API_HASH,
    )
    await telethon_client.start()
    storage_entity = await resolve_storage_entity(telethon_client, STORAGE_CHANNEL)
    groq_client = AsyncGroq(api_key=GROQ_API_KEY)

    await load_known_keys()

    # Collect newly arriving messages from groups the signed-in account can access.
    telethon_client.add_event_handler(collect_group_message, events.NewMessage(incoming=True))

    bot = Application.builder().token(BOT_TOKEN).build()
    bot.add_handler(CommandHandler("start", start_command))
    bot.add_handler(CommandHandler("help", help_command))
    bot.add_handler(CommandHandler("status", status_command))
    bot.add_handler(CommandHandler("groups", groups_command))
    bot.add_handler(CommandHandler("sync", sync_command))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling(drop_pending_updates=True)

    async def health(_request):
        return web.json_response({"ok": True, "service": "telegram-group-intelligence"})

    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()

    log.info("Telegram intelligence bot is running.")
    try:
        await asyncio.Event().wait()
    finally:
        await bot.updater.stop()
        await bot.stop()
        await bot.shutdown()
        await runner.cleanup()
        await telethon_client.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate-session", action="store_true")
    parser.add_argument("--list-dialogs", action="store_true")
    args = parser.parse_args()

    if args.generate_session:
        asyncio.run(generate_session())
    elif args.list_dialogs:
        asyncio.run(list_dialogs())
    else:
        # A laptop running the script without Render credentials is usually
        # preparing the Telethon session. Explain the correct command instead
        # of showing a confusing missing-Render-variables traceback.
        required_local = {
            "BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_API_ID": API_ID,
            "TELEGRAM_API_HASH": API_HASH,
            "TELEGRAM_SESSION": SESSION_STRING,
            "STORAGE_CHANNEL": STORAGE_CHANNEL,
            "GROQ_API_KEY": GROQ_API_KEY,
            "ADMIN_USER_ID": ADMIN_USER_ID,
        }
        missing_local = [name for name, value in required_local.items() if not value]
        if missing_local:
            print("This command starts the complete bot and requires all environment variables.")
            print("To generate your Telegram session on this laptop, run:")
            print("  python app.py --generate-session")
            print("Before running it, put TELEGRAM_API_ID and TELEGRAM_API_HASH in a .env file beside app.py.")
            print("Do not paste your session string, API hash, or tokens into chat or GitHub.")
            return
        try:
            asyncio.run(run_app())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
