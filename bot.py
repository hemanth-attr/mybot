import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Get token from environment (Render → Environment Variables)
TOKEN = os.getenv("TOKEN")

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! I am alive 🚀")

def main():
    # Create the application
    app = Application.builder().token(TOKEN).build()

    # Add command handlers
    app.add_handler(CommandHandler("start", start))

    # Run the bot
    app.run_polling()

if __name__ == "__main__":
    main()
