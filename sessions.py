# --- File: sessions.py ---

import os
import time
import re
import asyncio
import tempfile
import logging
import shutil
from collections import defaultdict
from contextlib import suppress
from functools import partial
import sqlite3 # Required for specific DB exceptions handled during test/start

# --- Pyrogram Imports ---
try:
    from pyrogram import Client, filters
    from pyrogram.handlers import MessageHandler
    # Import Pyrogram exceptions individually for clarity
    from pyrogram.errors import (
        AuthKeyUnregistered, UserDeactivated, AuthKeyInvalid, UserBlocked,
        FloodWait as PyroFloodWait, UserIsBlocked, InputUserDeactivated
    )
except ImportError:
    logging.error("Pyrogram dependency missing in sessions.py (should have been checked in main)")
    # Or raise error / sys.exit depending on how main handles it
    Client = None # Define as None to potentially avoid further NameErrors if import fails

# --- Telethon Imports ---
try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    # Import Telethon exceptions individually for clarity
    from telethon.errors import (
        SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError as TeleFloodWait,
        AuthKeyError, UserDeactivatedError, UserBlockedError,
        RpcCallFailError
    )
except ImportError:
    logging.error("Telethon dependency missing in sessions.py (should have been checked in main)")
    TelegramClient = None # Define as None

# --- Internal Imports ---
from config import ( API_ID, API_HASH, TELEGRAM_ID, MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER,
                     SESSION_LISTEN_DURATION, TEMP_EXTRACT_DIR_BASE, CLEANUP_RETRY_COUNT,
                     CLEANUP_RETRY_DELAY_SECONDS )
from database import set_user_last_error # Assuming other DB ops aren't needed here
from utils import map_error_to_user_message


# --- Global State for Active Sessions ---
# Stores active client objects, indexed by user_id and session_name_base
# Structure: { user_id: { session_name_base: {'client': <ClientObj>, 'library': 'pg'/'tl', 'phone': '...', 'auto_stop_task': <Task>} } }
active_sessions = defaultdict(dict)

# Lock to protect access to the `active_sessions` dictionary from concurrent tasks
sessions_lock = asyncio.Lock()

# Semaphore to limit the number of concurrent session *tests* or *starts*
# Prevents overwhelming the Telegram API during bulk operations or rapid clicks.
session_start_semaphore = asyncio.Semaphore(3)


async def test_and_extract_session(session_file_path, session_name_base, user_id):
    """
    Tests session file (prioritizing Pyrogram V2), extracts string if valid.
    Cleans up the temporary session_file_path provided reliably.
    Returns: (is_valid_for_storage: bool, session_string: str | None, user_error_message: str | None)
    """
    logging.info(f"[User:{user_id}] Testing/Extracting session '{session_name_base}' from: {session_file_path}")
    session_string = None
    error_message = None
    client_used = None # 'pg' or 'tl'
    pyro_test_client = None
    thon_test_client = None
    temp_pyro_session_dir = None # Keep track of temp dir for cleanup
    temp_thon_session_path = None # Keep track of temp file for Telethon

    # Ensure the input path exists before proceeding
    if not os.path.exists(session_file_path):
         logging.error(f"[User:{user_id}] Input session file does not exist: {session_file_path}")
         return False, None, "File Not Found"

    async with session_start_semaphore:
        # --- Try Pyrogram (Assume V2 session file) ---
        try:
            temp_pyro_session_dir = tempfile.mkdtemp(prefix=f"pyro_test_{user_id}_{session_name_base}_", dir=TEMP_EXTRACT_DIR_BASE)
            # Use session_name_base for the client name within the temp dir
            pyro_client_name_rel = os.path.join(temp_pyro_session_dir, session_name_base)
            pyro_session_file_dest = f"{pyro_client_name_rel}.session"

            shutil.copy2(session_file_path, pyro_session_file_dest)
            logging.debug(f"[User:{user_id}] Copied session file to Pyrogram temp path: {pyro_session_file_dest}")

            pyro_test_client = Client(
                name=pyro_client_name_rel, # Name points to the file in the temp dir
                api_id=API_ID,
                api_hash=API_HASH,
                # no workdir needed, name includes path
            )

            logging.debug(f"[User:{user_id}] Attempting Pyrogram connect for '{session_name_base}' using temp file.")
            # Add timeout for connect operation
            await asyncio.wait_for(pyro_test_client.connect(), timeout=30.0)

            me = await pyro_test_client.get_me()
            if me:
                logging.info(f"[User:{user_id}] Pyrogram connected successfully for '{session_name_base}'. Exporting string.")
                session_string = await pyro_test_client.export_session_string()
                client_used = 'pg'
                await pyro_test_client.disconnect() # Disconnect after successful operation
            else:
                error_message = "PG Auth Failed (get_me after connect)"
                logging.warning(f"[User:{user_id}] Pyrogram connect OK, but get_me failed for '{session_name_base}'.")
                await pyro_test_client.disconnect() # Disconnect after failed get_me

        except (AuthKeyUnregistered, UserDeactivated, AuthKeyInvalid, UserBlocked) as e:
            error_message = f"PG Invalid/Blocked ({type(e).__name__})"
            logging.warning(f"[User:{user_id}] Pyrogram test failed for '{session_name_base}': {error_message}")
        except (PyroFloodWait, asyncio.TimeoutError) as e:
            error_message = f"PG Test Wait ({type(e).__name__})"
            logging.warning(f"[User:{user_id}] Pyrogram test failed for '{session_name_base}': {error_message}")
        except sqlite3.OperationalError as e:
             # Check if it's the 'database is locked' error, which might happen despite temp dirs
             if "database is locked" in str(e).lower():
                  error_message = f"PG DbError (Locked)"
             else:
                  error_message = f"PG DbError ({e})"
             logging.warning(f"[User:{user_id}] Pyrogram test DB error for '{session_name_base}': {e}")
        except Exception as e:
            error_message = f"PG Test Error ({type(e).__name__})"
            logging.warning(f"[User:{user_id}] Unexpected Pyrogram test error for '{session_name_base}': {e}", exc_info=False)
        finally:
            # --- AGGRESSIVE PYROGRAM CLEANUP ---
            client_disconnected = False
            if pyro_test_client:
                 if pyro_test_client.is_connected:
                      logging.debug(f"[User:{user_id}] Disconnecting Pyrogram client '{session_name_base}' before cleanup.")
                      with suppress(Exception):
                           await pyro_test_client.disconnect() # disconnect is safer than stop if only connected
                      client_disconnected = True
                 # Explicitly clear reference *before* GC
                 pyro_test_client = None
                 # Explicitly trigger Garbage Collection
                 try:
                      import gc
                      gc.collect()
                      logging.debug(f"[User:{user_id}] Explicit GC requested post-Pyrogram for '{session_name_base}'.")
                 except Exception as gc_err:
                      logging.error(f"[User:{user_id}] Error during GC call post-Pyrogram: {gc_err}")
                 # Wait after disconnect (if happened) and GC
                 if client_disconnected: # Only sleep if we actually disconnected
                     await asyncio.sleep(1.0) # Wait 1 second

            # Clean up the temporary Pyrogram directory with retries
            if temp_pyro_session_dir and os.path.isdir(temp_pyro_session_dir):
                 logging.debug(f"[User:{user_id}] Cleaning up Pyrogram temp directory: {temp_pyro_session_dir}")
                 for attempt in range(CLEANUP_RETRY_COUNT):
                      try:
                           # ignore_errors=False initially to catch specific errors
                           shutil.rmtree(temp_pyro_session_dir)
                           logging.debug(f"[User:{user_id}] Successfully cleaned Pyrogram temp directory: {temp_pyro_session_dir}")
                           break # Exit loop on success
                      except PermissionError as e: # Specific catch for locks
                           logging.warning(f"[User:{user_id}] Attempt {attempt + 1}/{CLEANUP_RETRY_COUNT} failed Pyrogram temp delete '{temp_pyro_session_dir}' (Locked): {e}")
                           if attempt < CLEANUP_RETRY_COUNT - 1:
                                await asyncio.sleep(max(CLEANUP_RETRY_DELAY_SECONDS, 1.5)) # Increased retry delay
                           else:
                                logging.error(f"[User:{user_id}] Failed Pyrogram temp delete '{temp_pyro_session_dir}' (Locked) after retries.")
                                # Optionally, try one last time with ignore_errors=True as a fallback?
                                # try:
                                #     shutil.rmtree(temp_pyro_session_dir, ignore_errors=True)
                                #     logging.warning(f"[User:{user_id}] Final Pyrogram rmtree attempt with ignore_errors=True for '{temp_pyro_session_dir}'.")
                                # except Exception: pass # Ignore final attempt errors
                      except OSError as e: # Catch other OS-level errors
                           logging.warning(f"[User:{user_id}] Attempt {attempt + 1}/{CLEANUP_RETRY_COUNT} failed Pyrogram temp delete '{temp_pyro_session_dir}' (OSError): {e}")
                           if attempt < CLEANUP_RETRY_COUNT - 1:
                                await asyncio.sleep(max(CLEANUP_RETRY_DELAY_SECONDS, 1.5))
                           else:
                                logging.error(f"[User:{user_id}] Failed Pyrogram temp delete '{temp_pyro_session_dir}' (OSError) after retries.")
                      except Exception as e_final: # Catch unexpected errors during rmtree
                           logging.error(f"[User:{user_id}] Unexpected error cleaning Pyrogram temp '{temp_pyro_session_dir}': {e_final}")
                           break # Don't retry on unexpected errors
            elif temp_pyro_session_dir:
                 logging.debug(f"[User:{user_id}] Pyrogram temp directory path set, but dir not found: {temp_pyro_session_dir}")

            # Ensure client reference is cleared even if GC block had issues
            pyro_test_client = None


        # --- Try Telethon *only if* Pyrogram Failed (and session string not found) ---
        if not session_string and TelegramClient:
            logging.info(f"[User:{user_id}] Pyrogram test failed ({error_message}), trying Telethon for '{session_name_base}'.")
            current_pg_error = error_message
            error_message = None # Reset error for Telethon attempt

            # --- Option 1: Keep using original path (as before) ---
            # thon_session_source = session_file_path
            # --- Option 2: Use a temporary copy for Telethon as well (Recommended for isolation) ---
            try:
                # Create a temporary file for Telethon to use
                fd, temp_thon_session_path = tempfile.mkstemp(suffix=".session", prefix=f"thon_test_{user_id}_{session_name_base}_", dir=TEMP_EXTRACT_DIR_BASE)
                os.close(fd) # We just need the path, mkstemp opens it
                shutil.copy2(session_file_path, temp_thon_session_path)
                logging.debug(f"[User:{user_id}] Copied session file to Telethon temp path: {temp_thon_session_path}")
                thon_session_source = temp_thon_session_path
            except Exception as copy_err:
                 logging.error(f"[User:{user_id}] Failed to create/copy temp file for Telethon: {copy_err}")
                 thon_session_source = None # Prevent Telethon from starting if copy fails
                 error_message = "TL Temp File Error"
            # --- End Option 2 ---

            if thon_session_source: # Only proceed if we have a path (original or temp copy)
                try:
                    thon_test_client = TelegramClient(
                        thon_session_source, # Use the temp path if created, otherwise original
                        api_id=API_ID,
                        api_hash=API_HASH,
                        # Set a connection timeout for Telethon
                        # Note: Telethon timeout handling might differ; check its docs
                        # connection_retries=1, # Limit retries
                        # timeout=20 # Example timeout setting
                    )
                    logging.debug(f"[User:{user_id}] Attempting Telethon connect for '{session_name_base}' using '{thon_session_source}'")
                    # Add timeout for connect
                    await asyncio.wait_for(thon_test_client.connect(), timeout=30.0)

                    if await thon_test_client.is_user_authorized():
                        me = await thon_test_client.get_me()
                        if me:
                           logging.info(f"[User:{user_id}] Telethon connected successfully for '{session_name_base}'. Saving session string.")
                           session_string = thon_test_client.session.save()
                           client_used = 'tl'
                        else:
                            error_message = "TL Auth Failed (get_me)"
                            logging.warning(f"[User:{user_id}] Telethon connect OK, but get_me failed for '{session_name_base}'.")
                        await thon_test_client.disconnect() # Disconnect after success/get_me fail
                    else:
                        error_message = "TL Not Authorized (Pre-2FA?)"
                        logging.warning(f"[User:{user_id}] Telethon connection successful but not authorized for '{session_name_base}'.")
                        await thon_test_client.disconnect() # Disconnect if not authorized

                except SessionPasswordNeededError:
                     error_message = "TL 2FA Required"
                     logging.info(f"[User:{user_id}] Session '{session_name_base}' is likely Telethon format and requires 2FA.")
                     # Attempt to export string even with 2FA, might contain partial info useful for some cases
                     if thon_test_client and thon_test_client.session:
                         try:
                             temp_session_string = thon_test_client.session.save()
                             if temp_session_string:
                                 session_string = temp_session_string # Store it if successful
                                 client_used = 'tl'
                                 error_message = None # Clear error IF we got the string
                                 logging.info(f"[User:{user_id}] Extracted Telethon session string despite 2FA requirement.")
                         except Exception as export_err:
                              logging.warning(f"[User:{user_id}] Failed to export Telethon session string during 2FA: {export_err}")
                     # Ensure disconnected even if export failed
                     if thon_test_client and thon_test_client.is_connected():
                         await thon_test_client.disconnect()

                except sqlite3.OperationalError as e:
                     # Check common Telethon errors indicating incompatible format (Pyrogram V2)
                     if "no such column: version" in str(e).lower() or \
                        "no such table: sessions" in str(e).lower() or \
                        "database is locked" in str(e).lower():
                         error_message = "TL Session Format/DB Error"
                         logging.warning(f"[User:{user_id}] Telethon failed for '{session_name_base}', likely incompatible format or DB issue: {e}")
                     else:
                          error_message = f"TL DbError ({e})"
                          logging.warning(f"[User:{user_id}] Telethon DB error for '{session_name_base}': {e}")
                     if thon_test_client and thon_test_client.is_connected(): await thon_test_client.disconnect()

                except (AuthKeyError, UserDeactivatedError, UserBlockedError, PhoneCodeInvalidError) as e:
                     error_message = f"TL Invalid/Blocked/Error ({type(e).__name__.replace('Error','')})"
                     logging.warning(f"[User:{user_id}] Telethon test failed for '{session_name_base}': {error_message}")
                     if thon_test_client and thon_test_client.is_connected(): await thon_test_client.disconnect()
                except (TeleFloodWait, asyncio.TimeoutError) as e:
                     error_message = f"TL Test Wait ({type(e).__name__})"
                     logging.warning(f"[User:{user_id}] Telethon test failed for '{session_name_base}': {error_message}")
                     if thon_test_client and thon_test_client.is_connected(): await thon_test_client.disconnect()
                except Exception as e:
                     error_message = f"TL Test Error ({type(e).__name__})"
                     logging.warning(f"[User:{user_id}] Unknown Telethon test error for '{session_name_base}': {e}", exc_info=False)
                     if thon_test_client and thon_test_client.is_connected():
                        with suppress(Exception): await thon_test_client.disconnect()
                finally:
                    # Ensure disconnection for Telethon
                    if thon_test_client and thon_test_client.is_connected():
                        logging.debug(f"[User:{user_id}] Disconnecting Telethon client for '{session_name_base}' before cleanup.")
                        with suppress(Exception):
                            await thon_test_client.disconnect()

                    # *** ADD EXPLICIT GC + LONGER DELAY ***
                    thon_test_client = None # Ensure reference is removed *before* GC
                    import gc # Import the garbage collector module
                    gc.collect() # Suggest garbage collection run now
                    logging.debug(f"[User:{user_id}] Explicit GC requested after Telethon disconnect for '{session_name_base}'.")
                    await asyncio.sleep(1.0) # Increase delay slightly to 1 second

                    # Clean up the temporary Telethon file *if it was created*
                    if temp_thon_session_path and os.path.exists(temp_thon_session_path):
                        logging.debug(f"[User:{user_id}] Cleaning up Telethon temp file: {temp_thon_session_path}")
                        # Retry mechanism
                        for attempt in range(CLEANUP_RETRY_COUNT):
                            try:
                                os.remove(temp_thon_session_path)
                                logging.debug(f"[User:{user_id}] Successfully cleaned up Telethon temp file: {temp_thon_session_path}")
                                break # Exit loop on success
                            except PermissionError as e:
                                logging.warning(f"[User:{user_id}] Attempt {attempt + 1}/{CLEANUP_RETRY_COUNT} failed to delete Telethon temp file '{temp_thon_session_path}' (Permission Error): {e}")
                                if attempt < CLEANUP_RETRY_COUNT - 1:
                                    # Use a potentially longer delay between retries now
                                    await asyncio.sleep(CLEANUP_RETRY_DELAY_SECONDS * 1.5) # e.g., if delay was 1, now 1.5
                                else:
                                    logging.error(f"[User:{user_id}] Failed to delete Telethon temp file '{temp_thon_session_path}' due to lock/permission after {CLEANUP_RETRY_COUNT} attempts.")
                            except OSError as e:
                                logging.warning(f"[User:{user_id}] Attempt {attempt + 1}/{CLEANUP_RETRY_COUNT} failed to delete Telethon temp file '{temp_thon_session_path}' (OSError): {e}")
                                if attempt < CLEANUP_RETRY_COUNT - 1:
                                    await asyncio.sleep(CLEANUP_RETRY_DELAY_SECONDS * 1.5)
                                else:
                                    logging.error(f"[User:{user_id}] Failed to delete Telethon temp file '{temp_thon_session_path}' due to OSError after {CLEANUP_RETRY_COUNT} attempts.")
                            except Exception as e_final:
                                logging.error(f"[User:{user_id}] Unexpected error cleaning up Telethon temp file '{temp_thon_session_path}': {e_final}")
                                break
                    # No need for thon_test_client = None here again, already done before GC.


    # --- Determine Outcome ---
    is_valid_for_storage = bool(session_string)
    user_friendly_error = None
    final_outcome_log = f"[User:{user_id}] Test/Extract Result for '{session_name_base}': ValidForStorage={is_valid_for_storage}, ExtractedVia={client_used or 'None'}"

    if not is_valid_for_storage:
        # If storage is not valid, we MUST have an error message. Prioritize existing, else set generic.
        if not error_message: error_message = "Validation Failed (No String)"
        user_friendly_error = map_error_to_user_message(error_message)
        final_outcome_log += f", FinalError='{error_message}' (UserMsg='{user_friendly_error}')"
        logging.warning(final_outcome_log)
    elif is_valid_for_storage:
        logging.info(final_outcome_log)
        # Handle specific case: TL 2FA where we got a string but user should know it's limited
        if client_used == 'tl' and map_error_to_user_message("TL 2FA Required") in (user_friendly_error or ""):
             user_friendly_error = "🔐 2FA Enabled (Stored, but can't listen for OTPs)."
        elif error_message: # An error might have occurred (like 2FA) but we still got a string
             user_friendly_error = map_error_to_user_message(error_message) # Use the original error context
             logging.info(f"[User:{user_id}] Session '{session_name_base}' stored, but encountered non-fatal issue: {error_message}")

    # --- RELIABLE CLEANUP of the INPUT file path ---
    logging.debug(f"[User:{user_id}] Attempting cleanup of input temp file: {session_file_path}")
    if session_file_path and os.path.exists(session_file_path):
        deleted = False
        for attempt in range(CLEANUP_RETRY_COUNT):
            try:
                os.remove(session_file_path)
                logging.debug(f"[User:{user_id}] Successfully cleaned up input temp file: {session_file_path}")
                deleted = True
                break # Exit loop on success
            except PermissionError as e: # Catch PermissionError explicitly (often WinError 32)
                 logging.warning(f"[User:{user_id}] Attempt {attempt + 1}/{CLEANUP_RETRY_COUNT} failed to delete '{session_file_path}' (Permission Error): {e}")
                 if attempt < CLEANUP_RETRY_COUNT - 1:
                     await asyncio.sleep(CLEANUP_RETRY_DELAY_SECONDS)
                 else:
                     logging.error(f"[User:{user_id}] Failed to delete input file '{session_file_path}' due to lock/permission after {CLEANUP_RETRY_COUNT} attempts.")
            except OSError as e: # Catch other OS errors during deletion
                 logging.warning(f"[User:{user_id}] Attempt {attempt + 1}/{CLEANUP_RETRY_COUNT} failed to delete '{session_file_path}' (OSError): {e}")
                 if attempt < CLEANUP_RETRY_COUNT - 1:
                     await asyncio.sleep(CLEANUP_RETRY_DELAY_SECONDS)
                 else:
                     logging.error(f"[User:{user_id}] Failed to delete input file '{session_file_path}' due to OSError after {CLEANUP_RETRY_COUNT} attempts.")
            except Exception as e: # Catch any other unexpected error
                logging.error(f"[User:{user_id}] Unexpected error during input file cleanup '{session_file_path}': {e}")
                break # Don't retry on unexpected errors
        if not deleted and os.path.exists(session_file_path):
             # This case means retries failed for known errors or an unexpected error occurred
             # The specific error was logged above. We might need manual intervention.
             pass # Error already logged

    elif session_file_path:
        logging.debug(f"[User:{user_id}] Input temp file already gone or path invalid: {session_file_path}")


    return is_valid_for_storage, session_string, user_friendly_error


# --- OTP Handling ---

async def process_login_code(bot_client: Client, event_or_message, client_type: str, user_id: int, session_display_name: str):
    """
    Parses incoming messages from Telegram's service account (ID 777000) for login codes.
    Forwards the found code to the initiating user via the main bot.

    Args:
        bot_client: The main Pyrogram bot client instance (used for sending messages).
        event_or_message: The Telethon event or Pyrogram message object.
        client_type: 'telethon' or 'pyrogram'.
        user_id: The Telegram ID of the user who started the session.
        session_display_name: The phone number or name associated with the active session.
    """
    text = ""
    sender_id = None

    # Extract text and sender ID based on client type
    if client_type == 'telethon' and hasattr(event_or_message, 'message'):
        text = getattr(event_or_message.message, 'message', "")
        sender_id = getattr(event_or_message, 'sender_id', None)
    elif client_type == 'pyrogram' and hasattr(event_or_message, 'text'):
        text = event_or_message.text or ""
        # Get sender ID from `from_user` if available, default to service ID otherwise
        sender_id = getattr(event_or_message.from_user, 'id', None) if event_or_message.from_user else None
        sender_id = sender_id or (TELEGRAM_ID if event_or_message.chat and event_or_message.chat.id == TELEGRAM_ID else None)

    # Check if message is from the official Telegram account
    if sender_id == TELEGRAM_ID:
        # Regex to find 5+ digit codes following common keywords
        # Handles variations in language and formatting.
        match = re.search(
            # Non-capturing group for keywords: (Login code|...)
            # Followed by optional characters (non-greedy): .*?
            # Capturing group for the code: (\d{5,})
            r'(?:Login code|Kod:|Code:|Telegram code|Код подтверждения|코드|码|验证码).*?(\d{5,})',
            text, re.IGNORECASE | re.DOTALL
        )
        if match:
            login_code = match.group(1)
            logging.info(f"[User: {user_id}] Received login code for session '{session_display_name}'")
            # Prepare message to forward to the user
            message_text = (
                f"📱 Session: **{session_display_name}**\n"
                f"🔑 Login code: `{login_code}`\n"
            )
            try:
                # Send the code using the main bot client
                await bot_client.send_message(chat_id=user_id, text=message_text)
                logging.info(f"[User: {user_id}] Forwarded login code for '{session_display_name}' successfully.")
            except (UserIsBlocked, InputUserDeactivated, UserBlockedError, UserDeactivatedError) as e:
                 logging.warning(f"[User: {user_id}] Failed to send OTP code: User blocked/deactivated? Session='{session_display_name}', Error: {e}")
                 set_user_last_error(user_id, f"OTP Forward Fail (Blocked/Deactivated)")
                 # Consider stopping the problematic session?
                 # await stop_session_logic(user_id, session_name_base, "OTP Forward Fail")
            except RpcCallFailError as e: # Catch specific Telethon RPC errors
                 logging.error(f"[User: {user_id}] RPC error sending OTP for session '{session_display_name}': {e}")
                 set_user_last_error(user_id, f"OTP Forward Fail (RPC Error)")
            except Exception as e:
                 # Catch other potential Pyrogram/Telethon errors during send_message
                 logging.exception(f"[User: {user_id}] Error forwarding login code message for '{session_display_name}':")
                 set_user_last_error(user_id, f"OTP Forward Fail ({type(e).__name__})")
        else:
             # Log messages from Telegram service that don't contain a recognized code pattern (e.g., login alerts)
             logging.debug(f"[User: {user_id}] Non-code message from Telegram Service (ID:{TELEGRAM_ID}) for session '{session_display_name}': '{text[:100]}...'")
    # Silently ignore messages not from the Telegram service account


# --- Handler Wrappers (to be used with partial) ---

# Note: bot_client isn't needed for the handlers themselves, but for process_login_code
async def thon_handler_wrapper(event, bot_client: Client, user_id_arg: int, phone_number_arg: str):
    """ Telethon event handler wrapper. Creates a task to process the code. """
    # Offload the processing to avoid blocking the event loop
    asyncio.create_task(process_login_code(bot_client, event, 'telethon', user_id_arg, phone_number_arg))

async def pyro_handler_wrapper(client: Client, message, bot_client: Client, user_id_arg: int, phone_number_arg: str):
    """ Pyrogram message handler wrapper. Filters for Telegram ID and creates task. """
    # Ensure the message is from the correct sender before processing
    # Check both from_user and chat.id for robustness
    sender_id = getattr(message.from_user, 'id', None) if message.from_user else None
    chat_id = getattr(message.chat, 'id', None)
    if sender_id == TELEGRAM_ID or chat_id == TELEGRAM_ID:
        asyncio.create_task(process_login_code(bot_client, message, 'pyrogram', user_id_arg, phone_number_arg))
    # No need to log ignored messages here, process_login_code handles non-code messages from 777000


# --- Session Activation / Deactivation ---

async def stop_session_after_delay(bot_client: Client, user_id: int, session_name_base: str, delay_seconds: int):
    """
    Waits for a specified duration and then triggers the stop logic for a session.
    Sends a notification to the user upon stopping.

    Args:
        bot_client: The main Pyrogram bot client for sending notifications.
        user_id: The user ID owning the session.
        session_name_base: The internal identifier name of the session to stop.
        delay_seconds: The time to wait before stopping.
    """
    try:
        await asyncio.sleep(delay_seconds)
        logging.info(f"[User: {user_id}] Auto-stop timer triggered for '{session_name_base}' after {delay_seconds}s.")

        # Acquire lock to prevent race conditions if user stops manually near the timer expiry
        async with sessions_lock:
             # Double-check if the session still exists before attempting to stop
             if session_name_base not in active_sessions.get(user_id, {}):
                 logging.info(f"[User: {user_id}] Auto-stop for '{session_name_base}' skipped: Session already removed/stopped.")
                 return # Session was already stopped or removed

        # Call the main stop logic (lock is acquired inside stop_session_logic again)
        # Pass the bot_client needed for the stop function itself
        success, name_stopped = await stop_session_logic(bot_client, user_id, session_name_base, initiated_by="Auto-Timer")

        if success:
            stop_message = f"⏱️ Session `{name_stopped}` automatically stopped listening after {SESSION_LISTEN_DURATION // 60} minutes."
            try:
                await bot_client.send_message(user_id, stop_message)
            except Exception as e:
                logging.error(f"[User: {user_id}] Error sending auto-stop notification for '{name_stopped}': {e}")
        else:
            # Log if the auto-stop function failed (e.g., client couldn't disconnect)
            logging.warning(f"[User: {user_id}] Auto-stop action failed for '{session_name_base}' (see stop_session_logic logs).")
            # Avoid bothering user if stop logic silently failed, already logged warning/error in stop_session_logic
            # Optional: Notify user about failure? Maybe too noisy.
            # with suppress(Exception):
            #    await bot_client.send_message(user_id, f"❗️ Auto-stop for session '{session_name_base}' encountered an issue.")

    except asyncio.CancelledError:
        logging.info(f"[User: {user_id}] Auto-stop timer for '{session_name_base}' was cancelled (likely manual stop).")
    except Exception as e:
        logging.error(f"[User: {user_id}] CRITICAL Error in auto-stop task for '{session_name_base}': {e}", exc_info=True)


async def start_session_client_temporary(bot_client: Client, session_string: str, session_name_base: str, user_id: int, duration_seconds: int) -> tuple[bool, str, str | None]:
    """
    Starts a user client (Pyrogram or Telethon) using a session string for a temporary duration.
    Adds OTP handlers and schedules an automatic stop task. Handles connection errors.

    Args:
        bot_client: The main Pyrogram bot client (needed for OTP forwarding and timers).
        session_string: The session string to use for authentication.
        session_name_base: The unique identifier for this session attempt.
        user_id: The Telegram ID of the user initiating the start.
        duration_seconds: How long the session should remain active.

    Returns:
        tuple:
            - success (bool): True if the session was successfully started and listening.
            - final_session_name (str): The display name (phone number or base name).
            - error_message (str | None): Technical error string if activation failed, else None.
    """
    # --- Pre-checks (Limits and Existing Session) ---
    async with sessions_lock:
        active_count = len(active_sessions.get(user_id, {}))
        if active_count >= MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER:
            logging.warning(f"[User:{user_id}] Start cancelled for '{session_name_base}': Max active session limit ({MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER}) reached.")
            return False, session_name_base, f"Limit Reached ({MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER})"

        # Check if this *exact* session_name_base is already active
        if session_name_base in active_sessions.get(user_id, {}):
            existing_data = active_sessions[user_id][session_name_base]
            existing_task = existing_data.get('auto_stop_task')

            logging.info(f"[User:{user_id}] Session '{session_name_base}' is already active. Resetting timer.")

            # Cancel the old timer if it's still running
            if existing_task and not existing_task.done():
                existing_task.cancel()
                with suppress(asyncio.CancelledError): # Wait briefly for cancellation
                    await asyncio.wait_for(existing_task, timeout=1.0)

            # Create and store the new timer task
            new_task = asyncio.create_task(stop_session_after_delay(bot_client, user_id, session_name_base, duration_seconds))
            existing_data['auto_stop_task'] = new_task
            logging.info(f"[User:{user_id}] Reset timer for '{session_name_base}' to {duration_seconds}s.")

            # Return success, indicating the session is active (timer was just reset)
            return True, existing_data.get('phone', session_name_base), None

    # --- Attempt Activation ---
    user_client = None
    final_phone_name = session_name_base # Default display name
    client_library = None # 'pg' or 'tl'
    activation_error = None # Store technical error message from activation attempt

    # Use semaphore to limit concurrent API interactions during start
    async with session_start_semaphore:
        # Generate a unique in-memory name for the Pyrogram client instance
        # Using time ensures uniqueness even if the same session_name_base is restarted quickly.
        memory_client_name = f":memory:{user_id}_{session_name_base}_{int(time.time())}"

        # --- 1. Try Pyrogram First ---
        if Client:
            try:
                # Instantiate Pyrogram client with the session string and in-memory storage
                user_client = Client(
                    name=memory_client_name, # Use the unique in-memory name
                    api_id=API_ID,
                    api_hash=API_HASH,
                    session_string=session_string,
                    in_memory=True # Important: Do not create files on disk
                )
                logging.debug(f"[User:{user_id}] PG Attempting start() for {session_name_base}")

                # Use start() for Pyrogram, it handles connection and auth checks
                # Set a reasonable timeout for the start() operation
                await asyncio.wait_for(user_client.start(), timeout=45.0)

                # If start() succeeds without exception, we are connected and authorized
                me = user_client.me
                if me:
                    final_phone_name = getattr(me, 'phone_number', None) or session_name_base
                    client_library = "pg"
                    logging.info(f"[User:{user_id}] PG start() successful for {session_name_base} (Phone: {final_phone_name})")
                else:
                    # Should be extremely rare if start() succeeded
                    activation_error = "PG Start OK but no 'me'?"
                    logging.critical(f"[User:{user_id}] PG start() successful but user_client.me is None for {session_name_base}!")
                    await user_client.stop() # Use stop() for Pyrogram
                    user_client = None # Mark as failed

            # --- Pyrogram Error Handling ---
            except (AuthKeyUnregistered, UserDeactivated, AuthKeyInvalid, UserBlocked) as e:
                activation_error = f"PG Auth Fail ({type(e).__name__})"
                logging.warning(f"[User:{user_id}] PG Activation Failed (Auth Error on start): {activation_error} for '{session_name_base}'")
                # No need to explicitly stop if start() failed with auth error
                user_client = None
            except sqlite3.OperationalError as e: # Should be less likely with in_memory=True
                activation_error = "PG DB Locked (Mem Err?)" if "locked" in str(e) else f"PG DB Error ({type(e).__name__})"
                logging.warning(f"[User:{user_id}] PG unexpected DB error (in_memory) on activation '{session_name_base}': {e}")
                if user_client: await user_client.stop() # Attempt cleanup if client obj exists
                user_client = None
            except (PyroFloodWait, asyncio.TimeoutError) as e:
                wait_time = getattr(e, 'value', 'N/A') if isinstance(e, PyroFloodWait) else 'N/A'
                error_type = type(e).__name__
                activation_error = f"PG Start Wait({wait_time}s)/Timeout({error_type})"
                logging.warning(f"[User:{user_id}] PG Activation Failed (Wait/Timeout): {activation_error} for '{session_name_base}'")
                if user_client: # Attempt stop if timeout happened after client creation but during start
                     with suppress(Exception): await user_client.stop()
                user_client = None
            except Exception as e:
                activation_error = f"PG Activation Error ({type(e).__name__})"
                logging.exception(f"[User:{user_id}] PG Unexpected Activation Err (start) for {session_name_base}:") # Log full trace
                if user_client: # Try stopping if an error occurred after object creation
                     with suppress(Exception): await user_client.stop()
                user_client = None

        # --- 2. Try Telethon if Pyrogram Failed (or not available) AND String doesn't seem PG-specific invalid ---
        # Define errors that definitely mean the session is dead/invalid for *both* libs
        critical_session_errors = ["Deactivated", "Unregistered", "Invalid", "Blocked", "AuthKey"]
        pg_error_is_critical = any(err_tag in (activation_error or "") for err_tag in critical_session_errors)

        if not client_library and TelegramClient and not pg_error_is_critical:
            logging.info(f"[User:{user_id}] Trying Telethon activation for '{session_name_base}' (PG Failed/Skipped, Error: {activation_error or 'None'}).")
            tl_activation_error = None # Use separate var for TL error msg
            user_client = None # Ensure user_client is reset before TL attempt

            try:
                # Use StringSession for Telethon activation
                thon_session = StringSession(session_string)
                user_client = TelegramClient(thon_session, api_id=API_ID, api_hash=API_HASH)

                logging.debug(f"[User:{user_id}] TL Attempt connect {session_name_base}")
                # Telethon uses connect/disconnect. Connect with a timeout.
                await asyncio.wait_for(user_client.connect(), timeout=45.0)

                if await user_client.is_user_authorized():
                    me = await user_client.get_me()
                    if me:
                       final_phone_name = getattr(me, 'phone', None) or session_name_base
                       client_library = "tl"
                       logging.info(f"[User:{user_id}] TL connect() successful for {session_name_base} (Phone: {final_phone_name})")
                    else:
                       tl_activation_error = "TL Auth OK but get_me failed?"
                       logging.critical(f"[User:{user_id}] TL connect successful but get_me failed for {session_name_base}!")
                       await user_client.disconnect(); user_client = None # Mark failed
                else:
                    # This likely means 2FA is needed, which we don't handle during activation for listening.
                    # String might be valid, but we can't listen without the password.
                    tl_activation_error = "TL Not Authorized (Maybe 2FA?)"
                    logging.warning(f"[User:{user_id}] TL Activation Failed: User not authorized for '{session_name_base}' (2FA password likely required). Cannot listen for OTPs.")
                    await user_client.disconnect(); user_client = None

            # --- Telethon Error Handling ---
            except SessionPasswordNeededError:
                 tl_activation_error = "TL 2FA Required"
                 logging.warning(f"[User:{user_id}] TL Activation Failed: {tl_activation_error} for '{session_name_base}'")
                 # No client to disconnect if this happens early
                 if user_client and user_client.is_connected(): await user_client.disconnect()
                 user_client = None
            except (AuthKeyError, UserDeactivatedError, UserBlockedError, PhoneCodeInvalidError) as e:
                 tl_activation_error = f"TL Auth Fail ({type(e).__name__.replace('Error','')})"
                 logging.warning(f"[User:{user_id}] TL Activation Failed (Auth): {tl_activation_error} for '{session_name_base}'")
                 if user_client and user_client.is_connected(): await user_client.disconnect()
                 user_client = None
            except (TeleFloodWait, asyncio.TimeoutError) as e:
                 wait_time = getattr(e, 'seconds', 'N/A') if isinstance(e, TeleFloodWait) else 'N/A'
                 error_type = type(e).__name__
                 tl_activation_error = f"TL Connect Wait({wait_time}s)/Timeout({error_type})"
                 logging.warning(f"[User:{user_id}] TL Activation Failed (Wait/Timeout): {tl_activation_error} for '{session_name_base}'")
                 if user_client and user_client.is_connected(): await user_client.disconnect()
                 user_client = None
            except Exception as e:
                 tl_activation_error = f"TL Activation Error ({type(e).__name__})"
                 logging.exception(f"[User:{user_id}] TL Unexpected Activation Err {session_name_base}:") # Log trace
                 if user_client and user_client.is_connected(): await user_client.disconnect()
                 user_client = None

            # Use the Telethon error if TL was tried and failed, otherwise stick with the Pyrogram error.
            if not client_library:
                 activation_error = tl_activation_error or activation_error

    # --- Post-Activation: Setup Handlers and Timer if Successful ---
    if client_library and user_client:
        logging.info(f"[User:{user_id}] Session activated via {client_library.upper()}. Adding handler for '{final_phone_name}' (Key: {session_name_base}).")
        handler_id = None # Store handler reference if needed for removal (though client obj holds it)

        try:
            if client_library == "pg":
                # Create partial function for the handler, passing necessary arguments.
                # Crucially, pass the main `bot_client` for message sending.
                handler = partial(pyro_handler_wrapper, bot_client=bot_client, user_id_arg=user_id, phone_number_arg=final_phone_name)
                # Register the handler for private messages from the Telegram service ID
                handler_id = user_client.add_handler(MessageHandler(handler, filters.private & filters.user(TELEGRAM_ID)))

            elif client_library == "tl":
                # Create partial function for the Telethon handler wrapper.
                # Pass the main `bot_client` here too.
                handler = partial(thon_handler_wrapper, bot_client=bot_client, user_id_arg=user_id, phone_number_arg=final_phone_name)
                # Need an async wrapper because Telethon event handlers expect an async function
                async def th_event_wrapper(event): await handler(event)
                # Register the handler for new messages from the service ID in the service ID chat
                user_client.add_event_handler(th_event_wrapper, events.NewMessage(from_users=TELEGRAM_ID, chats=TELEGRAM_ID))
                # Note: Telethon handlers don't return an easily usable ID like Pyrogram

            # Schedule the automatic stop task
            auto_stop_task = asyncio.create_task(stop_session_after_delay(bot_client, user_id, session_name_base, duration_seconds))

            # Store the active client and its metadata under lock
            async with sessions_lock:
                active_sessions[user_id][session_name_base] = {
                    'client': user_client,
                    'library': client_library,
                    'phone': final_phone_name,
                    'auto_stop_task': auto_stop_task
                    # Optionally store 'handler_id' if needed for Pyrogram handler removal
                }

            logging.info(f"[User:{user_id}] Successfully started listening on '{final_phone_name}' ({client_library.upper()}, Key:{session_name_base}) for {duration_seconds}s.")
            set_user_last_error(user_id, None) # Clear last error on successful start
            return True, final_phone_name, None # Success

        except Exception as handler_err:
             # Catch errors specifically during handler setup (less likely but possible)
             logging.error(f"[User:{user_id}] Error adding handler for {session_name_base} ({client_library}): {handler_err}", exc_info=True)
             activation_error = f"Handler Setup Error ({type(handler_err).__name__})"
             # Need to stop/disconnect the client if handler setup failed *after* successful activation
             logging.warning(f"[User:{user_id}] Stopping/disconnecting client {session_name_base} due to handler setup error.")
             with suppress(Exception):
                  if client_library == 'pg': await user_client.stop()
                  elif client_library == 'tl': await user_client.disconnect()
             # Mark as failed activation since handler isn't running
             client_library = None
             user_client = None
             # Fall through to failure handling below

    # --- Activation Failure Handling ---
    if not client_library:
        final_error = activation_error or "Activation Failed (Unknown Reason)"
        user_friendly_error = map_error_to_user_message(final_error)
        logging.warning(f"[User:{user_id}] Failed final activation for '{session_name_base}'. Error: '{final_error}' UserMsg: '{user_friendly_error}'")

        # Save the error state for the user
        set_user_last_error(user_id, f"Activate Fail '{session_name_base}': {final_error}")

        # Check if the error suggests the session is fundamentally invalid (and maybe alert user?)
        is_critical_error = any(err_tag in final_error for err_tag in critical_session_errors)
        alert_message = f"❗️ Session '{session_name_base}' failed to activate: {user_friendly_error}"
        if is_critical_error:
            alert_message += "\n🗑️ This session seems invalid. Consider deleting it using /manage_sessions."

        # Send failure alert to user (suppress errors during alert sending)
        try:
             await bot_client.send_message(user_id, alert_message)
        except Exception as e_alert:
             logging.error(f"[User:{user_id}] Failed to send activation failure alert for {session_name_base}: {e_alert}")

        # Final check: ensure any partially created client object is stopped/disconnected
        if user_client:
             logging.debug(f"[User:{user_id}] Final cleanup check: Attempting to stop/disconnect failed activation client {session_name_base}...")
             with suppress(Exception):
                  # Prefer stop() if Pyrogram, fallback to disconnect() for Telethon
                  if hasattr(user_client, 'stop'): await user_client.stop()
                  elif hasattr(user_client, 'disconnect'): await user_client.disconnect()

        return False, session_name_base, final_error # Return failure status and technical error


async def stop_session_logic(bot_client: Client, user_id: int, session_name_base: str, initiated_by: str = "User") -> tuple[bool, str]:
    """
    Handles stopping an active user session: disconnects the client, cancels the
    auto-stop timer, and removes the session from the active dictionary.

    Uses stop() for Pyrogram and disconnect() for Telethon clients.

    Args:
        bot_client: The main Pyrogram bot client instance (potentially needed if stop triggers other actions).
                    Note: Currently not used directly within stop logic, but passed for consistency.
        user_id: The ID of the user owning the session.
        session_name_base: The identifier name of the session to stop.
        initiated_by: A string indicating the trigger (e.g., "User", "Auto-Timer", "Shutdown").

    Returns:
        tuple:
            - success (bool): True if the session was found and stopping process initiated successfully
                             (client disconnected or already was disconnected). False if the session
                             wasn't found in the active list or disconnection failed.
            - display_name (str): The display name (phone/session name) of the stopped session.
    """
    session_data = None
    display_name = session_name_base # Default name if data missing

    # Atomically check and remove the session data from the active dictionary
    async with sessions_lock:
        user_active_sessions = active_sessions.get(user_id)
        if user_active_sessions and session_name_base in user_active_sessions:
            # Pop the data; it won't be in active_sessions after this block if found
            session_data = user_active_sessions.pop(session_name_base)
            display_name = session_data.get('phone', session_name_base) # Get actual name used
            logging.info(f"[User:{user_id}] ({initiated_by}) Stopping session: '{display_name}' (Key: {session_name_base}). Removing from active dict.")
            # If this was the last session for the user, remove the user's key entirely
            if not user_active_sessions:
                del active_sessions[user_id]
                logging.info(f"[User:{user_id}] Removed empty user entry from active_sessions dict.")
        else:
            # Session not found in the active dictionary (maybe already stopped, or never started)
            logging.info(f"[User:{user_id}] ({initiated_by}) Stop requested for '{session_name_base}', but it was not found in the active sessions list.")
            # Indicate that no active session *was* stopped by this call
            return False, display_name

    # --- Perform Cleanup Actions (Outside the main lock) ---
    if session_data:
        client_to_stop = session_data.get('client')
        library = session_data.get('library')
        auto_stop_task = session_data.get('auto_stop_task')
        stop_success = False # Tracks if disconnection/stop action was successful

        # 1. Cancel the Auto-Stop Timer Task (if not initiated by it)
        if initiated_by != "Auto-Timer" and auto_stop_task and not auto_stop_task.done():
            logging.debug(f"[User:{user_id}] ({initiated_by}) Cancelling auto-stop timer for '{display_name}'.")
            auto_stop_task.cancel()
            # Allow the cancellation to propagate briefly
            with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                 await asyncio.wait_for(auto_stop_task, timeout=0.1) # Don't block long here
            logging.debug(f"[User:{user_id}] ({initiated_by}) Auto-stop timer cancellation request processed for '{display_name}'.")
        elif initiated_by == "Auto-Timer":
             logging.debug(f"[User:{user_id}] ({initiated_by}) Skipping timer cancellation as timer is the trigger for '{display_name}'.")
        # Else: No task, or task already done, or manual stop - no cancellation needed/possible.

        # 2. Stop / Disconnect the User Client
        if client_to_stop:
            action_description = f"{library} {'stop()' if library == 'pg' else 'disconnect()'}"
            try:
                is_connected = False
                stop_action_func = None

                # Determine correct method and check connection state
                if library == "pg" and Client and isinstance(client_to_stop, Client):
                    # Check connection status BEFORE trying to stop
                    if client_to_stop.is_connected:
                         is_connected = True
                         stop_action_func = client_to_stop.stop
                    else: # Already disconnected
                         logging.info(f"[User:{user_id}] ({initiated_by}) Pyrogram client for '{display_name}' was already disconnected.")
                         stop_success = True # Considered successfully stopped if already inactive

                elif library == "tl" and TelegramClient and isinstance(client_to_stop, TelegramClient):
                    # Check connection status BEFORE trying to disconnect
                    if client_to_stop.is_connected():
                        is_connected = True
                        stop_action_func = client_to_stop.disconnect
                    else: # Already disconnected
                        logging.info(f"[User:{user_id}] ({initiated_by}) Telethon client for '{display_name}' was already disconnected.")
                        stop_success = True

                else:
                     logging.warning(f"[User:{user_id}] ({initiated_by}) Client object for '{display_name}' is of unexpected type or library mismatch (Library: {library}, Type: {type(client_to_stop)}). Cannot reliably stop.")
                     # Mark as failure because we couldn't perform the action correctly.

                # Execute the stop/disconnect action if needed and possible
                if is_connected and stop_action_func:
                    logging.info(f"[User:{user_id}] ({initiated_by}) Attempting {action_description} for '{display_name}'...")
                    # Add timeout for stop/disconnect to prevent hangs
                    await asyncio.wait_for(stop_action_func(), timeout=15.0)
                    logging.info(f"[User:{user_id}] ({initiated_by}) Successfully executed {action_description} for '{display_name}'.")
                    stop_success = True
                # stop_success remains False if library check failed, or function was None, etc.

            except asyncio.TimeoutError:
                logging.error(f"[User:{user_id}] ({initiated_by}) Timeout waiting for {action_description} on '{display_name}'. Session removed from active, but client might linger.")
                stop_success = False # Failed to stop cleanly within timeout
            except Exception as e_stop:
                logging.error(f"[User:{user_id}] ({initiated_by}) Error during {action_description} for '{display_name}': {e_stop}", exc_info=False)
                stop_success = False # Failed due to error
        else:
             # This indicates data corruption or logic error if session was in dict but client missing
             logging.error(f"[User:{user_id}] ({initiated_by}) No client object found in active session data for '{display_name}' during stop attempt!")
             stop_success = False

        logging.info(f"[User:{user_id}] ({initiated_by}) Stop process finished for '{display_name}'. Outcome Success: {stop_success}")
        return stop_success, display_name

    else:
        # Should not be reached if the initial lock logic is correct, but acts as a safeguard.
        logging.error(f"[User:{user_id}] ({initiated_by}) Reached end of stop_session_logic for '{session_name_base}' without valid session_data retrieved (logic error).")
        return False, session_name_base