import os
import re
import json
import asyncio
import logging
import html
import joblib
import time
from datetime import datetime, timedelta
from flask import Flask, request
from unidecode import unidecode
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions, Bot, MessageEntity
)
from telegram.constants import ParseMode, ChatType, MessageEntityType
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, Application
)
from telegram.error import TelegramError
from urllib.parse import urlparse
from typing import cast
from hypercorn.asyncio import serve
from hypercorn.config import Config

# ================= Configuration =================
# Set your token here if you don't use environment variables, but ENV is recommended
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    print("FATAL: TOKEN environment variable not set. Please set it to your bot token.")

CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"
PORT = int(os.environ.get("PORT", 10000))
# IMPORTANT: Replace this with your actual group ID (e.g., -1001234567890)
ALLOWED_GROUP_ID = -1002810504524
WARNINGS_FILE = "warnings.json"
BEHAVIOR_FILE = "user_behavior.json"

SYSTEM_BOT_IDS = [136817688, 1087968824]
USERNAME_REQUIRED = False

# === WEBHOOK CONFIGURATION ===
WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
WEBHOOK_PATH = "/botupdates"
# =============================

# === URL Blocking Control ===
BLOCK_ALL_URLS = False
ALLOWED_DOMAINS = ["plus-ui.blogspot.com", "plus-ul.blogspot.com", "fineshopdesign.com"]

# === ENHANCED SPAM DETECTION CONFIG ===
SPAM_KEYWORDS = {
    "lottery", "deal", "coupon", "promo", "discount", "referral", "link in bio",
    "join now", "limited time", "don't miss", "hurry", "crypto",
    "investment", "paid group", "buy now"
}
SPAM_EMOJIS = {"😀", "😂", "🔥", "💯", "😍", "❤️", "🥳", "🎉", "💰", "💵", "🤑", "🤩"}
FORMATTING_ENTITY_TYPES = {
    MessageEntityType.BOLD, MessageEntityType.ITALIC, MessageEntityType.CODE,
    MessageEntityType.UNDERLINE, MessageEntityType.STRIKETHROUGH, MessageEntityType.SPOILER,
    MessageEntityType.PRE, MessageEntityType.BLOCKQUOTE
}
MAX_FORMATTING_ENTITIES = 5
MAX_INITIAL_MESSAGES = 3
FLOOD_INTERVAL = 5
FLOOD_MESSAGE_COUNT = 3

# ================= Global Variables for ML Model/User Data =================
ML_MODEL = None
TFIDF_VECTORIZER = None
warnings = {}
user_behavior = {}

# ================= Logging (Improved Format) =================
logging.basicConfig(
    # Added funcName for better log troubleshooting
    format="%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= Data Management =================
def load_data(file_path):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error(f"Error decoding JSON from {file_path}. Starting with empty data.")
            return {}
    return {}

def save_data(data, file_path):
    with open(file_path, "w") as f:
        json.dump(data, f, default=str, indent=4)

def load_all_data():
    global warnings, user_behavior
    warnings = load_data(WARNINGS_FILE)
    user_behavior = load_data(BEHAVIOR_FILE)

def save_all_data():
    save_data(warnings, WARNINGS_FILE)
    save_data(user_behavior, BEHAVIOR_FILE)

def add_warning(chat_id: int, user_id: int):
    chat_warns = warnings.setdefault(str(chat_id), {})
    user_warn = chat_warns.get(str(user_id), {"count": 0, "expiry": str(datetime.now()), "first_message": True})

    user_warn["count"] += 1
    user_warn["expiry"] = str(datetime.now() + timedelta(days=1))
    chat_warns[str(user_id)] = user_warn
    save_all_data()
    return user_warn["count"], user_warn["expiry"]

def clean_expired_warnings():
    now = datetime.now()
    for chat_id in list(warnings.keys()):
        for user_id in list(warnings[chat_id].keys()):
            try:
                expiry = datetime.fromisoformat(warnings[chat_id][user_id]["expiry"])
                if expiry < now:
                    del warnings[chat_id][user_id]
            except Exception:
                del warnings[chat_id][user_id]
        if not warnings[chat_id]:
            del warnings[chat_id]
    save_all_data()

# ================= Advanced Behavioral Analysis =================

def update_user_activity(user_id: int):
    user_id_str = str(user_id)
    now = time.time()
    
    activity = user_behavior.setdefault(user_id_str, {"messages": [], "initial_count": 0})
    
    activity["messages"] = [t for t in activity["messages"] if now - t < FLOOD_INTERVAL]
    activity["messages"].append(now)
    
    if activity["initial_count"] < MAX_INITIAL_MESSAGES:
        activity["initial_count"] += 1

    save_data(user_behavior, BEHAVIOR_FILE)
    return activity

def is_flood_spam(user_id: int) -> bool:
    user_id_str = str(user_id)
    activity = user_behavior.get(user_id_str, {"messages": []})
    
    return len(activity["messages"]) >= FLOOD_MESSAGE_COUNT

def is_first_message_critical(user_id: int) -> bool:
    user_id_str = str(user_id)
    activity = user_behavior.get(user_id_str, {"initial_count": 0})
    return activity["initial_count"] < MAX_INITIAL_MESSAGES


# ================= Spam Detection Functions =================
def rule_check(message_text: str, message_entities: list[MessageEntity] | None, user_id: int) -> (bool, str):
    
    is_critical_message = is_first_message_critical(user_id)
    
    normalized_text = unidecode(message_text)
    text_lower = normalized_text.lower()
    
    if "t.me/" in text_lower or "telegram.me/" in text_lower:
        return True, "Promotion is not allowed here!"

    if BLOCK_ALL_URLS or is_critical_message:
        url_finder = re.compile(r"((?:https?://|www\.|t\.me/)\S+)", re.I)
        found_urls = url_finder.findall(text_lower)
        
        allowed_domains_lower = [d.lower() for d in ALLOWED_DOMAINS]

        for url in found_urls:
            if "t.me/" in url.lower() or "telegram.me/" in url.lower():
                continue

            if not url.startswith(('http://', 'https://')):
                temp_url = 'http://' + url
            else:
                temp_url = url

            try:
                parsed_url = urlparse(temp_url)
                domain = parsed_url.netloc.split(':')[0].lower().replace("www.", "")
                
                if not domain and parsed_url.path:
                    domain = parsed_url.path.strip('/').split('/')[0].lower()

                if domain and domain not in allowed_domains_lower:
                    return True, "has sent a Link without authorization"
                    
            except Exception:
                return True, "has sent a malformed URL"

    if sum(c in SPAM_EMOJIS for c in message_text) > 5:
        return True, "sent excessive emojis"

    if any(word in text_lower for word in SPAM_KEYWORDS):
        return True, "sent suspicious keywords (e.g., promo/join now)"

    if message_entities:
        formatting_count = sum(1 for entity in message_entities if entity.type in FORMATTING_ENTITY_TYPES)
        text_length_limit = 200 if not is_critical_message else 100
        
        if formatting_count >= MAX_FORMATTING_ENTITIES and len(message_text) < text_length_limit:
            return True, "used excessive formatting/bolding (a common spam tactic)"
    
    if len(re.findall(r'[A-Z]{3,}', message_text)) > 3 or len(re.findall(r'[!?]{3,}', message_text)) > 1:
        return True, "used excessive capitalization or punctuation"
    
    if is_flood_spam(user_id):
        return True, "is flooding the chat (too many messages too quickly)"

    return False, None

def ml_check(message_text: str) -> bool:
    if ML_MODEL and TFIDF_VECTORIZER:
        processed_text = TFIDF_VECTORIZER.transform([unidecode(message_text)])
        prediction = ML_MODEL.predict(processed_text)[0]
        return prediction == 1
    return False

def is_spam(message_text: str, message_entities: list[MessageEntity] | None, user_id: int) -> (bool, str):
    if not message_text:
        return False, None

    is_rule_spam, reason = rule_check(message_text, message_entities, user_id)
    if is_rule_spam:
        return True, reason

    if ml_check(message_text):
        if is_first_message_critical(user_id):
             return True, "sent a spam message (ML/First Message Flag)"
        return True, "sent a spam message (ML Model)"
        
    return False, None

# ================= Flask App (for deployment) & Bot Setup =================
app = Flask(__name__)
bot = Bot(TOKEN)
application = ApplicationBuilder().bot(bot).build()

# ================= Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! I am an anti-spam bot. Use /help to see my commands."
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split(':')
    action = data[0]
    chat_id = int(data[1])
    user_id = int(data[2])

    if action == "cancel_warn" or action == "unmute":
        try:
            chat_admins = await context.bot.get_chat_administrators(chat_id)
            admin_ids = [admin.user.id for admin in chat_admins]
        except TelegramError:
            admin_ids = []

        if query.from_user.id not in admin_ids:
            await query.edit_message_text("You are not authorized to perform this action.")
            return

        user = await context.bot.get_chat_member(chat_id, user_id)

        if action == "unmute":
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_change_info=False, can_invite_users=True, can_pin_messages=False)
            )
            warnings[str(chat_id)].pop(str(user_id), None)
            save_all_data()
            await query.edit_message_text(f"✅ {user.user.first_name} has been unmuted and warnings cleared.")

        elif action == "cancel_warn":
            warnings[str(chat_id)].pop(str(user_id), None)
            save_all_data()
            await query.edit_message_text(f"❌ Warning for {user.user.first_name} has been cancelled.")


async def handle_status_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    load_all_data()
    clean_expired_warnings()
    
    if not update.message or (not update.message.text and not update.message.caption):
        return

    user = update.message.from_user
    chat = update.effective_chat
    text = update.message.text or update.message.caption or ""
    entities = update.message.entities or update.message.caption_entities

    if user.id in SYSTEM_BOT_IDS:
        return
        
    try:
        chat_admins = await chat.get_administrators()
        admin_ids = [admin.user.id for admin in chat_admins]
    except TelegramError:
        admin_ids = []

    if user.id in admin_ids:
        return
        
    update_user_activity(user.id) 

    async def handle_spam(reason_text: str):
        try:
            await update.message.delete()
        except TelegramError as e:
            logger.error(f"Failed to delete message: {e}")
        
        warn_count, expiry = add_warning(chat.id, user.id)
        expiry_str = datetime.fromisoformat(expiry).strftime("%d/%m/%Y %H:%M")
        
        user_mention = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>"
        user_display = f"{user_mention} (@{user.username})" if user.username else user_mention

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
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=caption,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        except TelegramError as e:
            logger.error(f"Failed to send warning message: {e}")


    if entities:
        for entity in entities:
            if entity.type == MessageEntityType.MENTION:
                mentioned_username = text[entity.offset:entity.offset + entity.length]
                if mentioned_username:
                    try:
                        mentioned_chat = await context.bot.get_chat(mentioned_username)
                        if mentioned_chat.type in [ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP]:
                            await handle_spam("Promotion not allowed (Channel/Group Mention)!")
                            return
                    except TelegramError:
                        pass
            
            is_link_entity = entity.type in [MessageEntityType.URL, MessageEntityType.TEXT_LINK]

            if is_link_entity:
                link_url = ""
                if entity.type == MessageEntityType.URL:
                    link_url = text[entity.offset:entity.offset + entity.length].lower()
                elif entity.type == MessageEntityType.TEXT_LINK:
                    link_url = entity.url.lower()

                if "t.me/" in link_url or "telegram.me/" in link_url:
                    await handle_spam("Promotion not allowed (Telegram Link Entity)!")
                    return

    is_spam_message, reason = is_spam(text, entities, user.id)
    if is_spam_message:
        await handle_spam(reason)
        return

    if USERNAME_REQUIRED and not user.username:
        await handle_spam("in order to be accepted in the group, please set up a username")
        return

# ================= Flask Routes for Webhooks and Health Checks =================

@app.route("/", methods=["GET"])
def home():
    """Health check route."""
    return "Bot is alive ✅"

@app.route("/ping", methods=["GET"])
def ping():
    """Health check route."""
    return "OK"

@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook(): # CHANGED: This is now a synchronous function
    """Route to receive updates from Telegram."""
    if request.json:
        # We must ensure the app is running before processing an update
        if not application.running:
             logger.warning("Application not running, skipping update.")
             return "Application not running", 503
             
        # --- CRUCIAL FIX: Using update_queue.put_nowait for synchronous handing of the update payload ---
        try:
            # Create the Update object from the incoming JSON
            update = Update.de_json(cast(dict, request.json), application.bot)
            
            # Put the update into the application's queue to be processed asynchronously by the bot's loop
            application.update_queue.put_nowait(update)
            
        except Exception as e:
            # Log error with full traceback for diagnostics, but return OK to Telegram.
            # This ensures Telegram's webhook system doesn't get disabled.
            logger.error(f"Error handling incoming update payload: {e}", exc_info=True)
        # ---------------------------------------------
            
    return "OK" # Always return 200 OK to Telegram instantly

# ================= Run Bot Server (Final Webhook Logic) =================

async def setup_bot_application():
    """Load model, register handlers, and initialize the application."""
    global ML_MODEL, TFIDF_VECTORIZER
    
    load_all_data()

    try:
        TFIDF_VECTORIZER = joblib.load('models/vectorizer.joblib') 
        ML_MODEL = joblib.load('models/model.joblib')             
        logger.info("ML model loaded successfully from disk.")
    except Exception as e:
        logger.warning(f"Failed to load ML model files: {e}")
        logger.warning("Bot will operate in rule-based mode only.")
        ML_MODEL = None
        TFIDF_VECTORIZER = None
        
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button, pattern="^(done|cancel_warn:.*|unmute:.*)$"))
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, message_handler))
    application.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER,
        handle_status_updates
    ))
    
    await application.initialize()
    await application.start()

async def setup_webhook():
    """Sets the webhook on Telegram."""
    if not WEBHOOK_URL:
        logger.error("FATAL: WEBHOOK_URL environment variable is not set. Cannot run in Webhook mode.")
        return False
        
    full_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    
    logger.info(f"Setting webhook to {full_url} on port {PORT}")
    try:
        await application.bot.set_webhook(url=full_url)
        logger.info("Webhook set successfully.")
        return True
    except TelegramError as e:
        logger.error(f"Failed to set webhook: {e}")
        return False

async def serve_app():
    """Starts the Hypercorn server to serve the Flask app and webhooks."""
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    
    await serve(app, config)
    
async def run_bot_server():
    """The main entry point for the bot in Webhook mode."""
    await setup_bot_application()
    
    # We set the webhook AFTER the application is started
    await setup_webhook()

    try:
        # This will block and run the web server
        await serve_app()
    finally:
        await application.stop()
        save_all_data()
        logger.info("Bot server shut down gracefully.")


def main():
    try:
        if os.name == 'nt':
             asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
             
        asyncio.run(run_bot_server())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully via interrupt.")
    except Exception as e:
        # Catch any exceptions that might crash the entire server
        logger.error(f"Fatal error in main loop: {e}", exc_info=True)

if __name__ == '__main__':
    main()
