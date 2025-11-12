import os
import re
import asyncio
import logging
import html
import joblib
import time
import random  # For auto-reactions
from datetime import datetime, timedelta
from flask import Flask, request
from unidecode import unidecode
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions, Bot, Message, MessageEntity, User, ChatMember,
    MessageOriginChannel, ReactionTypeEmoji, Chat
)
from telegram.constants import ParseMode, ChatType, MessageEntityType
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, Application
    # ChannelPostHandler is no longer needed
)
from telegram.error import TelegramError
from urllib.parse import urlparse
from typing import cast, Any, Optional
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.wsgi import WsgiToAsgi

# Import our database module
import database as db

# ================= Configuration =================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    logging.critical("FATAL: TOKEN environment variable not set.")

CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"
PORT = int(os.environ.get("PORT", 10000))

# --- This is no longer used for logic, only as a reference ---
# ALLOWED_GROUP_ID = -1002810504524 
SYSTEM_BOT_IDS = [136817688, 1087968824]
USERNAME_REQUIRED = False

WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
WEBHOOK_PATH = "/botupdates"

# --- NEW: Recommended for webhook security ---
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "a_very_strong_random_string_12345_replace_me")

# === URL Blocking Control ===
BLOCK_ALL_URLS = False
ALLOWED_DOMAINS = ["plus-ui.blogspot.com", "plus-ul.blogspot.com", "fineshopdesign.com"]

# === REACTION CONFIG ===
REACTION_LIST = ["🔥", "👍", "🤔", "😎", "🆒", "🫡", "❤️", "💯", "👀", "☃️", "🌚", "🎄", "⚡️", "🙏", "💘", "🏆", "👌", "👨‍💻", "🤗"]
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

# ================= Global Variables =================
ML_MODEL = None
TFIDF_VECTORIZER = None
# In-memory flood control cache
user_behavior = {}

# ================= Logging =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= Data Management =================
def _load_ml_model_sync(vectorizer_path, model_path):
    """Synchronous ML model loading helper."""
    vectorizer = joblib.load(vectorizer_path)
    model = joblib.load(model_path)
    return vectorizer, model

# ================= Advanced Behavioral Analysis =================

async def update_user_activity(chat_id: int, user_id: int):
    """Updates in-memory flood cache and persistent DB new-user count."""
    user_id_str = str(user_id)
    now = time.time()
    
    # 1. In-memory flood control
    activity = user_behavior.setdefault(user_id_str, {"messages": []})
    activity["messages"] = [t for t in activity["messages"] if now - t < FLOOD_INTERVAL]
    activity["messages"].append(now)
    
    # 2. Persistent initial message count (call the DB)
    await db.increment_user_initial_count(chat_id, user_id, MAX_INITIAL_MESSAGES)

def is_flood_spam(user_id: int) -> bool:
    """Checks flood status based on current in-memory data."""
    user_id_str = str(user_id)
    activity = user_behavior.get(user_id_str, {"messages": []})
    return len(activity["messages"]) >= FLOOD_MESSAGE_COUNT

async def is_first_message_critical(chat_id: int, user_id: int, strict_mode_enabled: bool) -> bool:
    """Checks if a user is a new user under strict mode, using the database."""
    if not strict_mode_enabled:
        return False
    initial_count = await db.get_user_initial_count(chat_id, user_id)
    return initial_count < MAX_INITIAL_MESSAGES


# ================= Spam Detection Functions =================

async def rule_check(message_text: str, message_entities: list[MessageEntity] | None, user_id: int, chat_id: int) -> tuple[bool, str | None]:
    
    settings = await db.get_chat_settings(chat_id)
    strict_mode_on = settings.get("strict_mode", False)
    
    is_critical_message = await is_first_message_critical(chat_id, user_id, strict_mode_on)
    
    normalized_text = unidecode(message_text)
    text_lower = normalized_text.lower()
    
    # Rule 1: Always block t.me links
    if "t.me/" in text_lower or "telegram.me/" in text_lower:
        return True, "Promotion is not allowed here!"

    # Rule 2: Block all other URLs if BLOCK_ALL_URLS is enabled (or for new users)
    if BLOCK_ALL_URLS or is_critical_message:
        url_finder = re.compile(r'((?:https?://|www\.|t\.me/)\S+|[a-zA-Z0-9-]+\.[a-zA-Z]{2,}\S*)', re.I)
        
        found_urls = url_finder.findall(text_lower)
        allowed_domains_lower = [d.lower() for d in ALLOWED_DOMAINS]

        for url in found_urls:
            if "t.me/" in url.lower() or "telegram.me/" in url.lower():
                continue
            if not url.startswith(('http://', 'https://')):
                temp_url = 'http://' + url
            else:
                temp_url = url
                
            # --- CLEANUP: Simplified this logic ---
            try:
                parsed_url = urlparse(temp_url)
                domain = parsed_url.netloc.split(':')[0].lower().replace("www.", "")

                if domain and domain not in allowed_domains_lower:
                    return True, "has sent a Link without authorization"
            except Exception:
                return True, "has sent a malformed URL"
            # --- END CLEANUP ---

    # Rule 2.5: Block @mentions for new users in strict mode
    if is_critical_message and message_entities:
        for entity in message_entities:
            if entity.type == MessageEntityType.MENTION:
                return True, "has sent a @mention (not allowed for new users)"

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
            return True, "used excessive formatting/bolding"
    
    # Rule 6: Flood check (uses in-memory data)
    if is_flood_spam(user_id):
        return True, "is flooding the chat"

    return False, None

async def ml_check(message_text: str, chat_id: int) -> bool:
    """Uses a trained ML model to detect tricky spam. Relies on DB settings."""
    settings = await db.get_chat_settings(chat_id)
    if not settings.get("ml_mode", False):
        return False
    if ML_MODEL and TFIDF_VECTORIZER:
        processed_text = TFIDF_VECTORIZER.transform([unidecode(message_text)])
        prediction = ML_MODEL.predict(processed_text)[0]
        return prediction == 1
    return False

async def is_spam(message_text: str, message_entities: list[MessageEntity] | None, user_id: int, chat_id: int) -> tuple[bool, str | None]:
    """Hybrid spam detection combining rules and ML."""
    if not message_text:
        return False, None

    is_rule_spam, reason = await rule_check(message_text, message_entities, user_id, chat_id)
    if is_rule_spam:
        return True, reason

    if await ml_check(message_text, chat_id):
        settings = await db.get_chat_settings(chat_id)
        is_critical = await is_first_message_critical(chat_id, user_id, settings.get("strict_mode", False))
        if is_critical:
              return True, "sent a spam message (ML/First Message Flag)"
        return True, "sent a spam message (ML Model)"
        
    return False, None


# ================= Bot Helper Functions =================

async def apply_auto_reaction(message: Message, bot: Bot):
    """Applies a random reaction from the REACTION_LIST to a message."""
    if not message:
        return
    try:
        reaction = random.choice(REACTION_LIST)
        
        await bot.set_message_reaction(
            chat_id=message.chat_id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=reaction)]
        )
        
    except TelegramError as e:
        logger.warning(f"Failed to apply auto-reaction in {message.chat_id}: {e}")

# --- NEW HELPER FUNCTION ---
async def get_admin_ids(chat: Chat, context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    """
    Gets admin IDs from cache or refreshes cache if expired (1-hour TTL).
    """
    admin_ids = context.chat_data.get("admin_ids")
    cache_expiry = context.chat_data.get("admin_cache_expiry", 0)
    now = time.time()

    if not admin_ids or now > cache_expiry:
        try:
            logger.info(f"Refreshing admin cache for chat {chat.id}...")
            chat_admins = await chat.get_administrators()
            admin_ids = [admin.user.id for admin in chat_admins]
            
            context.chat_data["admin_ids"] = admin_ids
            context.chat_data["admin_cache_expiry"] = now + 3600  # Cache for 1 hour
        except TelegramError as e:
            logger.error(f"Failed to refresh admin cache for {chat.id}: {e}")
            admin_ids = admin_ids or [] # Use old list if update fails
            
    return admin_ids or []
# --- END NEW HELPER FUNCTION ---

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Checks if the user sending the message is an admin, using a 1-hour cache.
    """
    if not update.effective_chat or not update.effective_user:
        return False
        
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("This command only works in groups.")
        return False

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # --- REFACTOR: Use the new helper ---
    admin_ids = await get_admin_ids(update.effective_chat, context)
    # --- END REFACTOR ---

    if user_id in admin_ids:
        return True
    
    await update.effective_message.reply_text("You must be an administrator to use this command.")
    return False

async def get_target_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[tuple[int, str]]:
    """Identifies the target user ID and their display name from a command."""
    message = update.effective_message
    if not message or not message.chat_id:
        return None
    user_id = None
    target_user: Optional[User] = None
    
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        user_id = target_user.id
    elif context.args and context.args[0].isdigit():
        user_id = int(context.args[0])
    elif context.args and context.args[0].startswith('@') and message.entities:
        for entity in message.entities:
            if entity.type == MessageEntityType.TEXT_MENTION:
                if entity.user:
                    target_user = entity.user
                    user_id = target_user.id
                    break
    
    if user_id:
        try:
            if not target_user:
                target_member = await context.bot.get_chat_member(message.chat_id, user_id)
                target_user = target_member.user
            user_display = f"<a href='tg://user?id={user_id}'>{html.escape(target_user.first_name)}</a>"
            return user_id, user_display
        except TelegramError as e:
            logger.warning(f"Failed to get chat member {user_id}: {e}")
            await message.reply_text(f"Could not find or resolve target user ID <code>{user_id}</code>.", parse_mode=ParseMode.HTML)
            return None
            
    await message.reply_text("Usage: Reply to a user... or use `/cmd [user_id]` or `/cmd [@username]`.")
    return None

def _create_unmute_permissions() -> ChatPermissions:
    """Returns a ChatPermissions object that grants all standard user permissions."""
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
        can_add_web_page_previews=True, can_change_info=False, can_invite_users=True,
        can_pin_messages=False
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
        "• `/warn [reason]`: Warn a user (reply or @username/ID).\n"
        "• `/mute [user]`: Mute a user for 24 hours.\n"
        "• `/unmute [user]`: Unmute a user and clear warnings.\n"
        "• `/ban [user]`: Permanently ban a user.\n"
        "• `/unban [user]`: Unban a user.\n"
        "• `/set_strict_mode [on/off]`: Toggle strict mode for new users.\n"
        "• `/set_ml_check [on/off]`: Toggle ML spam detection.\n"
        "• `/set_reaction_mode [on/off]`: Toggle auto-reactions.\n"
        "• `/check_permissions`: Check bot's admin rights in this chat."
    )
    await update.effective_message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat: return
    if not await is_admin(update, context): return
    target = await get_target_user_id(update, context)
    if not target: return
    target_id, target_display = target
    if target_id == context.bot.id:
        await update.effective_message.reply_text("I cannot mute myself!")
        return
    mute_duration = 24
    until_date = datetime.now() + timedelta(hours=mute_duration)
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=target_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date
        )
        caption = (
            f"🔇 **Muted User**\n"
            f"• User: {target_display}\n"
            f"• Duration: {mute_duration} hours."
        )
        keyboard = [[InlineKeyboardButton("✅ Unmute", callback_data=f"unmute:{update.effective_chat.id}:{target_id}")]]
        await update.effective_message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        logger.info(f"Admin {update.effective_user.id} muted user {target_id}")
    except TelegramError as e:
        await update.effective_message.reply_text(f"Failed to mute user: {e}")

async def unmute_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat: return
    if not await is_admin(update, context): return
    target = await get_target_user_id(update, context)
    if not target: return
    target_id, target_display = target
    if target_id == context.bot.id: return
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=target_id,
            permissions=_create_unmute_permissions()
        )
        await db.clear_warning_async(update.effective_chat.id, target_id)
        caption = (
            f"🔊 **Unmuted User**\n"
            f"• User: {target_display}\n"
            f"• Action: Unmuted and Warnings Cleared."
        )
        await update.effective_message.reply_text(caption, parse_mode=ParseMode.HTML)
        logger.info(f"Admin {update.effective_user.id} unmuted user {target_id}.")
    except TelegramError as e:
        await update.effective_message.reply_text(f"Failed to unmute user: {e}")
        
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat: return
    if not await is_admin(update, context): return
    target = await get_target_user_id(update, context)
    if not target: return
    target_id, target_display = target
    if target_id == context.bot.id:
        await update.effective_message.reply_text("I cannot ban myself!")
        return
    try:
        await context.bot.ban_chat_member(
            chat_id=update.effective_chat.id, user_id=target_id
        )
        await db.clear_warning_async(update.effective_chat.id, target_id)
        caption = (
            f"🔨 **Banned User**\n"
            f"• User: {target_display}\n"
            f"• Action: Permanently Banned."
        )
        keyboard = [[InlineKeyboardButton("↩️ Unban", callback_data=f"unban:{update.effective_chat.id}:{target_id}")]]
        await update.effective_message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        logger.info(f"Admin {update.effective_user.id} banned user {target_id}.")
    except TelegramError as e:
        await update.effective_message.reply_text(f"Failed to ban user: {e}")

async def unban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat: return
    if not await is_admin(update, context): return
    target = await get_target_user_id(update, context)
    if not target: return
    target_id, target_display = target
    if target_id == context.bot.id: return
    try:
        await context.bot.unban_chat_member(
            chat_id=update.effective_chat.id, user_id=target_id,
            only_if_banned=True
        )
        await db.clear_warning_async(update.effective_chat.id, target_id)
        caption = (
            f"🔓 **Unbanned User**\n"
            f"• User: {target_display}\n"
            f"• Action: Ban Lifted. User can rejoin."
        )
        await update.effective_message.reply_text(caption, parse_mode=ParseMode.HTML)
        logger.info(f"Admin {update.effective_user.id} unbanned user {target_id}.")
    except TelegramError as e:
        await update.effective_message.reply_text(f"Failed to unban user: {e}")

async def warn_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat: return
    if not await is_admin(update, context): return
    target = await get_target_user_id(update, context)
    if not target: return
    target_id, target_display = target
    reason = "Manually warned by Admin"
    if context.args:
        reason_args = context.args[1:] if (context.args[0].isdigit() or context.args[0].startswith('@')) else context.args
        if reason_args:
            reason = "Admin Warn: " + " ".join(reason_args)
    if target_id == context.bot.id:
        await update.effective_message.reply_text("I cannot warn myself!")
        return
    try:
        warn_count, expiry_dt = await db.add_warning_async(update.effective_chat.id, target_id)
        expiry_str = expiry_dt.strftime("%d/%m/%Y %H:%M")
        
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
                chat_id=update.effective_chat.id, user_id=target_id,
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
    except Exception as e:
        await update.effective_message.reply_text(f"Failed to process warning: {e}")

async def set_strict_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    if not await is_admin(update, context): return
    settings = await db.get_chat_settings(chat_id)
    if not context.args:
        current_state = "ON" if settings.get("strict_mode", False) else "OFF"
        await update.effective_message.reply_text(
            f"Current Strict New User Mode is **{current_state}**.\n"
            f"Usage: `/set_strict_mode on` or `off`\n\n"
            f"*(Enforces stricter rules on a user's first {MAX_INITIAL_MESSAGES} messages.)*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    arg = context.args[0].lower()
    if arg in ["on", "true", "enable", "1"]:
        await db.set_chat_setting(chat_id, 'strict_mode', True)
        message = "✅ Strict New User Mode **Enabled**."
    elif arg in ["off", "false", "disable", "0"]:
        await db.set_chat_setting(chat_id, 'strict_mode', False)
        message = "❌ Strict New User Mode **Disabled**."
    else:
        message = "Invalid argument. Use `on` or `off`."
    await update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def set_ml_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    if not await is_admin(update, context): return
    settings = await db.get_chat_settings(chat_id)
    if not context.args:
        current_state = "ON" if settings.get("ml_mode", False) else "OFF"
        await update.effective_message.reply_text(
            f"Current ML Spam Check Mode is **{current_state}**.\n"
            f"Usage: `/set_ml_check on` or `off`.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    arg = context.args[0].lower()
    if arg in ["on", "true", "enable", "1"]:
        await db.set_chat_setting(chat_id, 'ml_mode', True)
        message = "✅ ML Spam Check Mode **Enabled**."
    elif arg in ["off", "false", "disable", "0"]:
        await db.set_chat_setting(chat_id, 'ml_mode', False)
        message = "❌ ML Spam Check Mode **Disabled**."
    else:
        message = "Invalid argument. Use `on` or `off`."
    await update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def set_reaction_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggles the auto-reaction feature for admins and channels."""
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    if not await is_admin(update, context): return
    settings = await db.get_chat_settings(chat_id)
    if not context.args:
        current_state = "ON" if settings.get("auto_reaction", False) else "OFF"
        await update.effective_message.reply_text(
            f"Current Auto-Reaction Mode is **{current_state}**.\n"
            f"Usage: `/set_reaction_mode on` or `off`\n\n"
            f"*(Reacts to admin messages and linked channel posts.)*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    arg = context.args[0].lower()
    if arg in ["on", "true", "enable", "1"]:
        await db.set_chat_setting(chat_id, 'auto_reaction', True)
        message = "✅ Auto-Reactions **Enabled**."
    elif arg in ["off", "false", "disable", "0"]:
        await db.set_chat_setting(chat_id, 'auto_reaction', False)
        message = "❌ Auto-Reactions **Disabled**."
    else:
        message = "Invalid argument. Use `on` or `off`."
    await update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def check_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks if the bot has all the necessary permissions to function."""
    if not update.effective_chat or not context.bot:
        return
    if not await is_admin(update, context):
        return
        
    chat = update.effective_chat
    bot_id = context.bot.id
    
    try:
        bot_member = await chat.get_member(bot_id)
        
        # Check permissions
        can_delete = bot_member.can_delete_messages
        can_restrict = bot_member.can_restrict_members
        can_react = getattr(bot_member, 'can_set_message_reaction', False) # Safe check for older clients
        
        # Build the reply
        status_text = "🤖 **Bot Permissions Status**\n\n"
        status_text += f"{'✅' if can_delete else '❌'} **Can Delete Messages**: Required for anti-spam.\n"
        status_text += f"{'✅' if can_restrict else '❌'} **Can Restrict Members**: Required for mutes/bans.\n"
        status_text += f"{'✅' if can_react else '❌'} **Can Set Reactions**: Required for auto-reaction.\n\n"
        
        if can_delete and can_restrict:
            status_text += "All critical permissions are **active**! 👍"
        else:
            status_text += "⚠️ **Warning:** Bot is missing critical permissions. Please grant these rights."
            
        await update.effective_message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)
        
    except TelegramError as e:
        logger.error(f"Error checking permissions in {chat.id}: {e}")
        await update.effective_message.reply_text(f"Failed to check permissions: {e}")

# ================= Flask App & Bot Setup =================
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
    """Sends the message prompting the user to join channels."""
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
                caption=caption, reply_markup=reply_markup
            )
        except TelegramError: 
             await update.callback_query.message.reply_photo(
                 photo=JOIN_IMAGE, caption=caption, reply_markup=reply_markup
             )
    elif update.message:
        await update.message.reply_photo(
            photo=JOIN_IMAGE, caption=caption, reply_markup=reply_markup
        )

# ================= Handlers =================

async def periodic_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue function to periodically clean expired warnings and flood cache."""
    global user_behavior
    
    # 1. Clean expired warnings from DB
    await db.clean_expired_warnings_async()

    # 2. Clean in-memory flood cache
    now = time.time()
    one_hour_ago = now - 3600  # 1 hour
    cleaned_users = 0

    for user_id_str in list(user_behavior.keys()):
        activity = user_behavior.get(user_id_str)
        
        last_msg_time = 0
        if activity and activity.get("messages"):
            last_msg_time = max(activity["messages"])

        if not last_msg_time or last_msg_time < one_hour_ago:
            del user_behavior[user_id_str]
            cleaned_users += 1
            
    if cleaned_users > 0:
        logger.info(f"Cleaned {cleaned_users} inactive users from in-memory flood cache.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the file-gating process."""
    if not update.message: return
    if update.effective_chat.type == ChatType.PRIVATE:
        await send_join_message(update, context)
    else:
        await update.message.reply_text("Welcome! I am an anti-spam bot. Use /help to see my commands.")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: return
    data = query.data
    await query.answer()
    
    # ================= 1. Done / Verified (File Gating) =================
    if data == "done":
        user_id = query.from_user.id
        if await is_member_all(context, user_id):
            if query.message:
                try: await query.message.delete()
                except TelegramError as e: logger.warning(f"Failed to delete join message: {e}")
            chat_id = user_id 
            await context.bot.send_sticker(chat_id=chat_id, sticker=STICKER_ID)
            await context.bot.send_message(
                chat_id=chat_id, text=f"👋 Hello {query.from_user.first_name}!\n✨ Your theme is now ready..."
            )
            await context.bot.send_document(chat_id=chat_id, document=FILE_PATH)
        else:
            await query.answer( 
                "⚠️ You must join all channels and groups to download the file.",
                show_alert=True
            )
            return
            
    # ================= 2. Cancel Warn / Unmute / Unban (Admin Actions) =================
    elif data.startswith("cancel_warn:") or data.startswith("unmute:") or data.startswith("unban:"):
        action, chat_id_str, user_id_str = data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)

        try:
            member_status = await bot.get_chat_member(chat_id, query.from_user.id) 
            if member_status.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                await query.answer("You are not authorized.", show_alert=True)
                return
        except TelegramError:
            await query.answer("Could not verify your admin status.", show_alert=True)
            return
            
        await db.clear_warning_async(chat_id, user_id)
            
        if action == "unmute":
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat_id, user_id=user_id,
                    permissions=_create_unmute_permissions()
                )
            except TelegramError as e: logger.error(f"Failed to unrestrict {user_id}: {e}")
        elif action == "unban":
             try:
                await context.bot.unban_chat_member(
                    chat_id=chat_id, user_id=user_id, only_if_banned=True
                )
             except TelegramError as e: logger.error(f"Failed to unban {user_id}: {e}")
                
        try:
            user_to_act = await context.bot.get_chat_member(chat_id, user_id)
            user_display = f"<a href='tg://user?id={user_id}'>{html.escape(user_to_act.user.first_name)}</a>"
        except TelegramError:
            user_display = f"User ID <code>{user_id}</code>"
            
        current_time = datetime.now().strftime("%d/%m/%Y %H:%M")
        new_text = ""
        if query.message:
            if action == "unmute":
                new_text = f"🔊 {user_display} has been unmuted and warnings cleared."
            elif action == "cancel_warn":
                new_text = f"❌ {user_display}'s warnings have been reset."
            elif action == "unban":
                new_text = f"🔓 {user_display} has been unbanned. User can rejoin."
            
            if new_text:
                try:
                    await query.message.edit_text(
                        text=f"{new_text}\n• Action by: Admin\n• Time: <code>{current_time}</code>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=None
                    )
                except TelegramError as e:
                    logger.warning(f"Failed to edit button message: {e}")

async def handle_status_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            if member.is_bot and member.id != context.bot.id:
                try:
                    await context.bot.ban_chat_member(update.message.chat_id, member.id)
                except TelegramError as e: logger.warning(f"Could not ban bot {member.id}: {e}")
    if update.message.left_chat_member or update.message.new_chat_members:
        try: await update.message.delete()
        except TelegramError: pass


# --- 
# --- REFACTORED HANDLER ---
# ---
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    user = update.message.from_user
    chat = update.effective_chat
    
    # --- Check for message/caption early ---
    text = update.message.text or update.message.caption or ""
    entities = update.message.entities or update.message.caption_entities

    if not user or not chat: return
    
    # --- Handle channel forwards FIRST ---
    if update.message.forward_origin and isinstance(update.message.forward_origin, MessageOriginChannel):
        # This is a post from a linked channel
        settings = await db.get_chat_settings(chat.id) # Get group's settings
        if settings.get("auto_reaction", False):
            await apply_auto_reaction(update.message, context.bot)
        return # Don't spam check channel posts
    
    # --- Now check system bots ---
    if user.id in SYSTEM_BOT_IDS: return
        
    # --- REFACTOR: Use the new helper ---
    admin_ids = await get_admin_ids(chat, context)
    # --- END REFACTOR ---
    
    # --- Check for Admins ---
    if user.id in admin_ids:
        settings = await db.get_chat_settings(chat.id)
        # React to admin text or photos
        if settings.get("auto_reaction", False) and (text or update.message.photo): 
            await apply_auto_reaction(update.message, context.bot)
        return # Admins are never spam-checked
    
    # --- Flood check for ALL messages (including stickers/media) ---
    await update_user_activity(chat.id, user.id) 

    async def handle_spam(reason_text: str):
        try: await update.message.delete()
        except TelegramError as e: logger.error(f"Failed to delete message: {e}")
        
        warn_count, expiry_dt = await db.add_warning_async(chat.id, user.id)
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
            
            # --- This mute logic now applies to ANY group ---
            until_date = datetime.now() + timedelta(days=1)
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat.id, user_id=user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
            except TelegramError as e: logger.error(f"Failed to mute user {user.id}: {e}")
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        try:
            await context.bot.send_message(
                chat_id=chat.id, text=caption,
                reply_markup=reply_markup, parse_mode=ParseMode.HTML
            )
        except TelegramError as e: logger.error(f"Failed to send warning message: {e}")
            
    # --- Check if this is a text/caption message before text-based spam checks ---
    if not text:
        # It's a sticker, photo (no caption), etc.
        # Check for flood spam ONLY
        if is_flood_spam(user.id):
            await handle_spam("is flooding the chat (media/stickers)")
        return # No text, so no more checks needed

    # This part now only runs if `text` is present
    is_spam_message, reason = await is_spam(text, entities, user.id, chat.id)
    if is_spam_message:
        await handle_spam(reason or "sent a spam message")
        return

    if USERNAME_REQUIRED and not user.username:
        await handle_spam("please set up a username")
        return
        
# ================= Flask Routes =================

@app.route("/", methods=["GET"])
def home():
    return "Bot is alive ✅"

@app.route("/ping", methods=["GET"])
def ping():
    return "OK"

# --- SECURED WEBHOOK ---
@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook(): 
    # Check the secret token
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        logger.warning("Received update with invalid secret token.")
        return "Unauthorized", 403

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
# --- END SECURED WEBHOOK ---

# ================= Run Bot Server =================

async def setup_bot_application():
    global ML_MODEL, TFIDF_VECTORIZER
    
    await db.setup_database() 

    try:
        TFIDF_VECTORIZER, ML_MODEL = await asyncio.to_thread(_load_ml_model_sync, 'models/vectorizer.joblib', 'models/model.joblib')
        logger.info("ML model loaded successfully.")
    except FileNotFoundError:
        logger.warning("ML model files not found. Bot will operate in rule-based mode only.")
        ML_MODEL = None
        TFIDF_VECTORIZER = None
    except Exception as e:
        logger.warning(f"Failed to load ML model files: {e}")
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
    application.add_handler(CommandHandler("set_reaction_mode", set_reaction_mode, filters=filters.ChatType.GROUPS)) 
    application.add_handler(CommandHandler("check_permissions", check_permissions, filters=filters.ChatType.GROUPS))

    application.add_handler(CallbackQueryHandler(button, pattern="^(done|cancel_warn:.*|unmute:.*|unban:.*)$")) 
    
    # --- This filter now catches media/sticker floods ---
    application.add_handler(MessageHandler(
        filters.ChatType.GROUPS & (filters.ALL & ~filters.COMMAND), 
        message_handler
    ))
    
    application.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER,
        handle_status_updates
    ))
    
    application.job_queue.run_repeating(periodic_cleanup_job, interval=3600, first=5)
    logger.info("Scheduled periodic warning cleanup job.")
    
    await application.initialize()
    await application.start()

# --- SECURED WEBHOOK SETUP ---
async def setup_webhook():
    if not WEBHOOK_URL:
        logger.error("FATAL: WEBHOOK_URL environment variable is not set.")
        return False 
    full_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    logger.info(f"Setting webhook to {full_url} on port {PORT} with secret token.")
    try:
        await application.bot.set_webhook(
            url=full_url, 
            allowed_updates=Update.ALL_TYPES,
            secret_token=WEBHOOK_SECRET
        )
        logger.info("Webhook set successfully.")
        return True
    except TelegramError as e:
        logger.error(f"Failed to set webhook: {e}")
        return False
# --- END SECURED WEBHOOK SETUP ---

async def serve_app():
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    asgi_app = WsgiToAsgi(app)
    await serve(asgi_app, config)
    
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
        logger.info("Application stopped. Bot server shut down gracefully.")

def main():
    if not TOKEN:
        return
    if not WEBHOOK_URL:
        logger.critical("WEBHOOK_URL is missing. Bot will not receive updates.")
        
    # --- Recommend setting this ---
    if WEBHOOK_SECRET == "a_very_strong_random_string_12345_replace_me":
        logger.warning("Using default WEBHOOK_SECRET. Please set a strong, random string in your environment variables.")

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
