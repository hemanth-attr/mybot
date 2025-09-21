import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from flask import Flask
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions, Bot
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.error import TelegramError

# ================= Configuration =================
TOKEN = os.getenv("TOKEN")
CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0-Updated.zip"
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"
PORT = int(os.environ.get("PORT", 10000))
ALLOWED_GROUP_ID = -1002810504524  # Only this group will auto-mute users

# ================= Logging =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= Flask App =================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive ✅"

@app.route("/ping")
def ping():
    return "OK"

# ================= Bot Setup =================
bot = Bot(TOKEN)
application = ApplicationBuilder().bot(bot).build()

# ================= Global Data =================
warnings = {}  # {chat_id: {user_id: {"count": int, "expiry": datetime}}}
url_pattern = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+)", re.IGNORECASE)

# ================= Helper Functions =================
def add_warning(chat_id: int, user_id: int):
    chat_warns = warnings.setdefault(chat_id, {})
    user_warn = chat_warns.get(user_id, {"count": 0, "expiry": datetime.now()})
    
    user_warn["count"] += 1
    user_warn["expiry"] = datetime.now() + timedelta(days=1)
    chat_warns[user_id] = user_warn
    
    return user_warn["count"], user_warn["expiry"]

def clean_expired_warnings():
    now = datetime.now()
    for chat_id in list(warnings.keys()):
        for user_id in list(warnings[chat_id].keys()):
            if warnings[chat_id][user_id]["expiry"] < now:
                del warnings[chat_id][user_id]

async def safe_send_message(chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except TelegramError as e:
        logger.warning(f"Send message failed to {chat_id}: {e}")

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

# ================= Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_join_message(update, context)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "done":
        if await is_member_all(user_id):
            await safe_delete(query)

            # Send sticker as reply
            try:
                await query.message.reply_sticker(STICKER_ID)
            except TelegramError as e:
                logger.warning(f"Failed to send sticker: {e}")

            # Send greeting
            await query.message.reply_text(f"👋 Hello {query.from_user.first_name}!\n✨ Your theme is ready!")

            # Send file as reply
            try:
                await query.message.reply_document(
                    document=FILE_PATH,
                    filename="Plus-Ui-3.2.0-Updated.zip"
                )
            except TelegramError as e:
                logger.warning(f"Failed to send document: {e}")
        else:
            await safe_delete(query)
            await send_join_message(update, context, query=True)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clean_expired_warnings()
    
    if not update.message:
        return
    
    user = update.message.from_user
    chat = update.effective_chat
    text = update.message.text or ""
    
    # Skip admins
    chat_admins = await chat.get_administrators()
    admin_ids = [admin.user.id for admin in chat_admins]
    if user.id in admin_ids:
        return
    
    # Detect links or forwarded messages
    if update.message.forward_from or url_pattern.search(text):
        await update.message.delete()
        warn_count, expiry = add_warning(chat.id, user.id)
        expiry_str = expiry.strftime("%d/%m/%Y %H:%M")
        await update.message.reply_text(f"{user.first_name} ⚠ Warning ({warn_count}/3)")

        # Notify admins
        for admin in chat_admins:
            await safe_send_message(
                admin.user.id,
                f"@{user.username if user.username else user.first_name} [{user.id}] "
                f"sent a {'forwarded message' if update.message.forward_from else '🔗 Link'} "
                f"without authorization. Warn ({warn_count}/3) ❕ until {expiry_str}."
            )

        # Auto-mute on 3 warnings (only in your group)
        if warn_count >= 3 and chat.id == ALLOWED_GROUP_ID:
            try:
                until_date = datetime.now() + timedelta(days=1)
                await context.bot.restrict_chat_member(
                    chat_id=chat.id,
                    user_id=user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
                await safe_send_message(
                    chat.id,
                    f"{user.first_name} has been muted for 1 day for reaching 3 warnings ⚠"
                )
            except Exception as e:
                logger.warning(f"Failed to mute {user.id}: {e}")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel_warn":
        await safe_delete(query)
        await safe_send_message(query.from_user.id, "⚠ Warning canceled by admin.")

# ================= Run Bot =================
async def run_bot():
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    logger.info("Bot started ✅")
    await asyncio.Event().wait()

# ================= Main Entry Point =================
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
