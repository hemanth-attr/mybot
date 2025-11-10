import asyncio
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
            db_pool = await asyncpg.create_pool(DATABASE_URL)
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
                    ml_mode BOOLEAN DEFAULT FALSE
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
            logging.info("Database tables verified/created.")
        except Exception as e:
            logging.error(f"Error setting up database tables: {e}")

async def get_chat_settings(chat_id: int) -> dict:
    """Fetches the settings for a specific chat."""
    pool = await get_pool()
    if not pool:
        return {"strict_mode": False, "ml_mode": False} # Default on error

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT strict_mode, ml_mode FROM chat_settings WHERE chat_id = $1",
            chat_id
        )
        if row:
            return {"strict_mode": row['strict_mode'], "ml_mode": row['ml_mode']}
        
        # If no settings exist, create them
        await conn.execute(
            """
            INSERT INTO chat_settings (chat_id, strict_mode, ml_mode)
            VALUES ($1, FALSE, FALSE)
            ON CONFLICT (chat_id) DO NOTHING
            """,
            chat_id
        )
        return {"strict_mode": False, "ml_mode": False}

async def set_chat_setting(chat_id: int, setting_name: str, value: bool):
    """Updates a specific setting (strict_mode or ml_mode) for a chat."""
    pool = await get_pool()
    if not pool or setting_name not in ('strict_mode', 'ml_mode'):
        return

    # Using f-string here is SAFE because the variable is checked against a
    # hardcoded list ('strict_mode', 'ml_mode') and is not user input.
    query = f"""
        INSERT INTO chat_settings (chat_id, {setting_name})
        VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE
        SET {setting_name} = $2
    """
    async with pool.acquire() as conn:
        await conn.execute(query, chat_id, value)
        
async def add_warning_async(chat_id: int, user_id: int) -> tuple[int, datetime]:
    """
    Adds a warning to a user, or updates their existing warning.
    This entire function is a single, safe database transaction.
    It completely replaces your old add_warning_async function.
    """
    pool = await get_pool()
    if not pool:
        raise Exception("Database pool is not available.")

    new_expiry = datetime.now() + timedelta(days=1)
    
    async with pool.acquire() as conn:
        # This query does everything in one step:
        # 1. Tries to INSERT a new warning (1 count).
        # 2. If the (chat_id, user_id) already exists, it hits a CONFLICT.
        # 3. ON CONFLICT, it runs the UPDATE instead, incrementing the count.
        # 4. It returns the new count and expiry time.
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
    """Clears a user's warnings. Replaces your old function."""
    pool = await get_pool()
    if not pool:
        return

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM warnings WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id
        )

async def clean_expired_warnings_async():
    """Cleans up warnings. Replaces your old function."""
    pool = await get_pool()
    if not pool:
        return

    async with pool.acquire() as conn:
        now = datetime.now()
        result = await conn.execute(
            "DELETE FROM warnings WHERE expiry < $1",
            now
        )
        # result is a string like 'DELETE 5'
        count = int(result.split(' ')[-1])
        if count > 0:
            logging.info(f"Cleaned {count} expired warnings from database.")
