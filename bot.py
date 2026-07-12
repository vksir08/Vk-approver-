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
        [InlineKeyboardButton(text="Support", url="https://t.me/Vk_Devz")]
    ])

# Helper to verify active connection
async def get_current_chat(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get("connected_chat_id")
    if not chat_id:
        await message.answer("⚠️ No chat currently selected. Please run `/connect <chat_id>` first.", parse_mode="Markdown")
        return None
    return chat_id

# ==========================================
# BASIC COMMANDS & MANUAL CONNECTION
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Hello, {message.from_user.first_name}!\n\n"
        "I am a High-Speed Auto-Accept Bot running on Webhooks.\n\n"
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
        "🔹 `/broadcast` - Send a message to all accepted users\n"
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(Command("groupstats"))
async def cmd_groupstats(message: types.Message):
    # 1. Block anyone who isn't the owner
    if message.from_user.id != BOT_OWNER_ID:
        await message.answer("❌ **Permission Denied.** Only the Bot Owner can view global statistics.")
        return

    loading_msg = await message.answer("📊 Fetching network statistics from the database...")
    
    # 2. Pull all connected chats from MongoDB
    cursor = channels_collection.find({})
    channels = await cursor.to_list(length=None)
    
    if not channels:
        await loading_msg.edit_text("⚠️ No groups or channels are currently connected to the database.")
        return

    # 3. Build the Master Report
    stats_message = "🌐 **Global Network Statistics** 🌐\n\n"
    
    for chat in channels:
        chat_id = chat["_id"]
        chat_title = chat.get("title", "Unknown Chat (Reconnect to fix)")
        is_active = chat.get("is_active", True)
        
        # Format the limit nicely
        raw_limit = chat.get("limit", float('inf'))
        limit_display = "∞" if raw_limit == float('inf') else str(raw_limit)
        
        accepted = chat.get("accepted_count", 0)
        
        # Count how many users are waiting in the queue specifically for this chat
        pending_count = await pending_collection.count_documents({"chat_id": chat_id})
        
        status_icon = "🟢 ACTIVE (Auto-Accepting)" if is_active else "🔴 PAUSED (Routing to Queue)"
        
        stats_message += (
            f"🏷 **{chat_title}**\n"
            f"🆔 `{chat_id}`\n"
            f"⚙️ Status: {status_icon}\n"
            f"👥 Accepted Users: {accepted} / {limit_display}\n"
            f"⏳ Waiting in Queue: {pending_count}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )
        
    # 4. Safe Delivery (Handles Telegram's 4096 character limit for massive networks)
    await loading_msg.delete()
    
    if len(stats_message) <= 4000:
        await message.answer(stats_message, parse_mode="Markdown")
    else:
        # If the text is too long, split it into multiple messages
        for x in range(0, len(stats_message), 4000):
            await message.answer(stats_message[x:x+4000], parse_mode="Markdown")

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

        # Fetch the actual name of the channel/group from Telegram
        chat_info = await bot.get_chat(chat_id)
        chat_title = chat_info.title or "Unknown Chat"

        # Save the chat setup AND the title to the database
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

# ==========================================
# CONTROL SETTINGS
# ==========================================
@dp.message(Command("approved"))
async def cmd_approved(message: types.Message, command: CommandObject, state: FSMContext):
    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    if command.args:
        try:
            limit = int(command.args.strip())
            await channels_collection.update_one({"_id": chat_id}, {"$set": {"limit": limit}}, upsert=True)
            await message.answer(f"✅ Auto-accept limit set directly to **{limit}** members.")
        except ValueError:
            await message.answer("⚠️ Please provide a valid number. Example: `/approved 50`")
    else:
        # RUN IN BACKGROUND: Prevents the server from crashing on massive queues!
        asyncio.create_task(process_queue_clearance(message, chat_id))

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

@dp.message(Command("approved"))
async def cmd_approved(message: types.Message, command: CommandObject, state: FSMContext):
    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    if command.args:
        # Direct limit setting
        try:
            limit = int(command.args.strip())
            await channels_collection.update_one({"_id": chat_id}, {"$set": {"limit": limit}}, upsert=True)
            await message.answer(f"✅ Auto-accept limit set directly to **{limit}** members.")
        except ValueError:
            await message.answer("⚠️ Please provide a valid number. Example: `/approved 50`")
    else:
        # Clear the queue for the connected chat
        await process_queue_clearance(message, chat_id)

# ==========================================
# UPDATED WELCOME SYSTEM (TEXT & REPLY CAPABLE)
# ==========================================
@dp.message(Command("welcome"))
async def cmd_welcome(message: types.Message, state: FSMContext):
    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return

    if message.reply_to_message:
        replied_text = message.reply_to_message.text or message.reply_to_message.caption
        if not replied_text:
            await message.answer("⚠️ The message you replied to does not contain any text.")
            return

        await channels_collection.update_one({"_id": chat_id}, {"$set": {"welcome_message": replied_text}}, upsert=True)
        await message.answer(f"✅ **Welcome message updated via reply!**\n\nNew greeting text:\n\"{replied_text}\"", parse_mode="Markdown")
        await state.set_state(None)
    else:
        await message.answer("✍️ Send the custom welcome text for this chat (or reply to any text message with `/welcome` to set it instantly):")
        await state.set_state(BotConfigState.waiting_for_welcome)

@dp.message(BotConfigState.waiting_for_welcome)
async def process_welcome_text(message: types.Message, state: FSMContext):
    chat_id = await get_current_chat(message, state)
    if chat_id:
        await channels_collection.update_one({"_id": chat_id}, {"$set": {"welcome_message": message.text}}, upsert=True)
        await message.answer(f"✅ **Welcome message updated!**\n\nNew greeting text:\n\"{message.text}\"", parse_mode="Markdown")
        await state.set_state(None)

# ==========================================
# BROADCAST SYSTEM (CHANNEL SPECIFIC & GLOBAL)
# ==========================================
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    """Admin command to broadcast to the currently connected chat."""
    chat_id = await get_current_chat(message, state)
    if not chat_id:
        return
        
    await message.answer(f"📢 Send the message you want to broadcast to members of the connected chat (`{chat_id}`):")
    await state.update_data(broadcast_target=chat_id)
    await state.set_state(BotConfigState.waiting_for_broadcast)

@dp.message(Command("broadcast_all"))
async def cmd_broadcast_all(message: types.Message, state: FSMContext):
    """Owner-only command to broadcast globally across all databases."""
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
    
    # 1. Fetch users from Database
    if target == "ALL":
        cursor = users_collection.find({}, {"_id": 1})
    else:
        cursor = users_collection.find({"joined_chats": target}, {"_id": 1})
        
    users_list = await cursor.to_list(length=None)
    total_users = len(users_list)
    
    # 2. Check if Database is empty
    if total_users == 0:
        await message.answer(
            "⚠️ **Database Empty:** I found 0 users for this specific channel.\n"
            "*(If these users were in the queue from an older version of the bot, they may not be tagged with this channel ID yet).* ",
            parse_mode="Markdown"
        )
        return

    await message.answer(f"⏳ Broadcast starting... Attempting to send to {total_users} users.")
    
    success_count = 0
    fail_count = 0
    
    # 3. Execute Broadcast
    for user_doc in users_list:
        try:
            await message.copy_to(chat_id=user_doc["_id"])
            success_count += 1
            await asyncio.sleep(0.05) 
        except Exception as e:
            fail_count += 1
            print(f"Failed to send to {user_doc['_id']}: {e}")

    # 4. Final Diagnostic Report
    report = (
        f"✅ **Broadcast Finished!**\n\n"
        f"🎯 **Delivered:** {success_count}\n"
        f"❌ **Failed:** {fail_count} *(User blocked bot or strict privacy settings)*\n"
        f"📊 **Total in DB for this Chat:** {total_users}"
    )
    await message.answer(report, parse_mode="Markdown")

# ==========================================
# BACKGROUND QUEUE CLEARANCE LOGIC
# ==========================================
async def process_queue_clearance(message: types.Message, chat_id: int):
    cursor = pending_collection.find({"chat_id": chat_id})
    pending_users = await cursor.to_list(length=None) 
    
    if not pending_users:
        await message.answer("📭 There are no pending requests in the database queue for this chat.")
        return

    # Notify admin that the background task has successfully started
    await message.answer(
        f"⏳ **Background Task Started!**\n"
        f"Approving **{len(pending_users)}** users from the queue...\n"
        f"*(This may take a few minutes for large queues. I will DM you when it is 100% complete!)*", 
        parse_mode="Markdown"
    )
    
    channel_data = await channels_collection.find_one({"_id": chat_id})
    success_count = 0
    fail_count = 0
    
    for user_doc in pending_users:
        user_id = user_doc["user_id"]
        first_name = user_doc.get("first_name", "User")
        
        try:
            default_msg = f"Hello {first_name}! Your request to join was approved. Welcome! 🎉"
            welcome_text = channel_data.get("welcome_message", default_msg) if channel_data else default_msg
            
            await bot.send_message(chat_id=user_id, text=welcome_text, reply_markup=get_suggestion_keyboard())
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            
            success_count += 1
            await pending_collection.delete_one({"_id": user_doc["_id"]})
            
            # Move from pending DB to accepted DB
            await users_collection.update_one(
                {"_id": user_id}, 
                {
                    "$set": {"first_name": first_name},
                    "$addToSet": {"joined_chats": chat_id}
                }, 
                upsert=True
            )
        except Exception as e:
            fail_count += 1
            # Even if it fails, delete from pending so the bot never gets stuck on a broken user
            await pending_collection.delete_one({"_id": user_doc["_id"]})
            
        # Mandatory speed limit to keep Telegram happy (20 actions per second)
        await asyncio.sleep(0.05) 

    # Final database update and Admin notification
    await channels_collection.update_one({"_id": chat_id}, {"$inc": {"accepted_count": success_count}}, upsert=True)
    
    await message.answer(
        f"✅ **Queue Clearance Complete!**\n\n"
        f"🎯 **Successfully Approved:** {success_count}\n"
        f"❌ **Failed:** {fail_count} *(Users deleted their account or blocked the bot while waiting)*\n\n"
        f"All successful users have been permanently added to the broadcasting database.", 
        parse_mode="Markdown"
    )

# ==========================================
# HIGH-SPEED JOIN REQUEST & MANDATORY DM
# ==========================================
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
        await pending_collection.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {"$set": {"first_name": first_name}},
            upsert=True
        )
        return

    try:
        default_msg = f"Hello {first_name}! Your request to join was approved. Welcome! 🎉"
        welcome_text = channel_data.get("welcome_message", default_msg) if channel_data else default_msg

        await bot.send_message(chat_id=user_id, text=welcome_text, reply_markup=get_suggestion_keyboard())
        await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        
        # Update user record with their name and add the channel ID to their "joined_chats" list
        asyncio.create_task(users_collection.update_one(
            {"_id": user_id}, 
            {
                "$set": {"first_name": first_name},
                "$addToSet": {"joined_chats": chat_id} # <--- THIS IS THE 3RD POINT
            }, 
            upsert=True
        ))
        asyncio.create_task(channels_collection.update_one({"_id": chat_id}, {"$inc": {"accepted_count": 1}}, upsert=True))
    except Exception as e:
        print(f"Error handling high-speed request for {user_id}: {e}")

# ==========================================
# WEBHOOK LIFECYCLE MANAGERS
# ==========================================
async def on_startup(bot: Bot):
    webhook_url = f"{RENDER_URL}{WEBHOOK_PATH}"
    print(f"Setting up high-speed Webhook to: {webhook_url}")
    await bot.set_webhook(webhook_url, drop_pending_updates=True)

def main():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is running smoothly via Webhooks!"))
    
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    
    setup_application(app, dp, bot=bot)
    dp.startup.register(on_startup)
    
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == '__main__':
    main()