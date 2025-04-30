# --- File: config.py ---

import os
import logging
from dotenv import load_dotenv # Import load_dotenv

# --- Load Environment Variables ---
# Load variables from a .env file if it exists
# Place this near the top, before accessing environment variables
load_dotenv()
logging.info("Attempted to load environment variables from .env file.")

# --- Critical Bot Credentials (Fetch from Environment Variables) ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
API_ID_STR = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')

# --- Basic Validation and Type Conversion ---
API_ID = None
if API_ID_STR:
    if API_ID_STR.isdigit():
        API_ID = int(API_ID_STR)
    else:
        # Log error but rely on check_critical_config to potentially stop execution
        logging.error("API_ID environment variable is present but not an integer.")
# No need for explicit "missing" log here, check_critical_config covers it.

# --- Telegram Specific ---
TELEGRAM_ID = 777000  # Official Telegram Service Notifications ID

# --- File Handling & Directories ---
# Supported archive types for session file uploads
ARCHIVE_EXTENSIONS = ('.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz')
# Database file path
DATABASE_FILE = 'session_bot.db'
# Temporary directory for downloads (relative to the main script location)
TEMP_DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
# Base directory for temporary extraction folders
TEMP_EXTRACT_DIR_BASE = os.path.join(os.getcwd(), "temp_extract")

# --- Session Management ---
# Maximum number of user clients that can be actively listening for OTPs per user
MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER = 10
# Duration (in seconds) a session stays active listening for OTPs before auto-stopping
SESSION_LISTEN_DURATION = 30 * 60  # 30 minutes

# --- UI Configuration ---
# Number of sessions displayed per page in the /manage_sessions command
SESSIONS_PER_PAGE = 10

# --- Background Task Configuration ---
# How often (in seconds) the background cleanup task runs
BACKGROUND_CLEANUP_INTERVAL_SECONDS = 2 * 60 * 60  # Run cleanup every 2 hours
# How old (in seconds) temporary files/folders must be before cleanup removes them
BACKGROUND_CLEANUP_AGE_THRESHOLD_SECONDS = 1 * 60 * 60 # Delete items older than 1 hour

# --- Internal Behavior / Retry Configuration ---
# Number of retries for cleaning up temporary files if initial attempt fails (e.g., due to locks)
CLEANUP_RETRY_COUNT = 3
# Delay (in seconds) between cleanup retries
CLEANUP_RETRY_DELAY_SECONDS = 1.5 


# --- Post-Load Check for Critical Variables ---
# This function is essential as it's checked in main.py before starting the bot.
def check_critical_config():
    """Checks if essential configuration variables are loaded."""
    critical_vars = {'BOT_TOKEN': BOT_TOKEN, 'API_ID': API_ID, 'API_HASH': API_HASH}
    missing = [name for name, value in critical_vars.items() if value is None]
    if missing:
        # Changed message slightly to emphasize .env file
        logging.critical(f"CRITICAL Missing Configuration Variables in environment or .env file: {', '.join(missing)}. Exiting.")
        return False
    # Check if API_ID failed conversion
    if API_ID is None and API_ID_STR is not None:
         logging.critical("CRITICAL: API_ID is set but is not a valid integer. Exiting.")
         return False
    return True

# Example of how to use the check in main.py remains the same:
# import config
# if not config.check_critical_config():
#     exit(1)