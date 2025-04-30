# --- File: database.py ---

import sqlite3
import logging

# Import the database file path from the configuration
from config import DATABASE_FILE

def initialize_database():
    """
    Creates or updates the database schema. Ensures the 'users' and 'user_sessions'
    tables exist with the correct structure and indexes.
    Raises sqlite3.Error if initialization fails critically.
    """
    try:
        # The 'with' statement ensures the connection is closed and transactions are committed/rolled back.
        with sqlite3.connect(DATABASE_FILE) as conn:
            cursor = conn.cursor()

            # --- users Table ---
            # Stores basic user info, primarily the last recorded operational error.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    last_error TEXT DEFAULT NULL
                )
            ''')

            # --- user_sessions Table ---
            # Stores the actual session strings provided by users, linked to their ID.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    session_name TEXT NOT NULL COLLATE NOCASE, -- Make name case-insensitive for uniqueness
                    session_string TEXT NOT NULL,
                    added_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id),
                    UNIQUE(user_id, session_name) -- Enforces unique session names per user
                )
            ''')

            # --- Indexes ---
            # Improve query performance, especially for lookups by user_id and session_name.
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id)")

            conn.commit() # Commit changes if everything succeeded
            logging.info(f"Database '{DATABASE_FILE}' initialized/verified successfully.")

    except sqlite3.Error as e:
        logging.error(f"CRITICAL: Database initialization failed for '{DATABASE_FILE}': {e}")
        # Re-raise the exception so the calling code (main.py) knows initialization failed.
        raise


def get_user_last_error(user_id: int) -> str | None:
    """
    Retrieves the last recorded error message for a given user ID.
    Ensures the user exists in the 'users' table first.

    Args:
        user_id: The Telegram user ID.

    Returns:
        The error message string if found, otherwise None.
        Returns a generic "DB Error: ..." message if a database error occurs during retrieval.
    """
    try:
        with sqlite3.connect(DATABASE_FILE) as conn:
            cursor = conn.cursor()
            # Ensure user record exists (INSERT OR IGNORE does nothing if user_id is already present)
            cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            # Fetch the last error
            cursor.execute("SELECT last_error FROM users WHERE user_id = ?", (user_id,))
            data = cursor.fetchone()
            # data will be None if the user wasn't in the DB *before* the IGNORE,
            # or it will be a tuple (error_string_or_None,)
            return data[0] if data else None
    except sqlite3.Error as e:
        logging.error(f"DB get_user_last_error failed for user {user_id}: {e}")
        # Indicate a DB retrieval problem to the caller
        return f"DB Error: Failed to retrieve last error."


def set_user_last_error(user_id: int, error_message: str | None):
    """
    Sets or clears the last error message for a given user ID.
    Ensures the user exists in the 'users' table first.

    Args:
        user_id: The Telegram user ID.
        error_message: The error string to store, or None to clear the error.
    """
    try:
        with sqlite3.connect(DATABASE_FILE) as conn:
            cursor = conn.cursor()
            # Ensure user record exists
            cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            # Update the last error. Using `None` for error_message will store NULL.
            cursor.execute("UPDATE users SET last_error = ? WHERE user_id = ?", (error_message, user_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"DB set_user_last_error failed for user {user_id}: {e}")


def add_user_session(user_id: int, session_name: str, session_string: str) -> bool:
    """
    Adds a new user session string to the database.

    Args:
        user_id: The Telegram user ID.
        session_name: The name chosen by the user for this session.
        session_string: The actual Telethon/Pyrogram session string.

    Returns:
        True if the session was successfully added, False if a session with the
        same name already exists for this user or if a database error occurred.
    """
    try:
        with sqlite3.connect(DATABASE_FILE) as conn:
            cursor = conn.cursor()
            # Ensure user exists in the users table first
            cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            # Attempt to insert the new session.
            # INSERT OR IGNORE will do nothing if the UNIQUE constraint (user_id, session_name) is violated.
            cursor.execute(
                "INSERT OR IGNORE INTO user_sessions (user_id, session_name, session_string) VALUES (?, ?, ?)",
                (user_id, session_name, session_string)
            )
            conn.commit()
            # cursor.rowcount > 0 indicates that a row was actually inserted.
            # It will be 0 if the session_name was a duplicate for that user_id.
            if cursor.rowcount > 0:
                logging.info(f"[DB] Added session '{session_name}' for user {user_id}.")
                return True
            else:
                # Check if it already exists to differentiate duplicate vs error
                cursor.execute("SELECT 1 FROM user_sessions WHERE user_id = ? AND session_name = ?", (user_id, session_name))
                if cursor.fetchone():
                    logging.warning(f"[DB] Attempted to add duplicate session '{session_name}' for user {user_id}.")
                else:
                    # This case is less likely but could happen with race conditions or other errors
                    logging.error(f"[DB] Failed to add session '{session_name}' for user {user_id}, rowcount was 0 but session not found after insert.")
                return False # Indicate failure (either duplicate or other error)
    except sqlite3.Error as e:
        logging.error(f"DB add_user_session failed for user {user_id}, name '{session_name}': {e}")
        return False # Indicate failure


def get_user_sessions(user_id: int) -> list[str]:
    """
    Retrieves a list of all stored session names for a specific user.

    Args:
        user_id: The Telegram user ID.

    Returns:
        A list of session names (strings), ordered case-insensitively.
        Returns an empty list if the user has no sessions or an error occurs.
    """
    sessions = []
    try:
        with sqlite3.connect(DATABASE_FILE) as conn:
            # Read-only operations don't strictly need a cursor if results are fetched directly,
            # but using one is standard practice.
            cursor = conn.cursor()
            # Retrieve only the names, ordered for consistent display
            cursor.execute("SELECT session_name FROM user_sessions WHERE user_id = ? ORDER BY session_name COLLATE NOCASE", (user_id,))
            # fetchall() returns a list of tuples, e.g., [('session1',), ('session2',)]
            # We use a list comprehension to extract the first element (the name) from each tuple.
            sessions = [row[0] for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"DB get_user_sessions failed for user {user_id}: {e}")
        # Return empty list on error to prevent crashes in calling code expecting a list
        sessions = []
    return sessions


def get_session_string(user_id: int, session_name: str) -> str | None:
    """
    Retrieves the specific session string for a user and session name.

    Args:
        user_id: The Telegram user ID.
        session_name: The name of the session to retrieve.

    Returns:
        The session string if found, otherwise None. Also returns None on error.
    """
    try:
        with sqlite3.connect(DATABASE_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT session_string FROM user_sessions WHERE user_id = ? AND session_name = ?", (user_id, session_name))
            data = cursor.fetchone() # fetchone() returns None if no row matches, or a tuple (string,)
            return data[0] if data else None
    except sqlite3.Error as e:
        logging.error(f"DB get_session_string failed for user {user_id}, name '{session_name}': {e}")
        return None # Indicate failure


def delete_user_session_record(user_id: int, session_name: str) -> bool:
    """
    Permanently deletes a session record from the user_sessions table.
    Logs the action.

    Args:
        user_id: The Telegram user ID.
        session_name: The name of the session to delete.

    Returns:
        True if a session record was successfully deleted, False otherwise
        (e.g., session not found, or DB error).
    """
    success = False
    try:
        with sqlite3.connect(DATABASE_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_sessions WHERE user_id = ? AND session_name = ?",
                (user_id, session_name)
            )
            conn.commit()
            # Check if a row was actually deleted.
            success = cursor.rowcount > 0
            if success:
                logging.info(f"[DB] Permanently deleted session record '{session_name}' for user {user_id}.")
            else:
                logging.warning(f"[DB] Attempted to delete session record '{session_name}' for user {user_id}, but it was not found (or already deleted).")
    except sqlite3.Error as e:
        logging.error(f"DB error permanently deleting session '{session_name}' for user {user_id}: {e}")
        success = False # Ensure false is returned on error
    return success