import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
import asyncio

# ===== Logging =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== Environment Variables =====
TOKEN = os.getenv("TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")  # e.g., @Blogger_Templates_Updated
GROUP_USERNAME = os.getenv("GROUP_USERNAME")      # e.g., @Plus_UI_Official
FILE_URL = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"

if not all([TOKEN, CHANNEL_USERNAME, GROUP_USERNAME]):
    logger.error("❌ ERROR: TOKEN, CHANNEL_USERNAME, or GROUP_USERNAME not set.")
    exit()

# ===== Membership Check =====
async def is_member(bot, chat_id, user_id) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Error checking membership for {user_id} in {chat_id}: {e}")
        return False

# ===== Start Command =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    in_channel = await is_member(context.bot, CHANNEL_USERNAME, user_id)
    in_group = await is_member(context.bot, GROUP_USERNAME, user_id)

    if in_channel and in_group:
        await update.message.reply_text("✅ You are verified! Sending your file...")
        try:
            await context.bot.send_document(chat_id=user_id, document=FILE_URL)
        except Exception as e:
            logger.error(f"Error sending file: {e}")
            await update.message.reply_text(f"❌ Error sending file. Contact support.")
    else:
        # Buttons for joining
        keyboard = [
            [InlineKeyboardButton("Join Channel", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
            [InlineKeyboardButton("Join Group", url=f"https://t.me/{GROUP_USERNAME.lstrip('@')}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "❌ You must join both the Channel and Group to get the file.\n\n"
            "After joining, type 'Done'.",
            reply_markup=reply_markup
        )

# ===== Done Handler =====
async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# ===== Keep Alive for Render =====
from flask import Flask
import threading
app = Flask("")

@app.route("/")
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run).start()

# ===== Main =====
def main():
    logger.info("Starting bot...")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("(?i)^done$"), done))
    app.run_polling()

if __name__ == "__main__":
    main()
