import logging
import os
import threading
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ===================== CONFIG =====================
TOKEN = os.getenv("BOT_TOKEN")  # ✅ set BOT_TOKEN in Render
CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"
PORT = int(os.environ.get("PORT", 10000))
# ===================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app for Render healthcheck
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive ✅"

# ================= Bot Handlers =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_join_message(update, context)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "done":
        user_id = query.from_user.id
        if await is_member_all(context, user_id):
            await query.delete_message()

            username = query.from_user.first_name
            await context.bot.send_sticker(chat_id=user_id, sticker=STICKER_ID)

            text = (
                f"👋 Hello {username} !!!\n\n"
                "📚 This Bot Helps You In Downloading the latest Plus UI Blogger template version\n\n"
                "✨ Your theme is now ready..."
            )
            await context.bot.send_message(chat_id=user_id, text=text)
            await context.bot.send_document(chat_id=user_id, document=FILE_PATH)
        else:
            await query.delete_message()
            await send_join_message(update, context, query=True)

async def is_member_all(context, user_id: int) -> bool:
    for ch in CHANNELS:
        try:
            member = await context.bot.get_chat_member(ch, user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except Exception as e:
            logger.error(f"Error checking {ch}: {e}")
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

    caption = (
        "💡 Join All Channels & Groups To Download the Latest Plus UI Blogger Template !!!\n\n"
        "[Plus UI Official Group](https://t.me/Plus_UI_Official)\n\n"
        "After joining, press ✅ Done!!!"
    )

    if query:
        await update.callback_query.message.reply_photo(
            photo=JOIN_IMAGE,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_photo(
            photo=JOIN_IMAGE,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

# ================= Main =================

def run_bot():
    bot_app = Application.builder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CallbackQueryHandler(button))

    bot_app.run_polling()

if __name__ == "__main__":
    # Run bot in background
    threading.Thread(target=run_bot, daemon=True).start()

    # Run Flask
    app.run(host="0.0.0.0", port=PORT)
