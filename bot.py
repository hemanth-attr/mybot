import os
import re
import json
import asyncio
import logging
import html
import joblib
from datetime import datetime, timedelta
from flask import Flask
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions, Bot, MessageEntity
)
from telegram.constants import ParseMode, ChatType
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

# List of Telegram's internal bot IDs to ignore
SYSTEM_BOT_IDS = [136817688, 1087968824]

# Toggle the username requirement feature
USERNAME_REQUIRED = False

# === New Feature: URL Blocking Control ===
# Set to True to block all URLs not in ALLOWED_DOMAINS.
# Set to False to only block t.me links.
BLOCK_ALL_URLS = False

# List of domains to allow without a warning
ALLOWED_DOMAINS = ["plus-ui.blogspot.com", "plus-ul.blogspot.com", "fineshopdesign.com"]

# ================= Global Variables for ML Model =================
ML_MODEL = None
TFIDF_VECTORIZER = None
SPAM_KEYWORDS = {"free", "lottery", "click here", "subscribe", "win", "claim", "money", "deal"}
SPAM_EMOJIS = {"😀", "😂", "🔥", "💯", "😍", "❤️", "🥳", "🎉", "💰", "💵", "🤑"}

# ================= Logging =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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

# ================= Spam Detection Functions =================
def rule_check(message_text: str) -> (bool, str):
    """Checks for obvious spam patterns using simple rules."""
    text_lower = message_text.lower()

    # Rule 1: Always block t.me links
    if "t.me/" in text_lower:
        return True, "Promotion is not allowed here!"

    # Rule 2: Block all other URLs if BLOCK_ALL_URLS is enabled
    if BLOCK_ALL_URLS:
        url_finder = re.compile(r"((?:https?://|www\.|t\.me/)\S+)", re.I)
        found_urls = url_finder.findall(text_lower)
        for url in found_urls:
            try:
                parsed_url = urlparse(url)
                domain = parsed_url.netloc.lower().replace("www.", "")
                if not domain and parsed_url.path:
                    domain = parsed_url.path.strip('/').split('/')[0].lower()

                if domain not in [d.lower() for d in ALLOWED_DOMAINS]:
                    return True, "has sent a Link without authorization"
            except Exception:
                return True, "has sent a malformed URL"

    # Rule 3: Excessive emojis
    if sum(c in SPAM_EMOJIS for c in message_text) > 5:
        return True, "sent excessive emojis"

    # Rule 4: Suspicious keywords
    if any(word in text_lower for word in SPAM_KEYWORDS):
        return True, "sent suspicious keywords"

    # Rule 5: Unusually long messages
    #if len(message_text) > 500:
    #    return True, "sent an unusually long message"
    
    return False, None

def ml_check(message_text: str) -> bool:
    """Uses a trained ML model to detect tricky spam."""
    if ML_MODEL and TFIDF_VECTORIZER:
        processed_text = TFIDF_VECTORIZER.transform([message_text])
        prediction = ML_MODEL.predict(processed_text)[0]
        return prediction == 1
    return False

def is_spam(message_text: str) -> (bool, str):
    """Hybrid spam detection combining rules and ML."""
    if not message_text:
        return False, None

    # Layer 1: Rule-based check
    is_rule_spam, reason = rule_check(message_text)
    if is_rule_spam:
        return True, reason

    # Layer 2: Machine learning check
    if ml_check(message_text):
        return True, "sent a spam message"
        
    return False, None

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

# ================= Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_join_message(update, context)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # ================= Done / Verified =================
    if query.data == "done":
        user_id = query.from_user.id
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
        _, chat_id, target_user_id = query.data.split(":")
        chat_id = int(chat_id)
        target_user_id = int(target_user_id)
        
        # Check if the user is an admin
        try:
            chat_admins = await bot.get_chat_administrators(chat_id)
            admin_ids = [admin.user.id for admin in chat_admins]
        except TelegramError:
            admin_ids = []

        if query.from_user.id not in admin_ids:
            await query.answer(
                "⚠️ You don't have permission to do this operation\n💡 You Need to Be admin To do This operation",
                show_alert=True
            )
            return

        # Check the user's current warn count before resetting
        chat_id_str = str(chat_id)
        target_user_id_str = str(target_user_id)
        
        warns_cleared = 0
        if chat_id_str in warnings and target_user_id_str in warnings[chat_id_str]:
            warns_cleared = warnings[chat_id_str][target_user_id_str]["count"]

        # Reset the user's warnings in the database
        if chat_id_str in warnings and target_user_id_str in warnings[chat_id_str]:
            del warnings[chat_id_str][target_user_id_str]
            save_warnings()

        # Acknowledge the button press with a dynamic message
        if warns_cleared > 0:
            await query.answer(f"Warnings ({warns_cleared}/3) reset successfully.")
        else:
            await query.answer("Warnings already reset.")

        # Get user info and format the confirmation message
        target_user = await context.bot.get_chat_member(chat_id, target_user_id)
        user_display = f"@{target_user.user.username}" if target_user.user.username else f"{target_user.user.first_name}"
        current_time = datetime.now().strftime("%d/%m/%Y %H:%M")
        
        confirmation_text = (
            f"✅ <b>{user_display}'s</b> warnings have been reset!\n"
            f"• Action: Warnings reset ({warns_cleared}/3)\n"
            f"• Reset on: <code>{current_time}</code>"
        )

        # Use a try-except block to handle cases where the message is already deleted
        try:
            # Try to edit the original message to show the update
            await query.message.edit_text(
                confirmation_text,
                reply_markup=None, # Remove the button from the confirmation message
                parse_mode=ParseMode.HTML
            )
        except TelegramError:
            # If editing fails, send a new confirmation message instead
            await context.bot.send_message(
                chat_id=chat_id,
                text=confirmation_text,
                parse_mode=ParseMode.HTML
            )

    # ================= Unmute =================
    elif query.data.startswith("unmute"):
        _, chat_id, target_user_id = query.data.split(":")
        chat_id = int(chat_id)
        target_user_id = int(target_user_id)

        try:
            chat_admins = await bot.get_chat_administrators(chat_id)
            admin_ids = [admin.user.id for admin in chat_admins]
        except TelegramError:
            admin_ids = []

        if query.from_user.id not in admin_ids:
            await query.answer(
                "⚠️ You don't have permission to do this operation\n💡 You Need to Be admin To do This operation",
                show_alert=True
            )
            return

        # Acknowledge the button press
        await query.answer("User unmuted successfully.")

        # Correctly un-restrict the user's permissions
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_user_id,
                permissions=ChatPermissions(
                    can_send_messages=True, can_send_media_messages=True,
                    can_send_polls=True, can_send_other_messages=True,
                    can_add_web_page_previews=True, can_change_info=True,
                    can_invite_users=True, can_pin_messages=True
                )
            )
        except TelegramError:
            pass

        # === FIX: Clear warnings on unmute ===
        chat_id_str = str(chat_id)
        target_user_id_str = str(target_user_id)
        if chat_id_str in warnings and target_user_id_str in warnings[chat_id_str]:
            del warnings[chat_id_str][target_user_id_str]
            save_warnings()
            
        target_user = await context.bot.get_chat_member(chat_id, target_user_id)
        user_display = f"@{target_user.user.username}" if target_user.user.username else f"{target_user.user.first_name}"
        
        current_time = datetime.now().strftime("%d/%m/%Y %H:%M")
        confirmation_text = (
            f"🔊 <b>{user_display}</b> has been unmuted!\n"
            f"• Action: Unmuted\n"
            f"• Time: <code>{current_time}</code>"
        )

        # Use a try-except block to handle cases where the message is already deleted
        try:
            await query.message.edit_text(
                confirmation_text,
                reply_markup=None,
                parse_mode=ParseMode.HTML
            )
        except TelegramError:
            await context.bot.send_message(
                chat_id=chat_id,
                text=confirmation_text,
                parse_mode=ParseMode.HTML
            )

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
    caption = "💡 Join All Channels & Groups To Download the Latest Plus UI Blogger Template !!!\nAfter joining, press ✅ Done!!!"

    if query:
        await update.callback_query.message.reply_photo(photo=JOIN_IMAGE, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_photo(photo=JOIN_IMAGE, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clean_expired_warnings()
    if not update.message or not update.message.text:
        return

    user = update.message.from_user
    chat = update.effective_chat
    text = update.message.text or ""

    # Skip System bots
    if user.id in SYSTEM_BOT_IDS:
        return
        
    # Skip admins
    try:
        chat_admins = await chat.get_administrators()
        admin_ids = [admin.user.id for admin in chat_admins]
    except TelegramError:
        admin_ids = []

    if user.id in admin_ids:
        return

    # Unified function to handle and respond to spam/promotion
    async def handle_spam(reason_text: str):
        try:
            await update.message.delete()
        except TelegramError:
            pass
        
        warn_count, expiry = add_warning(chat.id, user.id)
        expiry_str = datetime.fromisoformat(expiry).strftime("%d/%m/%Y %H:%M")
        
        if user.username:
            user_display = f"@{user.username}"
        else:
            clickable_name = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>"
            user_display = clickable_name

        caption = ""
        keyboard = None

        if warn_count <= 2:
            caption = (
                f"{user_display} [<code>{user.id}</code>] {reason_text}.\n"
                f"Action: Warn ({warn_count}/3) ❕ until {expiry_str}."
            )
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{chat.id}:{user.id}")]]
        else:
            caption = (
                f"{user_display} [<code>{user.id}</code>] has exceeded the warning limit.\n"
                f"Action: Muted ({warn_count}/3) 🔇 until {expiry_str}."
            )
            keyboard = [[InlineKeyboardButton("✅ Unmute", callback_data=f"unmute:{chat.id}:{user.id}")]]
            if chat.id == ALLOWED_GROUP_ID:
                until_date = datetime.now() + timedelta(days=1)
                await context.bot.restrict_chat_member(
                    chat_id=chat.id,
                    user_id=user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(
            chat_id=chat.id,
            text=caption,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )

    # 1. Check for mentions of channels/groups/supergroups
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == MessageEntity.MENTION:
                mentioned_username = text[entity.offset:entity.offset + entity.length]
                if mentioned_username:
                    try:
                        mentioned_chat = await context.bot.get_chat(mentioned_username)
                        if mentioned_chat.type in [ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP]:
                            await handle_spam("Promotion not allowed!")
                            return
                    except TelegramError:
                        pass

    # 2. Check for hybrid spam (rules + ML)
    is_spam_message, reason = is_spam(text)
    if is_spam_message:
        await handle_spam(reason)
        return

    # 3. Check for users without a username (old rule)
    if USERNAME_REQUIRED and not user.username:
        await handle_spam("in order to be accepted in the group, please set up a username")
        return


# ================= Bot Status Updates Handler =================
async def handle_status_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            if member.is_bot:
                try:
                    await context.bot.kick_chat_member(update.message.chat_id, member.id)
                    await update.message.delete()
                    logger.info(f"Kicked bot {member.id} from group {update.message.chat_id}.")
                except TelegramError as e:
                    logger.warning(f"Could not kick bot {member.id}: {e}")
            else:
                try:
                    await update.message.delete()
                except TelegramError as e:
                    logger.warning(f"Could not delete 'user joined' message: {e}")
    
    elif update.message.left_chat_member:
        try:
            await update.message.delete()
        except TelegramError as e:
            logger.warning(f"Could not delete 'user left' message: {e}")

# ================= Run Bot =================
async def run_bot():
    global ML_MODEL, TFIDF_VECTORIZER
    
    # Load the pre-trained ML model and vectorizer from disk
    try:
        TFIDF_VECTORIZER = joblib.load('vectorizer.joblib')
        ML_MODEL = joblib.load('model.joblib')
        logger.info("ML model loaded successfully from disk.")
    except Exception as e:
        logger.error(f"Failed to load ML model: {e}")
        logger.warning("Bot will operate in rule-based mode only.")
        ML_MODEL = None
        TFIDF_VECTORIZER = None

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button, pattern="^(done|cancel_warn:.*|unmute:.*)$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER,
        handle_status_updates
    ))

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
