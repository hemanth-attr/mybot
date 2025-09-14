import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ===== Environment Variables =====
TOKEN = os.getenv("TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")   # e.g., @Blogger_Templates_Updated
GROUP_USERNAME = os.getenv("GROUP_USERNAME")       # e.g., @Plus_UI_Official

# File to send (GitHub raw link)
FILE_URL = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"

# ===== Membership Check =====
async def is_member(bot, chat_id, user_id) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

# ===== Start Command =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Check channel & group membership
    in_channel = await is_member(context.bot, CHANNEL_USERNAME, user_id)
    in_group = await is_member(context.bot, GROUP_USERNAME, user_id)

    if in_channel and in_group:
        await update.message.reply_text("✅ You are verified! Sending your file...")
        try:
            await context.bot.send_document(chat_id=user_id, document=FILE_URL)
        except Exception as e:
            await update.message.reply_text(f"❌ Error sending file: {e}")
    else:
        await update.message.reply_text(
            f"❌ You must join both:\n"
            f"📢 Channel: {CHANNEL_USERNAME}\n"
            f"👥 Group: {GROUP_USERNAME}\n\n"
            f"After joining, type *Done* to receive the file.",
            parse_mode="Markdown"
        )

# ===== Done Handler =====
async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# ===== Main Function =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("(?i)^done$"), done))

    # Run bot
    app.run_polling()

if __name__ == "__main__":
    main()
