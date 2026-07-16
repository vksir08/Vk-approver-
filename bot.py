import os
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from motor.motor_asyncio import AsyncIOMotorClient

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
# Note: Keep this named RENDER_EXTERNAL_URL in code, even if using Railway's domain!
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL") 
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", 0)) 

client = AsyncIOMotorClient(MONGO_URI)
db = client["telegram_bot_db"]
users_collection = db["users"]             
channels_collection = db["channels"]       
pending_collection = db["pending_requests"] 

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"

# ==========================================
# STATES & KEYBOARDS
# ==========================================
class BotConfigState(StatesGroup):
    waiting_for_number = State()
    waiting_for_welcome = State()
    waiting_for_broadcast = State()

def get_suggestion_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Join our Backup Channel", url="https://t.me/Vk_Devss")],
        [InlineKeyboardButton(text="Support", url="https://t.me/Vk_Devz")],
    ])

async def get_current_chat(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get("connected_chat_id")
    if not chat_id:
        await message.answer("⚠️ No chat currently selected. Please run `/connect <chat_id>` first.", parse_mode="Markdown")
        return None
    return chat_id

# ==========================================
# BASIC ADMIN COMMANDS
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Hello, {message.from_user.first_name}!\n\n"
        "I am a High-Speed Auto-Accept Bot running on Webhooks. Use /help for commands.\n\n"
        "🔹 To link a channel or group manually, use:\n`/connect -100xxxxxxxxxx`",
        parse_mode="Markdown"
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "⚙️ **Admin Commands:**\n"
        "🔹 `/connect <id>` - Link to a specific chat\n"
        "🔹 `/setlimit` - Set max users to auto-accept\n"
        "🔹 `/welcome` - Set custom welcome DM text (Tip: Reply to a message with this command!)\n"
        "🔹 `/approve_on` - Turn auto-accept ON\n"
        "🔹 `/approve_off` - Turn OFF (Routes users to Queue)\n"
        "🔹 `/approved` - Accept all users currently in the Queue\n"
        "🔹 `/approved <number>` - Set the accept limit directly\n"
        "🔹 `/broadcast` - Broadcast to members of the connected chat\n"
        "🔹 `/groupstats` - View status of all connected chats (Bot Owner Only)\n"
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(Command("connect"))
async def cmd_connect(message: types.Message, command: CommandObject, state: FSMContext):
    if not command.args:
        await message.answer("⚠️ Please provide a valid target Chat ID.\nExample: `/connect -1001234567890`")
        return

    try:
        chat_id = int(command.args.strip())
        member_status = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        
        if member_status.status not in ["administrator", "creator"]:
            await message.answer("⚠️ Bot is not an administrator in that chat. Please add me as an admin first!")
            return

        chat_info = await bot.get_chat(chat_id)
        chat_title = chat_info.title or "Unknown Chat"

        await channels_collection.update_one(
            {"_id": chat_id},
            {
                "$setOnInsert": {"is_active": True, "limit": float('inf'), "accepted_count": 0},
                "$set": {"title": chat_title}
            },
            upsert=True
        )
        
        await state.update_data(connected_chat_id=chat_id)
        await message.answer(
            f"✅ **Connected Successfully!**\nTarget Chat: **{chat_title}** (`{chat_id}`)\n\n"
            "Now you can manage this chat using the control commands.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"❌ Connection failed. Verify the ID is correct and the bot is an admin.\nError: `{str(e)}`")

@dp.message(Command("groupstats"))
async def cmd_groupstats(message: types.Message):
    if message.from_user.id != BOT_OWNER_ID:
        await message.answer("❌ **Permission Denied.** Only the Bot Owner can view global statistics.")
        return

    loading_msg = await message.answer("📊 Fetching network statistics from the database...")
    cursor = channels_collection.find({})
    channels = await cursor.to_list(length=None)
    
    if not channels:
        await loading_msg.edit_text("⚠️ No groups or channels are currently connected to the database.")
        return

    stats_message = "🌐 **Global Network Statistics** 🌐\n\n"
    for chat in channels:
        chat_id = chat["_id"]
        chat_title = chat.get("title", "Unknown Chat (Reconnect to fix)")
        is_active = chat.get("is_active", True)
        raw_limit = chat.get("limit", float('inf'))
        limit_display = "∞" if raw_limit == float('inf') else str(raw_limit)
        accepted = chat.get("accepted_count", 0)
        pending_count = await pending_collection.count_documents({"chat_id": chat_id})
        status_icon = "🟢 ACTIVE" if is_active else "🔴 PAUSED"
        
        stats_message += (
            f"🏷 **{chat_title}**\n"
            f"🆔 `{chat_id}`\n"
            f"⚙️ Status: {status_icon}\n"
            f"👥 Accepted Users: {accepted} / {limit_display}\n"
            f"⏳ Waiting in Queue: {pending_count}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )
        
    await loading_msg.delete()
    if len(stats_message) <= 4000:
        await message.answer(stats_message, parse_mode="Markdown")
    else:
        for x in range(0, len(stats_message), 4000):
            await message.answer(stats_message[x:x+4000], parse_mode="Markdown")

# ==========================================
# SETTINGS & BROADCASTS
# ==========================================
@dp.message(Command("approve_on"))
async def cmd_approve_on(message: types.Message, state: FSMContext):
    chat_id = await get_current_chat(message, state)
    if chat_id:
        await channels_collection.update_one({"_id": chat_id}, {"$set": {"is_active": True}}, upsert=True)
        await message.answer("✅ Live Auto-Accept turned **ON** for the active chat.")

@dp.message(Command("approve_off"))
async def cmd_approve_off(message: types.Message, state: FSMContext):
    chat_id = await get_current_chat(message, state)
    if chat_id:
        await channels_collection.update_one({"_id": chat_id}, {"$set": {"is_active": False}}, upsert=True)
        await message.answer("🛑 Auto-Accept turned **OFF**. New users will be placed in the Queue.")

@dp.message(Command("setlimit"))
async def cmd_setlimit(message: types.Message, state: FSMContext):
    chat_id = await get_current_chat(message, state)
    if chat_id:
        await message.answer("🔢 Reply with the maximum number of members to auto-accept:")
        await state.set_state(BotConfigState.waiting_for_number)

@dp.message(BotConfigState.waiting_for_number)
async def process_limit_number(message: types.Message, state: FSMContext):
    try:
        new_limit = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Please enter a valid numeric limit.")
        return
    chat_id = await get_current_chat(message, state)
    if chat_id:
        await channels_collection.update_one({"_id": chat_id}, {"$set": {"limit": new_limit}}, upsert=True)
        await message.answer(f"✅ Auto-accept limit set to **{new_limit}**.")
        await state.set_state(None)

@dp.message(Command("welcome"))
async def cmd_welcome(message: types.Message, state: FSMContext):
    chat_id = await get_current_chat(message, state)
    if not chat_id: return

    if message.reply_to_message:
        replied_text = message.reply_to_message.text or message.reply_to_message.caption
        if not replied_text:
            await message.answer("⚠️ The message you replied to does not contain any text.")
            return
        await channels_collection.update_one({"_id": chat_id}, {"$set": {"welcome_message": replied_text}}, upsert=True)
        await message.answer(f"✅ **Welcome message updated via reply!**\n\nNew greeting text:\n\"{replied_text}\"", parse_mode="Markdown")
        await state.set_state(None)
    else:
        await message.answer("✍️ Send the custom welcome text for this chat:")
        await state.set_state(BotConfigState.waiting_for_welcome)

@dp.message(BotConfigState.waiting_for_welcome)
async def process_welcome_text(message: types.Message, state: FSMContext):
    chat_id = await get_current_chat(message, state)
    if chat_id:
        await channels_collection.update_one({"_id": chat_id}, {"$set": {"welcome_message": message.text}}, upsert=True)
        await message.answer(f"✅ **Welcome message updated!**\n\nNew greeting text:\n\"{message.text}\"", parse_mode="Markdown")
        await state.set_state(None)

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    chat_id = await get_current_chat(message, state)
    if not chat_id: return
    await message.answer(f"📢 Send the message you want to broadcast to members of the connected chat (`{chat_id}`):")
    await state.update_data(broadcast_target=chat_id)
    await state.set_state(BotConfigState.waiting_for_broadcast)

@dp.message(Command("broadcast_all"))
async def cmd_broadcast_all(message: types.Message, state: FSMContext):
    if message.from_user.id != BOT_OWNER_ID:
        await message.answer("❌ **Permission Denied.** Only the Bot Owner can broadcast globally.")
        return
    await message.answer("⚠️ **[GLOBAL BROADCAST]** ⚠️\nSend the message you want to broadcast to ALL accepted users across ALL channels:")
    await state.update_data(broadcast_target="ALL")
    await state.set_state(BotConfigState.waiting_for_broadcast)

@dp.message(BotConfigState.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("broadcast_target")
    await state.set_state(None)
    
    if target == "ALL":
        cursor = users_collection.find({}, {"_id": 1})
    else:
        cursor = users_collection.find({"joined_chats": target}, {"_id": 1})
        
    users_list = await cursor.to_list(length=None)
    total_users = len(users_list)
    
    if total_users == 0:
        await message.answer("⚠️ **Database Empty:** I found 0 users for this specific channel.")
        return

    await message.answer(f"⏳ Broadcast starting... Attempting to send to {total_users} users.")
    success_count = 0
    fail_count = 0
    error_reasons = {}
    
    for user_doc in users_list:
        try:
            user_id = int(user_doc["_id"])
            await bot.copy_message(chat_id=user_id, from_chat_id=message.chat.id, message_id=message.message_id)
            success_count += 1
            await asyncio.sleep(0.05) 
        except Exception as e:
            fail_count += 1
            error_msg = str(e)
            error_reasons[error_msg] = error_reasons.get(error_msg, 0) + 1

    report = (
        f"✅ **Broadcast Finished!**\n\n"
        f"🎯 **Delivered:** {success_count}\n"
        f"❌ **Failed:** {fail_count}\n"
        f"📊 **Total in DB:** {total_users}\n"
    )
    if fail_count > 0:
        report += "\n🛑 **Exact Error Reasons:**\n"
        for err, count in error_reasons.items():
            report += f"- `{err}` ({count} users)\n"
            
    await message.answer(report, parse_mode="Markdown")

# ==========================================
# SUPERFAST QUEUE CLEARANCE LOGIC
# ==========================================
async def process_single_pending_user(user_doc, chat_id, channel_data, semaphore, results):
    """Processes a single queued user, ensuring approval happens even if DM fails."""
    async with semaphore:
        user_id = user_doc["user_id"]
        first_name = user_doc.get("first_name", "User")
        
        default_msg = f"Hello {first_name}! Your request to join was approved. Welcome! 🎉"
        welcome_text = channel_data.get("welcome_message", default_msg) if channel_data else default_msg
        
        approved_successfully = False
        
        # ACTION 1: FORCE THE APPROVAL FIRST
        try:
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            approved_successfully = True
        except Exception as e:
            results['fail'] += 1
            error_msg = f"Approve Error: {str(e)}"
            results['errors'][error_msg] = results['errors'].get(error_msg, 0) + 1
            await pending_collection.delete_one({"_id": user_doc["_id"]})
            return 

        # ACTION 2: ATTEMPT THE DM (NEVER LET DM FAILURE BLOCKS DATABASE SYNC)
        if approved_successfully:
            try:
                await bot.send_message(
                    chat_id=user_id, 
                    text=welcome_text, 
                    reply_markup=get_suggestion_keyboard()
                )
            except Exception:
                pass # Completely isolated error capture

            try:
                await pending_collection.delete_one({"_id": user_doc["_id"]})
                await users_collection.update_one(
                    {"_id": user_id}, 
                    {"$set": {"first_name": first_name}, "$addToSet": {"joined_chats": chat_id}}, 
                    upsert=True
                )
                results['success'] += 1
            except Exception:
                pass

        await asyncio.sleep(0.05)


async def process_queue_clearance(message: types.Message, chat_id: int):
    cursor = pending_collection.find({"chat_id": chat_id})
    pending_users = await cursor.to_list(length=None) 
    
    if not pending_users:
        await message.answer("📭 There are no pending requests in the database queue for this chat.")
        return

    await message.answer(f"⚡ **High-Speed Clearance Started!**\nProcessing **{len(pending_users)}** users concurrently in the background...", parse_mode="Markdown")
    
    channel_data = await channels_collection.find_one({"_id": chat_id})
    results = {'success': 0, 'fail': 0, 'errors': {}}
    
    semaphore = asyncio.Semaphore(20) 
    tasks = [process_single_pending_user(user, chat_id, channel_data, semaphore, results) for user in pending_users]
    
    await asyncio.gather(*tasks)
    await channels_collection.update_one({"_id": chat_id}, {"$inc": {"accepted_count": results['success']}}, upsert=True)
    
    report = (
        f"✅ **High-Speed Clearance Complete!**\n\n"
        f"🎯 **Successfully Approved:** {results['success']}\n"
        f"❌ **Failed:** {results['fail']}\n"
    )
    if results['fail'] > 0 or len(results['errors']) > 0:
        report += "\n🛑 **Exact Error Reasons:**\n"
        for err, count in results['errors'].items():
            report += f"- `{err}` ({count} users)\n"
            
    await message.answer(report, parse_mode="Markdown")

@dp.message(Command("approved"))
async def cmd_approved(message: types.Message, command: CommandObject, state: FSMContext):
    chat_id = await get_current_chat(message, state)
    if not chat_id: return

    if command.args:
        try:
            limit = int(command.args.strip())
            await channels_collection.update_one({"_id": chat_id}, {"$set": {"limit": limit}}, upsert=True)
            await message.answer(f"✅ Auto-accept limit set directly to **{limit}** members.")
        except ValueError:
            await message.answer("⚠️ Please provide a valid number. Example: `/approved 50`")
    else:
        asyncio.create_task(process_queue_clearance(message, chat_id))

# ==========================================
# SUPERFAST LIVE JOIN REQUEST & WAITING ROOM DM
# ==========================================
async def process_live_user_background(chat_id, user_id, first_name, welcome_text):
    """Approves live users instantly and completely isolates DM execution errors."""
    approved_successfully = False
    
    # 1. Clear approval first 
    try:
        await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        approved_successfully = True
    except Exception as e:
        print(f"Failed live background approval for {user_id}: {e}")
        return

    # 2. Fire DM and updates safely 
    if approved_successfully:
        try:
            await bot.send_message(chat_id=user_id, text=welcome_text, reply_markup=get_suggestion_keyboard())
        except Exception:
            pass # Isolated - even if they didn't /start, they remain approved in channel
            
        try:
            await asyncio.gather(
                users_collection.update_one(
                    {"_id": user_id}, 
                    {"$set": {"first_name": first_name}, "$addToSet": {"joined_chats": chat_id}}, 
                    upsert=True
                ),
                channels_collection.update_one({"_id": chat_id}, {"$inc": {"accepted_count": 1}}, upsert=True)
            )
        except Exception as e:
            print(f"Failed live background DB save for {user_id}: {e}")

@dp.chat_join_request()
async def handle_join_request(update: types.ChatJoinRequest):
    user_id = update.from_user.id
    chat_id = update.chat.id
    first_name = update.from_user.first_name

    channel_data = await channels_collection.find_one({"_id": chat_id})
    is_bot_active = channel_data.get("is_active", True) if channel_data else True
    max_limit = channel_data.get("limit", float('inf')) if channel_data else float('inf')
    current_count = channel_data.get("accepted_count", 0) if channel_data else 0

    if not is_bot_active or current_count >= max_limit:
        chat_title = channel_data.get("title", "our channel") if channel_data else "our channel"
        wait_text = (
            f"⏳ Hello {first_name}!\n\n"
            f"Your request to join **{chat_title}** has been received and you are in the Waiting Room.\n\n"
            f"⚠️ **IMPORTANT:** You must click /start below to confirm your spot in the queue!"
        )
        try:
            await bot.send_message(chat_id=user_id, text=wait_text, parse_mode="Markdown")
        except Exception:
            pass
            
        await pending_collection.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {"$set": {"first_name": first_name}},
            upsert=True
        )
        return

    default_msg = f"Hello {first_name}! Your request to join was approved. Welcome! 🎉"
    welcome_text = channel_data.get("welcome_message", default_msg) if channel_data else default_msg

    asyncio.create_task(process_live_user_background(chat_id, user_id, first_name, welcome_text))

# ==========================================
# WEBHOOK LIFECYCLE MANAGERS
# ==========================================
async def on_startup(bot: Bot):
    webhook_url = f"{RENDER_URL}{WEBHOOK_PATH}"
    print(f"Setting up high-speed Webhook to: {webhook_url}")
    
    await bot.set_webhook(
        webhook_url, 
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types() 
    )

def main():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is running smoothly via Webhooks!"))
    
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    
    setup_application(app, dp, bot=bot)
    dp.startup.register(on_startup)
    
    port = int(os.environ.get("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == '__main__':
    main()