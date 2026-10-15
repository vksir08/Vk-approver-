import os
import re
import asyncio
import logging
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# ==========================================
# CONFIGURATION & SAFETY CHECKS
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))

if not BOT_TOKEN or not MONGO_URI:
    raise ValueError("❌ FATAL: BOT_TOKEN or MONGO_URI is missing from environment variables!")

if BOT_OWNER_ID == 0:
    logging.warning("⚠️ BOT_OWNER_ID not set! All admin commands will be inaccessible.")

# --- CONSTANTS ---
BROADCAST_MAX = 10000          # Safety cap on broadcast recipients
QUEUE_BATCH_SIZE = 50          # Users per batch in queue clearance
BG_CONCURRENCY = 20            # Max concurrent background approvals
BG_SLEEP = 0.05                # Delay between API calls to avoid flood

# --- MONGODB SETUP ---
client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client["telegram_bot_db"]
users_collection = db["users"]
channels_collection = db["channels"]
pending_collection = db["pending_requests"]
banned_collection = db["banned_users"]
stats_collection = db["daily_stats"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"

# --- BACKGROUND TASK TRACKING (prevents GC) ---
_background_tasks: set[asyncio.Task] = set()


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def escape_md(text: str) -> str:
    """Escape Markdown v1 special characters in user-provided text."""
    if not text:
        return ""
    text = str(text)
    for char in ["_", "*", "`", "["]:
        text = text.replace(char, f"\\{char}")
    return text


async def is_authorized(message: types.Message) -> bool:
    """Check if the user is the bot owner. Sends denial if not."""
    if message.from_user.id != BOT_OWNER_ID:
        await message.answer("⛔ You are not authorized to use this command.")
        return False
    return True


async def verify_mongo():
    """Verify MongoDB connection on startup."""
    try:
        await client.admin.command("ping")
        logging.info("MongoDB connected successfully.")
    except Exception as e:
        logging.error(f"MongoDB connection failed: {e}")
        raise


async def send_long_message(chat_id: int, text: str, parse_mode: str = "Markdown"):
    """Send a message, splitting on newlines if it exceeds Telegram's 4096 char limit."""
    if len(text) <= 4096:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        return
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 4096:
            await bot.send_message(chat_id=chat_id, text=chunk.strip(), parse_mode=parse_mode)
            await asyncio.sleep(0.05)
            chunk = line + "\n"
        else:
            chunk += line + "\n"
    if chunk.strip():
        await bot.send_message(chat_id=chat_id, text=chunk.strip(), parse_mode=parse_mode)


def create_background_task(coro) -> asyncio.Task:
    """Create a tracked background task that won't be garbage collected."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ==========================================
# STATES & KEYBOARDS
# ==========================================
class BotConfigState(StatesGroup):
    waiting_for_number = State()
    waiting_for_welcome = State()
    waiting_for_broadcast = State()


def get_suggestion_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Join our Backup Channel", url="https://t.me/Vk_Devss")],
            [InlineKeyboardButton(text="Support", url="https://t.me/Vk_Devz")],
        ]
    )


async def get_current_chat(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get("connected_chat_id")
    if not chat_id:
        await message.answer(
            "⚠️ No chat currently selected. Please run `/connect <chat_id>` first.",
            parse_mode="Markdown",
        )
        return None
    return chat_id


# ==========================================
# RENDER KEEPALIVE & HEALTH
# ==========================================
async def keepalive_task():
    """Pings the Render URL every 5 minutes to prevent free tier sleep."""
    if not RENDER_URL:
        return
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(f"{RENDER_URL}/health", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    logging.debug(f"Keepalive ping: {resp.status}")
            except Exception as e:
                logging.warning(f"Keepalive failed: {e}")
            await asyncio.sleep(300)


@dp.startup()
async def on_startup(bot: Bot):
    await verify_mongo()

    create_background_task(keepalive_task())

    webhook_url = f"{RENDER_URL}{WEBHOOK_PATH}"
    logging.info(f"Setting up webhook to: {webhook_url}")
    await bot.set_webhook(
        webhook_url,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types(),
    )


# ==========================================
# BASIC & OWNER COMMANDS
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Hello, {escape_md(message.from_user.first_name)}!\n\n"
        "I am a High-Speed Auto-Accept Bot running on Webhooks. Use /help for commands.\n\n"
        "🔹 To link a channel or group manually, use:\n`/connect -100xxxxxxxxxx`",
        parse_mode="Markdown",
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "⚙️ **Admin Commands:**\n"
        "🔹 `/connect <id>` - Link to a specific chat\n"
        "🔹 `/disconnect` - Unlink from current chat\n"
        "🔹 `/settings` - View settings of current chat\n"
        "🔹 `/setlimit` - Set max users to auto-accept\n"
        "🔹 `/welcome` - Set custom welcome DM text\n"
        "🔹 `/approve_on` - Turn auto-accept ON\n"
        "🔹 `/approve_off` - Turn OFF (Routes users to Queue)\n"
        "🔹 `/approved` - Accept all users currently in the Queue\n"
        "🔹 `/approved <number>` - Set the accept limit directly\n"
        "🔹 `/pending` - Check users waiting in queue\n"
        "🔹 `/cleanup` - Clear queue for current chat\n"
        "🔹 `/resetcount` - Reset accepted users counter\n"
        "🔹 `/broadcast` - Broadcast to accepted members\n"
        "🔹 `/broadcast_pending` - Broadcast to queue members\n"
        "🔹 `/stats` - View today's approval stats\n"
        "🔹 `/ban <id>` - Ban user from joining\n"
        "🔹 `/unban <id>` - Unban user\n"
        "🔹 `/ping` - Check bot & DB latency\n"
        "🔹 `/groupstats` - View all chats (Owner Only)\n"
        "🔹 `/listchats` - List all chat IDs (Owner Only)\n"
        "🔹 `/cancel` - Cancel current operation\n"
    )
    await message.answer(help_text, parse_mode="Markdown")


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Nothing to cancel.")
        return
    await state.clear()
    await message.answer("❌ Operation cancelled.")


@dp.message(Command("ping"))
async def cmd_ping(message: types.Message):
    if not await is_authorized(message):
        return

    import time

    start = time.time()
    test_msg = await message.answer("Pinging...")
    end = time.time()

    try:
        await channels_collection.count_documents({})
        db_status = "✅ Connected"
    except Exception:
        db_status = "❌ Disconnected"

    latency = round((end - start) * 1000, 2)
    await test_msg.edit_text(
        f"🏓 **Pong!**\n\n⏱ Latency: `{latency}ms`\n💾 Database: {db_status}",
        parse_mode="Markdown",
    )


@dp.message(Command("listchats"))
async def cmd_listchats(message: types.Message):
    if not await is_authorized(message):
        return

    text = "📋 **Connected Chats:**\n"
    count = 0
    async for c in channels_collection.find({}, {"_id": 1, "title": 1}):
        count += 1
        text += f"• {escape_md(c.get('title', 'Unknown'))} (`{c['_id']}`)\n"

    if count == 0:
        await message.answer("No chats connected.")
        return

    await send_long_message(message.chat.id, text)


@dp.message(Command("groupstats"))
async def cmd_groupstats(message: types.Message):
    if not await is_authorized(message):
        return

    total, active = 0, 0
    text = "📊 **All Connected Chats:**\n\n"
    async for chat in channels_collection.find({}):
        total += 1
        is_active = chat.get("is_active", False)
        if is_active:
            active += 1
        status_emoji = "🟢" if is_active else "🔴"
        text += (
            f"• {escape_md(chat.get('title', 'Unknown'))} (`{chat['_id']}`) "
            f"{status_emoji} Accepted: {chat.get('accepted_count', 0)}\n"
        )

    text += f"\n**Total:** {total} | **Active:** {active} | **Paused:** {total - active}"
    await send_long_message(message.chat.id, text)


# ==========================================
# CONNECTION & SETTINGS MANAGEMENT
# ==========================================
@dp.message(Command("connect"))
async def cmd_connect(message: types.Message, command: CommandObject, state: FSMContext):
    if not await is_authorized(message):
        return

    if not command.args:
        await message.answer(
            "⚠️ Please provide a valid target Chat ID.\nExample: `/connect -1001234567890`"
        )
        return

    try:
        chat_id = int(command.args.strip())
    except ValueError:
        await message.answer("⚠️ Invalid Chat ID. Must be a number like `-1001234567890`.")
        return

    try:
        # Verify bot is admin
        bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            await message.answer(
                "⚠️ Bot is not an administrator in that chat. Please add me as an admin first!"
            )
            return

        # Verify calling user is admin of the target chat
        user_member = await bot.get_chat_member(chat_id=chat_id, user_id=message.from_user.id)
        if user_member.status not in ["administrator", "creator"]:
            await message.answer(
                "⚠️ You must be an administrator in that chat to connect me to it."
            )
            return

        chat_info = await bot.get_chat(chat_id)
        chat_title = chat_info.title or "Unknown Chat"

        await channels_collection.update_one(
            {"_id": chat_id},
            {
                "$setOnInsert": {"is_active": True, "limit": None, "accepted_count": 0},
                "$set": {"title": chat_title},
            },
            upsert=True,
        )

        await state.update_data(connected_chat_id=chat_id)
        await message.answer(
            f"✅ **Connected Successfully!**\nTarget Chat: **{escape_md(chat_title)}** (`{chat_id}`)",
            parse_mode="Markdown",
        )
    except TelegramForbiddenError:
        await message.answer("❌ Bot is not a member of that chat or has been blocked.")
    except Exception as e:
        logging.error(f"Connect error: {e}")
        await message.answer(
            f"❌ Connection failed. Verify ID and Bot Admin rights.\nError: `{escape_md(str(e))}`",
            parse_mode="Markdown",
        )


@dp.message(Command("disconnect"))
async def cmd_disconnect(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    data = await state.get_data()
    chat_id = data.get("connected_chat_id")
    if not chat_id:
        await message.answer("You are not connected to any chat.")
        return
    await state.clear()
    await message.answer(
        f"✅ Disconnected from `{chat_id}`. Bot will no longer manage this chat from this admin panel.",
        parse_mode="Markdown",
    )


@dp.message(Command("settings"))
async def cmd_settings(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    chat = await channels_collection.find_one({"_id": chat_id})
    if not chat:
        await message.answer("Chat not found in DB. Reconnect.")
        return

    limit = "∞" if chat.get("limit") is None else chat.get("limit")
    text = (
        f"⚙️ **Settings for {escape_md(chat.get('title', 'Unknown'))}**\n\n"
        f"🆔 ID: `{chat['_id']}`\n"
        f"Status: {'🟢 ACTIVE' if chat.get('is_active') else '🔴 PAUSED'}\n"
        f"Limit: {limit}\n"
        f"Accepted: {chat.get('accepted_count', 0)}\n"
        f"Welcome Msg: {'Set' if chat.get('welcome_message') else 'Default'}\n"
    )
    await message.answer(text, parse_mode="Markdown")


# ==========================================
# QUEUE & STATS COMMANDS
# ==========================================
@dp.message(Command("pending"))
async def cmd_pending(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    count = await pending_collection.count_documents({"chat_id": chat_id})
    await message.answer(
        f"⏳ There are **{count}** users waiting in the queue for this chat.",
        parse_mode="Markdown",
    )


@dp.message(Command("cleanup"))
async def cmd_cleanup(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    res = await pending_collection.delete_many({"chat_id": chat_id})
    await message.answer(f"🧹 Cleared **{res.deleted_count}** users from the queue.")


@dp.message(Command("resetcount"))
async def cmd_resetcount(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    await channels_collection.update_one({"_id": chat_id}, {"$set": {"accepted_count": 0}})
    await message.answer("✅ Accepted users counter reset to 0.")


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not await is_authorized(message):
        return

    today = datetime.now().strftime("%Y-%m-%d")
    doc = await stats_collection.find_one({"_id": today})
    if not doc:
        await message.answer("No approvals recorded today.")
        return

    live = doc.get("live", 0)
    queue = doc.get("queue", 0)
    text = (
        f"📈 **Daily Stats ({today})**\n\n"
        f"Total Live Approvals: {live}\n"
        f"Total Queue Approvals: {queue}\n"
        f"Total Joined: {live + queue}"
    )
    await message.answer(text, parse_mode="Markdown")


# ==========================================
# BAN / UNBAN LOGIC
# ==========================================
@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if not await is_authorized(message):
        return

    if not command.args:
        await message.answer("Usage: `/ban <user_id>`", parse_mode="Markdown")
        return

    try:
        user_id = int(command.args.strip())
    except ValueError:
        await message.answer("⚠️ Invalid User ID. Must be a number.")
        return

    await banned_collection.update_one(
        {"_id": user_id}, {"$set": {"banned_at": datetime.now()}}, upsert=True
    )
    await message.answer(f"🚫 User `{user_id}` banned from joining chats.", parse_mode="Markdown")


@dp.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    if not await is_authorized(message):
        return

    if not command.args:
        await message.answer("Usage: `/unban <user_id>`", parse_mode="Markdown")
        return

    try:
        user_id = int(command.args.strip())
    except ValueError:
        await message.answer("⚠️ Invalid User ID. Must be a number.")
        return

    await banned_collection.delete_one({"_id": user_id})
    await message.answer(f"✅ User `{user_id}` unbanned.", parse_mode="Markdown")


# ==========================================
# SETTINGS & BROADCASTS
# ==========================================
@dp.message(Command("approve_on"))
async def cmd_approve_on(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    await channels_collection.update_one(
        {"_id": chat_id}, {"$set": {"is_active": True}}, upsert=True
    )
    await message.answer("✅ Live Auto-Accept turned **ON**.")


@dp.message(Command("approve_off"))
async def cmd_approve_off(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    await channels_collection.update_one(
        {"_id": chat_id}, {"$set": {"is_active": False}}, upsert=True
    )
    await message.answer("🛑 Auto-Accept turned **OFF**.")


@dp.message(Command("setlimit"))
async def cmd_setlimit(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    await message.answer(
        "🔢 Reply with the maximum number of members to auto-accept (send 0 for infinite):"
    )
    await state.set_state(BotConfigState.waiting_for_number)


@dp.message(BotConfigState.waiting_for_number)
async def process_limit_number(message: types.Message, state: FSMContext):
    # Allow cancel
    if message.text and message.text.startswith("/cancel"):
        await state.set_state(None)
        await message.answer("❌ Operation cancelled.")
        return

    try:
        new_limit = int(message.text.strip())
        if new_limit < 0:
            raise ValueError
        if new_limit == 0:
            new_limit = None  # Infinity
    except ValueError:
        await message.answer("⚠️ Please enter a valid non-negative number (0 = infinite).")
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        await state.set_state(None)
        return

    await channels_collection.update_one(
        {"_id": chat_id}, {"$set": {"limit": new_limit}}, upsert=True
    )
    limit_text = "∞" if new_limit is None else new_limit
    await message.answer(f"✅ Auto-accept limit set to **{limit_text}**.")
    await state.set_state(None)


@dp.message(Command("welcome"))
async def cmd_welcome(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    if message.reply_to_message:
        replied_text = message.reply_to_message.text or message.reply_to_message.caption
        if not replied_text:
            await message.answer("⚠️ The message you replied to does not contain any text.")
            return
        await channels_collection.update_one(
            {"_id": chat_id}, {"$set": {"welcome_message": replied_text}}, upsert=True
        )
        await message.answer("✅ **Welcome message updated via reply!**", parse_mode="Markdown")
        await state.set_state(None)
    else:
        await message.answer("✍️ Send the custom welcome text for this chat:")
        await state.set_state(BotConfigState.waiting_for_welcome)


@dp.message(BotConfigState.waiting_for_welcome)
async def process_welcome_text(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/cancel"):
        await state.set_state(None)
        await message.answer("❌ Operation cancelled.")
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        await state.set_state(None)
        return

    await channels_collection.update_one(
        {"_id": chat_id}, {"$set": {"welcome_message": message.text}}, upsert=True
    )
    await message.answer("✅ **Welcome message updated!**", parse_mode="Markdown")
    await state.set_state(None)


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    await message.answer(
        f"📢 Send the message to broadcast to accepted members of `{chat_id}`:"
    )
    await state.update_data(broadcast_target=chat_id, broadcast_type="accepted")
    await state.set_state(BotConfigState.waiting_for_broadcast)


@dp.message(Command("broadcast_pending"))
async def cmd_broadcast_pending(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    await message.answer(
        f"📢 Send the message to broadcast to PENDING members of `{chat_id}`:"
    )
    await state.update_data(broadcast_target=chat_id, broadcast_type="pending")
    await state.set_state(BotConfigState.waiting_for_broadcast)


@dp.message(Command("broadcast_all"))
async def cmd_broadcast_all(message: types.Message, state: FSMContext):
    if not await is_authorized(message):
        return

    await message.answer("⚠️ **[GLOBAL BROADCAST]** Send message to ALL accepted users:")
    await state.update_data(broadcast_target="ALL", broadcast_type="accepted")
    await state.set_state(BotConfigState.waiting_for_broadcast)


@dp.message(BotConfigState.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/cancel"):
        await state.set_state(None)
        await message.answer("❌ Broadcast cancelled.")
        return

    data = await state.get_data()
    target = data.get("broadcast_target")
    b_type = data.get("broadcast_type")
    await state.set_state(None)

    # Build the query and cursor
    if target == "ALL":
        query = {}
        cursor = users_collection.find({}, {"_id": 1})
    elif b_type == "pending":
        query = {"chat_id": target}
        cursor = pending_collection.find({"chat_id": target}, {"user_id": 1, "_id": 0})
    else:
        query = {"joined_chats": target}
        cursor = users_collection.find({"joined_chats": target}, {"_id": 1})

    # Estimate total for safety check
    total_estimated = await (pending_collection if b_type == "pending" and target != "ALL" else users_collection).count_documents(
        query
    )

    if total_estimated == 0:
        await message.answer("⚠️ Found 0 users for this broadcast.")
        return

    if total_estimated > BROADCAST_MAX:
        await message.answer(
            f"⚠️ Too many recipients ({total_estimated}). Safety cap is {BROADCAST_MAX}. "
            f"Use `/broadcast_all` in smaller segments or raise BROADCAST_MAX."
        )
        return

    await message.answer(f"⏳ Broadcast starting... Target: {total_estimated} users.")
    success_count, fail_count = 0, 0

    async for user_doc in cursor:
        user_id = user_doc.get("_id") or user_doc.get("user_id")
        if not user_id:
            continue
        try:
            await bot.copy_message(
                chat_id=int(user_id),
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            success_count += 1
            await asyncio.sleep(BG_SLEEP)
        except TelegramRetryAfter as e:
            logging.warning(f"Rate limited, retrying after {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(
                    chat_id=int(user_id),
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                success_count += 1
            except Exception:
                fail_count += 1
        except TelegramForbiddenError:
            # User blocked the bot
            await users_collection.update_one({"_id": int(user_id)}, {"$set": {"active": False}})
            fail_count += 1
        except Exception:
            fail_count += 1

    await message.answer(
        f"✅ **Broadcast Finished!**\n🎯 Delivered: {success_count}\n❌ Failed: {fail_count}",
        parse_mode="Markdown",
    )


# ==========================================
# QUEUE CLEARANCE & LIVE JOIN LOGIC
# ==========================================
async def process_single_pending_user(
    user_doc, chat_id, channel_data, semaphore, results
):
    async with semaphore:
        user_id = user_doc["user_id"]
        first_name = user_doc.get("first_name", "User")

        # Check ban (find_one is more efficient than count_documents)
        banned = await banned_collection.find_one({"_id": user_id})
        if banned:
            results["fail"] += 1
            await pending_collection.delete_one({"_id": user_doc["_id"]})
            return

        default_msg = f"Hello {first_name}! Your request to join was approved. Welcome! 🎉"
        welcome_text = (
            channel_data.get("welcome_message", default_msg)
            if channel_data
            else default_msg
        )

        try:
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception as e:
            logging.warning(f"Failed to approve {user_id} in {chat_id}: {e}")
            results["fail"] += 1
            await pending_collection.delete_one({"_id": user_doc["_id"]})
            return

        try:
            await bot.send_message(
                chat_id=user_id,
                text=welcome_text,
                reply_markup=get_suggestion_keyboard(),
            )
        except Exception:
            pass  # Welcome DM failed, but approval succeeded

        try:
            await pending_collection.delete_one({"_id": user_doc["_id"]})
            await users_collection.update_one(
                {"_id": user_id},
                {
                    "$set": {"first_name": first_name},
                    "$addToSet": {"joined_chats": chat_id},
                },
                upsert=True,
            )
            await stats_collection.update_one(
                {"_id": datetime.now().strftime("%Y-%m-%d")},
                {"$inc": {"queue": 1}},
                upsert=True,
            )
            results["success"] += 1
        except Exception as e:
            logging.error(f"DB update error for {user_id}: {e}")

        await asyncio.sleep(BG_SLEEP)


async def process_queue_clearance(message: types.Message, chat_id: int):
    """Process pending queue in batches to avoid memory issues."""
    channel_data = await channels_collection.find_one({"_id": chat_id})

    total_count = await pending_collection.count_documents({"chat_id": chat_id})
    if total_count == 0:
        await message.answer("📭 Queue is empty.")
        return

    await message.answer(
        f"⚡ **High-Speed Clearance Started!**\nProcessing **{total_count}** users...",
        parse_mode="Markdown",
    )

    results = {"success": 0, "fail": 0}
    semaphore = asyncio.Semaphore(BG_CONCURRENCY)

    # Process in batches to avoid loading everything into memory
    skip = 0
    processed = 0
    while processed < total_count:
        batch = await pending_collection.find({"chat_id": chat_id}).skip(skip).to_list(length=QUEUE_BATCH_SIZE)
        if not batch:
            break

        tasks = [
            process_single_pending_user(user, chat_id, channel_data, semaphore, results)
            for user in batch
        ]
        await asyncio.gather(*tasks)

        processed += len(batch)
        skip += len(batch)

    await channels_collection.update_one(
        {"_id": chat_id}, {"$inc": {"accepted_count": results["success"]}}, upsert=True
    )
    await message.answer(
        f"✅ **Complete!**\n🎯 Approved: {results['success']}\n❌ Failed: {results['fail']}",
        parse_mode="Markdown",
    )


@dp.message(Command("approved"))
async def cmd_approved(
    message: types.Message, command: CommandObject, state: FSMContext
):
    if not await is_authorized(message):
        return

    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    if command.args:
        try:
            limit = int(command.args.strip())
            if limit < 0:
                raise ValueError
            if limit == 0:
                limit = None
            await channels_collection.update_one(
                {"_id": chat_id}, {"$set": {"limit": limit}}, upsert=True
            )
            await message.answer(
                f"✅ Auto-accept limit set to **{'∞' if limit is None else limit}**."
            )
        except ValueError:
            await message.answer("⚠️ Invalid number. Example: `/approved 50`")
    else:
        create_background_task(process_queue_clearance(message, chat_id))


# ==========================================
# LIVE JOIN REQUEST (with atomic limit check)
# ==========================================
async def process_live_user_background(
    chat_id, user_id, first_name, welcome_text
):
    """Background task: approve join request, send welcome, update DB."""
    try:
        # Approve the join request
        await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        logging.warning(f"Failed to approve {user_id} in {chat_id}: {e}")
        # Revert the count increment since approval failed
        await channels_collection.update_one(
            {"_id": chat_id}, {"$inc": {"accepted_count": -1}}
        )
        return

    # Send welcome DM (non-critical, don't fail on error)
    try:
        await bot.send_message(
            chat_id=user_id,
            text=welcome_text,
            reply_markup=get_suggestion_keyboard(),
        )
    except Exception:
        pass

    # Update DB
    try:
        await asyncio.gather(
            users_collection.update_one(
                {"_id": user_id},
                {
                    "$set": {"first_name": first_name},
                    "$addToSet": {"joined_chats": chat_id},
                },
                upsert=True,
            ),
            stats_collection.update_one(
                {"_id": datetime.now().strftime("%Y-%m-%d")},
                {"$inc": {"live": 1}},
                upsert=True,
            ),
        )
    except Exception as e:
        logging.error(f"DB update error in live approval for {user_id}: {e}")


@dp.chat_join_request()
async def handle_join_request(update: types.ChatJoinRequest):
    user_id = update.from_user.id
    chat_id = update.chat.id
    first_name = update.from_user.first_name

    # Check if user is banned
    banned = await banned_collection.find_one({"_id": user_id})
    if banned:
        try:
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception:
            pass
        return

    channel_data = await channels_collection.find_one({"_id": chat_id})
    is_bot_active = channel_data.get("is_active", True) if channel_data else True
    max_limit = channel_data.get("limit", None) if channel_data else None

    # If bot is paused, route to pending queue
    if not is_bot_active:
        chat_title = (
            escape_md(channel_data.get("title", "our channel"))
            if channel_data
            else "our channel"
        )
        wait_text = (
            f"⏳ Hello {escape_md(first_name)}!\n"
            f"Your request to join **{chat_title}** is in the Waiting Room.\n"
            f"Click /start to confirm your spot!"
        )
        try:
            await bot.send_message(chat_id=user_id, text=wait_text, parse_mode="Markdown")
        except Exception:
            pass

        await pending_collection.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {"$set": {"first_name": first_name}},
            upsert=True,
        )
        return

    # If there's a limit, use atomic check-and-increment to prevent race conditions
    if max_limit is not None:
        result = await channels_collection.find_one_and_update(
            {"_id": chat_id, "accepted_count": {"$lt": max_limit}},
            {"$inc": {"accepted_count": 1}},
        )
        if result is None:
            # Limit reached — route to pending
            chat_title = (
                escape_md(channel_data.get("title", "our channel"))
                if channel_data
                else "our channel"
            )
            wait_text = (
                f"⏳ Hello {escape_md(first_name)}!\n"
                f"Your request to join **{chat_title}** is in the Waiting Room.\n"
                f"Click /start to confirm your spot!"
            )
            try:
                await bot.send_message(
                    chat_id=user_id, text=wait_text, parse_mode="Markdown"
                )
            except Exception:
                pass

            await pending_collection.update_one(
                {"chat_id": chat_id, "user_id": user_id},
                {"$set": {"first_name": first_name}},
                upsert=True,
            )
            return
    else:
        # No limit — just increment
        await channels_collection.update_one(
            {"_id": chat_id}, {"$inc": {"accepted_count": 1}}, upsert=True
        )

    # Approve in background
    default_msg = f"Hello {first_name}! Your request to join was approved. Welcome! 🎉"
    welcome_text = (
        channel_data.get("welcome_message", default_msg)
        if channel_data
        else default_msg
    )
    create_background_task(
        process_live_user_background(chat_id, user_id, first_name, welcome_text)
    )


# ==========================================
# WEBHOOK SERVER & SHUTDOWN
# ==========================================
@dp.shutdown()
async def on_shutdown(bot: Bot):
    """Cleanup on shutdown."""
    client.close()
    logging.info("MongoDB connection closed. Bot shut down.")


def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running smoothly!"))
    app.router.add_get("/health", lambda r: web.Response(text="OK", status=200))

    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    port = int(os.environ.get("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
