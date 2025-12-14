import os
import logging
import asyncpg
from datetime import datetime, timedelta, timezone
from asyncio import Lock

# Get the database URL from the environment variable
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    logging.critical("FATAL: DATABASE_URL environment variable not set.")

# Global connection pool and lock
db_pool = None
_db_lock = Lock()

async def get_pool():
    """Initializes and returns the database connection pool."""
    global db_pool
    if db_pool is None:
        try:
            db_pool = await asyncpg.create_pool(
                DATABASE_URL, 
                command_timeout=10,
                max_inactive_connection_lifetime=300
            )
            logging.info("Database connection pool created successfully.")
        except Exception as e:
            logging.error(f"Failed to create database pool: {e}")
            return None
    return db_pool

async def setup_database():
    """
    Runs on bot startup to create all necessary tables.
    """
    async with _db_lock:
        pool = await get_pool()
        if not pool:
            logging.error("Cannot set up database, pool is not available.")
            return

        async with pool.acquire() as conn:
            try:
                # 1. Chat Settings Table
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS chat_settings (
                        chat_id BIGINT PRIMARY KEY,
                        strict_mode BOOLEAN DEFAULT FALSE,
                        ml_mode BOOLEAN DEFAULT FALSE,
                        auto_reaction BOOLEAN DEFAULT FALSE
                    )
                """)
                
                # 2. Warnings Table
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS warnings (
                        chat_id BIGINT,
                        user_id BIGINT,
                        count INTEGER DEFAULT 0,
                        expiry TIMESTAMPTZ,
                        PRIMARY KEY (chat_id, user_id)
                    )
                """)
                
                # 3. User Activity Table (Tracks initial counts & total messages)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_activity (
                        chat_id BIGINT,
                        user_id BIGINT,
                        initial_count INTEGER DEFAULT 0,
                        total_messages INTEGER DEFAULT 0,
                        PRIMARY KEY (chat_id, user_id)
                    )
                """)
                
                # 4. Announcements Table
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS announcements (
                        id SERIAL PRIMARY KEY,
                        chat_id BIGINT,
                        text TEXT,
                        type TEXT,
                        time_val TEXT,
                        last_run TIMESTAMPTZ DEFAULT NOW()
                    )
                """)

                # 5. RSS Feeds Table (NEW)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS rss_feeds (
                        id SERIAL PRIMARY KEY,
                        feed_url TEXT UNIQUE,
                        last_entry_id TEXT,
                        target_chat_id BIGINT
                    )
                """)
                
                # 6. Reputation Table (NEW)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS reputation (
                        user_id BIGINT PRIMARY KEY,
                        points INTEGER DEFAULT 0
                    )
                """)

                # 7. Bot Users Table (NEW - For Broadcast)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS bot_users (
                        user_id BIGINT PRIMARY KEY,
                        first_seen TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                
                logging.info("Database tables verified/created.")
                
                # --- SCHEMA MIGRATIONS (For existing databases) ---
                try:
                    await conn.execute("ALTER TABLE chat_settings ADD COLUMN IF NOT EXISTS auto_reaction BOOLEAN DEFAULT FALSE")
                    logging.info("Verified 'auto_reaction' column.")
                except Exception: pass
                
                try:
                    await conn.execute("ALTER TABLE user_activity ADD COLUMN IF NOT EXISTS total_messages INTEGER DEFAULT 0")
                    logging.info("Verified 'total_messages' column.")
                except Exception: pass
                
            except Exception as e:
                logging.error(f"Error setting up database tables: {e}")

# ================= CHAT SETTINGS =================

async def get_chat_settings(chat_id: int) -> dict:
    pool = await get_pool()
    if not pool:
        return {"strict_mode": False, "ml_mode": False, "auto_reaction": False} 

    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "SELECT strict_mode, ml_mode, auto_reaction FROM chat_settings WHERE chat_id = $1",
                chat_id
            )
        except asyncpg.exceptions.UndefinedColumnError:
            row = await conn.fetchrow(
                 "SELECT strict_mode, ml_mode, FALSE as auto_reaction FROM chat_settings WHERE chat_id = $1",
                chat_id
            )

        if row:
            return {"strict_mode": row['strict_mode'], "ml_mode": row['ml_mode'], "auto_reaction": row['auto_reaction']}
        
        try:
            await conn.execute(
                """
                INSERT INTO chat_settings (chat_id, strict_mode, ml_mode, auto_reaction)
                VALUES ($1, FALSE, FALSE, FALSE)
                ON CONFLICT (chat_id) DO NOTHING
                """,
                chat_id
            )
        except Exception as e:
             logging.warning(f"Failed to insert default chat_settings for {chat_id}: {e}")
            
        return {"strict_mode": False, "ml_mode": False, "auto_reaction": False}

async def set_chat_setting(chat_id: int, setting_name: str, value: bool):
    pool = await get_pool()
    if not pool or setting_name not in ('strict_mode', 'ml_mode', 'auto_reaction'):
        return

    query = f"""
        INSERT INTO chat_settings (chat_id, {setting_name})
        VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE
        SET {setting_name} = $2
    """
    async with pool.acquire() as conn:
        await conn.execute(query, chat_id, value)

# ================= WARNING SYSTEM =================

async def add_warning_async(chat_id: int, user_id: int) -> tuple[int, datetime]:
    pool = await get_pool()
    if not pool: raise Exception("No DB pool")
    
    new_expiry = datetime.now(timezone.utc) + timedelta(days=1)
    
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO warnings (chat_id, user_id, count, expiry)
            VALUES ($1, $2, 1, $3)
            ON CONFLICT (chat_id, user_id) DO UPDATE
            SET
                count = warnings.count + 1,
                expiry = $3
            RETURNING count, expiry
            """,
            chat_id, user_id, new_expiry
        )
        return row['count'], row['expiry']

async def clear_warning_async(chat_id: int, user_id: int):
    pool = await get_pool()
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM warnings WHERE chat_id = $1 AND user_id = $2", chat_id, user_id)

async def clean_expired_warnings_async():
    pool = await get_pool()
    if not pool: return
    async with pool.acquire() as conn:
        now = datetime.now(timezone.utc)
        await conn.execute("DELETE FROM warnings WHERE expiry < $1", now)

# ================= USER ACTIVITY & RANKS =================

async def get_user_initial_count(chat_id: int, user_id: int) -> int:
    pool = await get_pool()
    if not pool: return 0
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT initial_count FROM user_activity WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id
        )
        return count if count is not None else 0

async def increment_user_initial_count(chat_id: int, user_id: int, max_count: int):
    pool = await get_pool()
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_activity (chat_id, user_id, initial_count)
            VALUES ($1, $2, 1)
            ON CONFLICT (chat_id, user_id) DO UPDATE
            SET initial_count = LEAST(user_activity.initial_count + 1, $3)
            WHERE user_activity.initial_count < $3
            """,
            chat_id, user_id, max_count
        )

async def increment_total_messages(chat_id: int, user_id: int):
    """Increments the total message count for a user (for Ranking)."""
    pool = await get_pool()
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_activity (chat_id, user_id, total_messages) VALUES ($1, $2, 1)
            ON CONFLICT (chat_id, user_id) DO UPDATE 
            SET total_messages = user_activity.total_messages + 1
        """, chat_id, user_id)

async def get_user_rank_data(chat_id: int, user_id: int):
    """Fetches message count and reputation for the /info command."""
    pool = await get_pool()
    if not pool: return None, 0
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT total_messages, initial_count 
            FROM user_activity WHERE chat_id = $1 AND user_id = $2
        """, chat_id, user_id)
        
        rep = await conn.fetchval("SELECT points FROM reputation WHERE user_id = $1", user_id)
        return row, (rep or 0)

# ================= ANNOUNCEMENT SCHEDULER =================

async def add_announcement(chat_id: int, text: str, type_: str, time_val: str) -> int:
    pool = await get_pool()
    if not pool: return -1
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO announcements (chat_id, text, type, time_val) VALUES ($1, $2, $3, $4) RETURNING id",
            chat_id, text, type_, time_val
        )
        return row['id']

async def remove_announcement(ann_id: int):
    pool = await get_pool()
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM announcements WHERE id = $1", ann_id)

async def get_all_announcements():
    pool = await get_pool()
    if not pool: return []
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM announcements")

# ================= RSS FEEDS (NEW) =================

async def get_rss_feeds():
    pool = await get_pool()
    if not pool: return []
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM rss_feeds")

async def add_rss_feed(url: str, chat_id: int):
    pool = await get_pool()
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO rss_feeds (feed_url, target_chat_id) VALUES ($1, $2) ON CONFLICT (feed_url) DO NOTHING",
            url, chat_id
        )

async def remove_rss_feed(url: str, chat_id: int):
    pool = await get_pool()
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM rss_feeds WHERE feed_url = $1 AND target_chat_id = $2",
            url, chat_id
        )

async def update_rss_last_entry(feed_id: int, entry_id: str):
    pool = await get_pool()
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute("UPDATE rss_feeds SET last_entry_id = $1 WHERE id = $2", entry_id, feed_id)

# ================= REPUTATION SYSTEM (NEW) =================

async def add_reputation(user_id: int, points: int = 1):
    pool = await get_pool()
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO reputation (user_id, points) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET points = reputation.points + $2
        """, user_id, points)

async def get_top_reputation(limit=10):
    pool = await get_pool()
    if not pool: return []
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, points FROM reputation ORDER BY points DESC LIMIT $1", limit)

# ================= BROADCAST / PRIVATE USERS (NEW) =================

async def log_private_user(user_id: int):
    """Logs a user who has started the bot in private."""
    pool = await get_pool()
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id)

async def get_all_bot_users():
    """Fetches all users for broadcast."""
    pool = await get_pool()
    if not pool: return []
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT user_id FROM bot_users")
# ================= ADMIN SETTERS (bottom) =================

async def set_message_count(chat_id: int, user_id: int, count: int):
    """Manually sets the message count for a user in a specific group."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_activity (chat_id, user_id, total_messages) VALUES ($1, $2, $3)
            ON CONFLICT (chat_id, user_id) DO UPDATE SET total_messages = $3
        """, chat_id, user_id, count)

async def set_reputation(user_id: int, points: int):
    """Manually sets the reputation points for a user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO reputation (user_id, points) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET points = $2
        """, user_id, points)
