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
# Groq retired llama-3.3-70b-versatile for developer-tier accounts.
# Automatically migrate the old Render environment value to its recommended replacement.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
if GROQ_MODEL == "llama-3.3-70b-versatile":
    log.warning("GROQ_MODEL is retired; automatically switching to openai/gpt-oss-120b")
    GROQ_MODEL = "openai/gpt-oss-120b"
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip()
MAX_STORAGE_SCAN = int(os.getenv("MAX_STORAGE_SCAN", "2500"))
MAX_SYNC_PER_GROUP = int(os.getenv("MAX_SYNC_PER_GROUP", "300"))

LOCAL_TZ = ZoneInfo(TIMEZONE)
telethon_client = None
storage_entity = None
groq_client = None
known_keys = set()
selected_groups = {}
bot_paused = False
storage_lock = asyncio.Lock()


def now_local():
    return datetime.now(LOCAL_TZ)


def is_admin(update: Update) -> bool:
    return bool(
        update.effective_user
        and ADMIN_USER_ID
        and str(update.effective_user.id) == ADMIN_USER_ID
    )


def format_ai_for_telegram(text):
    """Convert common Markdown emphasis to Telegram HTML and remove stray markers."""
    import html
    value = (text or "").strip()
    # Escape raw HTML first; then add only the formatting tags we intentionally support.
    value = html.escape(value, quote=False)
    value = re.sub(r"\\*\\*(.+?)\\*\\*", r"<b>\\1</b>", value, flags=re.DOTALL)
    value = re.sub(r"(?<!\\*)\\*([^*\\n]+)\\*(?!\\*)", r"<i>\\1</i>", value)
    value = re.sub(r"__([^_\\n]+)__", r"<b>\\1</b>", value)
    value = re.sub(r"(?<!_)_([^_\\n]+)_(?!_)", r"<i>\\1</i>", value)
    value = re.sub(r"`([^`\\n]+)`", r"<code>\\1</code>", value)
    value = re.sub(r"(?m)^\\s*[-*]\\s+", "• ", value)
    value = value.replace("**", "").replace("__", "")
    return value


async def reply_long(message, text):
    """Send readable Telegram HTML with safe plain-text fallback."""
    text = text or "I couldn't find enough information to answer that."
    formatted = format_ai_for_telegram(text)
    # Split at paragraph boundaries to avoid cutting HTML tags where possible.
    chunks = []
    while len(formatted) > 3900:
        cut = formatted.rfind("\\n", 0, 3900)
        if cut < 1000:
            cut = 3900
        chunks.append(formatted[:cut])
        formatted = formatted[cut:].lstrip()
    if formatted:
        chunks.append(formatted)
    for chunk in chunks:
        try:
            await message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            log.warning("Telegram formatting failed; sending plain text", exc_info=True)
            plain = re.sub(r"</?[^>]+>", "", chunk)
            await message.reply_text(plain, disable_web_page_preview=True)


def parse_storage_record(raw):
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("kind") in ("group_message", "chat", "group_config_add", "group_config_remove"):
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
        if bot_paused:
            return
        if not event.is_group or not event.message:
            return
        chat = await event.get_chat()
        if not chat:
            return
        if getattr(chat, "id", None) == getattr(storage_entity, "id", None):
            return

        from telethon.utils import get_peer_id
        chat_id = str(get_peer_id(chat))
        if chat_id not in selected_groups:
            return

        msg = event.message
        text = (msg.message or "").strip()
        if not text and not msg.media:
            return

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


async def load_selected_groups():
    """Restore the selected-group allow-list from saved config records."""
    selected_groups.clear()
    changes = []
    async for msg in telethon_client.iter_messages(storage_entity, limit=MAX_STORAGE_SCAN):
        rec = parse_storage_record(msg.raw_text or "")
        if rec and rec.get("kind") in ("group_config_add", "group_config_remove"):
            changes.append(rec)
    for rec in reversed(changes):
        gid = str(rec.get("group_id", ""))
        if not gid:
            continue
        if rec["kind"] == "group_config_add":
            selected_groups[gid] = rec.get("group", "Telegram group")
        else:
            selected_groups.pop(gid, None)
    log.info("Restored %s monitored groups", len(selected_groups))


async def resolve_group_argument(value):
    """Resolve a group ID or public @username using the logged-in user account."""
    value = value.strip()
    if value.startswith("https://t.me/"):
        value = value.rstrip("/").split("/")[-1]
    if value.startswith("@") or not value.lstrip("-").isdigit():
        try:
            entity = await telethon_client.get_entity(value)
            if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
                return entity
        except Exception:
            pass
    if value.lstrip("-").isdigit():
        wanted = int(value)
        async for dialog in telethon_client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                from telethon.utils import get_peer_id
                if str(get_peer_id(dialog.entity)) == str(wanted) or int(getattr(dialog.entity, "id", 0) or 0) == abs(wanted):
                    return dialog.entity
    return None


async def addgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /addgroup GROUP_ID\nFirst run /groups and copy the group's ID.")
        return
    entity = await resolve_group_argument(" ".join(context.args))
    if not entity or not (getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)):
        await update.message.reply_text("Group not found. Ensure your personal Telegram account has joined it, then copy its ID from /groups.")
        return
    from telethon.utils import get_peer_id
    gid = str(get_peer_id(entity))
    name = (getattr(entity, "title", None) or "Telegram group")[:150]
    if gid in selected_groups:
        await update.message.reply_text(f"Already monitoring {name}.")
        return
    await write_record({"kind": "group_config_add", "group_id": gid, "group": name, "date": datetime.now(timezone.utc).isoformat()})
    selected_groups[gid] = name
    await update.message.reply_text(f"Added: {name}\nOnly new messages from this group will now be collected. Use /sync 1 to import recent history.")


async def removegroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /removegroup GROUP_ID")
        return
    value = " ".join(context.args).strip()
    entity = await resolve_group_argument(value)
    from telethon.utils import get_peer_id
    gid = str(get_peer_id(entity)) if entity else value
    if gid not in selected_groups:
        await update.message.reply_text("That group is not being monitored. Use /mygroups.")
        return
    name = selected_groups[gid]
    await write_record({"kind": "group_config_remove", "group_id": gid, "group": name, "date": datetime.now(timezone.utc).isoformat()})
    selected_groups.pop(gid, None)
    await update.message.reply_text(f"Removed: {name}. New messages will no longer be collected.")


async def mygroups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not selected_groups:
        await update.message.reply_text("No monitored groups. Use /groups, then /addgroup GROUP_ID.")
        return
    lines = [f"• {name} (ID: {gid})" for gid, name in selected_groups.items()]
    await reply_long(update.message, "Currently monitored groups:\n\n" + "\n".join(lines))


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
        if str(rec.get("group_id", "")) not in selected_groups:
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
    history = await get_chat_history(user_id, limit=8)
    previous_user_questions = [
        item.get("text", "") for item in history
        if item.get("role") == "user"
    ][-3:]
    retrieval_question = " ".join(previous_user_questions + [question])
    relevant = choose_relevant_records(records, retrieval_question, max_items=100)
    context = build_context(relevant)
    messages = [{
        "role": "system",
        "content": (
            "You are the user's friendly Telegram group chat companion. Speak directly to the user in simple, natural English. "
            "Never say 'the snippet you shared', 'messages you pasted', or 'excerpt you shared'. Refer naturally to "
            "the selected group and say 'in the messages I can access' only when explaining a data limitation. "
            "Do not claim to know the whole group or its full membership unless the supplied records prove it. "
            "Answer the user's actual wording. If the question is unclear (for example, 'how many people use the in their messages?'), "
            "ask one short clarification instead of guessing what they mean. "
            "For questions asking how many people, count distinct sender identities in the relevant selected messages; "
            "do not confuse number of messages with number of people. For questions asking how many messages contain a word, "
            "count matching messages only if the supplied context is sufficient; otherwise explain the limitation. "
            "Never invent numbers. State the scope briefly and plainly, without awkward disclaimers. "
            "Keep simple answers to 1-3 short sentences. Use a few bullets only when listing multiple useful points. "
            "For a conversation question, explain who said what in a natural way. For a follow-up, use recent chat history "
            "to understand references such as he, she, they, that, and why. "
            "Use occasional relevant emojis, not one on every line. Emphasize only key words or the answer; do not use "
            "asterisks, Markdown headings, or Markdown formatting. Write plain text and use simple bold/italic only if supported. "
            "Use ONLY the supplied group records for claims about members and messages. Never invent identities, relationships, "
            "motives, replies, dates, or events. Treat media-only posts as unknown. For unrelated general questions, answer normally."
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
    global bot_paused
    if not is_admin(update):
        await update.message.reply_text("Sorry, this is a private personal assistant.")
        return
    bot_paused = False
    await update.message.reply_text(
        "Bot resumed! Group monitoring and AI answers are enabled.\n\n"
        "Hey! I'm your Telegram Group Intelligence assistant.\n\n"
        "Ask me naturally, for example:\n"
        "• What happened in my selected groups today?\n"
        "• Find AWS discussions from yesterday.\n"
        "• Did anyone share an internship?\n\n"
        "Commands:\n"
        "/groups - list groups your account can access\n"
        "/addgroup GROUP_ID - monitor a group\n"
        "/mygroups - list monitored groups\n"
        "/removegroup GROUP_ID - stop monitoring a group\n"
        "/sync 1 - import 1 day of history from selected groups\n"
        "/stop - pause collection and AI replies\n"
        "/start - resume collection and AI replies\n"
        "/status - check the system\n"
        "/help - show help\n\n"
        "Only selected groups are collected and used for answers."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    if not is_admin(update):
        return
    bot_paused = True
    await update.message.reply_text(
        "Bot paused. I will stop collecting new group messages and answering AI questions. "
        "Send /start whenever you want to resume."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    count = len(known_keys)
    state = "PAUSED" if bot_paused else "RUNNING"
    await update.message.reply_text(
        f"System status: {state}.\nStored message keys loaded: {count}\n"
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
    from telethon.utils import get_peer_id
    marked = []
    async for dialog in telethon_client.iter_dialogs():
        if dialog.is_group:
            gid = str(get_peer_id(dialog.entity))
            prefix = "★ " if gid in selected_groups else ""
            marked.append(f"{prefix}{dialog.name} (ID: {gid})")
    await reply_long(update.message, "Groups visible to your account (★ = monitored):\n\n" + "\n".join(marked[:100]) + "\n\nAdd with /addgroup GROUP_ID.")


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if bot_paused:
        await update.message.reply_text("Bot is paused. Send /start before running /sync.")
        return
    days = 1
    if context.args:
        try:
            days = max(1, min(int(context.args[0]), 30))
        except ValueError:
            await update.message.reply_text("Use /sync 1 or /sync 7 (maximum 30 days).")
            return

    await update.message.reply_text(
        f"Starting history import for the last {days} day(s) from {len(selected_groups)} selected group(s)."
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    saved = 0
    scanned = 0
    skipped = 0

    try:
        if not selected_groups:
            await update.message.reply_text("No groups selected. Use /groups, then /addgroup GROUP_ID.")
            return
        async for dialog in telethon_client.iter_dialogs():
            if not dialog.is_group:
                continue
            from telethon.utils import get_peer_id
            dialog_group_id = str(get_peer_id(dialog.entity))
            if dialog_group_id not in selected_groups:
                continue
            per_group = 0
            try:
                async for msg in telethon_client.iter_messages(dialog.entity, limit=MAX_SYNC_PER_GROUP):
                    if bot_paused:
                        await update.message.reply_text("Sync stopped because the bot was paused.")
                        return
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
                        "key": f"{dialog_group_id}:{msg.id}",
                        "group_id": dialog_group_id,
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
    if bot_paused:
        await update.message.reply_text("I'm paused right now. Send /start to resume me.")
        return
    question = (update.message.text or "").strip()
    if not question:
        return

    async def keep_typing():
        # Telegram's native animated typing indicator expires after a few seconds.
        # Refresh it until the AI response is ready.
        while True:
            try:
                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id,
                    action=ChatAction.TYPING,
                )
            except Exception:
                log.debug("Could not refresh Telegram typing indicator", exc_info=True)
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    try:
        answer = await answer_question(update.effective_user.id, question)
        await save_chat_turn(update.effective_user.id, "user", question)
        await save_chat_turn(update.effective_user.id, "assistant", answer)
        await reply_long(update.message, answer)
    except Exception as exc:
        log.exception("AI answer failed")
        await update.message.reply_text(
            "I couldn't generate an answer. Check the Render logs for the exact error, "
            f"then try again. (Error: {str(exc)[:220]})"
        )
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


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
    await load_selected_groups()

    # Collect only messages from the selected-group allow-list.
    telethon_client.add_event_handler(collect_group_message, events.NewMessage(incoming=True))

    bot = Application.builder().token(BOT_TOKEN).concurrent_updates(4).build()
    bot.add_handler(CommandHandler("start", start_command))
    bot.add_handler(CommandHandler("stop", stop_command))
    bot.add_handler(CommandHandler("help", help_command))
    bot.add_handler(CommandHandler("status", status_command))
    bot.add_handler(CommandHandler("groups", groups_command))
    bot.add_handler(CommandHandler("addgroup", addgroup_command))
    bot.add_handler(CommandHandler("removegroup", removegroup_command))
    bot.add_handler(CommandHandler("mygroups", mygroups_command))
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
