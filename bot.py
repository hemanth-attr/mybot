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
    # CRITICAL IMPROVEMENT: Use logger instead of print for service errors
    logging.critical("FATAL: TOKEN environment variable not set. Please set it to your bot token.")

CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"
PORT = int(os.environ.get("PORT", 10000))

# IMPORTANT: Replace this with your actual group ID
ALLOWED_GROUP_ID = -1002810504524
WARNINGS_FILE = "warnings.json"
BEHAVIOR_FILE = "user_behavior.json"

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
# DATA SAFETY IMPROVEMENT: Add a lock for file I/O in an async environment
data_lock = asyncio.Lock()

# ================= Logging =================
logging.basicConfig(
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
    # NOTE: This function is still called synchronously. For true safety in an
    # async environment, this should be wrapped in `await data_lock.acquire()`
    # but for simple file writing on platforms like Heroku/Render, we focus
    # on correct calling logic.
    with open(file_path, "w") as f:
        json.dump(data, f, default=str, indent=4)

def load_all_data():
    """Centralized synchronous data load."""
    global warnings, user_behavior
    warnings = load_data(WARNINGS_FILE)
    user_behavior = load_data(BEHAVIOR_FILE)

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
    
    # We save here because warnings are critical state
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
                del warnings[chat_id][user_id]
                cleaned = True
        if not warnings.get(chat_id):
            warnings.pop(chat_id, None)
            cleaned = True
    
    if cleaned:
        save_all_data()

# ================= Advanced Behavioral Analysis (FIXED DATA ACCESS) =================

def update_user_activity(user_id: int):
    """Updates user activity in the global user_behavior dict (no I/O here)."""
    user_id_str = str(user_id)
    now = time.time()
    
    # We rely on load_all_data() at the start of message_handler()
    activity = user_behavior.setdefault(user_id_str, {"messages": [], "initial_count": 0})
    
    # Filter out old messages
    activity["messages"] = [t for t in activity["messages"] if now - t < FLOOD_INTERVAL]
    activity["messages"].append(now)
    
    if activity["initial_count"] < MAX_INITIAL_MESSAGES:
        activity["initial_count"] += 1
        
    # IMPORTANT: We no longer save data here on every message.
    # Saving is now done once at the end of message_handler's successful path.

    return activity

def is_flood_spam(user_id: int) -> bool:
    """Checks flood status based on current global data."""
    user_id_str = str(user_id)
    # Rely on global state, not local file load
    activity = user_behavior.get(user_id_str, {"messages": []})
    return len(activity["messages"]) >= FLOOD_MESSAGE_COUNT

def is_first_message_critical(user_id: int) -> bool:
    """Checks first message status based on current global data."""
    user_id_str = str(user_id)
    # Rely on global state, not local file load
    activity = user_behavior.get(user_id_str, {"initial_count": 0})
    return activity["initial_count"] < MAX_INITIAL_MESSAGES


# ================= Spam Detection Functions =================
# Simplified spam detection rules for brevity (using rules from your code)
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
            # Skip Telegram links since they are already caught by Rule 1
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
        # Load data locally before using ML - REMOVED: Rely on load_all_data() at start
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


# ================= File Download/Join Check Logic (RESTORED) =================

async def is_member_all(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Checks if a user is a member of all required channels."""
    for ch in CHANNELS:
        try:
            member = await context.bot.get_chat_member(ch, user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except TelegramError as e: # Better error handling
            logger.error(f"Error checking {ch} for user {user_id}: {e}")
            # If the channel check fails due to, e.g., bot not being an admin/member,
            # we should treat it as a failure to be safe.
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

    if query and update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_photo(photo=JOIN_IMAGE, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_photo(photo=JOIN_IMAGE, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


# ================= Handlers =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the file-gating process."""
    if not update.message:
        return
        
    if update.effective_chat.type == ChatType.PRIVATE:
        await send_join_message(update, context)
    else:
        # Simple welcome for group chat /start
        await update.message.reply_text("Welcome! I am an anti-spam bot. Use /help to see my commands.")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
        
    await query.answer()

    # ================= Done / Verified =================
    if query.data == "done":
        user_id = query.from_user.id
        if await is_member_all(context, user_id):
            await query.answer("Download initiated!", show_alert=False)
            if query.message:
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
            if query.message:
                # Use edit_message_caption to update the existing message if possible
                try:
                    await query.message.edit_caption(
                         caption="⚠️ **You must join all channels and groups to download the file.**\n\n"
                                 "💡 Join All Channels & Groups To Download the Latest Plus UI Blogger Template !!!\nAfter joining, press ✅ Done!!!",
                         reply_markup=query.message.reply_markup,
                         parse_mode=ParseMode.MARKDOWN
                    )
                except TelegramError:
                    # If editing fails (e.g., too old), send a new one
                    await send_join_message(update, context, query=True)
            
    # ================= Cancel Warn =================
    elif query.data.startswith("cancel_warn"):
        _, chat_id, target_user_id = query.data.split(":")
        chat_id = int(chat_id)
        target_user_id = int(target_user_id)

        chat_admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [admin.user.id for admin in chat_admins]
        if query.from_user.id not in admin_ids:
            await query.answer(
                "⚠️ You don't have permission to do this operation\n💡 You Need to Be admin To do This operation",
                show_alert=True
            )
            return

        # Load data just before modification to ensure it's fresh
        load_all_data() 
        
        await query.answer("Warnings reset successfully.")

        chat_id_str = str(chat_id)
        target_user_id_str = str(target_user_id)
        if chat_id_str in warnings and target_user_id_str in warnings[chat_id_str]:
            del warnings[chat_id_str][target_user_id_str]
            # CRITICAL FIX: Changed save_warnings() to save_all_data()
            save_all_data() 

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
        
        target_user = await context.bot.get_chat_member(chat_id, target_user_id)
        if target_user.user.username:
            user_display = f"@{target_user.user.username}"
        else:
            user_display = f"{target_user.user.first_name}"

        current_time = datetime.now().strftime("%d/%m/%Y %H:%M")
        
        if query.message:
            await query.message.edit_text(
                f"✅ {user_display}'s warnings have been reset!\n"
                f"• Action: Warns (0/3)\n"
                f"• Reset on: {current_time}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{chat_id}:{target_user_id}")]]
                )
            )

    # ================= Unmute =================
    elif query.data.startswith("unmute"):
        _, chat_id, target_user_id = query.data.split(":")
        chat_id = int(chat_id)
        target_user_id = int(target_user_id)

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

        target_user = await context.bot.get_chat_member(chat_id, target_user_id)
        if target_user.user.username:
            user_display = f"@{target_user.user.username}"
        else:
            user_display = f"{target_user.user.first_name}"
        
        current_time = datetime.now().strftime("%d/%m/%Y %H:%M")
        
        if query.message:
            await query.message.edit_text(
                f"🔊 {user_display} has been unmuted!\n"
                f"• Action: Unmuted\n"
                f"• Time: {current_time}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{chat_id}:{target_user_id}")]]
                )
            )

async def handle_status_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    # This logic handles new and left members, including bot kicking
    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            if member.is_bot:
                try:
                    # Check if the bot being kicked is the current bot (self)
                    if member.id == context.bot.id:
                        continue # Don't kick self if added
                        
                    await context.bot.kick_chat_member(update.message.chat_id, member.id)
                    logger.info(f"Kicked bot {member.id} upon join.")
                except TelegramError as e:
                    logger.warning(f"Could not kick bot {member.id}: {e}")
            
    elif update.message.left_chat_member:
        try:
            # Delete "user left" message
            await update.message.delete()
        except TelegramError:
            pass

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # CENTRALIZED DATA MANAGEMENT FOR THIS UPDATE
    # This assumes the file I/O is fast enough for a single update process.
    # For high concurrency, locks (like data_lock) and a true DB are needed.
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

    # Admins are exempt from spam checks
    if user.id in admin_ids:
        return
        
    # Update activity (modifies the global user_behavior dict)
    update_user_activity(user.id) 

    async def handle_spam(reason_text: str):
        try:
            await update.message.delete()
        except TelegramError as e:
            logger.error(f"Failed to delete message from {user.id}: {e}")
        
        # add_warning handles saving the warnings file
        warn_count, expiry = add_warning(chat.id, user.id)
        expiry_str = datetime.fromisoformat(expiry).strftime("%d/%m/%Y %H:%M")
        
        user_mention = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>"
        user_display = f"{user_mention} [<code>{user.id}</code>]"

        caption = ""
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
            
            # Muting is only enforced in the single allowed group
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


    # Check for mentions and links within entities (Telegram/Channel Promotion)
    if entities:
        for entity in entities:
            # Check for Channel/Group Mentions
            if entity.type == MessageEntityType.MENTION:
                mentioned_username = text[entity.offset:entity.offset + entity.length]
                if mentioned_username:
                    try:
                        # Attempt to resolve the mention to a chat object
                        mentioned_chat = await context.bot.get_chat(mentioned_username)
                        if mentioned_chat.type in [ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP]:
                            await handle_spam("Promotion not allowed (Channel/Group Mention)!")
                            # Save behavior on exit (spam detected)
                            save_data(user_behavior, BEHAVIOR_FILE) 
                            return
                    except TelegramError:
                        pass # Ignore if it's a private user mention or lookup fails
            
            # Check for Telegram Links in URL/Text_Link Entities
            is_link_entity = entity.type in [MessageEntityType.URL, MessageEntityType.TEXT_LINK]

            if is_link_entity:
                link_url = ""
                if entity.type == MessageEntityType.URL:
                    link_url = text[entity.offset:entity.offset + entity.length].lower()
                elif entity.type == MessageEntityType.TEXT_LINK:
                    link_url = entity.url.lower()

                if "t.me/" in link_url or "telegram.me/" in link_url:
                    await handle_spam("Promotion not allowed (Telegram Link Entity)!")
                    # Save behavior on exit (spam detected)
                    save_data(user_behavior, BEHAVIOR_FILE) 
                    return

    # Check for rule-based or ML spam
    is_spam_message, reason = is_spam(text, entities, user.id)
    if is_spam_message:
        await handle_spam(reason)
        # Save behavior on exit (spam detected)
        save_data(user_behavior, BEHAVIOR_FILE) 
        return

    # Check for username requirement
    if USERNAME_REQUIRED and not user.username:
        await handle_spam("in order to be accepted in the group, please set up a username")
        # Save behavior on exit (spam detected)
        save_data(user_behavior, BEHAVIOR_FILE) 
        return
        
    # Successful Path: Save updated user behavior before exiting
    save_data(user_behavior, BEHAVIOR_FILE) 

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
            # Use `put_nowait` for speed, assuming the application handles the load
            application.update_queue.put_nowait(update) 
            
        except Exception as e:
            logger.error(f"Error handling incoming update payload: {e}", exc_info=True)
            
    return "OK"

# ================= Run Bot Server (Final Webhook Logic) =================

async def setup_bot_application():
    global ML_MODEL, TFIDF_VECTORIZER
    
    load_all_data() # Initial load of all data

    try:
        # NOTE: Make sure these files exist in your deployment environment
        TFIDF_VECTORIZER = joblib.load('models/vectorizer.joblib') 
        ML_MODEL = joblib.load('models/model.joblib')          
        logger.info("ML model loaded successfully from disk.")
    except Exception as e:
        logger.warning(f"Failed to load ML model files: {e}")
        logger.warning("Bot will operate in rule-based mode only.")
        ML_MODEL = None
        TFIDF_VECTORIZER = None
        
    application.add_handler(CommandHandler("start", start))
    # NOTE: The pattern is correct for all callbacks
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
    
    await serve(app, config)
    
async def run_bot_server():
    await setup_bot_application()
    
    # We must ensure the webhook setup happens AFTER the application starts initializing
    if WEBHOOK_URL: 
        await setup_webhook()

    try:
        await serve_app()
    finally:
        await application.stop()
        save_all_data() # Final save on shutdown
        logger.info("Bot server shut down gracefully.")


def main():
    try:
        if os.name == 'nt':
             asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
             
        asyncio.run(run_bot_server())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully via interrupt.")
    except Exception as e:
        logger.error(f"Fatal error in main loop: {e}", exc_info=True)

if __name__ == '__main__':
    main()
