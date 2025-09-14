import os
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Environment variables
TOKEN = os.getenv("TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")   # e.g. @Blogger_Templates_Updated
GROUP_USERNAME = os.getenv("GROUP_USERNAME")       # e.g. @Plus_UI_Official

# GitHub raw file URL
FILE_URL = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"

async def check_membership(app, chat_id, user_id):
    try:
        member = await app.bot.get_chat_member(chat_id, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = user.id

    in_channel = await check_membership(context, CHANNEL_USERNAME, chat_id)
    in_group = await check_membership(context, GROUP_USERNAME, chat_id)

    if in_channel and in_group:
        await update.message.reply_text("✅ You are verified! Sending your file...")
        try:
            await context.bot.send_document(chat_id=chat_id, document=FILE_URL)
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

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("(?i)^done$"), done))

    app.run_polling()

if __name__ == "__main__":
    main()
