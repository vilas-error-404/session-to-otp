# --- File: utils.py ---

import os
import time
import re
import logging
import shutil # Needed for file operations

# --- Dependency Check & Import: Patoolib ---
# Placed here as archive extraction is a utility function.
try:
    import patoolib
    from patoolib.util import PatoolError # Specific exception
except ImportError:
    logging.warning("Utility module initialized, but 'patoolib' is not installed. Archive extraction will fail.")
    patoolib = None # Set to None to allow conditional checks later
    PatoolError = None # Define as None type

# --- Internal Imports ---
from config import TEMP_EXTRACT_DIR_BASE # Import configuration for default paths


def map_error_to_user_message(error_msg_technical: str | None) -> str:
    """
    Translates technical error strings (often from exceptions or internal codes)
    into more user-friendly messages suitable for displaying in Telegram alerts or messages.

    Args:
        error_msg_technical: The technical error string, potentially containing exception names
                           or specific codes. Can be None.

    Returns:
        A user-friendly string describing the error. Returns "Unknown Error" if input is None or empty.
    """
    if not error_msg_technical:
        return "Unknown Error (No details provided)"

    if "AUTH_KEY_DUPLICATED" in error_msg_technical or \
       "AuthKeyDuplicated" in error_msg_technical:
        # Specific error indicating session potentially active elsewhere
        return "❗️ Auth Key Used Elsewhere (Simultaneous Use Detected)." 

    # --- Critical Session/Account Errors ---
    if "UserDeactivated" in error_msg_technical or \
       "AuthKeyUnregistered" in error_msg_technical or \
       "AuthKeyInvalid" in error_msg_technical or \
       "AuthKeyError" in error_msg_technical:
        # These indicate the session is fundamentally broken or the account is gone.
        return "❗️ Session Invalid / Account Deactivated."

    if "UserBlocked" in error_msg_technical or \
       "UserIsBlocked" in error_msg_technical:
        # User banned by Telegram.
        return "❗️ Account Blocked by Telegram."

    # --- Authentication / Authorization Issues ---
    if "SESSION_PASSWORD_NEEDED" in error_msg_technical or \
       "2FA Required" in error_msg_technical:
        # User has Two-Factor Authentication enabled. Bot can't proceed without password.
        # Treat as warning for adding, error for listening.
        return "🔐 2FA Enabled (Password Needed)"

    if "PHONE_CODE_INVALID" in error_msg_technical:
        # An incorrect login code was provided (less common with session strings).
        return "🔑 Invalid Login Code."

    if "Not Authorized" in error_msg_technical: # Catches "TL Not Authorized", "PG Not Authorized"?
         # Generic authorization failure (might be before 2FA check, or other reasons).
         return "❗️ Authorization Failed (Access Denied)."

    if "Auth Failed (get_me)" in error_msg_technical: # e.g., "PG Auth Failed (get_me)"
        # Connected successfully, but couldn't verify the user account details. Rare.
        return "❗️ Auth Verification Failed."

    # --- Temporary / Operational Errors ---
    if "FLOOD_WAIT" in error_msg_technical or \
       "FloodWait" in error_msg_technical or \
       "Wait" in error_msg_technical: # Catch variations like "PG Start Wait"
        # Rate limited by Telegram API.
        match = re.search(r'(\d+)', error_msg_technical) # Try to extract wait time
        wait_time_str = f" for {match.group(1)}s" if match else ""
        return f"⏳ Telegram Flood Wait{wait_time_str}. Please try again later."

    if "Timeout" in error_msg_technical:
         # Operation took too long.
         return "⏳ Operation Timed Out. Please try again later."

    if "DbError (Locked)" in error_msg_technical or "database is locked" in error_msg_technical.lower():
         # SQLite database contention (should be less frequent with proper structure).
         return "⏳ Database Busy. Please try again shortly."

    # --- Format/Compatibility Errors ---
    if "Session Format" in error_msg_technical: # e.g., "TL Session Format Error", "PG DbError (Maybe Telethon Session?)"
         return "⚠️ Incompatible Session Format (Try adding again)."

    if "File Not Found" in error_msg_technical: # Specific error from session test
        return "❌ Internal Error: Session file disappeared before testing."

    if "Temp File Error" in error_msg_technical: # Specific error from session test
         return "❌ Internal Error: Failed to create temporary file for testing."

    # --- Archive Extraction Errors ---
    if "Extraction Err:" in error_msg_technical or "Extract Fail" in error_msg_technical:
         # Grab the core part of the patoolib error if possible
         details = error_msg_technical.split(':', 1)[-1].strip()
         return f"❌ Archive Extraction Failed: {details[:60]}" # Limit length

    if "Archive library (patoolib) missing" in error_msg_technical:
        return "❌ Archive processing library (patoolib) is missing on the server."

    if "No .session files found in archive" in error_msg_technical: # Handle this specific message
        return "ℹ️ No `.session` files found inside the archive."

    # --- Fallback ---
    # Try to extract a meaningful part if none of the above matched.
    # Remove common prefixes/suffixes.
    fallback_msg = error_msg_technical.split('(')[0].replace('Error', '').strip()
    if not fallback_msg or len(fallback_msg) > 60: # If too generic or too long, use a simpler message
         fallback_msg = "An unexpected issue occurred"

    logging.debug(f"Mapping technical error '{error_msg_technical}' to user message '{fallback_msg}'")
    return f"❗️ Error: {fallback_msg}." # Keep it relatively concise


def extract_archive_to_temp(archive_file_path: str, user_id: int) -> tuple[list[str], list[str], str | None, str | None]:
    """
    Extracts an archive file to a unique temporary directory for the user.
    Scans the extracted contents for `.session` files.

    Args:
        archive_file_path: Path to the downloaded archive file.
        user_id: The user ID, used for creating a unique temporary directory name.

    Returns:
        tuple:
            - extracted_session_files (list[str]): A list of absolute paths to the found `.session` files
                                                   within the temporary extraction directory.
            - skipped_files (list[str]): A list of filenames (not paths) that were in the archive
                                         but were not `.session` files (optional usage).
            - error_msg (str | None): A technical error message string if extraction failed, otherwise None.
                                      This string is suitable for logging or passing to map_error_to_user_message.
            - temp_extraction_dir_path (str | None): The absolute path to the temporary directory created for
                                                     this extraction, or None if creation failed. Caller should
                                                     clean this up after processing contents (unless error occurred).
    """
    # Create a unique temporary directory path for this specific extraction operation
    # e.g., /path/to/temp_extract/1234567_1678886400
    temp_user_extract_path = os.path.join(TEMP_EXTRACT_DIR_BASE, f'{user_id}_{int(time.time())}')

    extracted_session_files = []
    skipped_files = []
    error_msg = None # Technical error message for return

    # --- Check if patoolib is available ---
    if patoolib is None or PatoolError is None:
        logging.error(f"[User:{user_id}] Archive extraction skipped: patoolib is not installed or loaded.")
        return [], [], "Archive library (patoolib) missing", None # <<< Added None for dir path

    try:
        logging.info(f"[User:{user_id}] Starting extraction of '{os.path.basename(archive_file_path)}' to temporary path: {temp_user_extract_path}")

        # Ensure the base extraction directory exists, then create the user-specific temp dir
        os.makedirs(TEMP_EXTRACT_DIR_BASE, exist_ok=True) # Ensure parent exists
        os.makedirs(temp_user_extract_path, exist_ok=True) # Create unique subdir

        # --- Perform Extraction ---
        # verbosity=0 silences most patoolib output, adjust if needed for debugging
        patoolib.extract_archive(archive_file_path, outdir=temp_user_extract_path, verbosity=-1)
        logging.info(f"[User:{user_id}] Archive extraction completed to {temp_user_extract_path}.")

        # --- Scan for .session Files ---
        found_session = False
        logging.debug(f"[User:{user_id}] Scanning extracted path {temp_user_extract_path} for session files...")
        for root, _, files in os.walk(temp_user_extract_path):
            for file in files:
                if file.lower().endswith('.session'):
                    # Found a session file
                    found_session = True
                    full_path = os.path.join(root, file)
                    extracted_session_files.append(full_path)
                    logging.debug(f"[User:{user_id}] Found session file: {full_path}")
                else:
                    # Track non-session files found (optional)
                    skipped_files.append(file)

        if not found_session:
            # Report that no session files were found.
            logging.warning(f"[User:{user_id}] No .session files found in the extracted archive contents at {temp_user_extract_path}.")
            # Use a specific indicator, but don't treat as critical error
            error_msg = "No .session files found in archive"

    except PatoolError as e:
        # Specific error from the extraction library
        error_msg = f"Extraction Err: {e}"
        logging.error(f"[User:{user_id}] PatoolError during extraction of '{os.path.basename(archive_file_path)}': {e}")
        # Attempt to clean up partially extracted files IF an error occurred
        if os.path.isdir(temp_user_extract_path):
             shutil.rmtree(temp_user_extract_path, ignore_errors=True)
        temp_user_extract_path = None # Set path to None as it should be gone or unusable

    except Exception as e:
        # Catch any other unexpected exceptions during makedirs, walk, etc.
        error_msg = f"Extract Fail ({type(e).__name__})"
        logging.exception(f"[User:{user_id}] Unexpected error during archive extraction process for '{os.path.basename(archive_file_path)}':")
        # Attempt cleanup on general exception too
        if os.path.isdir(temp_user_extract_path):
            shutil.rmtree(temp_user_extract_path, ignore_errors=True)
        temp_user_extract_path = None # Set path to None

    # Note: The caller (e.g., handle_archive_upload) is now responsible for cleaning up
    # the `temp_user_extract_path` AFTER processing the session files found within it,
    # *unless* an error occurred during this function (in which case cleanup was attempted here
    # and temp_user_extract_path is None). The original downloaded archive file remains
    # the caller's responsibility to clean up regardless.

    # Log final counts before returning
    log_level = logging.INFO if not error_msg or error_msg == "No .session files found in archive" else logging.WARNING
    logging.log(log_level, f"[User:{user_id}] Extraction function finished. Found sessions: {len(extracted_session_files)}, Skipped non-sessions: {len(skipped_files)}, Error: {error_msg}, Temp Dir: {temp_user_extract_path}")

    # <<< Updated return statement: Add temp_user_extract_path at the end
    return extracted_session_files, skipped_files, error_msg, temp_user_extract_path