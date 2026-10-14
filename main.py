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
    if not command.args:
        await message.answer("⚠️ Usage: <code>/unblacklist USER_ID</code>")
        return
    try:
        uid = int(command.args.strip())
    except ValueError:
        await message.answer("❌ Invalid ID.")
        return
    await blacklist_col.delete_one({"_id": uid})
    await message.answer(f"✅ User <code>{uid}</code> unblacklisted.")


# ──────────────────────────────────────────────
# 6. QUEUE MANAGEMENT
# ──────────────────────────────────────────────
@dp.message(Command("queue"))
async def cmd_queue(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    pending = await queue_col.find({"chat_id": cid}).to_list(length=50)
    if not pending:
        await message.answer("📭 Queue is empty.")
        return
    txt = f"⏳ <b>Queue ({len(pending)})</b>\n\n"
    for i, u in enumerate(pending, 1):
        icon = "✅" if u.get("confirmed") else "⏳"
        txt += f"{i}. {icon} {u.get('first_name', '?')} — <code>{u['user_id']}</code>\n"
        if i >= 30:
            txt += f"\n... +{len(pending) - 30} more"
            break
    await message.answer(txt)


@dp.message(Command("clearqueue"))
async def cmd_clearqueue(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    count = await queue_col.count_documents({"chat_id": cid})
    if count == 0:
        await message.answer("📭 Queue already empty.")
        return
    await queue_col.delete_many({"chat_id": cid})
    await message.answer(f"🗑 Cleared <b>{count}</b> pending requests.")


@dp.message(Command("approved"))
async def cmd_approved(message: types.Message, command: CommandObject, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    if command.args:
        try:
            val = int(command.args.strip())
            await channels_col.update_one({"_id": cid}, {"$set": {"limit": val}})
            await message.answer(f"✅ Limit set to <b>{limit_display(val)}</b>.")
        except ValueError:
            await message.answer("⚠️ Example: <code>/approved 50</code>")
        return
    asyncio.create_task(process_queue_clearance(message, cid))


# ──────────────────────────────────────────────
# HIGH-SPEED QUEUE CLEARANCE
# ──────────────────────────────────────────────
async def process_single_approval(doc, chat_id, ch_data, sem, results):
    async with sem:
        uid = doc["user_id"]
        fname = doc.get("first_name", "User")

        try:
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=uid)
        except Exception as e:
            results["fail"] += 1
            err = str(e)
            results["errors"][err] = results["errors"].get(err, 0) + 1
            await queue_col.delete_one({"_id": doc["_id"]})
            return

        try:
            welcome = ch_data.get("welcome_message", "")
            if welcome:
                text = render_template(
                    welcome,
                    name=fname, fullname=fname, username="N/A",
                    userid=uid, chat=ch_data.get("title", ""),
                    chatid=chat_id, count=ch_data.get("accepted_count", 0) + results["success"] + 1,
                    date=datetime.now().strftime("%Y-%m-%d"),
                )
            else:
                text = default_welcome_text(fname)
            await bot.send_message(chat_id=uid, text=text, reply_markup=default_welcome_kb())
        except Exception:
            pass

        try:
            await asyncio.gather(
                queue_col.delete_one({"_id": doc["_id"]}),
                users_col.update_one(
                    {"_id": uid},
                    {"$set": {"first_name": fname, "last_seen": datetime.utcnow()}, "$addToSet": {"joined_chats": chat_id}},
                    upsert=True,
                ),
            )
            results["success"] += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)


async def process_queue_clearance(message, chat_id):
    ch_data = await channels_col.find_one({"_id": chat_id}) or {}
    expiry = ch_data.get("expiry_hours", 0)
    expired_count = 0

    if expiry > 0:
        cutoff = datetime.utcnow() - timedelta(hours=expiry)
        expired = await queue_col.find({"chat_id": chat_id, "queued_at": {"$lt": cutoff}}).to_list(length=None)
        for ex in expired:
            try:
                await bot.decline_chat_join_request(chat_id=chat_id, user_id=ex["user_id"])
            except Exception:
                pass
            await queue_col.delete_one({"_id": ex["_id"]})
        expired_count = len(expired) if expired else 0

    pending = await queue_col.find({"chat_id": chat_id}).to_list(length=None)
    if not pending and expired_count == 0:
        await message.answer("📭 Queue is empty.")
        return

    await message.answer(f"⚡ Processing <b>{len(pending)}</b> users… (Expired rejected: {expired_count})")
    results = {"success": 0, "fail": 0, "errors": {}}
    sem = asyncio.Semaphore(25)
    tasks = [process_single_approval(doc, chat_id, ch_data, sem, results) for doc in pending]
    await asyncio.gather(*tasks)

    await channels_col.update_one({"_id": chat_id}, {"$inc": {"accepted_count": results["success"]}}, upsert=True)
    report = f"✅ <b>Queue Cleared!</b>\n\n🎯 Approved: <b>{results['success']}</b>\n❌ Failed: <b>{results['fail']}</b>\n⏱ Expired: <b>{expired_count}</b>\n"
    if results["errors"]:
        report += "\n🛑 <b>Errors:</b>\n"
        for err, cnt in list(results["errors"].items())[:5]:
            report += f"• <code>{err[:80]}</code> ({cnt}x)\n"
    await message.answer(report)


# ──────────────────────────────────────────────
# 7. BROADCAST
# ──────────────────────────────────────────────
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    await state.update_data(broadcast_target=cid)
    await message.answer("📢 Send the message to broadcast:")
    await state.set_state(S.broadcast)


@dp.message(Command("broadcast_all"))
async def cmd_broadcast_all(message: types.Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await message.answer("❌ Owner only.")
        return
    await state.update_data(broadcast_target="ALL")
    await message.answer("📢 <b>[GLOBAL]</b> Send the broadcast message:")
    await state.set_state(S.broadcast)


@dp.message(S.broadcast)
async def proc_broadcast(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("broadcast_target")
    await state.set_state(None)
    if not target:
        await message.answer("⚠️ No target.")
        return

    if target == "ALL":
        cursor = users_col.find({}, {"_id": 1})
    else:
        cursor = users_col.find({"joined_chats": target}, {"_id": 1})
    users = await cursor.to_list(length=None)
    total = len(users)
    if total == 0:
        await message.answer("⚠️ No users found.")
        return

    status_msg = await message.answer(f"⏳ Broadcasting to <b>{total}</b> users…")
    ok = fail = 0
    for u in users:
        try:
            await bot.copy_message(chat_id=int(u["_id"]), from_chat_id=message.chat.id, message_id=message.message_id)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await status_msg.edit_text(f"✅ <b>Broadcast Done!</b>\n\n🎯 Delivered: {ok}\n❌ Failed: {fail}\n📊 Total: {total}")


# ──────────────────────────────────────────────
# 8. STATS & EXPORT
# ──────────────────────────────────────────────
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    ch = await channels_col.find_one({"_id": cid})
    if not ch:
        await message.answer("⚠️ Channel not found.")
        return
    pending = await queue_col.count_documents({"chat_id": cid})
    total = await users_col.count_documents({"joined_chats": cid})
    await message.answer(
        f"📊 <b>{ch.get('title', '?')}</b>\n\n"
        f"🆔 <code>{cid}</code>\n"
        f"⚙️ {'🟢 Active' if ch.get('is_active') else '🔴 Paused'}\n"
        f"👥 Accepted: <b>{ch.get('accepted_count', 0)}</b> / {limit_display(ch.get('limit', 0))}\n"
        f"⏳ Queue: <b>{pending}</b>\n"
        f"📦 DB Total: <b>{total}</b>\n"
        f"🔗 Force-Sub: <code>{ch.get('force_sub', 0)}</code>\n"
        f"⏱ Expiry: <b>{ch.get('expiry_hours', 0)}h</b>\n"
        f"👤 Admins: <b>{len(ch.get('admins', []))}</b>"
    )


@dp.message(Command("groupstats"))
async def cmd_groupstats(message: types.Message):
    if not is_owner(message.from_user.id):
        await message.answer("❌ Owner only.")
        return
    chats = await channels_col.find({}).to_list(length=None)
    if not chats:
        await message.answer("📭 No channels.")
        return
    txt = "🌐 <b>Global Stats</b>\n\n"
    for c in chats:
        pending = await queue_col.count_documents({"chat_id": c["_id"]})
        icon = "🟢" if c.get("is_active") else "🔴"
        txt += f"{icon} <b>{c.get('title', '?')}</b> (<code>{c['_id']}</code>)\n   👥 {c.get('accepted_count', 0)}/{limit_display(c.get('limit', 0))} | ⏳ {pending}\n\n"
    for i in range(0, len(txt), 4000):
        await message.answer(txt[i : i + 4000])


@dp.message(Command("export"))
async def cmd_export(message: types.Message, state: FSMContext):
    cid = await require_chat(message, state)
    if not cid:
        return
    users = await users_col.find({"joined_chats": cid}, {"_id": 1, "first_name": 1, "last_seen": 1}).to_list(length=None)
    if not users:
        await message.answer("⚠️ No users to export.")
        return
    data = []
    for u in users:
        entry = {"user_id": u["_id"], "name": u.get("first_name", "")}
        if "last_seen" in u:
            entry["last_seen"] = u["last_seen"].isoformat()
        data.append(entry)
    json_str = json.dumps(data, indent=2)
    filename = f"export_{cid}_{int(time.time())}.json"
    tmp_path = os.path.join(tempfile.gettempdir(), filename)
    with open(tmp_path, "w") as f:
        f.write(json_str)
    try:
        await message.answer_document(FSInputFile(tmp_path, filename=filename), caption=f"📊 <b>{len(data)}</b> users")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@dp.message(Command("dropdb"))
async def cmd_dropdb(message: types.Message):
    if not is_owner(message.from_user.id):
        await message.answer("❌ Owner only.")
        return
    for col in [channels_col, users_col, queue_col, blacklist_col, logs_col]:
        await col.drop()
    await message.answer("🗑 <b>ALL DATA WIPED.</b>")


# ──────────────────────────────────────────────
# 10. JOIN REQUEST HANDLER
# ──────────────────────────────────────────────
async def process_live_approval(chat_id, user_id, first_name, ch_data):
    try:
        await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        logger.warning(f"Approve failed: {user_id} in {chat_id}: {e}")
        return

    try:
        welcome = ch_data.get("welcome_message", "")
        if welcome:
            text = render_template(
                welcome, name=first_name, fullname=first_name, username="N/A",
                userid=user_id, chat=ch_data.get("title", ""), chatid=chat_id,
                count=ch_data.get("accepted_count", 0) + 1,
                date=datetime.now().strftime("%Y-%m-%d"),
            )
        else:
            text = default_welcome_text(first_name)
        await bot.send_message(chat_id=user_id, text=text, reply_markup=default_welcome_kb())
    except Exception:
        pass

    try:
        await asyncio.gather(
            users_col.update_one(
                {"_id": user_id},
                {"$set": {"first_name": first_name, "last_seen": datetime.utcnow()}, "$addToSet": {"joined_chats": chat_id}},
                upsert=True,
            ),
            channels_col.update_one({"_id": chat_id}, {"$inc": {"accepted_count": 1}}, upsert=True),
        )
    except Exception as e:
        logger.warning(f"DB failed: {user_id}: {e}")

    await log_event(chat_id, "AUTO_APPROVE", user_id, first_name)


@dp.chat_join_request()
async def handle_join_request(update: types.ChatJoinRequest):
    user = update.from_user
    chat_id = update.chat.id
    user_id = user.id
    first_name = user.first_name

    if await is_blacklisted(user_id):
        try:
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception:
            pass
        await log_event(chat_id, "REJECT_BLACKLIST", user_id, first_name)
        return

    ch_data = await get_or_create_channel(chat_id)
    if update.chat.title and ch_data.get("title") != update.chat.title:
        await channels_col.update_one({"_id": chat_id}, {"$set": {"title": update.chat.title}})

    is_active = ch_data.get("is_active", True)
    limit = ch_data.get("limit", 0)
    count = ch_data.get("accepted_count", 0)

    force_sub = ch_data.get("force_sub", 0)
    if force_sub:
        try:
            member = await bot.get_chat_member(chat_id=force_sub, user_id=user_id)
            if member.status not in ("member", "administrator", "creator"):
                try:
                    fs_chat = await bot.get_chat(force_sub)
                    fs_title = fs_chat.title or "Required Channel"
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"⚠️ <b>{first_name}</b>, join <b>{fs_title}</b> first.",
                        reply_markup=force_sub_kb(fs_title, fs_chat.username, force_sub),
                    )
                except Exception:
                    pass
                await log_event(chat_id, "FORCE_SUB_BLOCK", user_id, first_name)
                return
        except Exception:
            pass

    expiry = ch_data.get("expiry_hours", 0)
    if expiry > 0:
        cutoff = datetime.utcnow() - timedelta(hours=expiry)
        async for ex in queue_col.find({"chat_id": chat_id, "queued_at": {"$lt": cutoff}}):
            try:
                await bot.decline_chat_join_request(chat_id=chat_id, user_id=ex["user_id"])
            except Exception:
                pass
            await queue_col.delete_one({"_id": ex["_id"]})

    if is_active and (limit == 0 or count < limit):
        asyncio.create_task(process_live_approval(chat_id, user_id, first_name, ch_data))
    else:
        chat_title = ch_data.get("title", "our channel")
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"⏳ Hello <b>{first_name}</b>!\n\nYour request to join <b>{chat_title}</b> is in the <b>Waiting Room</b>.\n\n👇 Confirm your spot!",
                reply_markup=confirm_spot_kb(chat_id, user_id),
            )
        except Exception:
            pass
        await queue_col.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {"$set": {"first_name": first_name, "queued_at": datetime.utcnow(), "confirmed": False}},
            upsert=True,
        )
        await log_event(chat_id, "QUEUED", user_id, first_name)


# ──────────────────────────────────────────────
# 11. CALLBACK HANDLERS
# ──────────────────────────────────────────────
@dp.callback_query(ConfirmCb.filter())
async def cb_confirm_spot(callback: CallbackQuery, callback_data: ConfirmCb):
    if callback.from_user.id != callback_data.uid:
        await callback.answer("❌ Not your button.", show_alert=True)
        return
    await queue_col.update_one({"chat_id": callback_data.cid, "user_id": callback_data.uid}, {"$set": {"confirmed": True}})
    await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Spot confirmed!</b>")
    await callback.answer("✅ Confirmed!")


# ──────────────────────────────────────────────
# 12. WEB ENDPOINTS
# ──────────────────────────────────────────────
async def health_check(request: web.Request) -> web.Response:
    checks = {"bot": False, "db": False}
    try:
        await bot.get_me()
        checks["bot"] = True
    except Exception:
        pass
    try:
        await mongo_client.admin.command("ping")
        checks["db"] = True
    except Exception:
        pass
    status = 200 if all(checks.values()) else 503
    return web.json_response({"status": "ok" if status == 200 else "degraded", "checks": checks}, status=status)


async def root_info(request: web.Request) -> web.Response:
    try:
        me = await bot.get_me()
        return web.json_response({"bot": me.username, "engine": "AutoAccept Pro"})
    except Exception:
        return web.json_response({"engine": "AutoAccept Pro", "status": "starting"})


# ──────────────────────────────────────────────
# 13. WEBHOOK LIFECYCLE
# ──────────────────────────────────────────────
async def on_startup(bot: Bot):
    webhook_url = f"{BASE_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "chat_join_request"],
    )
    logger.info(f"✅ Webhook set: {webhook_url}")


async def on_shutdown(bot: Bot):
    logger.info("🛑 Shutting down…")
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.session.close()
    mongo_client.close()
    logger.info("✅ Done.")


# ──────────────────────────────────────────────
# 14. MAIN
# ──────────────────────────────────────────────
def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    app.router.add_get("/health", health_check)
    app.router.add_get("/", root_info)

    handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    logger.info(f"🚀 Starting on port {PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()