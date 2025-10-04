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
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    logging.critical("FATAL: TOKEN environment variable not set. Please set it to your bot token.")

CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"
PORT = int(os.environ.get("PORT", 10000))

# IMPORTANT: Replace this with your actual group ID
ALLOWED_GROUP_ID = -1002810504524
WARNINGS_FILE = "warnings.json"
BEHAVIOR_FILE = "user_behavior.json" # Used for flood/initial message tracking

SYSTEM_BOT_IDS = [136817688, 1087968824]
USERNAME_REQUIRED = False

# === WEBHOOK CONFIGURATION (CRUCIAL FOR DEPLOYMENT) ===
WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
WEBHOOK_PATH = "/botupdates"
# =============================

# === URL Blocking Control ===
BLOCK_ALL_URLS = False
ALLOWED_DOMAINS = ["plus-ui.blogspot.com", "plus-ul.blogspot.com", "fineshopdesign.com"]

# === SPAM DETECTION CONFIG ===
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

# ================= Logging =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= Data Management (Synchronous for simplified files) =================
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
    """Centralized synchronous data load."""
    global warnings, user_behavior
    warnings.update(load_data(WARNINGS_FILE))
    user_behavior.update(load_data(BEHAVIOR_FILE))

def save_all_data():
    """Centralized synchronous data save."""
    save_data(warnings, WARNINGS_FILE)
    save_data(user_behavior, BEHAVIOR_FILE)

def add_warning(chat_id: int, user_id: int):
    """Adds a warning and saves data immediately."""
    chat_warns = warnings.setdefault(str(chat_id), {})
    user_warn = chat_warns.get(str(user_id), {"count": 0, "expiry": str(datetime.now()), "first_message": True})

    user_warn["count"] += 1
    user_warn["expiry"] = str(datetime.now() + timedelta(days=1))
    chat_warns[str(user_id)] = user_warn
    
    save_all_data() 
    return user_warn["count"], user_warn["expiry"]

def clean_expired_warnings():
    """Cleans up warnings. Assumes warnings data is current."""
    now = datetime.now()
    cleaned = False
    for chat_id in list(warnings.keys()):
        for user_id in list(warnings[chat_id].keys()):
            try:
                expiry = datetime.fromisoformat(warnings[chat_id][user_id]["expiry"])
                if expiry < now:
                    del warnings[chat_id][user_id]
                    cleaned = True
            except Exception:
                # Handle cases with malformed expiry data by deleting the entry
                warnings[chat_id].pop(user_id, None)
                cleaned = True
        if not warnings.get(chat_id):
            warnings.pop(chat_id, None)
            cleaned = True
    
    if cleaned:
        save_all_data()

# ================= Advanced Behavioral Analysis (FIXED DATA ACCESS) =================

def update_user_activity(user_id: int):
    """Updates user activity in the global user_behavior dict."""
    user_id_str = str(user_id)
    now = time.time()
    
    activity = user_behavior.setdefault(user_id_str, {"messages": [], "initial_count": 0})
    
    # Filter out old messages
    activity["messages"] = [t for t in activity["messages"] if now - t < FLOOD_INTERVAL]
    activity["messages"].append(now)
    
    if activity["initial_count"] < MAX_INITIAL_MESSAGES:
        activity["initial_count"] += 1

    # NOTE: Behavior saving is deferred to the end of message_handler for efficiency

def is_flood_spam(user_id: int) -> bool:
    """Checks flood status based on current global data."""
    user_id_str = str(user_id)
    activity = user_behavior.get(user_id_str, {"messages": []})
    return len(activity["messages"]) >= FLOOD_MESSAGE_COUNT

def is_first_message_critical(user_id: int) -> bool:
    """Checks first message status based on current global data."""
    user_id_str = str(user_id)
    activity = user_behavior.get(user_id_str, {"initial_count": 0})
    return activity["initial_count"] < MAX_INITIAL_MESSAGES


# ================= Spam Detection Functions =================
def rule_check(message_text: str, message_entities: list[MessageEntity] | None, user_id: int) -> (bool, str):
    
    is_critical_message = is_first_message_critical(user_id)
    normalized_text = unidecode(message_text)
    text_lower = normalized_text.lower()
    
    # Rule 1: Always block t.me links
    if "t.me/" in text_lower or "telegram.me/" in text_lower:
        return True, "Promotion is not allowed here!"

    # Rule 2: Block all other URLs if BLOCK_ALL_URLS is enabled (or for new users)
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

    # Rule 3: Excessive emojis
    if sum(c in SPAM_EMOJIS for c in message_text) > 5:
        return True, "sent excessive emojis"

    # Rule 4: Suspicious keywords
    if any(word in text_lower for word in SPAM_KEYWORDS):
        return True, "sent suspicious keywords (e.g., promo/join now)"

    # Rule 5: Excessive formatting (if entities are present)
    if message_entities:
        formatting_count = sum(1 for entity in message_entities if entity.type in FORMATTING_ENTITY_TYPES)
        if formatting_count >= MAX_FORMATTING_ENTITIES:
            return True, "used excessive formatting/bolding (a common spam tactic)"
    
    # Rule 6: Flood check
    if is_flood_spam(user_id):
        return True, "is flooding the chat (too many messages too quickly)"

    return False, None

def ml_check(message_text: str) -> bool:
    """Uses a trained ML model to detect tricky spam. Relies on global data."""
    if ML_MODEL and TFIDF_VECTORIZER:
        # Note: We rely on load_all_data() at the start of message_handler
        processed_text = TFIDF_VECTORIZER.transform([unidecode(message_text)])
        prediction = ML_MODEL.predict(processed_text)[0]
        return prediction == 1
    return False

def is_spam(message_text: str, message_entities: list[MessageEntity] | None, user_id: int) -> (bool, str):
    """Hybrid spam detection combining rules and ML."""
    if not message_text:
        return False, None

    # Layer 1: Rule-based check
    is_rule_spam, reason = rule_check(message_text, message_entities, user_id)
    if is_rule_spam:
        return True, reason

    # Layer 2: Machine learning check
    if ml_check(message_text):
        if is_first_message_critical(user_id):
              return True, "sent a spam message (ML/First Message Flag)"
        return True, "sent a spam message (ML Model)"
        
    return False, None

# ================= Flask App (for deployment) & Bot Setup =================
app = Flask(__name__)
bot = Bot(TOKEN)
application = ApplicationBuilder().bot(bot).build()


# ================= File Download/Join Check Logic =================

async def is_member_all(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Checks if a user is a member of all required channels."""
    for ch in CHANNELS:
        try:
            member = await context.bot.get_chat_member(ch, user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except TelegramError as e: 
            logger.error(f"Error checking {ch} for user {user_id}: {e}")
            return False
    return True

async def send_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback=False):
    """Sends the message prompting the user to join channels and download the file."""
    keyboard = [
        [
            InlineKeyboardButton("📢 Join Channel 1", url=f"https://t.me/{CHANNELS[0].strip('@')}"),
            InlineKeyboardButton("👥 Join Group", url=f"https://t.me/{CHANNELS[1].strip('@')}")
        ],
        [InlineKeyboardButton("✅ Done!!!", callback_data="done")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    caption = "💡 Join All Channels & Groups To Download the Latest Plus UI Blogger Template !!!\nAfter joining, press ✅ Done!!!"

    if is_callback and update.callback_query and update.callback_query.message:
        # Use reply_photo to send a NEW message in response to the callback
        await update.callback_query.message.reply_photo(
            photo=JOIN_IMAGE, 
            caption=caption, 
            reply_markup=reply_markup, 
            parse_mode=ParseMode.HTML
        )
    elif update.message:
        await update.message.reply_photo(
            photo=JOIN_IMAGE, 
            caption=caption, 
            reply_markup=reply_markup, 
            parse_mode=ParseMode.HTML
        )


# ================= Handlers =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the file-gating process."""
    if not update.message:
        return
        
    if update.effective_chat.type == ChatType.PRIVATE:
        await send_join_message(update, context)
    else:
        await update.message.reply_text("Welcome! I am an anti-spam bot. Use /help to see my commands.")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
        
    data = query.data
    
    # ================= 1. Done / Verified (File Gating) =================
    if data == "done":
        user_id = query.from_user.id
        if await is_member_all(context, user_id):
            
            # SUCCESS PATH: Answer the query without an alert
            await query.answer("Download initiated!", show_alert=False) 
            
            # Edit the button message to acknowledge success
            if query.message:
                await query.message.edit_caption(caption="✅ Verification successful! Initiating download...")
            
            # Send file document in private chat
            chat_id = user_id 
            await context.bot.send_sticker(chat_id=chat_id, sticker=STICKER_ID)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"👋 Hello {query.from_user.first_name}!\n✨ Your theme is now ready..."
            )
            await context.bot.send_document(chat_id=chat_id, document=FILE_PATH)
            
        else:
            # FAILURE PATH: Answer the query with the alert (This fixes the reported issue)
            await query.answer( 
                "⚠️ You must join all channels and groups to download the file.",
                show_alert=True
            )
            # Re-send the join message (after deleting the old one for cleanup)
            if query.message:
                 await query.message.delete()
            await send_join_message(update, context, is_callback=True)
            return
            
    # ================= 2. Cancel Warn / Unmute (Admin Actions) =================
    elif data.startswith("cancel_warn:") or data.startswith("unmute:"):
        action, chat_id_str, user_id_str = data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # --- Check Admin Permissions ---
        try:
            chat_admins = await context.bot.get_chat_administrators(chat_id)
            admin_ids = [admin.user.id for admin in chat_admins]
        except TelegramError:
            admin_ids = []

        if query.from_user.id not in admin_ids:
            await query.answer("You are not authorized to perform this action.", show_alert=True)
            return
        
        # --- Acknowledge and proceed ---
        await query.answer("Processing action...", show_alert=False)
            
        # --- Reset Warnings (applies to both cancel_warn and unmute) ---
        user_id_str = str(user_id)
        if chat_id_str in warnings and user_id_str in warnings[chat_id_str]:
             del warnings[chat_id_str][user_id_str]
             save_all_data()
             
        # --- Unmute User ---
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=True, can_send_media_messages=True,
                    can_send_polls=True, can_send_other_messages=True,
                    can_add_web_page_previews=True, can_change_info=False,
                    can_invite_users=True, can_pin_messages=False 
                )
            )
        except TelegramError as e:
            logger.error(f"Failed to unrestrict chat member {user_id}: {e}")
            pass
            
        user_to_act = await context.bot.get_chat_member(chat_id, user_id)
        user_display = f"<a href='tg://user?id={user_id}'>{html.escape(user_to_act.user.first_name)}</a>"
        current_time = datetime.now().strftime("%d/%m/%Y %H:%M")

        # --- Edit Message ---
        if query.message:
            if action == "unmute":
                await query.message.edit_text(
                    f"🔊 {user_display} has been unmuted and warnings cleared!\n"
                    f"• Action: Unmuted and Warns Reset (0/3)\n• Time: <code>{current_time}</code>",
                    parse_mode=ParseMode.HTML
                )
            elif action == "cancel_warn":
                 await query.message.edit_text(
                    f"❌ {user_display}'s warnings have been reset!\n"
                    f"• Action: Warns Reset (0/3)\n• Reset on: <code>{current_time}</code>",
                    parse_mode=ParseMode.HTML
                )


async def handle_status_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            if member.is_bot and member.id != context.bot.id: # Don't kick self
                try:
                    await context.bot.kick_chat_member(update.message.chat_id, member.id)
                except TelegramError as e:
                    logger.warning(f"Could not kick bot {member.id}: {e}")
            
    if update.message.left_chat_member or update.message.new_chat_members:
        try:
            # Delete join/leave messages
            await update.message.delete()
        except TelegramError:
            pass

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # CRITICAL: Load data once at the start
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
        
    # Update activity (modifies the global user_behavior dict)
    update_user_activity(user.id) 

    async def handle_spam(reason_text: str):
        try:
            await update.message.delete()
        except TelegramError as e:
            logger.error(f"Failed to delete message: {e}")
        
        # add_warning handles saving the warnings file
        warn_count, expiry = add_warning(chat.id, user.id)
        expiry_str = datetime.fromisoformat(expiry).strftime("%d/%m/%Y %H:%M")
        
        user_mention = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>"
        user_display = f"{user_mention} [<code>{user.id}</code>]"

        keyboard = None

        if warn_count <= 2:
            caption = (
                f"{user_display} {reason_text}.\n"
                f"Action: Warn ({warn_count}/3) ❕ until {expiry_str}."
            )
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{chat.id}:{user.id}")]]
        else:
            caption = (
                f"{user_display} has exceeded the warning limit.\n"
                f"Action: Muted ({warn_count}/3) 🔇 until {expiry_str}."
            )
            keyboard = [[InlineKeyboardButton("✅ Unmute", callback_data=f"unmute:{chat.id}:{user.id}")]]
            
            # Enforce Mute
            if chat.id == ALLOWED_GROUP_ID:
                until_date = datetime.now() + timedelta(days=1)
                try:
                    await context.bot.restrict_chat_member(
                        chat_id=chat.id,
                        user_id=user.id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=until_date
                    )
                except TelegramError as e:
                    logger.error(f"Failed to mute user {user.id}: {e}")
        
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
            
    # Check for entity-based spam (mentions/links)
    if entities:
        for entity in entities:
            if entity.type == MessageEntityType.MENTION:
                mentioned_username = text[entity.offset:entity.offset + entity.length]
                if mentioned_username:
                    try:
                        mentioned_chat = await context.bot.get_chat(mentioned_username)
                        if mentioned_chat.type in [ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP]:
                            await handle_spam("Promotion not allowed (Channel/Group Mention)!")
                            save_all_data() # Save behavior state on exit
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
                    save_all_data() # Save behavior state on exit
                    return

    # Check for rule-based or ML spam
    is_spam_message, reason = is_spam(text, entities, user.id)
    if is_spam_message:
        await handle_spam(reason)
        save_all_data() # Save behavior state on exit
        return

    # Check for username requirement
    if USERNAME_REQUIRED and not user.username:
        await handle_spam("in order to be accepted in the group, please set up a username")
        save_all_data() # Save behavior state on exit
        return
        
    # Successful Path: Save updated user behavior before exiting
    save_all_data()


# ================= Flask Routes for Webhooks and Health Checks =================

@app.route("/", methods=["GET"])
def home():
    return "Bot is alive ✅"

@app.route("/ping", methods=["GET"])
def ping():
    return "OK"

@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook(): 
    if request.json:
        if not application.running:
             logger.warning("Application not running, skipping update.")
             return "Application not running", 503
             
        try:
            update = Update.de_json(cast(dict, request.json), application.bot)
            # Use `put_nowait` for speed in the webhook listener
            application.update_queue.put_nowait(update) 
            
        except Exception as e:
            logger.error(f"Error handling incoming update payload: {e}", exc_info=True)
            
    return "OK"

# ================= Run Bot Server (Final Webhook Logic) =================

async def setup_bot_application():
    global ML_MODEL, TFIDF_VECTORIZER
    
    # Initial load of all data
    load_all_data() 

    try:
        # Load ML model files from disk (ensure these paths exist in deployment)
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
    if not WEBHOOK_URL:
        logger.error("FATAL: WEBHOOK_URL environment variable is not set. Cannot run in Webhook mode.")
        # If no webhook URL is set, the bot must be run in polling mode (not supported by this code's main function)
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
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    
    # Hypercorn serves the Flask app asynchronously
    await serve(app, config)
    
async def run_bot_server():
    """Main function to setup bot and start the web server."""
    await setup_bot_application()
    
    # Must run the setup webhook logic AFTER the application starts initializing
    if WEBHOOK_URL: 
        await setup_webhook()

    try:
        await serve_app()
    finally:
        # This runs when the web server is gracefully shut down
        await application.stop()
        save_all_data() # Final save on shutdown
        logger.info("Bot server shut down gracefully.")


def main():
    if not WEBHOOK_URL:
        logger.critical("Bot is configured for webhooks but WEBHOOK_URL is missing. It will not receive updates.")
        # Add guidance for running in polling mode if desired, or exit.

    try:
        # Required for Windows if running locally
        if os.name == 'nt':
             asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
             
        asyncio.run(run_bot_server())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully via interrupt.")
    except Exception as e:
        logger.error(f"Fatal error in main loop: {e}", exc_info=True)

if __name__ == '__main__':
    main()
