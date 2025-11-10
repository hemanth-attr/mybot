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
    ChatPermissions, Bot, MessageEntity, User, ChatMember,
    MessageOriginChannel  # <-- FIX 1: Added necessary import
)
from telegram.constants import ParseMode, ChatType, MessageEntityType
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, Application
)
from telegram.error import TelegramError
from urllib.parse import urlparse
from typing import cast, Any, Optional
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

STRICT_NEW_USER_MODE = False 

# === NEW FEATURE TOGGLES ===
ENABLE_ML_SPAM_CHECK = False 
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
    # NOTE: Assumes 'models/' directory exists with the required files.
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

async def clear_warning_async(chat_id: int, user_id: int):
    """Clears a user's warnings asynchronously and saves data."""
    def _reset_warn_sync():
        chat_id_str = str(chat_id)
        user_id_str = str(user_id)
        if chat_id_str in warnings and user_id_str in warnings[chat_id_str]:
            del warnings[chat_id_str][user_id_str]
            save_all_data()

    await asyncio.to_thread(_reset_warn_sync)

async def clean_expired_warnings_async():
    """Cleans up warnings asynchronously and saves data if changes were made."""
    now = datetime.now()
    cleaned = False
    
    def _clean_sync():
        """Performs cleanup synchronously on a worker thread."""
        nonlocal cleaned
        
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


# ================= Advanced Behavioral Analysis =================

def update_user_activity(user_id: int):
    """Updates user activity in the global user_behavior dict (in-memory update)."""
    user_id_str = str(user_id)
    now = time.time()
    
    activity = user_behavior.setdefault(user_id_str, {"messages": [], "initial_count": 0})
    
    activity["messages"] = [t for t in activity["messages"] if now - t < FLOOD_INTERVAL]
    activity["messages"].append(now)
    
    if activity["initial_count"] < MAX_INITIAL_MESSAGES:
        activity["initial_count"] += 1

def is_flood_spam(user_id: int) -> bool:
    """Checks flood status based on current global data."""
    user_id_str = str(user_id)
    activity = user_behavior.get(user_id_str, {"messages": []})
    return len(activity["messages"]) >= FLOOD_MESSAGE_COUNT

def is_first_message_critical(user_id: int) -> bool:
    """Checks if a user is a new user under strict mode."""
    global STRICT_NEW_USER_MODE
    
    if not STRICT_NEW_USER_MODE:
        return False
        
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
    global ENABLE_ML_SPAM_CHECK 
    
    if not ENABLE_ML_SPAM_CHECK: 
        return False
        
    if ML_MODEL and TFIDF_VECTORIZER:
        processed_text = TFIDF_VECTORIZER.transform([unidecode(message_text)])
        prediction = ML_MODEL.predict(processed_text)[0]
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


# ================= Bot Helper Functions (Admin, Target) =================

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if the user sending the message is an admin of the chat."""
    if not update.effective_chat or not update.effective_user:
        return False
        
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("This command only works in groups.")
        return False

    try:
        sender_member: ChatMember = await update.effective_chat.get_member(update.effective_user.id)
        if sender_member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
            return True
    except TelegramError as e:
        logger.error(f"Error checking admin status: {e}")
        await update.effective_message.reply_text("I cannot determine admin status. Check bot permissions.")
        return False
        
    await update.effective_message.reply_text("You must be an administrator to use this command.")
    return False

async def get_target_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[tuple[int, str]]:
    """
    Identifies the target user ID and their display name from a command.
    """
    message = update.effective_message
    if not message or not message.chat_id:
        return None

    user_id = None
    target_user: Optional[User] = None
    
    # 1. Check for a reply
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        user_id = target_user.id

    # 2. Check for user ID in arguments
    elif context.args and context.args[0].isdigit():
        user_id = int(context.args[0])
        
    # ### FIX 3: Removed the non-functional `MENTION` check. 
    # Only `TEXT_MENTION` is reliably parsable here.
    # 3. Check for a mention entity in the message
    elif context.args and context.args[0].startswith('@') and message.entities:
        for entity in message.entities:
            if entity.type == MessageEntityType.TEXT_MENTION:
                # A text mention is a username linked to a user who doesn't have a public @username
                # or when a user is mentioned by their name. The user object is directly in the entity.
                if entity.user:
                    target_user = entity.user
                    user_id = target_user.id
                    break # Found our user, no need to check other entities
    
    if user_id:
        try:
            # If we only have the ID, we need to fetch the user object to get their name
            if not target_user:
                target_member = await context.bot.get_chat_member(message.chat_id, user_id)
                target_user = target_member.user
            
            user_display = f"<a href='tg://user?id={user_id}'>{html.escape(target_user.first_name)}</a>"
            return user_id, user_display
            
        except TelegramError as e:
            logger.warning(f"Failed to get chat member {user_id}: {e}")
            await message.reply_text(f"Could not find or resolve target user ID <code>{user_id}</code>.", parse_mode=ParseMode.HTML)
            return None
            
    await message.reply_text("Usage: Reply to a user's message, or use the command with their User ID or by @mentioning them (e.g., `/mute 123456789` or `/mute @username`).")
    return None

# ### NEW: Helper function to generate permissions for unmuting.
# This avoids code duplication and fixes the TypeError from the old library version.
def _create_unmute_permissions() -> ChatPermissions:
    """Returns a ChatPermissions object that grants all standard user permissions."""
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=False, # Typically keep these false unless intended
        can_invite_users=True,
        can_pin_messages=False # Typically keep these false unless intended
    )

# ================= Admin Command Handlers =================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays help information about the bot's commands."""
    help_text = (
        "🤖 **Bot Commands & Features**\n\n"
        "**User Commands:**\n"
        "• `/start`: Initiate the file download process (in private chat).\n"
        "• `/help`: Show this help message.\n\n"
        "**Admin Commands (Group Only):**\n"
        "• `/warn [reason]`: Issue a manual warning to a user (reply or use @username/ID).\n"
        "• `/mute [username/ID]`: Mute a user for 24 hours.\n"
        "• `/unmute [username/ID]`: Unmute a restricted user and clear their warnings. (Also works via button).\n"
        "• `/ban [username/ID]`: Permanently ban a user. (Also works via button).\n"
        "• `/unban [username/ID]`: Unban a user and clear their warnings.\n"
        "• `/set_strict_mode [on/off]`: Toggle strict link/spam checks for new users.\n"
        "• `/set_ml_check [on/off]`: Toggle the Machine Learning spam detection model."
    )
    await update.effective_message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await is_admin(update, context):
        return

    target = await get_target_user_id(update, context)
    if not target:
        return

    target_id, target_display = target
    admin_display = f"<a href='tg://user?id={update.effective_user.id}'>{html.escape(update.effective_user.first_name)}</a>"
    
    if target_id == context.bot.id:
        await update.effective_message.reply_text("I cannot mute myself!")
        return
        
    mute_duration = 24
    until_date = datetime.now() + timedelta(hours=mute_duration)
    
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date
        )
        
        caption = (
            f"🔇 **Muted User**\n"
            f"• User: {target_display}\n"
            
            f"• Duration: {mute_duration} hours.\n"
            f"• Reason: Manually enforced mute."
        )
        
        keyboard = [[InlineKeyboardButton("✅ Unmute", callback_data=f"unmute:{update.effective_chat.id}:{target_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.effective_message.reply_text(caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

        logger.info(f"Admin {update.effective_user.id} muted user {target_id} for 24 hours.")
        
    except TelegramError as e:
        await update.effective_message.reply_text(f"Failed to mute user: {e}", parse_mode=ParseMode.HTML)
        logger.error(f"Failed to mute user {target_id}: {e}")

async def unmute_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await is_admin(update, context):
        return

    target = await get_target_user_id(update, context)
    if not target:
        return

    target_id, target_display = target
    admin_display = f"<a href='tg://user?id={update.effective_user.id}'>{html.escape(update.effective_user.first_name)}</a>"
    
    if target_id == context.bot.id:
        await update.effective_message.reply_text("I cannot unmute myself!")
        return

    try:
        # ### FIX: Use the new helper function to get the correct ChatPermissions object.
        # This resolves the TypeError by providing all the expected arguments.
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target_id,
            permissions=_create_unmute_permissions()
        )
        
        await clear_warning_async(update.effective_chat.id, target_id)

        caption = (
            f"🔊 **Unmuted User**\n"
            f"• User: {target_display}\n"
            
            f"• Action: Unmuted and Warnings Cleared."
        )
        
        await update.effective_message.reply_text(caption, parse_mode=ParseMode.HTML)
        logger.info(f"Admin {update.effective_user.id} unmuted user {target_id}.")
        
    except TelegramError as e:
        await update.effective_message.reply_text(f"Failed to unmute user. Check bot permissions (e.g., 'Restrict Members'): {e}", parse_mode=ParseMode.HTML)
        logger.error(f"Failed to unmute user {target_id}: {e}")
        
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await is_admin(update, context):
        return

    target = await get_target_user_id(update, context)
    if not target:
        return

    target_id, target_display = target
    admin_display = f"<a href='tg://user?id={update.effective_user.id}'>{html.escape(update.effective_user.first_name)}</a>"
    
    if target_id == context.bot.id:
        await update.effective_message.reply_text("I cannot ban myself!")
        return

    try:
        await context.bot.ban_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target_id
        )
        
        await clear_warning_async(update.effective_chat.id, target_id)

        caption = (
            f"🔨 **Banned User**\n"
            f"• User: {target_display}\n"
            
            f"• Action: Permanently Banned and Warnings Cleared."
        )
        
        keyboard = [[InlineKeyboardButton("↩️ Unban", callback_data=f"unban:{update.effective_chat.id}:{target_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.effective_message.reply_text(caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        logger.info(f"Admin {update.effective_user.id} banned user {target_id}.")
        
    except TelegramError as e:
        await update.effective_message.reply_text(f"Failed to ban user: {e}", parse_mode=ParseMode.HTML)
        logger.error(f"Failed to ban user {target_id}: {e}")

async def unban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await is_admin(update, context):
        return

    target = await get_target_user_id(update, context)
    if not target:
        return

    target_id, target_display = target
    admin_display = f"<a href='tg://user?id={update.effective_user.id}'>{html.escape(update.effective_user.first_name)}</a>"
    
    if target_id == context.bot.id:
        await update.effective_message.reply_text("I cannot unban myself!")
        return

    try:
        # ### FIX: Added `only_if_banned=True` to make the action more robust.
        await context.bot.unban_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target_id,
            only_if_banned=True
        )
        
        await clear_warning_async(update.effective_chat.id, target_id)

        # ### FIX: Clarified the unban message.
        caption = (
            f"🔓 **Unbanned User**\n"
            f"• User: {target_display}\n"
            
            f"• Action: Ban Lifted. The user can rejoin the group now."
        )
        
        await update.effective_message.reply_text(caption, parse_mode=ParseMode.HTML)
        logger.info(f"Admin {update.effective_user.id} unbanned user {target_id}.")
        
    except TelegramError as e:
        await update.effective_message.reply_text(f"Failed to unban user: {e}", parse_mode=ParseMode.HTML)
        logger.error(f"Failed to unban user {target_id}: {e}")


async def warn_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await is_admin(update, context):
        return

    target = await get_target_user_id(update, context)
    if not target:
        return

    target_id, target_display = target
    
    reason = "Manually warned by Admin"
    if context.args:
        # Re-join args after potentially identifying the user from the first arg
        reason_args = context.args[1:] if (context.args[0].isdigit() or context.args[0].startswith('@')) else context.args
        if reason_args:
            reason = "Admin Warn: " + " ".join(reason_args)

    if target_id == context.bot.id:
        await update.effective_message.reply_text("I cannot warn myself!")
        return

    try:
        warn_count, expiry = await add_warning_async(update.effective_chat.id, target_id)
        expiry_dt = datetime.fromisoformat(expiry)
        expiry_str = expiry_dt.strftime("%d/%m/%Y %H:%M")
        
        admin_display = f"<a href='tg://user?id={update.effective_user.id}'>{html.escape(update.effective_user.first_name)}</a>"
        
        if warn_count <= 2:
            caption = (
                f"⚠️ **Warning Issued**\n"
                f"• User: {target_display}\n"
                
                f"• Action: Warn ({warn_count}/3) ❕ until {expiry_str}.\n"
                f"• Reason: {html.escape(reason)}"
            )
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{update.effective_chat.id}:{target_id}")]]
        else:
            until_date = datetime.now() + timedelta(days=1)
            await context.bot.restrict_chat_member(
                chat_id=update.effective_chat.id,
                user_id=target_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            
            caption = (
                f"🔇 **User Muted**\n"
                f"• User: {target_display}\n"
                
                f"• Action: Muted ({warn_count}/3) 🔇 until {expiry_str}.\n"
                f"• Reason: {html.escape(reason)}"
            )
            keyboard = [[InlineKeyboardButton("✅ Unmute", callback_data=f"unmute:{update.effective_chat.id}:{target_id}")]]
            
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.effective_message.reply_text(caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        logger.info(f"Admin {update.effective_user.id} warned user {target_id}. Count: {warn_count}")

    except TelegramError as e:
        await update.effective_message.reply_text(f"Failed to process warning: {e}", parse_mode=ParseMode.HTML)
        logger.error(f"Failed to manually warn user {target_id}: {e}")
        
async def set_strict_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global STRICT_NEW_USER_MODE
    
    if not update.effective_chat or update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await is_admin(update, context):
        return
        
    if not context.args:
        current_state = "ON" if STRICT_NEW_USER_MODE else "OFF"
        await update.effective_message.reply_text(
            f"Current Strict New User Mode is **{current_state}**.\n"
            f"Usage: `/set_strict_mode on` or `/set_strict_mode off`\n\n"
            f"*(This mode enforces stricter rules (like link blocking) on a user's first {MAX_INITIAL_MESSAGES} messages.)*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
        
    arg = context.args[0].lower()
    
    if arg in ["on", "true", "enable", "1"]:
        STRICT_NEW_USER_MODE = True
        message = "✅ Strict New User Mode **Enabled**.\nNew users (first 3 messages) will now face stricter link and spam checks."
    elif arg in ["off", "false", "disable", "0"]:
        STRICT_NEW_USER_MODE = False
        message = "❌ Strict New User Mode **Disabled**.\nAll users are now subject to the same content rules, regardless of message count."
    else:
        message = "Invalid argument. Use `on` or `off`."
        
    await update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    logger.info(f"Admin {update.effective_user.id} set STRICT_NEW_USER_MODE to {STRICT_NEW_USER_MODE}")

async def set_ml_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ENABLE_ML_SPAM_CHECK 
    
    if not update.effective_chat or update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await is_admin(update, context):
        return
        
    if not context.args:
        current_state = "ON" if ENABLE_ML_SPAM_CHECK else "OFF"
        await update.effective_message.reply_text(
            f"Current ML Spam Check Mode is **{current_state}**.\n"
            f"Usage: `/set_ml_check on` or `/set_ml_check off`\n\n"
            f"*(This toggles the ML model-based spam detection. Disabling it runs only rule-based checks.)*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
        
    arg = context.args[0].lower()
    
    if arg in ["on", "true", "enable", "1"]:
        ENABLE_ML_SPAM_CHECK = True
        message = "✅ ML Spam Check Mode **Enabled**.\nMessages will now be filtered using the trained ML model."
    elif arg in ["off", "false", "disable", "0"]:
        ENABLE_ML_SPAM_CHECK = False
        message = "❌ ML Spam Check Mode **Disabled**.\nSpam filtering is now purely rule-based."
    else:
        message = "Invalid argument. Use `on` or `off`."
        
    await update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    logger.info(f"Admin {update.effective_user.id} set ENABLE_ML_SPAM_CHECK to {ENABLE_ML_SPAM_CHECK}")


# ================= Flask App (for deployment) & Bot Setup =================
app = Flask(__name__)
application = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
bot = application.bot 


# ================= File Download/Join Check Logic =================

async def is_member_all(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Checks if a user is a member of all required channels."""
    for ch in CHANNELS:
        try:
            member = await bot.get_chat_member(ch, user_id) 
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
    caption = "💡 Join All Channels & Groups To Download the Latest Plus UI Blogger Template!!!\nAfter joining, press ✅ Done!!!"

    if is_callback and update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.message.edit_caption(
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        except TelegramError as e: 
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
    await query.answer()
    
    # ================= 1. Done / Verified (File Gating) =================
    if data == "done":
        user_id = query.from_user.id
                
        if await is_member_all(context, user_id):
            
            if query.message:
                try:
                    await query.message.delete()
                except TelegramError as e:
                    logger.warning(f"Failed to delete join message on success: {e}")
            
            chat_id = user_id 
            await context.bot.send_sticker(chat_id=chat_id, sticker=STICKER_ID)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"👋 Hello {query.from_user.first_name}!\n✨ Your theme is now ready..."
            )
            await context.bot.send_document(chat_id=chat_id, document=FILE_PATH)
            
        else:
            await query.answer( 
                "⚠️ You must join all channels and groups to download the file.",
                show_alert=True
            )
            caption_fail = "💡 Verification failed. You must join all channels & groups. Please join and press ✅ Done!!!"
            
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
            
    # ================= 2. Cancel Warn / Unmute / Unban (Admin Actions - Inline) =================
    elif data.startswith("cancel_warn:") or data.startswith("unmute:") or data.startswith("unban:"):
        action, chat_id_str, user_id_str = data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        # Check Admin Permissions
        try:
            member_status = await bot.get_chat_member(chat_id, query.from_user.id) 
            if member_status.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                await query.answer("You are not authorized to perform this action.", show_alert=True)
                return
        except TelegramError:
            await query.answer("Could not verify your admin status.", show_alert=True)
            return
            
        
        await clear_warning_async(chat_id, user_id)
            
        if action == "unmute":
            try:
                # ### FIX: Use the new helper function to get the correct ChatPermissions object.
                await context.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=_create_unmute_permissions()
                )
            except TelegramError as e:
                logger.error(f"Failed to unrestrict chat member {user_id}: {e}")
                
        elif action == "unban":
             try:
                # ### FIX: Added `only_if_banned=True` to make the action more robust.
                await context.bot.unban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    only_if_banned=True
                )
             except TelegramError as e:
                logger.error(f"Failed to unban user {user_id}: {e}")
                
        try:
            user_to_act = await context.bot.get_chat_member(chat_id, user_id)
            user_display = f"<a href='tg://user?id={user_id}'>{html.escape(user_to_act.user.first_name)}</a>"
        except TelegramError:
            user_display = f"User ID <code>{user_id}</code>"
            
        admin_display = f"<a href='tg://user?id={query.from_user.id}'>{html.escape(query.from_user.first_name)}</a>"
        current_time = datetime.now().strftime("%d/%m/%Y %H:%M")

        new_text = ""
        if query.message:
            if action == "unmute":
                new_text = (
                    f"🔊 {user_display} has been unmuted and warnings cleared!\n"
                    f"• Action: Unmuted and Warns Reset (0/3)\n"
                    
                    f"• Time: <code>{current_time}</code>"
                )
            elif action == "cancel_warn":
                new_text = (
                    f"❌ {user_display}'s warnings have been reset!\n"
                    f"• Action: Warns Reset (0/3)\n"
                    
                    f"• Reset on: <code>{current_time}</code>"
                )
            elif action == "unban":
                # ### FIX: Clarified the unban message for button callbacks too.
                new_text = (
                    f"🔓 {user_display} has been unbanned.\n"
                    f"• Action: Ban Lifted. User can rejoin.\n"
                    
                    f"• Time: <code>{current_time}</code>"
                )
            
            if new_text:
                try:
                    await query.message.edit_text(
                        text=new_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=None
                    )
                except TelegramError as e:
                    logger.warning(f"Failed to edit warning/mute message: {e}")


async def handle_status_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            if member.is_bot and member.id != context.bot.id:
                try:
                    await context.bot.ban_chat_member(update.message.chat_id, member.id)
                except TelegramError as e:
                    logger.warning(f"Could not ban bot {member.id}: {e}")
            
    if update.message.left_chat_member or update.message.new_chat_members:
        try:
            await update.message.delete()
        except TelegramError:
            pass

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not (update.message.text or update.message.caption):
        return

    user = update.message.from_user
    chat = update.effective_chat
    text = update.message.text or update.message.caption or ""
    entities = update.message.entities or update.message.caption_entities

    if not user or not chat:
        return

    if user.id in SYSTEM_BOT_IDS:
        return
        
    # ### FIX 2: Admin Caching Logic to prevent API rate-limiting ###
    # This replaces the original get_administrators() call
    admin_ids = context.chat_data.get("admin_ids")
    cache_expiry = context.chat_data.get("admin_cache_expiry", 0)
    now = time.time()

    if not admin_ids or now > cache_expiry:
        try:
            logger.info(f"Refreshing admin cache for chat {chat.id}...")
            chat_admins = await chat.get_administrators()
            admin_ids = [admin.user.id for admin in chat_admins]
            
            # Store in context
            context.chat_data["admin_ids"] = admin_ids
            context.chat_data["admin_cache_expiry"] = now + 3600  # Cache for 1 hour
            
        except TelegramError as e:
            logger.error(f"Failed to refresh admin cache for {chat.id}: {e}")
            admin_ids = admin_ids or [] # Use old list if update fails, or empty list
    # --- End Admin Caching ---

    if user.id in admin_ids:
        return
    
    # ### FIX 1: Corrected check for channel forwards ###
    if update.message.forward_origin and isinstance(update.message.forward_origin, MessageOriginChannel):
        # This message was forwarded from a channel
        await handle_spam("forwarded a message from a channel")
        return
    
    update_user_activity(user.id) 

    async def handle_spam(reason_text: str):
        try:
            await update.message.delete()
        except TelegramError as e:
            logger.error(f"Failed to delete message: {e}")
        
        warn_count, expiry = await add_warning_async(chat.id, user.id)
        expiry_dt = datetime.fromisoformat(expiry)
        expiry_str = expiry_dt.strftime("%d/%m/%Y %H:%M")
        
        user_mention = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>"
        user_display = f"{user_mention} [<code>{user.id}</code>]"

        keyboard = None
        caption = ""

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
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
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
            is_link_entity = entity.type in [MessageEntityType.URL, MessageEntityType.TEXT_LINK]
            
            if is_link_entity:
                link_url = ""
                if entity.type == MessageEntityType.URL:
                    link_url = text[entity.offset:entity.offset + entity.length].lower()
                elif entity.type == MessageEntityType.TEXT_LINK and entity.url:
                    link_url = entity.url.lower()

                if "t.me/" in link_url or "telegram.me/" in link_url:
                    await handle_spam("Promotion not allowed (Telegram Link Entity)!")
                    # Data is saved inside add_warning_async, no need to save again
                    return

    is_spam_message, reason = is_spam(text, entities, user.id)
    if is_spam_message:
        await handle_spam(reason or "sent a spam message")
        return

    if USERNAME_REQUIRED and not user.username:
        await handle_spam("in order to be accepted in the group, please set up a username")
        return
        
    # ### PERFORMANCE FIX: Removed save_all_data_async() from here.
    # It was causing excessive I/O on every single message.
    # Data is now saved only when warnings are added or cleared.

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
            application.update_queue.put_nowait(update) 
            
        except Exception as e:
            logger.error(f"Error handling incoming update payload: {e}", exc_info=True)
            
    return "OK"

# ================= Run Bot Server (Final Webhook Logic) =================

async def setup_bot_application():
    global ML_MODEL, TFIDF_VECTORIZER
    
    await load_all_data_async() 

    try:
        # NOTE: Assumes you have a 'models' directory with these files.
        TFIDF_VECTORIZER, ML_MODEL = await asyncio.to_thread(_load_ml_model_sync, 'models/vectorizer.joblib', 'models/model.joblib')
        logger.info("ML model loaded successfully from disk.")
    except FileNotFoundError:
        logger.warning("ML model files not found. Bot will operate in rule-based mode only.")
        ML_MODEL = None
        TFIDF_VECTORIZER = None
    except Exception as e:
        logger.warning(f"Failed to load ML model files: {e}")
        logger.warning("Bot will operate in rule-based mode only.")
        ML_MODEL = None
        TFIDF_VECTORIZER = None
        
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command)) 
    
    application.add_handler(CommandHandler("mute", mute_user, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("unmute", unmute_user_command, filters=filters.ChatType.GROUPS)) 
    application.add_handler(CommandHandler("ban", ban_user, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("unban", unban_user_command, filters=filters.ChatType.GROUPS)) 
    application.add_handler(CommandHandler("warn", warn_user_command, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("set_strict_mode", set_strict_mode, filters=filters.ChatType.GROUPS)) 
    application.add_handler(CommandHandler("set_ml_check", set_ml_check, filters=filters.ChatType.GROUPS)) 

    application.add_handler(CallbackQueryHandler(button, pattern="^(done|cancel_warn:.*|unmute:.*|unban:.*)$")) 
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, message_handler))
    application.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER,
        handle_status_updates
    ))
    
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
        await application.bot.set_webhook(url=full_url, allowed_updates=Update.ALL_TYPES)
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
    """Main function to setup bot and start the web server."""
    await setup_bot_application()
    
    if WEBHOOK_URL: 
        await setup_webhook()

    try:
        await serve_app()
    finally:
        logger.info("Shutting down application...")
        await application.stop()
        logger.info("Application stopped. Performing final state persistence.")
        
        await save_all_data_async()
        
        logger.info("Bot server shut down gracefully.")


def main():
    if not TOKEN:
        # The critical log at the top already covers this, but an extra check here is fine.
        return
        
    if not WEBHOOK_URL:
        logger.critical("Bot is configured for webhooks but WEBHOOK_URL is missing. It will not receive updates.")

    try:
        # This policy is only needed for specific Windows environments, but it's safe to keep.
        if os.name == 'nt':
             asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
             
        asyncio.run(run_bot_server())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully via interrupt.")
    except Exception as e:
        logger.error(f"Fatal error in main loop: {e}", exc_info=True)

if __name__ == '__main__':
    main()
