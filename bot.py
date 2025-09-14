import os
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# Environment variables
TOKEN = os.getenv("TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")   # e.g. @YourChannel
GROUP_USERNAME = os.getenv("GROUP_USERNAME")       # e.g. @YourGroup

# GitHub raw URL of the file
FILE_URL = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"

def check_membership(bot, chat_id, user_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

def start(update: Update, context: CallbackContext):
    user = update.message.from_user
    chat_id = user.id

    in_channel = check_membership(context.bot, CHANNEL_USERNAME, chat_id)
    in_group = check_membership(context.bot, GROUP_USERNAME, chat_id)

    if in_channel and in_group:
        update.message.reply_text("✅ You are verified! Sending your file...")
        try:
            context.bot.send_document(chat_id=chat_id, document=FILE_URL)
        except Exception as e:
            update.message.reply_text(f"❌ Error sending file: {e}")
    else:
        update.message.reply_text(
            f"❌ You must join both:\n"
            f"📢 Channel: {CHANNEL_USERNAME}\n"
            f"👥 Group: {GROUP_USERNAME}\n\n"
            f"After joining, type *Done* to receive the file.",
            parse_mode="Markdown"
        )

def done(update: Update, context: CallbackContext):
    start(update, context)

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.regex("(?i)^done$"), done))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
