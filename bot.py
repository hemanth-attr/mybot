import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from flask import Flask
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions, Bot, InputFile
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.error import TelegramError

# ================= Configuration =================
TOKEN = os.getenv("TOKEN")
CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"
PORT = int(os.environ.get("PORT", 10000))
ALLOWED_GROUP_ID = -1002810504524  # Only this group will auto-mute users
WARNINGS_FILE = "warnings.json"

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
url_pattern = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+)", re.IGNORECASE)

# ----------------- Warnings Persistence -----------------
def load_warnings():
    if os.path.exists(WARNINGS_FILE):
        with open(WARNINGS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_warnings():
    with open(WARNINGS_FILE, "w") as f:
        json.dump(warnings, f, default=str)

warnings = load_warnings()

def add_warning(chat_id: int, user_id: int):
    chat_warns = warnings.setdefault(str(chat_id), {})
    user_warn = chat_warns.get(str(user_id), {"count": 0, "expiry": str(datetime.now())})

    user_warn["count"] += 1
    user_warn["expiry"] = str(datetime.now() + timedelta(days=1))
    chat_warns[str(user_id)] = user_warn
    save_warnings()

    return user_warn["count"], user_warn["expiry"]

def clean_expired_warnings():
    now = datetime.now()
    for chat_id in list(warnings.keys()):
        for user_id in list(warnings[chat_id].keys()):
            expiry = datetime.fromisoformat(warnings[chat_id][user_id]["expiry"])
            if expiry < now:
                del warnings[chat_id][user_id]
    save_warnings()

# ================= Safe Telegram Functions =================
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

# ================= Helper Functions =================
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

            # Send sticker + greeting + ZIP in private chat
            await safe_send_sticker(user_id, STICKER_ID)
            await safe_send_message(user_id, f"👋 Hello {query.from_user.first_name}!\n✨ Your theme is ready!")
            await safe_send_document(user_id, FILE_PATH)
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
        try:
            await update.message.delete()
        except Exception:
            logger.warning("Failed to delete user message")

        warn_count, expiry = add_warning(chat.id, user.id)
        expiry_str = datetime.fromisoformat(expiry).strftime("%d/%m/%Y %H:%M")
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

# ================= Run Bot =================
async def run_bot():
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button, pattern="^done$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

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
