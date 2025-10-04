import os
import re
import json
import asyncio
import logging
import html
import joblib
import time
from datetime import datetime, timedelta
from flask import Flask
# --- NEW REQUIRED IMPORT ---
from unidecode import unidecode
# ---------------------------
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions, Bot, MessageEntity
)
from telegram.constants import ParseMode, ChatType, MessageEntityType
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.error import TelegramError
from urllib.parse import urlparse

# ================= Configuration =================
# Set your token here if you don't use environment variables, but ENV is recommended
TOKEN = os.getenv("TOKEN") 
if not TOKEN:
    print("FATAL: TOKEN environment variable not set. Please set it to your bot token.")
    # Exiting here is safer than running with no token, for a real bot.
    # raise ValueError("TOKEN environment variable is missing.") 

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
    MessageEntityType.PRE, MessageEntityType.BLOCKQUOTE # Added Blockquote
}
MAX_FORMATTING_ENTITIES = 5     # Max allowed formatting entities
MAX_INITIAL_MESSAGES = 3        # Stricter checks for first X messages
FLOOD_INTERVAL = 5              # Seconds
FLOOD_MESSAGE_COUNT = 3         # Max messages in FLOOD_INTERVAL seconds

# ================= Global Variables for ML Model/User Data =================
ML_MODEL = None
TFIDF_VECTORIZER = None
warnings = {}
user_behavior = {}

# ================= Logging =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
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
                # Handle corrupted data gracefully
                del warnings[chat_id][user_id] 
        if not warnings[chat_id]:
            del warnings[chat_id]
    save_all_data()

# ================= Advanced Behavioral Analysis =================

def update_user_activity(user_id: int):
    """Tracks message count and time for flood detection, and initial message count."""
    user_id_str = str(user_id)
    now = time.time()
    
    # Initialize or update user's flood history
    activity = user_behavior.setdefault(user_id_str, {"messages": [], "initial_count": 0})
    
    # Update flood messages (remove old ones)
    activity["messages"] = [t for t in activity["messages"] if now - t < FLOOD_INTERVAL]
    activity["messages"].append(now)
    
    # Increment initial message count (up to a max)
    if activity["initial_count"] < MAX_INITIAL_MESSAGES:
        activity["initial_count"] += 1

    save_data(user_behavior, BEHAVIOR_FILE)
    return activity

def is_flood_spam(user_id: int) -> bool:
    """Checks for rapid message flooding."""
    user_id_str = str(user_id)
    activity = user_behavior.get(user_id_str, {"messages": []})
    
    # Check if user sent FLOOD_MESSAGE_COUNT or more messages in FLOOD_INTERVAL seconds
    return len(activity["messages"]) >= FLOOD_MESSAGE_COUNT

def is_first_message_critical(user_id: int) -> bool:
    """Returns True if the user is within their first MAX_INITIAL_MESSAGES."""
    user_id_str = str(user_id)
    activity = user_behavior.get(user_id_str, {"initial_count": 0})
    return activity["initial_count"] < MAX_INITIAL_MESSAGES


# ================= Spam Detection Functions =================
def rule_check(message_text: str, message_entities: list[MessageEntity] | None, user_id: int) -> (bool, str):
    """Checks for obvious spam patterns using rules, applying stricter checks for new users."""
    
    # Determine if a stricter check is needed
    is_critical_message = is_first_message_critical(user_id)
    
    # --- Linguistic Enhancement: Unicode Normalization ---
    normalized_text = unidecode(message_text)
    text_lower = normalized_text.lower()
    
    # Rule 1: Always block t.me or telegram.me links (Entity check covers this, but keep as fallback)
    if "t.me/" in text_lower or "telegram.me/" in text_lower:
        return True, "Promotion is not allowed here!"

    # Rule 2: Block all other URLs if BLOCK_ALL_URLS is enabled (Stricter for critical messages)
    if BLOCK_ALL_URLS or is_critical_message:
        url_finder = re.compile(r"((?:https?://|www\.|t\.me/)\S+)", re.I)
        found_urls = url_finder.findall(text_lower)
        
        allowed_domains_lower = [d.lower() for d in ALLOWED_DOMAINS]

        for url in found_urls:
            if "t.me/" in url.lower() or "telegram.me/" in url.lower():
                continue # Already handled above, skip re-checking

            if not url.startswith(('http://', 'https://')):
                temp_url = 'http://' + url
            else:
                temp_url = url

            try:
                parsed_url = urlparse(temp_url)
                # The netloc (domain) or path (if no scheme, e.g., example.com/path)
                domain = parsed_url.netloc.split(':')[0].lower().replace("www.", "")
                
                if not domain and parsed_url.path:
                    # Catch cases like "example.com" which is parsed as path
                    domain = parsed_url.path.strip('/').split('/')[0].lower()

                # If URL is found and its domain is NOT in the allowed list
                if domain and domain not in allowed_domains_lower:
                    return True, "has sent a Link without authorization"
                    
            except Exception:
                return True, "has sent a malformed URL"

    # Rule 3: Excessive emojis
    if sum(c in SPAM_EMOJIS for c in message_text) > 5:
        return True, "sent excessive emojis"

    # Rule 4: Suspicious keywords (checked against normalized text)
    if any(word in text_lower for word in SPAM_KEYWORDS):
        return True, "sent suspicious keywords (e.g., promo/join now)"

    # Rule 5: Excessive Formatting (Pattern Detection)
    if message_entities:
        formatting_count = sum(1 for entity in message_entities if entity.type in FORMATTING_ENTITY_TYPES)
        # Apply stricter check for shorter/critical messages
        text_length_limit = 200 if not is_critical_message else 100 
        
        if formatting_count >= MAX_FORMATTING_ENTITIES and len(message_text) < text_length_limit:
            return True, "used excessive formatting/bolding (a common spam tactic)"
    
    # Rule 6: Excessive Capitalization/Punctuation
    if len(re.findall(r'[A-Z]{3,}', message_text)) > 3 or len(re.findall(r'[!?]{3,}', message_text)) > 1:
        return True, "used excessive capitalization or punctuation"
    
    # Rule 7: Flood Check (Behavioral)
    if is_flood_spam(user_id):
        return True, "is flooding the chat (too many messages too quickly)"

    return False, None

def ml_check(message_text: str) -> bool:
    """Uses a trained ML model to detect tricky spam."""
    if ML_MODEL and TFIDF_VECTORIZER:
        # Process text using the same normalization as the rules
        processed_text = TFIDF_VECTORIZER.transform([unidecode(message_text)]) 
        prediction = ML_MODEL.predict(processed_text)[0]
        # Assuming 1 means spam
        return prediction == 1
    return False

def is_spam(message_text: str, message_entities: list[MessageEntity] | None, user_id: int) -> (bool, str):
    """Hybrid spam detection combining rules and ML."""
    if not message_text:
        return False, None

    # Layer 1: Rule-based check (includes flood check)
    is_rule_spam, reason = rule_check(message_text, message_entities, user_id)
    if is_rule_spam:
        return True, reason

    # Layer 2: Machine learning check
    if ml_check(message_text):
        # Apply ML model more strictly to new users
        if is_first_message_critical(user_id):
             return True, "sent a spam message (ML/First Message Flag)"
        # Or, always allow ML check for high confidence spam
        return True, "sent a spam message (ML Model)"
        
    return False, None

# ================= Flask App (for deployment) =================
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

# ================= Handlers (Placeholder for other essential handlers) =================
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
        # Simplified: For production, you'd check admin permissions here.
        if query.from_user.id not in [admin.user.id for admin in await context.bot.get_chat_administrators(chat_id)]:
            await query.edit_message_text("You are not authorized to perform this action.")
            return

        user = await context.bot.get_chat_member(chat_id, user_id)

        if action == "unmute":
            # Remove Mute restriction (allow all)
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_change_info=False, can_invite_users=True, can_pin_messages=False)
            )
            # Clear warnings
            warnings[str(chat_id)].pop(str(user_id), None)
            save_all_data()
            await query.edit_message_text(f"✅ {user.user.first_name} has been unmuted and warnings cleared.")

        elif action == "cancel_warn":
            # Clear warnings
            warnings[str(chat_id)].pop(str(user_id), None)
            save_all_data()
            await query.edit_message_text(f"❌ Warning for {user.user.first_name} has been cancelled.")


async def handle_status_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This is where you would typically handle /start (if it was a private message)
    # and new member joins/leaves (not part of spam detection core logic, so simplified)
    pass


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    load_all_data() # Ensure data is fresh
    clean_expired_warnings()
    
    if not update.message or (not update.message.text and not update.message.caption):
        return # Ignore non-text messages (unless they are media with promotion in caption, which we will check)

    user = update.message.from_user
    chat = update.effective_chat
    text = update.message.text or update.message.caption or ""
    entities = update.message.entities or update.message.caption_entities # Get entities from text or caption

    # Skip System bots & Admins
    if user.id in SYSTEM_BOT_IDS:
        return
        
    try:
        chat_admins = await chat.get_administrators()
        admin_ids = [admin.user.id for admin in chat_admins]
    except TelegramError:
        admin_ids = []

    if user.id in admin_ids:
        return
        
    # --- BEHAVIORAL TRACKING: Update user activity BEFORE checking for spam ---
    # This is crucial for flood detection and first-message checks
    update_user_activity(user.id) 

    # Unified function to handle and respond to spam/promotion
    async def handle_spam(reason_text: str):
        try:
            await update.message.delete()
        except TelegramError as e:
            logger.error(f"Failed to delete message: {e}")
        
        warn_count, expiry = add_warning(chat.id, user.id)
        expiry_str = datetime.fromisoformat(expiry).strftime("%d/%m/%Y %H:%M")
        
        # User Display
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
                # Mute the user for 1 day
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


    # --- LAYER 1: ENTITY-BASED CHECK (The most precise promotion catch) ---
    if entities:
        for entity in entities:
            # Case A: Check for Mentions (@channelname)
            if entity.type == MessageEntityType.MENTION:
                mentioned_username = text[entity.offset:entity.offset + entity.length]
                if mentioned_username:
                    try:
                        # Fetch the chat to verify it's a channel/group mention, not just a user
                        mentioned_chat = await context.bot.get_chat(mentioned_username)
                        if mentioned_chat.type in [ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP]:
                            await handle_spam("Promotion not allowed (Channel/Group Mention)!")
                            return
                    except TelegramError:
                        # Ignore if chat is private or doesn't exist
                        pass
            
            # Case B: Check for embedded links (TEXT_LINK) or URL entities
            is_link_entity = entity.type in [MessageEntityType.URL, MessageEntityType.TEXT_LINK]

            if is_link_entity:
                link_url = ""
                if entity.type == MessageEntityType.URL:
                    link_url = text[entity.offset:entity.offset + entity.length].lower()
                elif entity.type == MessageEntityType.TEXT_LINK:
                    link_url = entity.url.lower()

                # Rule: Block t.me or telegram.me links via entity check
                if "t.me/" in link_url or "telegram.me/" in link_url:
                    await handle_spam("Promotion not allowed (Telegram Link Entity)!")
                    return

    # --- LAYER 2: HYBRID SPAM CHECK (Rules + ML) ---
    # This also includes the flood check and the rest of the URL/Keyword rules
    is_spam_message, reason = is_spam(text, entities, user.id)
    if is_spam_message:
        await handle_spam(reason)
        return

    # --- LAYER 3: Username requirement (The old rule) ---
    if USERNAME_REQUIRED and not user.username:
        # Note: You might want a softer warning/mute for this
        await handle_spam("in order to be accepted in the group, please set up a username")
        return


# ================= Run Bot =================
async def run_bot():
    global ML_MODEL, TFIDF_VECTORIZER
    
    load_all_data()

    # Load the pre-trained ML model and vectorizer from disk
    try:
        TFIDF_VECTORIZER = joblib.load('vectorizer.joblib')
        ML_MODEL = joblib.load('model.joblib')
        logger.info("ML model loaded successfully from disk.")
    except Exception as e:
        logger.warning(f"Failed to load ML model files (model.joblib or vectorizer.joblib): {e}")
        logger.warning("Bot will operate in rule-based mode only (still highly effective).")
        ML_MODEL = None
        TFIDF_VECTORIZER = None

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button, pattern="^(done|cancel_warn:.*|unmute:.*)$"))
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, message_handler)) 
    application.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER,
        handle_status_updates
    ))

    # The rest of the setup is for standard web/polling deployment
    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        logger.info("Bot is running in polling mode.")
        await asyncio.Event().wait()
    except Exception as e:
        logger.error(f"Bot failed to start: {e}")

def main():
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully.")
    finally:
        save_all_data() # Save the final state of warnings and behavior

if __name__ == '__main__':
    main()
