import os
import re
import json
import asyncio
import logging
import html
import joblib
import time
import functools
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
from typing import cast, Any
from hypercorn.asyncio import serve
from hypercorn.config import Config

# ================= Configuration =================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    logging.critical("FATAL: TOKEN environment variable not set. Please set it to your bot token.")

# FIX: Restoring missing list literals
CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"
PORT = int(os.environ.get("PORT", 10000))

# IMPORTANT: Replace this with your actual group ID
ALLOWED_GROUP_ID = -1002810504524
WARNINGS_FILE = "warnings.json"
BEHAVIOR_FILE = "user_behavior.json" 

# FIX: Restoring missing list literal
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

# ================= Data Management (Blocking I/O - ONLY called via asyncio.to_thread) =================
def load_data(file_path):
    """Synchronous file read."""
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error(f"Error decoding JSON from {file_path}. Starting with empty data.")
            return {}
    return {}

def save_data(data, file_path):
    """Synchronous file write."""
    with open(file_path, "w") as f:
        json.dump(data, f, default=str, indent=4)

def save_all_data():
    """Centralized synchronous data save (called by helper functions on worker threads)."""
    save_data(warnings, WARNINGS_FILE)
    save_data(user_behavior, BEHAVIOR_FILE)

def _load_ml_model_sync(vectorizer_path, model_path):
    """Synchronous ML model loading helper."""
    vectorizer = joblib.load(vectorizer_path)
    model = joblib.load(model_path)
    return vectorizer, model

# ================= Asynchronous Data Management (Wrapped I/O) =================

async def load_all_data_async():
    """Centralized asynchronous data load, offloading file I/O to a separate thread."""
    global warnings, user_behavior
    try:
        warnings_data = await asyncio.to_thread(load_data, WARNINGS_FILE)
        behavior_data = await asyncio.to_thread(load_data, BEHAVIOR_FILE)
        warnings.update(warnings_data)
        user_behavior.update(behavior_data)
    except Exception as e:
        logger.error(f"Failed to load persistence data asynchronously: {e}")
        warnings.clear()
        user_behavior.clear()

async def save_all_data_async():
    """Centralized asynchronous data save, offloading file I/O to a separate thread."""
    try:
        await asyncio.to_thread(functools.partial(save_data, warnings, WARNINGS_FILE))
        await asyncio.to_thread(functools.partial(save_data, user_behavior, BEHAVIOR_FILE))
    except Exception as e:
        logger.error(f"Failed to save persistence data asynchronously: {e}")

async def add_warning_async(chat_id: int, user_id: int):
    """Adds a warning and saves data immediately (asynchronously)."""
    
    def _update_and_save_sync():
        chat_warns = warnings.setdefault(str(chat_id), {})
        user_warn = chat_warns.get(str(user_id), {"count": 0, "expiry": str(datetime.now()), "first_message": True})

        user_warn["count"] += 1
        user_warn["expiry"] = str(datetime.now() + timedelta(days=1))
        chat_warns[str(user_id)] = user_warn
        
        save_all_data() # Synchronous save on the worker thread
        return user_warn["count"], user_warn["expiry"]

    warn_count, expiry = await asyncio.to_thread(_update_and_save_sync)
    return warn_count, expiry

async def clean_expired_warnings_async():
    """Cleans up warnings asynchronously and saves data if changes were made."""
    now = datetime.now()
    cleaned = False
    
    def _clean_sync():
        """Performs cleanup synchronously on a worker thread."""
        nonlocal cleaned
        
        # NOTE: Using list() copies the keys to allow deletion during iteration
        for chat_id in list(warnings.keys()):
            for user_id in list(warnings[chat_id].keys()):
                try:
                    expiry = datetime.fromisoformat(warnings[chat_id][user_id]["expiry"])
                    if expiry < now:
                        del warnings[chat_id][user_id]
                        cleaned = True
                except Exception:
                    warnings[chat_id].pop(user_id, None)
                    cleaned = True
            if not warnings.get(chat_id):
                warnings.pop(chat_id, None)
                cleaned = True
        
        if cleaned:
            save_all_data()
            logger.info("Expired warnings cleaned and data saved.")
            
    await asyncio.to_thread(_clean_sync)


# ================= Advanced Behavioral Analysis (Memory Access) =================

def update_user_activity(user_id: int):
    """Updates user activity in the global user_behavior dict (in-memory update)."""
    user_id_str = str(user_id)
    now = time.time()
    
    # FIX: Correcting corrupted dict/list literal
    activity = user_behavior.setdefault(user_id_str, {"messages": [], "initial_count": 0})
    
    activity["messages"] = [t for t in activity["messages"] if now - t < FLOOD_INTERVAL]
    activity["messages"].append(now)
    
    if activity["initial_count"] < MAX_INITIAL_MESSAGES:
        activity["initial_count"] += 1

def is_flood_spam(user_id: int) -> bool:
    """Checks flood status based on current global data."""
    user_id_str = str(user_id)
    # FIX: Correcting corrupted dict literal
    activity = user_behavior.get(user_id_str, {"messages": []})
    return len(activity["messages"]) >= FLOOD_MESSAGE_COUNT

def is_first_message_critical(user_id: int) -> bool:
    """Checks first message status based on current global data."""
    user_id_str = str(user_id)
    activity = user_behavior.get(user_id_str, {"initial_count": 0})
    return activity["initial_count"] < MAX_INITIAL_MESSAGES


# ================= Spam Detection Functions =================
def rule_check(message_text: str, message_entities: list[MessageEntity] | None, user_id: int) -> tuple[bool, str | None]:
    
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
        # FIX: Restoring missing list construction
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
                # FIX: Correcting the netloc parsing to grab the domain and handling potential index error
                domain = parsed_url.netloc.split(':')[0].lower().replace("www.", "")
                
                if not domain and parsed_url.path:
                    # FIX: Correcting the path parsing and lowercasing
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
        processed_text = TFIDF_VECTORIZER.transform([unidecode(message_text)])
        prediction = ML_MODEL.predict(processed_text)[0] # Ensure we grab the first prediction
        return prediction == 1
    return False

def is_spam(message_text: str, message_entities: list[MessageEntity] | None, user_id: int) -> tuple[bool, str | None]:
    """Hybrid spam detection combining rules and ML."""
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
# We must build the application to get the bot instance with JobQueue capability
application = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
bot = application.bot # Get the bot instance from the application


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
    # FIX: Correcting corrupted keyboard construction
    keyboard = [
        [
            InlineKeyboardButton("📢 Join Channel 1", url=f"https://t.me/{CHANNELS[0].strip('@')}"),
            InlineKeyboardButton("👥 Join Group", url=f"https://t.me/{CHANNELS[1].strip('@')}")
        ],
        [InlineKeyboardButton("✅ Done!!!", callback_data="done")]
    ]
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    caption = "💡 Join All Channels & Groups To Download the Latest Plus UI Blogger Template!!!\nAfter joining, press ✅ Done!!!"

    if is_callback and update.callback_query and update.callback_query.message:
        try:
            # Edit caption only (no need to send photo again)
            await update.callback_query.message.edit_caption(
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        except TelegramError as e: 
             # Fallback to sending a new photo if editing fails (e.g., message too old)
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

async def periodic_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue function to periodically clean expired warnings."""
    # Load data for the worker thread before cleaning
    await load_all_data_async()
    await clean_expired_warnings_async()

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
        
        await load_all_data_async()
        
        if await is_member_all(context, user_id):
            
            # SUCCESS PATH
            await query.answer("Download initiated!", show_alert=False) 
            
            if query.message:
                await query.message.edit_caption(caption="✅ Verification successful! Initiating download...")
            
            chat_id = user_id 
            await context.bot.send_sticker(chat_id=chat_id, sticker=STICKER_ID)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"👋 Hello {query.from_user.first_name}!\n✨ Your theme is now ready..."
            )
            await context.bot.send_document(chat_id=chat_id, document=FILE_PATH)
            
        else:
            # FAILURE PATH: Answer the query with the alert
            await query.answer( 
                "⚠️ You must join all channels and groups to download the file.",
                show_alert=True
            )
            caption_fail = "💡 Verification failed. You must join all channels & groups. Please join and press ✅ Done!!!"
            
            # FIX: Correcting corrupted keyboard construction
            keyboard = [
                [
                    InlineKeyboardButton("📢 Join Channel 1", url=f"https://t.me/{CHANNELS[0].strip('@')}"),
                    InlineKeyboardButton("👥 Join Group", url=f"https://t.me/{CHANNELS[1].strip('@')}")
                ],
                [InlineKeyboardButton("✅ Done!!!", callback_data="done")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if query.message:
                try:
                    await query.message.edit_caption(
                        caption=caption_fail,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML
                    )
                except TelegramError as e:
                    logger.warning(f"Failed to edit caption on failure: {e}. Falling back to send_join_message.")
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
            # FIX: Restoring missing list literal
            admin_ids = []

        if query.from_user.id not in admin_ids:
            await query.answer("You are not authorized to perform this action.", show_alert=True)
            return
        
        await query.answer("Processing action...", show_alert=False)
            
        # --- Reset Warnings (applies to both cancel_warn and unmute) ---
        user_id_str = str(user_id)
        chat_id_str = str(chat_id)

        # Perform the state mutation and immediate save asynchronously
        def _reset_warn_sync():
            if chat_id_str in warnings and user_id_str in warnings[chat_id_str]:
                del warnings[chat_id_str][user_id_str]
                save_all_data()
        
        await asyncio.to_thread(_reset_warn_sync)
            
        # --- Unmute User ---
        if action == "unmute":
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
    # CRITICAL: Load data asynchronously at the start of processing 
    await load_all_data_async()
    
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
        # FIX: Restoring missing list literal
        admin_ids = []

    if user.id in admin_ids:
        return
        
    # Update activity (modifies the global user_behavior dict in memory)
    update_user_activity(user.id) 

    async def handle_spam(reason_text: str):
        try:
            await update.message.delete()
        except TelegramError as e:
            logger.error(f"Failed to delete message: {e}")
        
        # add_warning_async handles asynchronous saving
        warn_count, expiry = await add_warning_async(chat.id, user.id)
        expiry_str = datetime.fromisoformat(expiry).strftime("%d/%m/%Y %H:%M")
        
        user_mention = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>"
        user_display = f"{user_mention} [<code>{user.id}</code>]"

        keyboard = None

        if warn_count <= 2:
            caption = (
                f"{user_display} {reason_text}.\n"
                f"Action: Warn ({warn_count}/3) ❕ until {expiry_str}."
            )
            # FIX: Restoring corrupted keyboard list
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{chat.id}:{user.id}")]]
        else:
            caption = (
                f"{user_display} has exceeded the warning limit.\n"
                f"Action: Muted ({warn_count}/3) 🔇 until {expiry_str}."
            )
            # FIX: Restoring corrupted keyboard list
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
            
    # Check for specific link entities (Telegram promotion)
    if entities:
        for entity in entities:
            # FIX: Restoring incomplete entity type list
            is_link_entity = entity.type in [MessageEntityType.URL, MessageEntityType.TEXT_LINK]
            
            # Check for channel/group mention entities (non-API heavy check)
            if entity.type == MessageEntityType.MENTION:
                # Rule check is enough for general prevention. Skip API-heavy check here.
                pass
            
            # Check for Telegram link entities
            if is_link_entity:
                link_url = ""
                if entity.type == MessageEntityType.URL:
                    link_url = text[entity.offset:entity.offset + entity.length].lower()
                elif entity.type == MessageEntityType.TEXT_LINK:
                    link_url = entity.url.lower()

                if "t.me/" in link_url or "telegram.me/" in link_url:
                    await handle_spam("Promotion not allowed (Telegram Link Entity)!")
                    # Save user behavior here before returning early.
                    await save_all_data_async() 
                    return

    # Check for rule-based or ML spam
    is_spam_message, reason = is_spam(text, entities, user.id)
    if is_spam_message:
        await handle_spam(reason or "sent a spam message")
        await save_all_data_async() 
        return

    # Check for username requirement
    if USERNAME_REQUIRED and not user.username:
        await handle_spam("in order to be accepted in the group, please set up a username")
        await save_all_data_async() 
        return
        
    # Successful Path: Save updated user behavior before exiting
    await save_all_data_async()


# ================= Flask Routes for Webhooks and Health Checks =================

# FIX: Restoring missing methods list
@app.route("/", methods=["GET"])
def home():
    return "Bot is alive ✅"

# FIX: Restoring missing methods list
@app.route("/ping", methods=["GET"])
def ping():
    return "OK"

# FIX: Restoring missing methods list
@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook(): 
    if request.json:
        if not application.running:
            logger.warning("Application not running, skipping update.")
            return "Application not running", 503
            
        try:
            update = Update.de_json(cast(dict, request.json), application.bot)
            application.update_queue.put_nowait(update) 
            
        except Exception as e:
            logger.error(f"Error handling incoming update payload: {e}", exc_info=True)
            
    return "OK"

# ================= Run Bot Server (Final Webhook Logic) =================

async def setup_bot_application():
    global ML_MODEL, TFIDF_VECTORIZER
    
    # 1. Initial asynchronous load of all data
    await load_all_data_async() 

    # 2. Load ML model files asynchronously (Blocking I/O offloaded)
    try:
        TFIDF_VECTORIZER, ML_MODEL = await asyncio.to_thread(_load_ml_model_sync, 'models/vectorizer.joblib', 'models/model.joblib')
        logger.info("ML model loaded successfully from disk.")
    except Exception as e:
        logger.warning(f"Failed to load ML model files: {e}")
        logger.warning("Bot will operate in rule-based mode only.")
        ML_MODEL = None
        TFIDF_VECTORIZER = None
        
    # 3. Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button, pattern="^(done|cancel_warn:.*|unmute:.*)$"))
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, message_handler))
    application.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER,
        handle_status_updates
    ))
    
    # 4. Schedule periodic cleanup using JobQueue
    application.job_queue.run_repeating(periodic_cleanup_job, interval=3600, first=5)
    logger.info("Scheduled periodic warning cleanup job.")
    
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
    # FIX: Restoring corrupted list literal
    config.bind = [f"0.0.0.0:{PORT}"]
    
    await serve(app, config)
    
async def run_bot_server():
    """Main function to setup bot and start the web server."""
    await setup_bot_application()
    
    if WEBHOOK_URL: 
        await setup_webhook()

    try:
        await serve_app()
    finally:
        # Graceful Shutdown: Stop application and ensure final data save is AWAITED
        logger.info("Shutting down application...")
        await application.stop()
        logger.info("Application stopped. Performing final state persistence.")
        
        # FINAL SAVE is now asynchronous and awaited
        await save_all_data_async()
        
        logger.info("Bot server shut down gracefully.")


def main():
    if not WEBHOOK_URL:
        logger.critical("Bot is configured for webhooks but WEBHOOK_URL is missing. It will not receive updates.")

    try:
        if os.name == 'nt':
             asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
             
        asyncio.run(run_bot_server())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully via interrupt.")
    except Exception as e:
        logger.error(f"Fatal error in main loop: {e}", exc_info=True)

if __name__ == '__main__':
    # Ensure you have the 'models/' directory with 'vectorizer.joblib' and 'model.joblib'
    # files, or the bot will run in rule-based mode only.
    main()
