import os
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, KeyboardButtonRequestChat, ReplyKeyboardRemove
)
from motor.motor_asyncio import AsyncIOMotorClient

# ==========================================
# CONFIGURATION
# ==========================================
# Load secrets from the cloud environment (Render Environment Variables)
BOT_TOKEN = '8689246254:AAGxwjqpdUdN_-jefP9PjvLzCKVYIugttV0'
MONGO_URI = "mongodb+srv://vikkixsir1221_db_user:14HtD8cv0SWYC835@cluster0.s1dnbig.mongodb.net/?appName=Cluster0"

# Database Initialization
client = AsyncIOMotorClient(MONGO_URI)
db = client["telegram_bot_db"]
users_collection = db["users"]             # Stores accepted users for broadcasts
channels_collection = db["channels"]       # Stores channel limits, status, and welcome texts
pending_collection = db["pending_requests"] # Stores users waiting when bot is OFF or limit reached

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ==========================================
# STATES & KEYBOARDS
# ==========================================
class BotConfigState(StatesGroup):
    waiting_for_chat = State()
    waiting_for_number = State()
    waiting_for_welcome = State()
    waiting_for_broadcast = State()

def get_suggestion_keyboard():
    """Inline keyboard sent to users in their welcome DM."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Join our Backup Channel", url="https://t.me/your_backup")],
        [InlineKeyboardButton(text="Join our Discussion Group", url="https://t.me/your_group")]
    ])

def get_chat_selector():
    """Native Telegram buttons prompting the admin to share a channel or group with the bot."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📢 Select Channel", request_chat=KeyboardButtonRequestChat(request_id=1, chat_is_channel=True, bot_is_member=True)),
                KeyboardButton(text="👥 Select Group", request_chat=KeyboardButtonRequestChat(request_id=2, chat_is_channel=False, bot_is_member=True))
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

# ==========================================
# BASIC COMMANDS
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Hello, {message.from_user.first_name}!\n\n"
        "I am an advanced Auto-Accept Bot. Add me to your channel as an admin with 'Add Users' permissions to get started.\n\n"
        "Send /help to see all configuration commands.",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "⚙️ **Admin Commands:**\n"
        "🔹 `/setlimit` - Set max users to auto-accept\n"
        "🔹 `/welcome` - Set custom welcome DM text\n"
        "🔹 `/approve_on` - Turn auto-accept ON\n"
        "🔹 `/approve_off` - Turn OFF (Routes users to Queue)\n"
        "🔹 `/approved` - Accept all users currently in the Queue\n"
        "🔹 `/approved <number>` - Set the accept limit directly\n"
        "🔹 `/broadcast` - Send a message to all accepted users\n"
    )
    await message.answer(help_text, parse_mode="Markdown")

# ==========================================
# ADMIN CONFIGURATION TRIGGERS
# ==========================================
@dp.message(Command("setlimit"))
async def cmd_setlimit(message: types.Message, state: FSMContext):
    await state.update_data(action_type="SETLIMIT")
    await message.answer("Set Limit: Please select the target channel/group below:", reply_markup=get_chat_selector())
    await state.set_state(BotConfigState.waiting_for_chat)

@dp.message(Command("welcome"))
async def cmd_welcome(message: types.Message, state: FSMContext):
    await state.update_data(action_type="WELCOME")
    await message.answer("Set Welcome Text: Please select the target channel/group below:", reply_markup=get_chat_selector())
    await state.set_state(BotConfigState.waiting_for_chat)

@dp.message(Command("approve_on"))
async def cmd_approve_on(message: types.Message, state: FSMContext):
    await state.update_data(action_type="ON")
    await message.answer("Turn ON: Please select the target channel/group below:", reply_markup=get_chat_selector())
    await state.set_state(BotConfigState.waiting_for_chat)

@dp.message(Command("approve_off"))
async def cmd_approve_off(message: types.Message, state: FSMContext):
    await state.update_data(action_type="OFF")
    await message.answer("Turn OFF: Please select the target channel/group below:", reply_markup=get_chat_selector())
    await state.set_state(BotConfigState.waiting_for_chat)

@dp.message(Command("approved"))
async def cmd_approved(message: types.Message, command: CommandObject, state: FSMContext):
    if command.args:
        try:
            limit = int(command.args.strip())
            await state.update_data(action_type="DIRECT_LIMIT", limit_val=limit)
            await message.answer(f"Apply limit of {limit}: Select target channel/group below:", reply_markup=get_chat_selector())
        except ValueError:
            await message.answer("⚠️ Please provide a valid number. Example: `/approved 50`")
            return
    else:
        await state.update_data(action_type="CLEAR_QUEUE")
        await message.answer("Clear Queue: Select the channel/group to accept all pending users:", reply_markup=get_chat_selector())
        
    await state.set_state(BotConfigState.waiting_for_chat)

# ==========================================
# TARGET CHAT RESOLUTION
# ==========================================
@dp.message(F.chat_shared)
async def process_chat_selection(message: types.Message, state: FSMContext):
    chat_id = message.chat_shared.chat_id
    user_data = await state.get_data()
    action = user_data.get("action_type")

    removing_msg = await message.answer("Processing...", reply_markup=ReplyKeyboardRemove())
    await removing_msg.delete()

    if action in ["ON", "OFF"]:
        is_active = (action == "ON")
        await channels_collection.update_one({"_id": chat_id}, {"$set": {"is_active": is_active}}, upsert=True)
        status = "✅ Enabled (Auto-Accepting)" if is_active else "🛑 Disabled (Routing to Queue)"
        await message.answer(f"Status updated! The bot is now **{status}** for this chat.", parse_mode="Markdown")
        await state.clear()
        
    elif action == "DIRECT_LIMIT":
        limit = user_data["limit_val"]
        await channels_collection.update_one({"_id": chat_id}, {"$set": {"limit": limit}}, upsert=True)
        await message.answer(f"✅ Success! Auto-accept limit set to **{limit}** members.", parse_mode="Markdown")
        await state.clear()
        
    elif action == "SETLIMIT":
        await state.update_data(target_chat_id=chat_id)
        await message.answer("🔢 Reply with the maximum number of members to auto-accept:")
        await state.set_state(BotConfigState.waiting_for_number)
        
    elif action == "WELCOME":
        await state.update_data(target_chat_id=chat_id)
        await message.answer("✍️ Please send the new welcome message text. (Formatting is supported)")
        await state.set_state(BotConfigState.waiting_for_welcome)
        
    elif action == "CLEAR_QUEUE":
        await process_queue_clearance(message, chat_id)
        await state.clear()

# ==========================================
# STATE HANDLERS (TEXT INPUTS)
# ==========================================
@dp.message(BotConfigState.waiting_for_number)
async def process_limit_number(message: types.Message, state: FSMContext):
    try:
        new_limit = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Please enter a valid numeric limit.")
        return

    user_data = await state.get_data()
    chat_id = user_data['target_chat_id']
    
    await channels_collection.update_one({"_id": chat_id}, {"$set": {"limit": new_limit}}, upsert=True)
    await message.answer(f"✅ Limit set to **{new_limit}**.", parse_mode="Markdown")
    await state.clear()

@dp.message(BotConfigState.waiting_for_welcome)
async def process_welcome_text(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    chat_id = user_data['target_chat_id']
    
    await channels_collection.update_one({"_id": chat_id}, {"$set": {"welcome_message": message.text}}, upsert=True)
    await message.answer(f"✅ Welcome message updated to:\n\n\"{message.text}\"")
    await state.clear()

# ==========================================
# BROADCAST SYSTEM
# ==========================================
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    await message.answer("📢 Send the message you want to broadcast to all accepted users (Text, Photo, etc.):")
    await state.set_state(BotConfigState.waiting_for_broadcast)

@dp.message(BotConfigState.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("⏳ Broadcast starting...")
    
    cursor = users_collection.find({}, {"_id": 1})
    success_count = 0
    
    async for user_doc in cursor:
        try:
            await message.copy_to(chat_id=user_doc["_id"])
            success_count += 1
            await asyncio.sleep(0.05) 
        except Exception:
            pass 

    await message.answer(f"✅ Broadcast finished! Delivered to **{success_count}** users.", parse_mode="Markdown")

# ==========================================
# CORE: JOIN REQUEST HANDLER & QUEUE
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
        
        await users_collection.update_one({"_id": user_id}, {"$set": {"first_name": first_name}}, upsert=True)
        await channels_collection.update_one({"_id": chat_id}, {"$inc": {"accepted_count": 1}}, upsert=True)
    except Exception as e:
        print(f"Failed to process user {user_id}: {e}")

async def process_queue_clearance(message: types.Message, chat_id: int):
    cursor = pending_collection.find({"chat_id": chat_id})
    pending_users = await cursor.to_list(length=None) 
    
    if not pending_users:
        await message.answer("📭 There are no pending requests in the database queue for this chat.")
        return

    await message.answer(f"⏳ Approving **{len(pending_users)}** users from the queue...", parse_mode="Markdown")
    
    channel_data = await channels_collection.find_one({"_id": chat_id})
    success_count = 0
    
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
            await users_collection.update_one({"_id": user_id}, {"$set": {"first_name": first_name}}, upsert=True)
            await asyncio.sleep(0.05) 
            
        except Exception:
            await pending_collection.delete_one({"_id": user_doc["_id"]})

    await channels_collection.update_one({"_id": chat_id}, {"$inc": {"accepted_count": success_count}}, upsert=True)
    await message.answer(f"✅ Cleared queue! Successfully approved **{success_count}/{len(pending_users)}** users.", parse_mode="Markdown")

# ==========================================
# RENDER.COM WEB SERVER & BOT STARTUP
# ==========================================
async def handle_ping(request):
    """Dummy endpoint so Render knows the service is healthy."""
    return web.Response(text="Bot is alive and running!")

async def keep_alive_server():
    """Starts a lightweight web server on the port Render expects."""
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def main():
    print("Starting web server for Render...")
    await keep_alive_server()
    
    print("Bot is successfully running...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())