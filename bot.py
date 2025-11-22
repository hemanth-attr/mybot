import os
import re
import asyncio
import logging
import html
import joblib
import time
import random
import emoji  # REQUIRED: pip install emoji
from datetime import datetime, timedelta
from flask import Flask, request
from unidecode import unidecode
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions, Bot, Message, MessageEntity, User, ChatMember,
    MessageOriginChannel, MessageOriginChat, ReactionTypeEmoji, Chat
)
from telegram.constants import ParseMode, ChatType, MessageEntityType
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, Application
)
from telegram.error import TelegramError
from urllib.parse import urlparse
from typing import cast, Optional
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.wsgi import WsgiToAsgi

# ---------------------------------------------------------------------------
# ⚠️ IMPORTANT: Ensure you have a file named 'database.py' in your repo!
# ---------------------------------------------------------------------------
import database as db

# ================= Configuration =================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    logging.critical("FATAL: TOKEN environment variable not set.")

# Webhook Configuration for Render
WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
WEBHOOK_PATH = "/botupdates"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "replace_this_with_random_string")
PORT = int(os.environ.get("PORT", 10000))

# Bot Settings
CHANNELS = ["@Blogger_Templates_Updated", "@Plus_UI_Official"]
JOIN_IMAGE = "https://raw.githubusercontent.com/hemanth-attr/mybot/main/thumbnail.png"
FILE_PATH = "https://github.com/hemanth-attr/mybot/raw/main/files/Plus-Ui-3.2.0%20(Updated).zip"
STICKER_ID = "CAACAgUAAxkBAAE7GgABaMbdL0TUWT9EogNP92aPwhOpDHwAAkwXAAKAt9lUs_YoJCwR4mA2BA"

SYSTEM_BOT_IDS = [136817688, 1087968824] # Telegram service notifications
USERNAME_REQUIRED = False

# Spam & Reaction Settings
BLOCK_ALL_URLS = False
ALLOWED_DOMAINS = ["plus-ui.blogspot.com", "plus-ul.blogspot.com", "fineshopdesign.com"]

REACTION_LIST = ["🔥", "❤️‍🔥", "👍", "🤔", "😎", "🆒", "🫡", "❤️", "💯", "👀", "☃️", "🌚", "🎄", "⚡️", "🙏", "💘", "🏆", "👌", "👨‍💻", "🤗"]

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

# ================= Logging =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= Data Management =================
def _load_ml_model_sync(vectorizer_path, model_path):
    """Synchronous ML model loading helper."""
    try:
        vectorizer = joblib.load(vectorizer_path)
        model = joblib.load(model_path)
        return vectorizer, model
    except Exception as e:
        logger.error(f"Failed to load ML models: {e}")
        return None, None

# ================= Helper Functions =================

def is_single_emoji(text: str) -> bool:
    """Checks if the text consists of exactly one valid emoji."""
    if not text: return False
    try:
        # Remove variation selectors for accurate counting
        clean_text = text.strip().replace('\ufe0f', '')
        return emoji.is_emoji(clean_text) and len(emoji.emoji_list(clean_text)) == 1
    except Exception:
        return False

async def update_user_activity(chat_id: int, user_id: int):
    user_id_str = str(user_id)
    now = time.time()
    
    # Update in-memory flood cache
    activity = user_behavior.setdefault(user_id_str, {"messages": []})
    activity["messages"] = [t for t in activity["messages"] if now - t < FLOOD_INTERVAL]
    activity["messages"].append(now)
    
    # Update persistent DB count
    await db.increment_user_initial_count(chat_id, user_id, MAX_INITIAL_MESSAGES)

def is_flood_spam(user_id: int) -> bool:
    user_id_str = str(user_id)
    activity = user_behavior.get(user_id_str, {"messages": []})
    return len(activity["messages"]) >= FLOOD_MESSAGE_COUNT

async def is_first_message_critical(chat_id: int, user_id: int, strict_mode_enabled: bool) -> bool:
    if not strict_mode_enabled: return False
    initial_count = await db.get_user_initial_count(chat_id, user_id)
    return initial_count < MAX_INITIAL_MESSAGES

async def get_admin_ids(chat: Chat, context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    admin_ids = context.chat_data.get("admin_ids")
    cache_expiry = context.chat_data.get("admin_cache_expiry", 0)
    now = time.time()

    if not admin_ids or now > cache_expiry:
        try:
            chat_admins = await chat.get_administrators()
            admin_ids = [admin.user.id for admin in chat_admins]
            context.chat_data["admin_ids"] = admin_ids
            context.chat_data["admin_cache_expiry"] = now + 3600 
        except TelegramError:
            admin_ids = admin_ids or []
    return admin_ids or []

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_chat or not update.effective_user: return False
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("This command only works in groups.")
        return False

    admin_ids = await get_admin_ids(update.effective_chat, context)
    if update.effective_user.id in admin_ids:
        return True
    
    await update.effective_message.reply_text("🚫 Admin only.")
    return False

async def get_target_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[tuple[int, str]]:
    message = update.effective_message
    if not message or not message.chat_id: return None
    user_id = None
    target_user: Optional[User] = None
    
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        user_id = target_user.id
    elif context.args and context.args[0].isdigit():
        user_id = int(context.args[0])
    
    if user_id:
        try:
            if not target_user:
                target_member = await context.bot.get_chat_member(message.chat_id, user_id)
                target_user = target_member.user
            user_display = f"<a href='tg://user?id={user_id}'>{html.escape(target_user.first_name)}</a>"
            return user_id, user_display
        except TelegramError:
            pass
            
    await message.reply_text("Usage: Reply to a user or use /cmd [user_id]")
    return None

def _create_unmute_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_other_messages=True,
        can_add_web_page_previews=True, can_invite_users=True
    )

# ================= Spam Detection Logic =================

async def is_spam(message_text: str, message_entities: list[MessageEntity] | None, user_id: int, chat_id: int) -> tuple[bool, str | None]:
    if not message_text: return False, None
    
    settings = await db.get_chat_settings(chat_id)
    strict_mode = settings.get("strict_mode", False)
    is_critical = await is_first_message_critical(chat_id, user_id, strict_mode)
    text_lower = unidecode(message_text).lower()

    # 1. Telegram Links
    if "t.me/" in text_lower or "telegram.me/" in text_lower:
        return True, "sent a Telegram link"

    # 2. Other Links (Strict Mode or Block All)
    if BLOCK_ALL_URLS or is_critical:
        if re.search(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', text_lower):
             if not any(d in text_lower for d in ALLOWED_DOMAINS):
                 return True, "sent an unauthorized link"

    # 3. Excessive Emojis
    if sum(c in SPAM_EMOJIS for c in message_text) > 5:
        return True, "sent excessive spam emojis"

    # 4. Keywords
    if any(w in text_lower for w in SPAM_KEYWORDS):
        return True, "sent spam keywords"
        
    # 5. Formatting
    if message_entities:
        if sum(1 for e in message_entities if e.type in FORMATTING_ENTITY_TYPES) >= MAX_FORMATTING_ENTITIES:
            return True, "used excessive formatting"

    # 6. ML Check
    if settings.get("ml_mode", False) and ML_MODEL and TFIDF_VECTORIZER:
        try:
            processed = TFIDF_VECTORIZER.transform([unidecode(message_text)])
            if ML_MODEL.predict(processed)[0] == 1:
                return True, "sent a spam message (AI Detection)"
        except Exception:
            pass

    return False, None

# ================= Handlers =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    if update.effective_chat.type == ChatType.PRIVATE:
        # Send Join Message
        keyboard = [
            [InlineKeyboardButton("📢 Channel 1", url=f"https://t.me/{CHANNELS[0].strip('@')}"),
             InlineKeyboardButton("👥 Group", url=f"https://t.me/{CHANNELS[1].strip('@')}")],
            [InlineKeyboardButton("✅ Done", callback_data="done")]
        ]
        await update.message.reply_photo(
            photo=JOIN_IMAGE, 
            caption="💡 **Join Channels to Unlock!**\n\n1. Join the channels above.\n2. Click 'Done' to get the file.", 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("✅ Bot is active and protecting this group.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 **Bot Commands**\n\n"
        "**Admins (Private Chat):**\n"
        "• **Forward** a message from your group to me.\n"
        "• I will react to the original message in the group.\n"
        "• Reply to my confirmation with an emoji to change the reaction.\n\n"
        "**Admins (Group):**\n"
        "/mute, /unmute, /ban, /unban, /warn\n"
        "/set_strict_mode [on/off]\n"
        "/set_ml_check [on/off]"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "done":
        user_id = query.from_user.id
        is_member = True
        for ch in CHANNELS:
            try:
                m = await context.bot.get_chat_member(ch, user_id)
                if m.status not in ['member', 'administrator', 'creator']:
                    is_member = False; break
            except: is_member = False; break
        
        if is_member:
            await query.message.delete()
            await context.bot.send_document(user_id, FILE_PATH, caption="📂 Here is your file!")
        else:
            await query.answer("⚠️ You haven't joined all channels!", show_alert=True)
            
    elif data.startswith(("unmute:", "unban:", "cancel_warn:")):
        chat_id = int(data.split(":")[1])
        target_id = int(data.split(":")[2])
        try:
            admin = await context.bot.get_chat_member(chat_id, query.from_user.id)
            if admin.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                await query.answer("🚫 Admin only.", show_alert=True); return
        except: return

        await db.clear_warning_async(chat_id, target_id)
        
        if "unmute" in data:
            await context.bot.restrict_chat_member(chat_id, target_id, _create_unmute_permissions())
        elif "unban" in data:
            await context.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
            
        await query.message.edit_text(f"✅ Action completed by {query.from_user.first_name}.")

# --- Group Admin Commands ---
async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    target = await get_target_user_id(update, context)
    if target:
        await context.bot.restrict_chat_member(update.effective_chat.id, target[0], ChatPermissions(False), 
                                             until_date=datetime.now() + timedelta(days=1))
        await update.message.reply_text(f"🔇 Muted {target[1]} for 24h.", parse_mode=ParseMode.HTML)

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    target = await get_target_user_id(update, context)
    if target:
        await context.bot.restrict_chat_member(update.effective_chat.id, target[0], _create_unmute_permissions())
        await db.clear_warning_async(update.effective_chat.id, target[0])
        await update.message.reply_text(f"🔊 Unmuted {target[1]}.", parse_mode=ParseMode.HTML)

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    target = await get_target_user_id(update, context)
    if target:
        await context.bot.ban_chat_member(update.effective_chat.id, target[0])
        await update.message.reply_text(f"🔨 Banned {target[1]}.", parse_mode=ParseMode.HTML)

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    target = await get_target_user_id(update, context)
    if target:
        await context.bot.unban_chat_member(update.effective_chat.id, target[0], only_if_banned=True)
        await update.message.reply_text(f"🔓 Unbanned {target[1]}.", parse_mode=ParseMode.HTML)

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    target = await get_target_user_id(update, context)
    if target:
        count, _ = await db.add_warning_async(update.effective_chat.id, target[0])
        if count > 2:
            await context.bot.restrict_chat_member(update.effective_chat.id, target[0], ChatPermissions(False),
                                                 until_date=datetime.now() + timedelta(days=1))
            await update.message.reply_text(f"🔇 {target[1]} muted (Max Warns).", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"⚠️ {target[1]} warned ({count}/3).", parse_mode=ParseMode.HTML)

async def set_strict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    val = context.args and context.args[0].lower() in ['on', 'true']
    await db.set_chat_setting(update.effective_chat.id, 'strict_mode', val)
    await update.message.reply_text(f"Strict Mode: {val}")

async def set_ml(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    val = context.args and context.args[0].lower() in ['on', 'true']
    await db.set_chat_setting(update.effective_chat.id, 'ml_mode', val)
    await update.message.reply_text(f"ML Mode: {val}")

# ================= Message Handler (The Core) =================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user = update.message.from_user
    chat = update.effective_chat

    # --- 1. Private Chat (Admin Auto-Reaction Logic) ---
    if chat.type == ChatType.PRIVATE:
        # Case A: Forwarded Message (Trigger)
        if update.message.forward_origin:
            origin = update.message.forward_origin
            if isinstance(origin, (MessageOriginChannel, MessageOriginChat)):
                target_chat_id = origin.chat.id
                target_msg_id = origin.message_id
                
                # 🛑 SECURITY CHECK: Is user Admin in that group?
                try:
                    mem = await context.bot.get_chat_member(target_chat_id, user.id)
                    if mem.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                        await update.message.reply_text("🚫 You are not an admin there.")
                        return
                except TelegramError:
                    await update.message.reply_text("⚠️ I'm not in that group/channel.")
                    return

                # Determine Reaction (Custom Caption OR Random)
                custom_emoji = None
                # If forwarding media with a caption that is ONE emoji, use it
                if update.message.caption and is_single_emoji(update.message.caption):
                    custom_emoji = update.message.caption.strip()
                else:
                    custom_emoji = random.choice(REACTION_LIST)
                
                try:
                    await context.bot.set_message_reaction(target_chat_id, target_msg_id, [ReactionTypeEmoji(custom_emoji)])
                    
                    # Save context so they can reply to change it
                    context.user_data['last_react_chat'] = target_chat_id
                    context.user_data['last_react_msg'] = target_msg_id
                    
                    await update.message.reply_text(f"✅ Reacted with {custom_emoji}\nReply with a different emoji to change it!", quote=True)
                except Exception as e:
                    await update.message.reply_text(f"❌ Error reacting: {e}")
            return

        # Case B: Reply with Emoji (Update Reaction)
        if update.message.reply_to_message and update.message.text:
            if is_single_emoji(update.message.text):
                target_chat = context.user_data.get('last_react_chat')
                target_msg = context.user_data.get('last_react_msg')
                
                if target_chat and target_msg:
                    try:
                        await context.bot.set_message_reaction(target_chat, target_msg, [ReactionTypeEmoji(update.message.text.strip())])
                        await update.message.reply_text(f"🔄 Updated to {update.message.text}")
                    except Exception as e: 
                         await update.message.reply_text(f"❌ Failed: {e}")
                else:
                    await update.message.reply_text("⚠️ Session expired. Forward message again.")
            return
        return

    # --- 2. Group Chat (Spam Protection ONLY) ---
    
    # Cleanup Service Messages (Join/Left)
    if update.message.new_chat_members or update.message.left_chat_member:
        try: await update.message.delete()
        except: pass
        return

    if not user or user.id in SYSTEM_BOT_IDS: return
    
    # Check Admin (Admins bypass spam check)
    if user.id in (await get_admin_ids(chat, context)): return

    # Flood Control
    await update_user_activity(chat.id, user.id)

    # Spam Check
    text = update.message.text or update.message.caption or ""
    if not text:
        # Media-only flood check
        if is_flood_spam(user.id):
            await update.message.delete()
            return

    is_spam_bool, reason = await is_spam(text, update.message.entities, user.id, chat.id)
    if is_spam_bool:
        try: await update.message.delete()
        except: pass
        
        warn_count, _ = await db.add_warning_async(chat.id, user.id)
        if warn_count > 2:
            try: 
                await context.bot.restrict_chat_member(chat.id, user.id, ChatPermissions(False), until_date=datetime.now() + timedelta(days=1))
                await context.bot.send_message(chat.id, f"🔇 {user.first_name} muted (Max Warns).")
            except: pass
        else:
            await context.bot.send_message(chat.id, f"⚠️ {user.first_name} warned ({warn_count}/3). Reason: {reason}")

# ================= Server Setup =================
app = Flask(__name__)
application = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
bot = application.bot

@app.route("/", methods=["GET"])
def index(): return "Bot Running"

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return "Forbidden", 403
    if request.json:
        try:
            update = Update.de_json(cast(dict, request.json), application.bot)
            application.update_queue.put_nowait(update)
        except: pass
    return "OK"

async def main():
    await db.setup_database()
    global ML_MODEL, TFIDF_VECTORIZER
    ML_MODEL, TFIDF_VECTORIZER = await asyncio.to_thread(_load_ml_model_sync, 'models/vectorizer.joblib', 'models/model.joblib')
    
    # Register Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mute", mute_user, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("unmute", unmute_user, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("ban", ban_user, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("unban", unban_user, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("warn", warn_user, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("set_strict_mode", set_strict, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("set_ml_check", set_ml, filters=filters.ChatType.GROUPS))
    
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))
    
    application.job_queue.run_repeating(lambda c: db.clean_expired_warnings_async(), interval=3600, first=10)
    
    await application.initialize()
    await application.start()
    
    # Webhook Setup
    if WEBHOOK_URL:
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET)
        logging.info(f"Webhook set to {WEBHOOK_URL}")

    # Server Start
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    await serve(WsgiToAsgi(app), config)
    
    await application.stop()
    if db.db_pool: await db.db_pool.close()

if __name__ == "__main__":
    try:
        if os.name == 'nt': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
