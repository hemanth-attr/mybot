import os
import re
import asyncio
import logging
import html
import joblib
import time
import random
from datetime import datetime, timedelta, time as dt_time
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
)
from telegram.error import TelegramError, BadRequest
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

SYSTEM_BOT_IDS = [136817688, 1087968824]
USERNAME_REQUIRED = False

WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
WEBHOOK_PATH = "/botupdates"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "a_very_strong_random_string_12345_replace_me")

# === URL Blocking Control ===
BLOCK_ALL_URLS = False
ALLOWED_DOMAINS = ["plus-ui.blogspot.com", "plus-ul.blogspot.com", "fineshopdesign.com"]

# === REACTION CONFIG (SAFE LIST ONLY) ===
# Used when you send ONLY a link.
REACTION_LIST = [
    "🔥", "❤️‍🔥", "👍", "🤔", "😎", "🆒", "🫡", "❤️", "💯", "👀", 
    "☃️", "🌚", "🎄", "⚡️", "🙏", "💘", "🏆", "👌", "👨‍💻", "🤗",
    "🤝", "🍌", "🌭", "🐳", "💊", "🕊️"
]

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
user_behavior = {} # In-memory flood control cache
# Add this here:
URL_FINDER_REGEX = re.compile(r'((?:https?://|www\.|t\.me/)\S+|[a-zA-Z0-9-]+\.[a-zA-Z]{2,}\S*)', re.I)

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
        found_urls = URL_FINDER_REGEX.findall(text_lower)
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

                if domain and domain not in allowed_domains_lower:
                    return True, "has sent a Link without authorization"
            except Exception:
                return True, "has sent a malformed URL"

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

    admin_ids = await get_admin_ids(update.effective_chat, context)

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

# --- NEW: REPORT COMMAND ---
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg.reply_to_message:
        await msg.reply_text("ℹ️ Reply to a message with `/report` to alert admins.")
        return

    reported_msg = msg.reply_to_message
    reported_user = reported_msg.from_user
    status_msg = await msg.reply_text("📨 Alerting admins...")
    
    # Get admins
    admin_ids = await get_admin_ids(chat, context)
    
    report_text = (
        f"🚨 **New Report in {html.escape(chat.title)}**\n"
        f"• **Reporter:** {user.mention_html()}\n"
        f"• **Target:** {reported_user.mention_html()} (ID: `{reported_user.id}`)\n"
        f"• <a href='{reported_msg.link}'>Go to Message</a>"
    )

    # Admin Buttons
    # Syntax: action:chat_id:user_id:msg_id
    data_base = f":{chat.id}:{reported_user.id}:{reported_msg.message_id}"
    keyboard = [
        [
            InlineKeyboardButton("🗑 Del", callback_data=f"rep_del{data_base}"),
            InlineKeyboardButton("🔇 Mute", callback_data=f"rep_mute{data_base}")
        ],
        [
            InlineKeyboardButton("🔨 Ban", callback_data=f"rep_ban{data_base}"),
            InlineKeyboardButton("🚫 Ignore", callback_data="rep_ignore")
        ]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    sent_count = 0
    for admin_id in admin_ids:
        try:
            await context.bot.send_message(chat_id=admin_id, text=report_text, parse_mode=ParseMode.HTML, reply_markup=markup)
            sent_count += 1
        except Exception: pass

    await status_msg.edit_text(f"✅ Reported to {sent_count} admins.")

# --- NEW: SCHEDULER (/ntf) ---
async def execute_announcement(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    try:
        await context.bot.send_message(chat_id=job.chat_id, text=job.data)
    except Exception as e:
        logger.error(f"Failed to send announcement: {e}")

async def ntf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. Define variables immediately
    chat = update.effective_chat
    user = update.effective_user
    args = context.args

    # 2. SEPARATE LOGIC: Private vs Group
    # We check Private Chat FIRST to avoid triggering is_admin's error message
    if chat.type == ChatType.PRIVATE:
        # PRIVATE CHAT: Only allow System Admins
        if user.id not in SYSTEM_BOT_IDS:
            return  # Ignore random users
        # If valid System Admin, we proceed (we do NOT call is_admin)
    else:
        # GROUP CHAT: Check actual admin permissions
        if not await is_admin(update, context):
            return  # is_admin handles the "Not Admin" reply

    chat_id = chat.id

    # --- LIST / REMOVE ---
    if args and args[0].lower() == "list":
        rows = await db.get_all_announcements()
        # In private, show ALL. In group, show group's only.
        if chat.type == ChatType.PRIVATE:
            display_rows = rows
        else:
            display_rows = [r for r in rows if r['chat_id'] == chat_id]
            
        if not display_rows:
            await update.message.reply_text("No active announcements found.")
            return
        text = "📅 **Active Schedules:**\n"
        for r in display_rows:
            text += f"ID `{r['id']}` | Chat `{r['chat_id']}` | {r['type']} {r['time_val']} | {r['text'][:15]}...\n"
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    if args and args[0].lower() == "remove":
        try:
            ann_id = int(args[1])
            await db.remove_announcement(ann_id)
            for job in context.job_queue.get_jobs_by_name(f"ann_{ann_id}"):
                job.schedule_removal()
            await update.message.reply_text(f"✅ Announcement {ann_id} removed.")
        except:
            await update.message.reply_text("Invalid ID.")
        return

    # --- CREATE NEW SCHEDULE ---
    # Need at least 3 parts: /ntf <cmd> <time> <text>
    # logic: split by whitespace, max 3 splits -> [cmd, sub, time, text]
    parts = update.message.text.split(None, 3)

    if len(parts) < 4:
        await update.message.reply_text(
            "📢 **Scheduler Help**\n"
            "`/ntf daily 14:00 Text`\n"
            "`/ntf every 1h Text`\n"
            "`/ntf once 30m Text`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    sub_cmd = parts[1].lower() # e.g. "daily"
    time_val = parts[2]        # e.g. "14:00"
    msg_text = parts[3]        # The rest of the message (PRESERVES NEWLINES)
    
    # A. If in Group -> Schedule Immediately for THIS group
    if chat.type != ChatType.PRIVATE:
        await schedule_announcement(chat.id, sub_cmd, time_val, msg_text, context, update.message)
        return

    # B. If in Private -> Show Selection Wizard
    context.user_data['ntf_draft'] = {
        'cmd': sub_cmd,
        'time': time_val,
        'text': msg_text
    }
    
    keyboard = []
    # 1. Add specific channels from config
    for ch in CHANNELS:
        keyboard.append([InlineKeyboardButton(f"Channel: {ch}", callback_data=f"ntf_sel_{ch}")])
        
    # 2. Add 'All' option
    keyboard.append([InlineKeyboardButton("📢 Send to All Channels", callback_data="ntf_sel_ALL_CHANNELS")])
    
    # 3. Add Cancel
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="ntf_sel_cancel")])
    
    await update.message.reply_text(
        f"📝 **Draft Announcement**\n"
        f"• Type: {sub_cmd}\n"
        f"• Time: {time_val}\n"
        f"• Text: {html.escape(msg_text)}\n\n"
        f"👇 **Where should I schedule this?**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
# --- HELPER TO EXECUTE SCHEDULE ---
async def schedule_announcement(target_chat_id, sub_cmd, time_val, msg_text, context, reply_msg=None):
    try:
        ann_id = await db.add_announcement(target_chat_id, msg_text, sub_cmd, time_val)
        
        # Schedule logic
        if sub_cmd == "daily":
            h, m = map(int, time_val.split(':'))
            context.job_queue.run_daily(execute_announcement, dt_time(hour=h, minute=m), chat_id=target_chat_id, data=msg_text, name=f"ann_{ann_id}")
        elif sub_cmd in ["every", "once"]:
            unit = time_val[-1].lower()
            val = int(time_val[:-1])
            secs = val * 60 if unit == 'm' else val * 3600
            if sub_cmd == "every":
                context.job_queue.run_repeating(execute_announcement, interval=secs, first=10, chat_id=target_chat_id, data=msg_text, name=f"ann_{ann_id}")
            else:
                context.job_queue.run_once(execute_announcement, when=secs, chat_id=target_chat_id, data=msg_text, name=f"ann_{ann_id}")
        
        result_text = f"✅ Scheduled for `{target_chat_id}` (ID: `{ann_id}`)"
        if reply_msg:
            await reply_msg.reply_text(result_text, parse_mode=ParseMode.MARKDOWN)
        return result_text
    except Exception as e:
        err_text = f"❌ Error: {e}"
        if reply_msg:
            await reply_msg.reply_text(err_text)
        return err_text
# ================= Admin Command Handlers =================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays help information about the bot's commands."""
    help_text = (
        "🤖 **Bot Commands & Features**\n\n"
        "**User Commands:**\n"
        "• `/start`: Initiate the file download process (in private chat).\n"
        "• `/help`: Show this help message.\n\n"
        "**Admin Commands (Group Only):**\n"
        "• `/warn [reason]`: Warn a user.\n"
        "• `/mute [user]`: Mute a user for 24 hours.\n"
        "• `/unmute [user]`: Unmute a user.\n"
        "• `/ban [user]`: Permanently ban a user.\n"
        "• `/unban [user]`: Unban a user.\n"
        "• `/set_strict_mode [on/off]`: Toggle strict mode for new users.\n"
        "• `/set_ml_check [on/off]`: Toggle ML spam detection.\n"
        "• `/set_reaction_mode [on/off]`: Toggle auto-reactions.\n"
        "• `/check_permissions`: Check bot's admin rights in this chat.\n"
        "• `/replyto <link> <text>`: Reply to a message via link.\n"
        "• `/react <link> <emoji>`: React to a message via link.\n"
        "• `/unreact <link>`: Remove reactions from a message via link.\n"
        "• `/edit <link> <text>`: Edit a bot's message via link.\n"
        "• `/del <link>`: Delete a message via link.\n"
        "• `/pin <link>`: Pin a message via link."
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
    chat = update.effective_chat
    user = update.effective_user
    
    # --- 1. Admin Permission Check ---
    if not await is_admin(update, context):
        # Exception: Allow System Admins to use command in Private Chat
        if chat.type == ChatType.PRIVATE and user.id in SYSTEM_BOT_IDS:
            pass
        else:
            return

    # --- 2. PRIVATE CHAT MODE (Remote Warn via Link) ---
    if chat.type == ChatType.PRIVATE:
        if not context.args:
            await update.message.reply_text("usage: `/warn <link> <reason>`", parse_mode=ParseMode.MARKDOWN)
            return
            
        target_arg = context.args[0]
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Violation of rules."
        
        # Parse the link to get Chat ID and Message ID
        ids = _parse_link_identifiers(target_arg)
        
        if ids:
            target_chat_id, target_msg_id = ids
            try:
                # Step A: Reply to the bad message in the group with the warning
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=f"⚠️ <b>Admin Warning</b>\nReason: {html.escape(reason)}",
                    reply_to_message_id=target_msg_id,
                    parse_mode=ParseMode.HTML
                )
                
                # Step B: Delete the bad message
                await context.bot.delete_message(chat_id=target_chat_id, message_id=target_msg_id)
                
                await update.message.reply_text("✅ **Success:** Message deleted and warning sent to the group.")
            except TelegramError as e:
                await update.message.reply_text(f"❌ **Error:** {e}")
            return
            
        else:
            await update.message.reply_text("⚠️ Invalid link. Use `/warn <link> <reason>`")
            return

    # --- 3. GROUP CHAT MODE (Your Original Feature) ---
    # This block runs ONLY in groups, preserving the DB counting and Muting logic.
    
    target = await get_target_user_id(update, context)
    if not target: return
    target_id, target_display = target
    
    reason = "Manually warned by Admin"
    if context.args:
        # If args exist, check if the first one was the user ID/Username (which get_target handled)
        # If so, the reason starts at index 1. If it was a reply, reason starts at 0.
        if (context.args[0].isdigit() or context.args[0].startswith('@')):
             reason_args = context.args[1:]
        else:
             reason_args = context.args
             
        if reason_args:
            reason = "Admin Warn: " + " ".join(reason_args)

    if target_id == context.bot.id:
        await update.message.reply_text("I cannot warn myself!")
        return

    try:
        # Add to Database
        warn_count, expiry_dt = await db.add_warning_async(chat.id, target_id)
        expiry_str = expiry_dt.strftime("%d/%m/%Y %H:%M")
        
        # Decide Action (Warn vs Mute)
        if warn_count <= 2:
            caption = (
                f"⚠️ **Warning Issued**\n"
                f"• User: {target_display}\n"
                f"• Action: Warn ({warn_count}/3) ❕ until {expiry_str}.\n"
                f"• Reason: {html.escape(reason)}"
            )
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_warn:{chat.id}:{target_id}")]]
        else:
            # Exceeded limit -> Mute
            until_date = datetime.now() + timedelta(days=1)
            await context.bot.restrict_chat_member(
                chat_id=chat.id, user_id=target_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            caption = (
                f"🔇 **User Muted**\n"
                f"• User: {target_display}\n"
                f"• Action: Muted ({warn_count}/3) 🔇 until {expiry_str}.\n"
                f"• Reason: {html.escape(reason)}"
            )
            keyboard = [[InlineKeyboardButton("✅ Unmute", callback_data=f"unmute:{chat.id}:{target_id}")]]
            
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        
    except Exception as e:
        await update.message.reply_text(f"Failed to process warning: {e}")

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
        
        if can_delete and can_restrict and can_react:
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
    # --- NTF SELECTION HANDLER ---
    if data.startswith("ntf_sel_"):
        if data == "ntf_sel_cancel":
            if 'ntf_draft' in context.user_data:
                del context.user_data['ntf_draft']
            await query.message.edit_text("❌ Scheduling cancelled.")
            return
            
        draft = context.user_data.get('ntf_draft')
        if not draft:
            await query.message.edit_text("⚠️ Session expired. Please run /ntf again.")
            return
            
        target = data.replace("ntf_sel_", "")
        
        # Determine targets
        targets = []
        if target == "ALL_CHANNELS":
            targets = CHANNELS # Uses your global CHANNELS list
        else:
            targets = [target]
            
        results = []
        for t in targets:
            res = await schedule_announcement(t, draft['cmd'], draft['time'], draft['text'], context)
            results.append(res)
            
        final_text = "**Done!**\n" + "\n".join(results)
        await query.message.edit_text(final_text, parse_mode=ParseMode.MARKDOWN)
        
        # Clean up draft
        if 'ntf_draft' in context.user_data:
            del context.user_data['ntf_draft']
        return
    # --- REPORT ACTIONS ---
    if data.startswith("rep_"):
        if data == "rep_ignore":
            await query.message.delete()
            return

        action, c_id, u_id, m_id = data.split(":")
        c_id, u_id, m_id = int(c_id), int(u_id), int(m_id)

        try:
            if "del" in action:
                await context.bot.delete_message(c_id, m_id)
                await query.message.edit_text("✅ Deleted")
            elif "mute" in action:
                until = datetime.now() + timedelta(hours=24)
                await context.bot.restrict_chat_member(c_id, u_id, ChatPermissions(can_send_messages=False), until_date=until)
                await query.message.edit_text("✅ Muted 24h")
            elif "ban" in action:
                await context.bot.ban_chat_member(c_id, u_id)
                await query.message.edit_text("✅ Banned")
        except Exception as e:
            await query.message.edit_text(f"❌ Error: {e}")
        return
    
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

# --- HELPER FOR LINK PARSING (Reused in commands) ---
def _parse_link_identifiers(link: str) -> Optional[tuple[Any, int]]:
    """
    Parses a telegram message link to extract chat ID and message ID.
    Returns (chat_id, message_id) or None.
    """
    link_pattern = r"(?:https?://)?(?:www\.)?t\.me/(?:c/)?(\d+|[\w\d_]+)/(\d+)"
    match = re.search(link_pattern, link)
    if not match:
        return None

    chat_identifier = match.group(1)
    message_id = int(match.group(2))

    if chat_identifier.isdigit():
        return int(f"-100{chat_identifier}"), message_id
    else:
        return f"@{chat_identifier}", message_id


# --- REPLY TO COMMAND (NEW) ---
async def reply_to_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replies to a specific message link with text."""
    # FIX: Check raw text length first to avoid errors
    if not update.message.text: 
        return

    # FIX: Split the raw message text into exactly 3 parts:
    # 0: /replyto
    # 1: link
    # 2: The rest of the message (preserves newlines/formatting)
    parts = update.message.text.split(None, 2)

    if len(parts) < 3:
        await update.message.reply_text("Usage: `/replyto <link> <text>`", parse_mode=ParseMode.MARKDOWN)
        return

    link = parts[1]
    text_to_send = parts[2] # This variable now keeps your line breaks!
    user = update.effective_user

    ids = _parse_link_identifiers(link)
    if not ids:
        await update.message.reply_text("❌ Invalid link format.")
        return
    final_chat_id, message_id = ids

    try:
        member = await context.bot.get_chat_member(chat_id=final_chat_id, user_id=user.id)
        if member.status not in ["administrator", "creator", "owner"]:
            await update.message.reply_text("⛔ You must be an Admin of that chat to use this.")
            return
    except TelegramError:
        await update.message.reply_text("❌ I cannot access that chat.")
        return

    try:
        await context.bot.send_message(chat_id=final_chat_id, text=text_to_send, reply_to_message_id=message_id)
        await update.message.reply_text("✅ Reply sent successfully.")
    except TelegramError as e:
        await update.message.reply_text(f"❌ Failed to reply: {e}")


# --- REACT COMMAND (NEW) ---
async def react_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reacts to a specific message link with an emoji."""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: `/react <link> <emoji>`", parse_mode=ParseMode.MARKDOWN)
        return
    link = context.args[0]
    emoji_to_set = context.args[1]
    user = update.effective_user

    ids = _parse_link_identifiers(link)
    if not ids:
        await update.message.reply_text("❌ Invalid link format.")
        return
    final_chat_id, message_id = ids

    try:
        member = await context.bot.get_chat_member(chat_id=final_chat_id, user_id=user.id)
        if member.status not in ["administrator", "creator", "owner"]:
            await update.message.reply_text("⛔ You must be an Admin of that chat to use this.")
            return
    except TelegramError:
        await update.message.reply_text("❌ I cannot access that chat.")
        return

    try:
        await context.bot.set_message_reaction(chat_id=final_chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji=emoji_to_set)])
        await update.message.reply_text(f"✅ Reacted with {emoji_to_set}")
    except TelegramError as e:
        await update.message.reply_text(f"❌ Failed: {e}")

# --- UNREACT COMMAND ---
async def unreact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes reaction from a message."""
    if not context.args:
        await update.message.reply_text("Usage: `/unreact <message_link>`", parse_mode=ParseMode.MARKDOWN)
        return
    link = context.args[0]
    user = update.effective_user
    
    ids = _parse_link_identifiers(link)
    if not ids:
        await update.message.reply_text("❌ Invalid link format.")
        return
    final_chat_id, message_id = ids

    try:
        member = await context.bot.get_chat_member(chat_id=final_chat_id, user_id=user.id)
        if member.status not in ["administrator", "creator", "owner"]:
            await update.message.reply_text("⛔ You must be an Admin to use this.")
            return 
    except TelegramError:
        await update.message.reply_text("❌ I cannot access that chat.")
        return

    try:
        await context.bot.set_message_reaction(chat_id=final_chat_id, message_id=message_id, reaction=[])
        await update.message.reply_text("✅ Reaction removed.")
    except TelegramError as e:
        await update.message.reply_text(f"❌ Failed: {e}")


# --- EDIT MESSAGE COMMAND (RECOMMENDED NEW FEATURE) ---
async def edit_message_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Edits a message sent by the bot via its link.
    Usage: /edit <link> <new_text>
    """
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: `/edit <link> <new_text>`", parse_mode=ParseMode.MARKDOWN)
        return
    link = context.args[0]
    new_text = " ".join(context.args[1:])
    user = update.effective_user

    ids = _parse_link_identifiers(link)
    if not ids:
        await update.message.reply_text("❌ Invalid link format.")
        return
    final_chat_id, message_id = ids

    # Security Check
    try:
        member = await context.bot.get_chat_member(chat_id=final_chat_id, user_id=user.id)
        if member.status not in ["administrator", "creator", "owner"]:
            await update.message.reply_text("⛔ You must be an Admin to use this.")
            return
    except TelegramError:
        await update.message.reply_text("❌ I cannot access that chat.")
        return

    try:
        await context.bot.edit_message_text(chat_id=final_chat_id, message_id=message_id, text=new_text)
        await update.message.reply_text("✅ Message edited successfully.")
    except TelegramError as e:
        if "message to edit not found" in str(e).lower():
            await update.message.reply_text("❌ Failed. (I can only edit messages *I* sent).")
        else:
            await update.message.reply_text(f"❌ Failed to edit: {e}")


# --- DELETE MESSAGE COMMAND (RECOMMENDED NEW FEATURE) ---
async def delete_message_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Deletes a message via its link.
    Usage: /del <link>
    """
    if not context.args:
        await update.message.reply_text("Usage: `/del <link>`", parse_mode=ParseMode.MARKDOWN)
        return
    link = context.args[0]
    user = update.effective_user

    ids = _parse_link_identifiers(link)
    if not ids:
        await update.message.reply_text("❌ Invalid link format.")
        return
    final_chat_id, message_id = ids

    # Security Check
    try:
        member = await context.bot.get_chat_member(chat_id=final_chat_id, user_id=user.id)
        if member.status not in ["administrator", "creator", "owner"]:
            await update.message.reply_text("⛔ You must be an Admin to use this.")
            return
    except TelegramError:
        await update.message.reply_text("❌ I cannot access that chat.")
        return

    try:
        await context.bot.delete_message(chat_id=final_chat_id, message_id=message_id)
        await update.message.reply_text("✅ Message deleted.")
    except TelegramError as e:
        await update.message.reply_text(f"❌ Failed to delete: {e}")


# --- PIN MESSAGE COMMAND (RECOMMENDED NEW FEATURE) ---
async def pin_message_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Pins a message via its link.
    Usage: /pin <link>
    """
    if not context.args:
        await update.message.reply_text("Usage: `/pin <link>`", parse_mode=ParseMode.MARKDOWN)
        return
    link = context.args[0]
    user = update.effective_user

    ids = _parse_link_identifiers(link)
    if not ids:
        await update.message.reply_text("❌ Invalid link format.")
        return
    final_chat_id, message_id = ids

    # Security Check
    try:
        member = await context.bot.get_chat_member(chat_id=final_chat_id, user_id=user.id)
        if member.status not in ["administrator", "creator", "owner"]:
            await update.message.reply_text("⛔ You must be an Admin to use this.")
            return
    except TelegramError:
        await update.message.reply_text("❌ I cannot access that chat.")
        return

    try:
        await context.bot.pin_chat_message(chat_id=final_chat_id, message_id=message_id)
        await update.message.reply_text("✅ Message pinned.")
    except TelegramError as e:
        await update.message.reply_text(f"❌ Failed to pin: {e}")


# --- UPDATED PRIVATE REACTION HANDLER ---
async def handle_private_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles private messages.
    1. Only Link -> Random SAFE emoji.
    2. Link + Any Text -> Forces THAT text as emoji.
    """
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if text.startswith('/'):
        return
    user = update.effective_user
    
    # Re-use parser logic (simplified for this handler since we need the match object too)
    link_pattern = r"(?:https?://)?(?:www\.)?t\.me/(?:c/)?(\d+|[\w\d_]+)/(\d+)"
    match = re.search(link_pattern, text)

    if not match:
        return

    # Extract Chat ID
    chat_identifier = match.group(1)
    message_id = int(match.group(2))
    
    final_chat_id = None
    if chat_identifier.isdigit():
        final_chat_id = int(f"-100{chat_identifier}")
    else:
        final_chat_id = f"@{chat_identifier}"

    # Security Check (Silent fail if not admin)
    try:
        member = await context.bot.get_chat_member(chat_id=final_chat_id, user_id=user.id)
        if member.status not in ["administrator", "creator", "owner"]:
            return 
    except TelegramError:
        return

    # Detect Reaction
    clean_text = text.replace(match.group(0), "").strip()
    selected_reaction = None
    
    if clean_text:
        selected_reaction = clean_text
    else:
        selected_reaction = random.choice(REACTION_LIST)

    # Apply the Reaction
    try:
        await context.bot.set_message_reaction(
            chat_id=final_chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=selected_reaction)]
        )
        await update.message.reply_text(f"✅ Reacted with {selected_reaction}")
        
    except TelegramError as e:
        await update.message.reply_text(f"❌ **Failed.**\nTelegram rejected '{selected_reaction}'.\nMake sure it is a valid single emoji.")

# --- MESSAGE HANDLER ---
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    
    # --- Status Updates (Join/Left) ---
    if update.message.new_chat_members or update.message.left_chat_member:
        if update.message.new_chat_members:
            for member in update.message.new_chat_members:
                if member.is_bot and member.id != context.bot.id:
                    try:
                        await context.bot.ban_chat_member(update.message.chat_id, member.id)
                    except TelegramError: pass
        try: 
            await update.message.delete()
        except TelegramError: pass
        return 
        
    user = update.message.from_user
    chat = update.effective_chat
    
    text = update.message.text or update.message.caption or ""
    entities = update.message.entities or update.message.caption_entities

    if not user or not chat: return
    
    if user.id in SYSTEM_BOT_IDS: return
        
    admin_ids = await get_admin_ids(chat, context)
    
    # Check for Admins
    if user.id in admin_ids:
        return # Admins are ignored for spam checks
    
    # --- Regular User Spam Checks ---
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

    if not text:
        if is_flood_spam(user.id):
            await handle_spam("flooding (media)")
        return

    is_spam_message, reason = await is_spam(text, entities, user.id, chat.id)
    if is_spam_message:
        await handle_spam(reason or "spam detected")
        return

    if USERNAME_REQUIRED and not user.username:
        await handle_spam("no username")
        return
        
# ================= Flask Routes =================

@app.route("/", methods=["GET"])
def home():
    return "Bot is alive ✅"

@app.route("/ping", methods=["GET"])
def ping():
    return "OK"

@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook(): 
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
    # NEW HANDLERS
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("ntf", ntf_command))
    
    # LOAD SAVED SCHEDULES
    rows = await db.get_all_announcements()
    for row in rows:
        try:
            ann_id, c_id, txt, typ, val = row['id'], row['chat_id'], row['text'], row['type'], row['time_val']
            if typ == "daily":
                h, m = map(int, val.split(':'))
                application.job_queue.run_daily(execute_announcement, dt_time(hour=h, minute=m), chat_id=c_id, data=txt, name=f"ann_{ann_id}")
            elif typ == "every":
                unit = val[-1].lower()
                v = int(val[:-1])
                secs = v * 60 if unit == 'm' else v * 3600
                application.job_queue.run_repeating(execute_announcement, interval=secs, chat_id=c_id, data=txt, name=f"ann_{ann_id}")
            # --- ADD THIS BLOCK ---
            elif typ == "once":
                # For 'once', we need to check if it's still valid or just run it on delay
                unit = val[-1].lower()
                v = int(val[:-1])
                secs = v * 60 if unit == 'm' else v * 3600
                # Note: This resets the timer on restart. 
                # For exact precision, you'd need to save the target timestamp in DB, not just "30m"
                application.job_queue.run_once(execute_announcement, when=secs, chat_id=c_id, data=txt, name=f"ann_{ann_id}")
            # ----------------------
        except Exception: pass
    
    application.add_handler(CommandHandler("mute", mute_user, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("unmute", unmute_user_command, filters=filters.ChatType.GROUPS)) 
    application.add_handler(CommandHandler("ban", ban_user, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("unban", unban_user_command, filters=filters.ChatType.GROUPS)) 
    # FIND THIS LINE:
    # application.add_handler(CommandHandler("warn", warn_user_command, filters=filters.ChatType.GROUPS))
    # REPLACE IT WITH THIS (Removes the Group filter):
    application.add_handler(CommandHandler("warn", warn_user_command))
    application.add_handler(CommandHandler("set_strict_mode", set_strict_mode, filters=filters.ChatType.GROUPS)) 
    application.add_handler(CommandHandler("set_ml_check", set_ml_check, filters=filters.ChatType.GROUPS)) 
    application.add_handler(CommandHandler("set_reaction_mode", set_reaction_mode, filters=filters.ChatType.GROUPS)) 
    application.add_handler(CommandHandler("check_permissions", check_permissions, filters=filters.ChatType.GROUPS))
    
    # --- NEW LINK COMMANDS ---
    application.add_handler(CommandHandler("unreact", unreact_command))
    application.add_handler(CommandHandler("replyto", reply_to_command))
    application.add_handler(CommandHandler("react", react_command))
    application.add_handler(CommandHandler("edit", edit_message_command))
    application.add_handler(CommandHandler("del", delete_message_command))
    application.add_handler(CommandHandler("pin", pin_message_command))

    # REPLACE WITH THIS:
    application.add_handler(CallbackQueryHandler(button, pattern="^(done|ntf_sel_.*|cancel_warn:.*|unmute:.*|unban:.*|rep_.*)$")) 
    
    # --- HANDLER FOR PRIVATE REACTIONS ---
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private_reaction))

    # --- HANDLER FOR GROUP MESSAGES ---
    application.add_handler(MessageHandler(
        filters.ChatType.GROUPS & (filters.ALL & ~filters.COMMAND), 
        message_handler
    ))
    
    application.job_queue.run_repeating(periodic_cleanup_job, interval=3600, first=5)
    logger.info("Scheduled periodic warning cleanup job.")
    
    await application.initialize()
    await application.start()

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

async def serve_app():
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    asgi_app = WsgiToAsgi(app)
    await serve(asgi_app, config)
    
async def run_bot_server():
    """Main function to setup bot and start the web server."""
    await setup_bot_application()
    
    # 1. Define the server config
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    asgi_app = WsgiToAsgi(app)

    # 2. Define a background task to set the webhook LATER
    async def set_webhook_delayed():
        await asyncio.sleep(3) # Wait 3 seconds for Hypercorn to start
        if WEBHOOK_URL:
            await setup_webhook()
            
    # 3. Start the background task
    asyncio.create_task(set_webhook_delayed())

    try:
        # 4. Start the server (This blocks until the bot stops)
        await serve(asgi_app, config)
    finally:
        logger.info("Shutting down application...")
        await application.stop()
        if db.db_pool:
            logger.info("Closing database pool...")
            await db.db_pool.close()
        logger.info("Application stopped. Bot server shut down gracefully.")

def main():
    if not TOKEN:
        return
    if not WEBHOOK_URL:
        logger.critical("WEBHOOK_URL is missing. Bot will not receive updates.")
        
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
