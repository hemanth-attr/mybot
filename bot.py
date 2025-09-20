import logging
import os
import asyncio
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import TelegramError

# ==== Configuration ====
TOKEN = os.getenv("TOKEN")
CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"
PORT = int(os.environ.get("PORT", 10000))

# ==== Logging ====
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==== Flask App ====
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive ✅"

@app.route("/ping")
def ping():
    return "OK"

# ==== Bot Setup ====
bot = Bot(TOKEN)
application = ApplicationBuilder().bot(bot).build()

# ==== Telegram Safe Functions ====
async def safe_send_message(chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except TelegramError as e:
        logger.warning(f"Send message failed to {chat_id}: {e}")

async def safe_send_document(chat_id, document):
    try:
        await bot.send_document(chat_id=chat_id, document=document)
    except TelegramError as e:
        logger.warning(f"Send document failed to {chat_id}: {e}")

async def safe_send_sticker(chat_id, sticker):
    try:
        await bot.send_sticker(chat_id=chat_id, sticker=sticker)
    except TelegramError as e:
        logger.warning(f"Send sticker failed to {chat_id}: {e}")

async def safe_send_photo(message_obj, photo, caption, reply_markup):
    try:
        await message_obj.reply_photo(
            photo=photo,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        logger.warning(f"Send photo failed: {e}")

async def safe_delete(callback_query):
    try:
        await callback_query.delete_message()
    except TelegramError as e:
        logger.warning(f"Delete message failed: {e}")

# ==== Bot Handlers ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_join_message(update, context)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "done":
        if await is_member_all(user_id):
            await safe_delete(query)
            await safe_send_sticker(user_id, STICKER_ID)
            await safe_send_message(user_id, f"👋 Hello {query.from_user.first_name}!\n✨ Your theme is ready!")
            await safe_send_document(user_id, FILE_PATH)
        else:
            await safe_delete(query)
            await send_join_message(update, context, query=True)

async def is_member_all(user_id: int) -> bool:
    for ch in CHANNELS:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except TelegramError as e:
            logger.warning(f"Membership check failed for {ch}: {e}")
            return False
    return True

async def send_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE, query=False):
    keyboard = [
        [
            InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{CHANNELS[0].strip('@')}"),
            InlineKeyboardButton("👥 Join Group", url=f"https://t.me/{CHANNELS[1].strip('@')}")
        ],
        [InlineKeyboardButton("✅ Done!!!", callback_data="done")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    caption = "💡 Join all channels & groups to download the latest Plus UI Blogger Template! Press ✅ Done after joining."

    if query:
        await safe_send_photo(update.callback_query.message, JOIN_IMAGE, caption, reply_markup)
    else:
        await safe_send_photo(update.message, JOIN_IMAGE, caption, reply_markup)

# ==== Run Bot in Polling Mode ====
async def run_bot():
    try:
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(button))

        # Initialize and start polling
        await application.initialize()
        await application.start()
        await application.updater.start_polling()  # <-- This handles all updates
        logger.info("Bot started with polling ✅")

        # Keep running
        await asyncio.Event().wait()
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        await asyncio.sleep(5)
        await run_bot()

# ==== Main Entry Point ====
async def main():
    bot_task = asyncio.create_task(run_bot())

    # Run Flask app
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    await serve(app, config)

    bot_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())
