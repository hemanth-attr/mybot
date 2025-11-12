import os
import logging
import asyncpg
from datetime import datetime, timedelta

# Get the database URL from the environment variable we set on Render
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    logging.critical("FATAL: DATABASE_URL environment variable not set.")

# A global variable to hold the connection pool
db_pool = None

async def get_pool():
    """Initializes and returns the database connection pool."""
    global db_pool
    if db_pool is None:
        try:
            # --- IMPROVEMENT: Added timeout for Neon reliability ---
            # This helps survive "cold starts"
            db_pool = await asyncpg.create_pool(DATABASE_URL, command_timeout=10)
            logging.info("Database connection pool created successfully.")
        except Exception as e:
            logging.error(f"Failed to create database pool: {e}")
            return None
    return db_pool

async def setup_database():
    """
    Runs on bot startup to create all necessary tables.
    """
    pool = await get_pool()
    if not pool:
        logging.error("Cannot set up database, pool is not available.")
        return

    async with pool.acquire() as conn:
        try:
            # Table for chat-specific settings
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id BIGINT PRIMARY KEY,
                    strict_mode BOOLEAN DEFAULT FALSE,
                    ml_mode BOOLEAN DEFAULT FALSE,
                    auto_reaction BOOLEAN DEFAULT FALSE
                )
            """)
            
            # Table for user warnings
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS warnings (
                    chat_id BIGINT,
                    user_id BIGINT,
                    count INTEGER DEFAULT 0,
                    expiry TIMESTAMPTZ,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
            
            # Table for persistent user activity (tracks new users)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_activity (
                    chat_id BIGINT,
                    user_id BIGINT,
                    initial_count INTEGER DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
            
            logging.info("Database tables verified/created.")
            
            # --- IMPROVEMENT: Proactively add the new column if it's missing ---
            try:
                await conn.execute(
                    "ALTER TABLE chat_settings ADD COLUMN IF NOT EXISTS auto_reaction BOOLEAN DEFAULT FALSE"
                )
                logging.info("Verified 'auto_reaction' column exists in chat_settings.")
            except Exception as e:
                logging.warning(f"Could not alter table to add auto_reaction: {e}")
            # --- END IMPROVEMENT ---
            
        except Exception as e:
            logging.error(f"Error setting up database tables: {e}")

async def get_chat_settings(chat_id: int) -> dict:
    """Fetches the settings for a specific chat."""
    pool = await get_pool()
    if not pool:
        return {"strict_mode": False, "ml_mode": False, "auto_reaction": False} 

    async with pool.acquire() as conn:
        # --- IMPROVEMENT: Handle case where auto_reaction column doesn't exist yet ---
        try:
            row = await conn.fetchrow(
                "SELECT strict_mode, ml_mode, auto_reaction FROM chat_settings WHERE chat_id = $1",
                chat_id
            )
        except asyncpg.exceptions.UndefinedColumnError:
            logging.warning("auto_reaction column missing, falling back.")
            row = await conn.fetchrow(
                 "SELECT strict_mode, ml_mode, FALSE as auto_reaction FROM chat_settings WHERE chat_id = $1",
                chat_id
            )
        # --- END IMPROVEMENT ---

        if row:
            return {"strict_mode": row['strict_mode'], "ml_mode": row['ml_mode'], "auto_reaction": row['auto_reaction']}
        
        # If no settings exist, create them
        try:
            await conn.execute(
                """
                INSERT INTO chat_settings (chat_id, strict_mode, ml_mode, auto_reaction)
                VALUES ($1, FALSE, FALSE, FALSE)
                ON CONFLICT (chat_id) DO NOTHING
                """,
                chat_id
            )
        except Exception:
             pass # Ignore errors if table is in a weird state, will use default
            
        return {"strict_mode": False, "ml_mode": False, "auto_reaction": False}

async def set_chat_setting(chat_id: int, setting_name: str, value: bool):
    """Updates a specific setting (strict_mode, ml_mode, or auto_reaction) for a chat."""
    pool = await get_pool()
    
    if not pool or setting_name not in ('strict_mode', 'ml_mode', 'auto_reaction'):
        logging.warning(f"Invalid setting name '{setting_name}' passed to set_chat_setting.")
        return

    query = f"""
        INSERT INTO chat_settings (chat_id, {setting_name})
        VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE
        SET {setting_name} = $2
    """
    async with pool.acquire() as conn:
        await conn.execute(query, chat_id, value)
        
# --- WARNING FUNCTIONS ---

async def add_warning_async(chat_id: int, user_id: int) -> tuple[int, datetime]:
    pool = await get_pool()
    if not pool:
        raise Exception("Database pool is not available.")
    new_expiry = datetime.now() + timedelta(days=1)
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
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM warnings WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id
        )

async def clean_expired_warnings_async():
    pool = await get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        now = datetime.now()
        result = await conn.execute(
            "DELETE FROM warnings WHERE expiry < $1",
            now
        )
        count = int(result.split(' ')[-1])
        if count > 0:
            logging.info(f"Cleaned {count} expired warnings from database.")
            
# --- NEW USER ACTIVITY FUNCTIONS ---

async def get_user_initial_count(chat_id: int, user_id: int) -> int:
    pool = await get_pool()
    if not pool:
        return 0
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT initial_count FROM user_activity WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id
        )
        return count if count is not None else 0

async def increment_user_initial_count(chat_id: int, user_id: int, max_count: int):
    pool = await get_pool()
    if not pool:
        return
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
