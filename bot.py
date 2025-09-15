import logging
import os
import asyncio
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ===================== CONFIG =====================
TOKEN = os.getenv("TOKEN")  # BOT_TOKEN in Render Environment Variables
CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]  # your channels/groups
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"  # your image
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"  # your template file
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"  # your sticker
PORT = int(os.environ.get("PORT", 10000))  # Flask port
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
            InlineKeyboardButton("📢 Join Channel 1", url=f"https://t.me/{CHANNELS[0].strip('@')}"),
            InlineKeyboardButton("👥 Join Group", url=f"https://t.me/{CHANNELS[1].strip('@')}")
        ],
        [InlineKeyboardButton("✅ Done!!!", callback_data="done")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    caption = (
        "💡 Join All Channels & Groups To Download the Latest Plus UI Blogger Template !!!<br><br>"
        '<a href="https://t.me/Plus_UI_Official">Plus UI Official Group</a><br><br>'
        "After joining, press ✅ Done!!!"
    )

    if query:
        await update.callback_query.message.reply_photo(
            photo=JOIN_IMAGE,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_photo(
            photo=JOIN_IMAGE,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )

# ================= Main =================

async def main():
    bot_app = ApplicationBuilder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CallbackQueryHandler(button))

    # Run bot + Flask together
    async def run_flask():
        loop = asyncio.get_event_loop()
        from hypercorn.asyncio import serve
        from hypercorn.config import Config
        config = Config()
        config.bind = [f"0.0.0.0:{PORT}"]
        await serve(app, config)

    await asyncio.gather(
        bot_app.run_polling(),
        run_flask()
    )

if __name__ == "__main__":
    asyncio.run(main())
