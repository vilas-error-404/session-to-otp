# --- File: handlers/messages.py ---

import os
import logging
import re
import time
import shutil
import asyncio
from contextlib import suppress # For suppressing errors during cleanup

# --- Pyrogram Imports ---
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.handlers import MessageHandler
from pyrogram.errors import FloodWait as PyroFloodWait # Specific exception

# --- Patoolib Check (Optional but good practice) ---
# We rely on the utility function, which checks internally,
# but adding a note or check here can be informative.


# --- Internal Imports ---
from config import ( ARCHIVE_EXTENSIONS, TEMP_DOWNLOAD_DIR
                    )
from utils import extract_archive_to_temp, map_error_to_user_message # Core archive extraction logic and error mapping
from sessions import test_and_extract_session # Session testing and string extraction
from database import add_user_session, get_session_string, get_user_sessions, set_user_last_error # DB interactions


# === Message Handler Functions ===

async def handle_archive_upload(client: Client, message: Message):
    """
    Handles incoming documents, expecting supported archive files containing .session files.
    Processes the archive, extracts sessions, validates them, and adds valid ones to the database.
    Provides feedback to the user throughout the process. Reliably cleans up temporary files.
    """
    user_id = message.chat.id
    if not message.document:
        # This handler is specifically filtered for documents, but good to double-check
        logging.debug(f"[User:{user_id}] Received non-document message in document handler (unexpected).")
        return

    file_info = message.document
    file_name = getattr(file_info, 'file_name', 'unknown_file')
    mime_type = getattr(file_info, 'mime_type', 'application/octet-stream')
    # file_id = file_info.file_id # Might be useful for logging later

    logging.info(f"[User:{user_id}] Received document: Name='{file_name}', MIME='{mime_type}'. Starting processing.")
    set_user_last_error(user_id, None) # Clear last error on new upload attempt

    # --- 1. Validate File Type ---

    # Reject direct .session file uploads
    if file_name and file_name.lower().endswith(".session"):
        logging.warning(f"[User:{user_id}] Rejected direct .session file upload: {file_name}")
        await message.reply_text(
            f"⚠️ **Direct Session Files Not Allowed:**\n"
            f"Please place `{file_name}` inside an archive (like `.zip` or `.rar`) and send the archive instead.\n\n"
            f"See /help for details."
        )
        return

    # Check if it looks like a supported archive based on extension or MIME type
    is_supported_archive = (file_name and file_name.lower().endswith(ARCHIVE_EXTENSIONS)) or \
                           (mime_type and mime_type in ['application/zip', 'application/x-rar-compressed',
                                                'application/x-7z-compressed', 'application/x-tar',
                                                'application/gzip', 'application/x-bzip2', 'application/x-xz'])

    if not is_supported_archive:
        logging.warning(f"[User:{user_id}] Rejected unsupported file type: {file_name}, MIME: {mime_type}")
        await message.reply_text(
            f"⚠️ **Unsupported File Type:** `{file_name or 'file'}`.\n"
            f"Please send an archive file (`.zip`, `.rar`, `.7z`, etc.) containing your `.session` files."
        )
        return

    # --- 2. Download and Process Archive ---
    proc_msg = await message.reply_text(f"📥 Downloading archive `{file_name}`...")
    temp_download_path = None # Path where the archive is saved temporarily
    temp_extraction_dir_path = None # Explicit path to the created extraction dir <<< UPDATED

    try:
        # Ensure download directory exists
        os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)
        # Create a unique filename for the download to avoid conflicts
        unique_download_name = f"{user_id}_{int(time.time())}_{os.path.basename(file_name)}"
        temp_download_path = os.path.join(TEMP_DOWNLOAD_DIR, unique_download_name)

        # Download the media
        actual_download_path = await client.download_media(message=message, file_name=temp_download_path)

        if not actual_download_path or not os.path.exists(actual_download_path):
             raise Exception("Download failed or file path is invalid after download attempt.")
        temp_download_path = actual_download_path # Use the path returned by Pyrogram

        logging.info(f"[User:{user_id}] Downloaded archive '{file_name}' to temp path: '{temp_download_path}'.")

        # --- 3. Extract Archive ---
        await proc_msg.edit_text(f"⚙️ Extracting sessions from `{file_name}`...")
        # Call the utility function to handle extraction
        # <<< UPDATED: Receive the extraction path as the 4th item >>>
        extracted_paths, _, extraction_error, temp_extraction_dir_path = extract_archive_to_temp(temp_download_path, user_id)

        # Handle extraction errors
        if extraction_error and extraction_error != "No .session files found in archive":
            user_friendly_extraction_err = map_error_to_user_message(extraction_error)
            logging.warning(f"[User:{user_id}] Extraction failed for '{file_name}': {extraction_error}")
            await proc_msg.edit_text(f"❌ Archive Extraction Failed: `{user_friendly_extraction_err}`")
            set_user_last_error(user_id, f"Upload Extract Fail: {extraction_error}")
            # Raise an exception to trigger the 'finally' block for download cleanup.
            # The temp_extraction_dir_path should be None if extraction failed internally (see utils.py).
            raise Exception(f"Extraction failed with error: {extraction_error}")

        if not extracted_paths:
            user_info_msg = f"ℹ️ No `.session` files found inside the archive `{file_name}`."
            await proc_msg.edit_text(user_info_msg)
            set_user_last_error(user_id, "Upload Success (No Sessions Found)")
            # Normal exit, ensure cleanup happens in finally block
            return # Nothing more to process

        logging.info(f"[User:{user_id}] Extracted {len(extracted_paths)} potential session file(s) from archive to '{temp_extraction_dir_path}'.")

        # Prepare list of sessions to process: [(temp_file_path, session_base_name), ...]
        sessions_to_process = []
        for file_path in extracted_paths:
             s_name_base = os.path.splitext(os.path.basename(file_path))[0]
             s_name_base = re.sub(r'[^\w\-.]', '_', s_name_base)
             if not s_name_base:
                 s_name_base = f"session_{int(time.time()*1000)}"
             sessions_to_process.append((file_path, s_name_base))


        # --- 4. Validate Sessions and Add to DB ---
        added_count, db_dup_count, invalid_count = 0, 0, 0
        invalid_details = [] # List of tuples: (name, reason_str)
        db_error_names = []

        total_to_validate = len(sessions_to_process)
        await proc_msg.edit_text(f"🛡️ Validating {total_to_validate} session file(s)... (This may take a moment)")

        # <<< Note: The session files paths (`f_path`) passed below still exist *inside* the temp_extraction_dir_path >>>
        validation_tasks = [
            asyncio.create_task(test_and_extract_session(f_path, s_name, user_id))
            for f_path, s_name in sessions_to_process
        ]

        results = await asyncio.gather(*validation_tasks, return_exceptions=True)

        processed_count = 0
        status_update_threshold = max(1, total_to_validate // 5)

        for i, result in enumerate(results):
            processed_count += 1
            if processed_count % status_update_threshold == 0 or processed_count == total_to_validate:
                 with suppress(Exception): # Suppress potential errors editing msg if deleted mid-process
                    await proc_msg.edit_text(f"💾 Validating & Saving... ({processed_count}/{total_to_validate})")

            _temp_f_path, session_name = sessions_to_process[i]

            if isinstance(result, Exception):
                invalid_count += 1
                reason = f"Task Error: {type(result).__name__}"
                invalid_details.append((session_name, reason))
                logging.error(f"[User:{user_id}] Validation Task for '{session_name}' failed with exception: {result}", exc_info=result)
            else:
                is_valid, session_str, user_error_msg_for_display = result

                if is_valid and session_str:
                    add_success = add_user_session(user_id, session_name, session_str)
                    if add_success:
                        added_count += 1
                        if user_error_msg_for_display:
                            logging.info(f"[User:{user_id}] Added session '{session_name}' to DB. (Validation Note: {user_error_msg_for_display})")
                        else:
                            logging.info(f"[User:{user_id}] Added session '{session_name}' to DB successfully.")
                    else:
                        if get_session_string(user_id, session_name): # Check if duplicate name caused failure
                            db_dup_count += 1
                            logging.info(f"[User:{user_id}] Session '{session_name}' already exists in DB (duplicate).")
                        else:
                            db_error_names.append(session_name)
                            logging.error(f"[User:{user_id}] DB Error: Failed to add session '{session_name}' after successful validation.")
                else:
                    invalid_count += 1
                    reason = user_error_msg_for_display or "Validation Failed"
                    invalid_details.append((session_name, reason))
                    logging.warning(f"[User:{user_id}] Invalid session '{session_name}' skipped. Reason: {reason}")
                # Note: test_and_extract_session cleaned up its *own* temporary file (_temp_f_path points to the *copy* it used).
                # The original extracted files inside temp_extraction_dir_path are still there.

        # --- 5. Final Summary Message ---
        summary_title = f"✅ Archive Processed: `{file_name}`"
        summary_parts = [summary_title, ""]

        if added_count > 0: summary_parts.append(f"💾 New Sessions Added: **{added_count}**")
        if db_dup_count > 0: summary_parts.append(f"ℹ️ Sessions Already Stored: **{db_dup_count}**")
        if invalid_count > 0: summary_parts.append(f"❌ Invalid / Skipped Sessions: **{invalid_count}**")
        if db_error_names:
             error_list_str = ', '.join([f"`{n}`" for n in db_error_names[:3]]) # Show first 3 names
             if len(db_error_names) > 3: error_list_str += ", ..."
             summary_parts.append(f"⚠️ DB Save Errors: **{len(db_error_names)}** ({error_list_str})")

        if not any([added_count, db_dup_count, invalid_count, db_error_names]) and extraction_error == "No .session files found in archive":
             # This was already covered by the "No .session files found" message editing proc_msg earlier
             # Avoid double-messaging, just let the finally block run. Maybe update proc_msg one last time if needed.
              pass # summary_parts.append("ℹ️ No session files found in the archive.")
        elif not any([added_count, db_dup_count, invalid_count, db_error_names]) and not extraction_error:
            # This case should ideally not happen if extract_archive worked but found no paths? Defensive check.
            summary_parts.append("❓ No session files found or processed.")
        elif added_count == 0 and db_dup_count == 0 and invalid_count > 0:
             summary_parts.append("\nℹ️ No new valid sessions were found in this archive.")

        if invalid_details:
            summary_parts.append("\n**Skipped Details:**")
            for name, reason in invalid_details[:5]:
                 summary_parts.append(f"• `{name}` ({reason})")
            if len(invalid_details) > 5: summary_parts.append("• ... (and others)")

        final_session_list = get_user_sessions(user_id)
        if final_session_list:
            summary_parts.append(f"\nUse /manage_sessions ({len(final_session_list)} total) to view and activate.")

        final_summary = "\n".join(summary_parts).strip()
        # Only update the message if there's content to show (avoid overwriting specific "No sessions found" msg)
        if len(summary_parts) > 2 : # Check if more than title and blank line
             await proc_msg.edit_text(final_summary)

        set_user_last_error(user_id, f"Upload Success ({added_count} added, {invalid_count} invalid)") # Record summary status

    # --- Error Handling for Download/Extraction Phases ---
    except PyroFloodWait as e:
        logging.warning(f"[User:{user_id}] Flood wait encountered during archive processing: {e.value}s")
        await proc_msg.edit_text(f"⏳ Flood Wait: Telegram is limiting requests. Please wait {e.value} seconds and try again.")
        set_user_last_error(user_id, f"Upload Flood Wait: {e.value}s")
    except Exception as e:
        # Catch exceptions from download, extraction, or validation phases
        error_type = type(e).__name__
        logging.exception(f"[User:{user_id}] CRITICAL error processing archive '{file_name}':") # Log full trace
        error_message_for_user = f"❌ **Critical Error** processing archive `{file_name}`: {error_type}.\nCheck logs or contact admin if the issue persists."
        try:
            await proc_msg.edit_text(error_message_for_user)
        except Exception as edit_err:
            logging.error(f"[User:{user_id}] Failed to edit final error message: {edit_err}")
            with suppress(Exception): # Suppress errors sending final fallback error
                await client.send_message(user_id, error_message_for_user)
        # Record the critical error only if it wasn't the specific handled extraction error
        if "Extraction failed with error" not in str(e):
            set_user_last_error(user_id, f"Upload Critical Error: {error_type}")

    # --- Final Cleanup (Guaranteed Execution) ---
    finally:
        logging.debug(f"[User:{user_id}] Entering final cleanup block for archive processing '{file_name}'.")
        # 1. Delete the original downloaded archive file
        if temp_download_path and os.path.exists(temp_download_path):
            logging.info(f"[User:{user_id}] Cleaning up temporary download file: {temp_download_path}")
            with suppress(OSError, Exception):
                 os.remove(temp_download_path)

        # 2. Delete the temporary extraction directory using the explicit path <<< UPDATED >>>
        # This removes the container directory and all its contents (original extracted files, etc.).
        if temp_extraction_dir_path and os.path.isdir(temp_extraction_dir_path):
            logging.info(f"[User:{user_id}] Cleaning up temporary extraction directory: {temp_extraction_dir_path}")
            shutil.rmtree(temp_extraction_dir_path, ignore_errors=True)
        elif temp_extraction_dir_path: # Log if path was provided but it wasn't a directory (shouldn't happen)
             logging.warning(f"[User:{user_id}] Temporary extraction path existed but was not a directory: {temp_extraction_dir_path}")

        logging.debug(f"[User:{user_id}] Finished final cleanup block for '{file_name}'.")


# === Registration Function ===

def register_message_handlers(client: Client):
    """Adds message handlers defined in this file to the Pyrogram client."""
    # Add the handler for document uploads in private chats
    client.add_handler(MessageHandler(handle_archive_upload, filters.document & filters.private))

    logging.info("Registered message handlers: document handler")