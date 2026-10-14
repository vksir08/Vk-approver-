"""
⚡ AutoAccept Pro — Telegram Join Request Bot
"""

import os
import json
import asyncio
import logging
import time
import tempfile
from datetime import datetime, timedelta
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    FSInputFile,
)
from aiogram.filters.callback_data import CallbackData
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from motor.motor_asyncio import AsyncIOMotorClient

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("AutoAcceptPro")

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))
PORT = int(os.environ.get("PORT", "10000"))
BACKUP_CHANNEL = os.environ.get("BACKUP_CHANNEL", "Vk_Devss")
SUPPORT_USER = os.environ.get("SUPPORT_USER", "Vk_Devz")

if not BOT_TOKEN:
    logger.critical("BOT_TOKEN not set! Exiting.")
    raise SystemExit(1)
if not BASE_URL:
    logger.warning("RENDER_EXTERNAL_URL not set — webhook may fail.")

# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["autoaccept_pro"]
channels_col = db["channels"]
users_col = db["users"]
queue_col = db["queue"]
blacklist_col = db["blacklist"]
logs_col = db["audit_logs"]

# ──────────────────────────────────────────────
# BOT & DISPATCHER
# ──────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"


# ──────────────────────────────────────────────
# CALLBACK DATA
# ──────────────────────────────────────────────
class ConfirmCb(CallbackData, prefix="cf"):
    cid: int
    uid: int


# ──────────────────────────────────────────────
# FSM STATES
# ──────────────────────────────────────────────
class S(StatesGroup):
    limit = State()
    welcome = State()
    broadcast = State()
    force_sub = State()
    log_channel = State()
    expiry = State()


# ──────────────────────────────────────────────
# KEYBOARDS
# ──────────────────────────────────────────────
def default_welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Backup Channel", url=f"https://t.me/{BACKUP_CHANNEL}")],
            [InlineKeyboardButton(text="🛠 Support", url=f"https://t.me/{SUPPORT_USER}")],
        ]
    )


def confirm_spot_kb(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    payload = ConfirmCb(cid=chat_id, uid=user_id).pack()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm My Spot", callback_data=payload)],
        ]
    )


def force_sub_kb(title: str, username: Optional[str], chat_id: int) -> InlineKeyboardMarkup:
    if username:
        url = f"https://t.me/{username}"
    else:
        url = f"https://t.me/c/{str(chat_id)[4:]}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🔗 Join {title}", url=url)],
        ]
    )


# ──────────────────────────────────────────────
# TEMPLATE ENGINE
# ──────────────────────────────────────────────
def render_template(template: str, **kw) -> str:
    result = template
    for key, val in kw.items():
        result = result.replace(f"{{{key}}}", str(val))
    return result


def default_welcome_text(name: str) -> str:
    return f"🎉 Hello <b>{name}</b>!\n\nYour join request has been <b>approved</b>. Welcome aboard!"


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
async def get_chat(state: FSMContext) -> Optional[int]:
    data = await state.get_data()
    return data.get("connected_chat_id")


async def require_chat(message: types.Message, state: FSMContext) -> Optional[int]:
    cid = await get_chat(state)
    if not cid:
        await message.answer("⚠️ No chat connected. Use <code>/connect &lt;chat_id&gt;</code> first.")
    return cid


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


async def is_blacklisted(user_id: int) -> bool:
    return await blacklist_col.find_one({"_id": user_id}) is not None


async def log_event(chat_id: int, event: str, user_id: int, detail: str = ""):
    try:
        await logs_col.insert_one(
            {
                "chat_id": chat_id,
                "event": event,
                "user_id": user_id,
                "detail": detail,
                "ts": datetime.utcnow(),
            }
        )
        ch = await channels_col.find_one({"_id": chat_id}, {"log_channel": 1})
        log_ch = ch.get("log_channel", 0) if ch else 0
        if log_ch:
            try:
                await bot.send_message(
                    chat_id=log_ch,
                    text=f"📋 <b>{event}</b>\n👤 <code>{user_id}</code>\n💬 {detail}",
                )
            except Exception:
                pass
    except Exception:
        pass


async def get_or_create_channel(chat_id: int) -> dict:
    doc = await channels_col.find_one({"_id": chat_id})
    if doc:
        return doc
    defaults = {
        "_id": chat_id,
        "is_active": True,
        "limit": 0,
        "accepted_count": 0,
        "welcome_message": "",
        "force_sub": 0,
        "log_channel": 0,
        "expiry_hours": 0,
        "admins": [],
        "title": "",
    }
    await channels_col.insert_one(defaults)
    return defaults


def limit_display(limit: int) -> str:
    return "∞" if limit == 0 else str(limit)


# ──────────────────────────────────────────────
# 1. CORE COMMANDS
# ──────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Hello, <b>{message.from_user.first_name}</b>!\n\n"
        "I'm <b>AutoAccept Pro</b> — a high-speed join request bot.\n"
        "Use /help to see all commands.\n\n"
        "🔹 Quick start: <code>/connect -100xxxxxxxxxx</code>"
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    txt = (
        "⚙️ <b>Channel Management</b>\n"
        "├ /connect <code>ID</code> — Link a channel/group\n"
        "├ /disconnect — Unlink current session\n"
        "├ /remove — Delete channel from bot DB\n"
        "├ /channels — List all connected chats\n\n"
        "⚙️ <b>Auto-Accept Settings</b>\n"
        "├ /approve_on — Enable auto-accept\n"
        "├ /approve_off — Pause (queue mode)\n"
        "├ /setlimit — Set max accept count (0 = ∞)\n"
        "├ /welcome — Set custom welcome DM\n"
        "├ /forcesub — Set required channel to join first\n"
        "├ /setexpiry — Auto-reject queue after X hours\n"
        "├ /setlog — Set audit-log channel\n\n"
        "⚙️ <b>Queue & Approvals</b>\n"
        "├ /approved — Approve all in queue\n"
        "├ /approved <code>N</code> — Set limit directly\n"
        "├ /queue — View pending requests\n"
        "├ /clearqueue — Drop all pending\n\n"
        "⚙️ <b>Admin & Security</b>\n"
        "├ /addadmin <code>ID</code> — Add channel admin\n"
        "├ /deladmin <code>ID</code> — Remove admin\n"
        "├ /admins — List channel admins\n"
        "├ /blacklist <code>ID</code> — Block a user globally\n"
        "├ /unblacklist <code>ID</code> — Unblock\n\n"
        "⚙️ <b>Broadcast & Stats</b>\n"
        "├ /broadcast — DM members of current chat\n"
        "├ /stats — Chat statistics\n"
        "├ /export — Export users as JSON\n"
    )
    if is_owner(message.from_user.id):
        txt += (
            "\n👑 <b>Owner-Only</b>\n"
            "├ /broadcast_all — DM every user in DB\n"
            "├ /groupstats — Global network stats\n"
            "├ /dropdb — ⚠️ Wipe all data\n"
        )
    await message.answer(txt)


# ──────────────────────────────────────────────
# 2. CHANNEL MANAGEMENT
# ──────────────────────────────────────────────
@dp.message(Command("connect"))
async def cmd_connect(message: types.Message, command: CommandObject, state: FSMContext):
    if not command.args:
        await message.answer("⚠️ Provide a Chat ID.\nExample: <code>/connect -1001234567890</code>")
        return
    try:
        chat_id = int(command.args.strip())
    except ValueError:
        await message.answer("❌ Invalid ID.")
        return
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        if member.status not in ("administrator", "creator"):
            await message.answer("⚠️ I'm not admin there. Add me as admin first!")
            return
        chat_info = await bot.get_chat(chat_id)
        title = chat_info.title or "Unknown"
        existing = await channels_col.find_one({"_id": chat_id})
        if existing:
            await channels_col.update_one({"_id": chat_id}, {"$set": {"title": title}})
        else:
            await channels_col.insert_one(
                {
                    "_id": chat_id,
                    "title": title,
                    "is_active": True,
                    "limit": 0,
                    "accepted_count": 0,
                    "welcome_message": "",
                    "force_sub": 0,
                    "log_channel": 0,
                    "expiry_hours": 0,
                    "admins": [message.from_user.id],
                }
            )
        await state.update_data(connected_chat_id=chat_id)
        await message.answer(
            f"✅ <b>Connected!</b>\nChat: <b>{title}</b> (<code>{chat_id}</code>)"
        )
    except Exception as e:
        await message.answer(f"❌ Failed: <code>{e}</code>")


@dp.message(Command("disconnect"))
async def cmd_disconnect(message: types.Message, state: FSMContext):
    cid = await get_chat(state)
    if cid:
        await state.update_data(connected_chat_id=None)
        await message.answer(f"🔓 Disconnected from <code>{cid}</code>.")
    else:
        await message.answer("⚠️ No chat connected.")


@dp.message(Command("remove"))
async def cmd_remove(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    await channels_col.delete_one({"_id": cid})
    await queue_col.delete_many({"chat_id": cid})
    await state.update_data(connected_chat_id=None)
    await message.answer(f"🗑 Channel <code>{cid}</code> removed.")


@dp.message(Command("channels"))
async def cmd_channels(message: types.Message):
    chats = await channels_col.find({}, {"title": 1, "is_active": 1, "accepted_count": 1}).to_list(length=100)
    if not chats:
        await message.answer("📭 No channels connected.")
        return
    txt = "📋 <b>Connected Channels</b>\n\n"
    for c in chats:
        icon = "🟢" if c.get("is_active", True) else "🔴"
        txt += f"{icon} <b>{c.get('title', '?')}</b> — <code>{c['_id']}</code> ({c.get('accepted_count', 0)})\n"
    await message.answer(txt)


# ──────────────────────────────────────────────
# 3. AUTO-ACCEPT SETTINGS
# ──────────────────────────────────────────────
@dp.message(Command("approve_on"))
async def cmd_approve_on(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    await channels_col.update_one({"_id": cid}, {"$set": {"is_active": True}})
    await message.answer("✅ Auto-accept <b>ON</b>.")


@dp.message(Command("approve_off"))
async def cmd_approve_off(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    await channels_col.update_one({"_id": cid}, {"$set": {"is_active": False}})
    await message.answer("🛑 Auto-accept <b>OFF</b> — new users go to queue.")


@dp.message(Command("setlimit"))
async def cmd_setlimit(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    await message.answer("🔢 Send max users to auto-accept (0 = unlimited):")
    await state.set_state(S.limit)


@dp.message(S.limit)
async def proc_limit(message: types.Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Enter a valid non-negative number.")
        return
    cid = await get_chat(state)
    if cid:
        await channels_col.update_one({"_id": cid}, {"$set": {"limit": val}})
        await message.answer(f"✅ Limit set to <b>{limit_display(val)}</b>.")
    await state.set_state(None)


@dp.message(Command("welcome"))
async def cmd_welcome(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    if message.reply_to_message:
        text = message.reply_to_message.text or message.reply_to_message.caption or ""
        if not text:
            await message.answer("⚠️ No text in replied message.")
            return
        await channels_col.update_one({"_id": cid}, {"$set": {"welcome_message": text}})
        await message.answer(f"✅ Welcome set via reply:\n\n<code>{text[:500]}</code>")
    else:
        ch = await channels_col.find_one({"_id": cid}, {"welcome_message": 1})
        current = ch.get("welcome_message", "") if ch else ""
        hint = "✍️ Send the new welcome message.\n\n<b>Variables:</b> <code>{name} {fullname} {username} {userid} {chat} {count} {date}</code>\n\n"
        hint += f"📌 <b>Current:</b> {('Default' if not current else '<code>' + current[:300] + '</code>')}"
        await message.answer(hint)
        await state.set_state(S.welcome)


@dp.message(S.welcome)
async def proc_welcome(message: types.Message, state: FSMContext):
    cid = await get_chat(state)
    if cid:
        await channels_col.update_one({"_id": cid}, {"$set": {"welcome_message": message.text}})
        await message.answer(f"✅ Welcome updated!\n\n<code>{message.text[:500]}</code>")
    await state.set_state(None)


@dp.message(Command("forcesub"))
async def cmd_forcesub(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    await message.answer("🔗 Send the Chat ID users must join first (0 to disable):")
    await state.set_state(S.force_sub)


@dp.message(S.force_sub)
async def proc_forcesub(message: types.Message, state: FSMContext):
    try:
        val = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Enter a valid number.")
        return
    cid = await get_chat(state)
    if cid:
        await channels_col.update_one({"_id": cid}, {"$set": {"force_sub": val}})
        await message.answer(f"✅ Force-sub set to <code>{val}</code>." if val else "✅ Force-sub <b>disabled</b>.")
    await state.set_state(None)


@dp.message(Command("setexpiry"))
async def cmd_expiry(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    await message.answer("⏱ Hours before auto-reject (0 = never):")
    await state.set_state(S.expiry)


@dp.message(S.expiry)
async def proc_expiry(message: types.Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Enter a valid non-negative number.")
        return
    cid = await get_chat(state)
    if cid:
        await channels_col.update_one({"_id": cid}, {"$set": {"expiry_hours": val}})
        await message.answer(f"✅ Expiry set to <b>{val}h</b>.")
    await state.set_state(None)


@dp.message(Command("setlog"))
async def cmd_setlog(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    await message.answer("📋 Log channel Chat ID (0 to disable):")
    await state.set_state(S.log_channel)


@dp.message(S.log_channel)
async def proc_log_channel(message: types.Message, state: FSMContext):
    try:
        val = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Enter a valid number.")
        return
    cid = await get_chat(state)
    if cid:
        await channels_col.update_one({"_id": cid}, {"$set": {"log_channel": val}})
        await message.answer(f"✅ Log channel set to <code>{val}</code>.")
    await state.set_state(None)


# ──────────────────────────────────────────────
# 4. ADMIN MANAGEMENT
# ──────────────────────────────────────────────
@dp.message(Command("addadmin"))
async def cmd_addadmin(message: types.Message, command: CommandObject, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    if not command.args:
        await message.answer("⚠️ Usage: <code>/addadmin USER_ID</code>")
        return
    try:
        uid = int(command.args.strip())
    except ValueError:
        await message.answer("❌ Invalid ID.")
        return
    await channels_col.update_one({"_id": cid}, {"$addToSet": {"admins": uid}})
    await message.answer(f"✅ Admin <code>{uid}</code> added.")


@dp.message(Command("deladmin"))
async def cmd_deladmin(message: types.Message, command: CommandObject, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    if not command.args:
        await message.answer("⚠️ Usage: <code>/deladmin USER_ID</code>")
        return
    try:
        uid = int(command.args.strip())
    except ValueError:
        await message.answer("❌ Invalid ID.")
        return
    await channels_col.update_one({"_id": cid}, {"$pull": {"admins": uid}})
    await message.answer(f"✅ Admin <code>{uid}</code> removed.")


@dp.message(Command("admins"))
async def cmd_admins(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    ch = await channels_col.find_one({"_id": cid}, {"admins": 1})
    admins = ch.get("admins", []) if ch else []
    txt = f"👤 <b>Admins for</b> <code>{cid}</code>:\n\n"
    for uid in admins:
        txt += f"• <code>{uid}</code>{' 👑' if uid == OWNER_ID else ''}\n"
    if OWNER_ID not in admins:
        txt += f"• <code>{OWNER_ID}</code> 👑 (global owner)\n"
    await message.answer(txt or "📭 No admins.")


# ──────────────────────────────────────────────
# 5. BLACKLIST
# ──────────────────────────────────────────────
@dp.message(Command("blacklist"))
async def cmd_blacklist(message: types.Message, command: CommandObject):
    if not is_owner(message.from_user.id):
        await message.answer("❌ Owner only.")
        return
    if not command.args:
        await message.answer("⚠️ Usage: <code>/blacklist USER_ID</code>")
        return
    try:
        uid = int(command.args.strip())
    except ValueError:
        await message.answer("❌ Invalid ID.")
        return
    await blacklist_col.update_one({"_id": uid}, {"$set": {"ts": datetime.utcnow()}}, upsert=True)
    await message.answer(f"🚫 User <code>{uid}</code> blacklisted.")


@dp.message(Command("unblacklist"))
async def cmd_unblacklist(message: types.Message, command: CommandObject):
    if not is_owner(message.from_user.id):
        await message.answer("❌ Owner only.")
        return
    if not command.arg