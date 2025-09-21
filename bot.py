import os
import re
import json
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
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.error import TelegramError

# ================= Configuration =================
TOKEN = os.getenv("TOKEN")
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"
PORT = int(os.environ.get("PORT", 10000))
ALLOWED_GROUP_ID = -1002810504524
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

# ================= Warnings =================
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

# ================= Safe Send =================
async def safe_send_message(chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
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

async def safe_delete(callback_query):
    try:
        await callback_query.delete_message()
    except TelegramError as e:
        logger.warning(f"Delete message failed: {e}")

# ================= Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_message(update.effective_chat.id, "👋 Welcome! Please follow the instructions.")

# ================= Button Handler =================
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # ================= Done / Verified =================
    if query.data == "done":
        user_id = query.from_user.id
        await safe_send_message(user_id, "✨ Verified! Your theme is ready.")
        await safe_send_document(user_id, FILE_PATH)

    # ================= Cancel Warn =================
    elif query.data.startswith("cancel_warn"):
        _, chat_id, user_id = query.data.split(":")
        chat_id = int(chat_id)
        user_id = int(user_id)

        # Admin check
        chat_admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [admin.user.id for admin in chat_admins]
        if query.from_user.id not in admin_ids:
            await query.answer(
                "Blogger Templates\n⚠️ You don't have permission to do this operation\n💡 You Need to Be admin To do This operation",
                show_alert=True
            )
            return

        # Reset warnings
        chat_id_str = str(chat_id)
        user_id_str = str(user_id)
        if chat_id_str in warnings and user_id_str in warnings[chat_id_str]:
            del warnings[chat_id_str][user_id_str]
            save_warnings()

        # Remove mute if applied
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_change_info=True,
                    can_invite_users=True,
                    can_pin_messages=True
                )
            )
        except TelegramError:
            pass

        current_time = datetime.now().strftime("%d/%m/%Y %H:%M")
        await query.message.edit_text(
            f"✅ @{query.from_user.username}'s warnings have been reset!\n"
            f"• Action: Warns (0/3)\n"
            f"• Reset on: {current_time}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{chat_id}:{user_id}")]]
            )
        )

    # ================= Unmute =================
    elif query.data.startswith("unmute"):
        _, chat_id, user_id = query.data.split(":")
        chat_id = int(chat_id)
        user_id = int(user_id)

        # Admin check
        chat_admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [admin.user.id for admin in chat_admins]
        if query.from_user.id not in admin_ids:
            await query.answer(
                "⚠️ You don't have permission to do this operation\n💡 You Need to Be admin To do This operation",
                show_alert=True
            )
            return

        # Remove mute
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=True,
                can_invite_users=True,
                can_pin_messages=True
            )
        )

        current_time = datetime.now().strftime("%d/%m/%Y %H:%M")
        await query.message.edit_text(
            f"🔊 @{query.from_user.username} has been unmuted!\n"
            f"• Action: Unmuted\n"
            f"• Time: {current_time}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{chat_id}:{user_id}")]]
            )
        )

# ================= Message Handler =================
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

    # Detect links or forwards
    if update.message.forward_from or re.search(r"(https?://\S+|www\.\S+|t\.me/\S+)", text, re.I):
        try:
            await update.message.delete()
        except Exception:
            pass

        warn_count, expiry = add_warning(chat.id, user.id)
        expiry_str = datetime.fromisoformat(expiry).strftime("%d/%m/%Y %H:%M")

        # Format messages exactly like your requested template
        if warn_count == 1:
            caption = (
                f"@{user.username if user.username else user.first_name} [{user.id}] sent a spam message.\n"
                f"Action: Warn (1/3) ❕ until {expiry_str}."
            )
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{chat.id}:{user.id}")]]

        elif warn_count == 2:
            caption = (
                f"@{user.username if user.username else user.first_name} [{user.id}] sent a spam message.\n"
                f"Action: Warn (2/3) ❗ until {expiry_str}."
            )
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{chat.id}:{user.id}")]]

        else:  # Final warn (3/3)
            caption = (
                f"@{user.username if user.username else user.first_name} [{user.id}] sent a spam message.\n"
                f"• Warns now: (3/3) ❕ until {expiry_str}.\n"
                f"• Action: Muted 🔇"
            )
            keyboard = [[InlineKeyboardButton("✅ Unmute", callback_data=f"unmute:{chat.id}:{user.id}")]]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await bot.send_message(
            chat_id=chat.id,
            text=caption,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )

        # Auto-mute if 3 warnings
        if warn_count >= 3 and chat.id == ALLOWED_GROUP_ID:
            until_date = datetime.now() + timedelta(days=1)
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )

# ================= Run Bot =================
async def run_bot():
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button, pattern="^(done|cancel_warn:.*|unmute:.*)$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await asyncio.Event().wait()

async def main():
    bot_task = asyncio.create_task(run_bot())

    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    await serve(app, config)

    bot_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())
