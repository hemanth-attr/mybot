import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Enable logging for debugging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== Environment Variables and Configuration =====
TOKEN = os.getenv("TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")   # e.g., @Blogger_Templates_Updated
GROUP_USERNAME = os.getenv("GROUP_USERNAME")       # e.g., @Plus_UI_Official

# File to send (GitHub raw link)
FILE_URL = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"

# Check if environment variables are set
if not all([TOKEN, CHANNEL_USERNAME, GROUP_USERNAME]):
    logger.error("❌ ERROR: One or more environment variables (TOKEN, CHANNEL_USERNAME, GROUP_USERNAME) are not set.")
    exit()

# ===== Membership Check =====
async def is_member(bot, chat_id, user_id) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        # Check for any of the valid member statuses
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"❌ Error checking membership for user {user_id} in chat {chat_id}: {e}")
        return False

# ===== Start Command Handler =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"User {user_id} started the bot.")

    # Check channel and group membership
    in_channel = await is_member(context.bot, CHANNEL_USERNAME, user_id)
    in_group = await is_member(context.bot, GROUP_USERNAME, user_id)

    if in_channel and in_group:
        logger.info(f"User {user_id} is a verified member. Sending file.")
        await update.message.reply_text("✅ You are verified! Sending your file...")
        try:
            await context.bot.send_document(chat_id=user_id, document=FILE_URL)
        except Exception as e:
            logger.error(f"❌ Error sending file to user {user_id}: {e}")
            await update.message.reply_text(f"❌ Error sending file. Please contact support. Error details: `{e}`", parse_mode="Markdown")
    else:
        logger.info(f"User {user_id} is not verified. Providing instructions.")
        await update.message.reply_text(
            f"❌ You must join both:\n"
            f"📢 Channel: {CHANNEL_USERNAME}\n"
            f"👥 Group: {GROUP_USERNAME}\n\n"
            f"After joining, type *Done* to receive the file.",
            parse_mode="Markdown"
        )

# ===== Done Message Handler =====
async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"User {update.effective_user.id} typed 'Done'.")
    await start(update, context)

# ===== Main Function and Bot Initialization =====
def main():
    logger.info("Starting bot...")
    app = ApplicationBuilder().token(TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("(?i)^done$"), done))

    # Run bot
    app.run_polling()
    logger.info("Bot is running.")

if __name__ == "__main__":
    main()
