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
from urllib.parse import urlparse

# ================= Configuration =================
TOKEN = os.getenv("TOKEN")
CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"
PORT = int(os.environ.get("PORT", 10000))
ALLOWED_GROUP_ID = -1002810504524
WARNINGS_FILE = "warnings.json"

# List of domains to allow without a warning
ALLOWED_DOMAINS = ["plus-ui.blogspot.com", "plus-ul.blogspot.com", "fineshopdesign.com"]

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
    await send_join_message(update, context)

# ================= Button Handler =================
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    # ================= Done / Verified =================
    if query.data == "done":
        if await is_member_all(context, user_id):
            await query.answer("Download initiated!", show_alert=False)
            await query.delete_message()
            await context.bot.send_sticker(chat_id=user_id, sticker=STICKER_ID)
            await context.bot.send_message(
                chat_id=user_id,
                text=f"👋 Hello {query.from_user.first_name}!\n✨ Your theme is now ready..."
            )
            await context.bot.send_document(chat_id=user_id, document=FILE_PATH)
        else:
            await query.answer(
                "⚠️ You must join all channels and groups to download the file.",
                show_alert=True
            )
            await query.delete_message()
            await send_join_message(update, context, query=True)

    # ================= Cancel Warn =================
    elif query.data.startswith("cancel_warn"):
        _, chat_id, user_id = query.data.split(":")
        chat_id = int(chat_id)
        user_id = int(user_id)

        chat_admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [admin.user.id for admin in chat_admins]
        if query.from_user.id not in admin_ids:
            await query.answer(
                "⚠️ You don't have permission to do this operation\n💡 You Need to Be admin To do This operation",
                show_alert=True
            )
            return

        await query.answer("Warnings reset successfully.")

        chat_id_str = str(chat_id)
        user_id_str = str(user_id)
        if chat_id_str in warnings and user_id_str in warnings[chat_id_str]:
            del warnings[chat_id_str][user_id_str]
            save_warnings()

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

        chat_admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [admin.user.id for admin in chat_admins]
        if query.from_user.id not in admin_ids:
            await query.answer(
                "⚠️ You don't have permission to do this operation\n💡 You Need to Be admin To do This operation",
                show_alert=True
            )
            return

        await query.answer("User unmuted successfully.")

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
            f"🔊 @{query.from_user.username} has been unmuted!\n"
            f"• Action: Unmuted\n"
            f"• Time: {current_time}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{chat_id}:{user_id}")]]
            )
        )

# ================= Start Func =================
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
    caption = "💡 Join All Channels & Groups To Download the Latest Plus UI Blogger Template !!!\nAfter joining, press ✅ Done!!!"

    if query:
        await update.callback_query.message.reply_photo(photo=JOIN_IMAGE, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_photo(photo=JOIN_IMAGE, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
# ================= Message Handler =================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clean_expired_warnings()
    if not update.message:
        return

    user = update.message.from_user
    chat = update.effective_chat
    text = update.message.text or ""

    # Check if the message is a forward or contains a URL
    if update.message.forward_from:
        is_url_spam = True
    else:
        is_url_spam = False
        # Regex to find all potential URLs
        url_finder = re.compile(r"((?:https?://|www\.|t\.me/)\S+)", re.I)
        found_urls = url_finder.findall(text)

        for url in found_urls:
            try:
                # Remove protocol/www. and convert to lowercase for consistent checking
                parsed_url = urlparse(url)
                domain = parsed_url.netloc.lower().replace("www.", "")
                # If no domain is parsed, it might be a t.me link.
                if not domain:
                    domain = parsed_url.path.split('/')[0].lower() if parsed_url.path else ''

                # Check if the domain is NOT in the allowed list
                if domain not in [d.lower() for d in ALLOWED_DOMAINS]:
                    is_url_spam = True
                    break
            except Exception:
                is_url_spam = True # Assume it's spam if parsing fails
                break

    # If it's a forward or a non-allowed URL, apply the warning logic
    if is_url_spam:
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
