# --- File: handlers/callbacks.py ---

import asyncio
import logging
from contextlib import suppress

# --- Pyrogram Imports ---
from pyrogram import Client
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified, FloodWait as PyroFloodWait
from pyrogram.handlers import CallbackQueryHandler

# --- Internal Imports ---
from .ui_helpers import (send_manage_sessions_page, send_status_message,
                         send_help_message, generate_confirmation_keyboard,
                         # generate_session_list_keyboard, # Not needed directly here
                         )
from sessions import (start_session_client_temporary, stop_session_logic,
                      active_sessions, sessions_lock, MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER,
                      SESSION_LISTEN_DURATION)
from database import (get_session_string, delete_user_session_record,
                      get_user_sessions, set_user_last_error)
from utils import map_error_to_user_message
# from config import SESSION_LISTEN_DURATION, MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER # Already imported


async def handle_callback_query(client: Client, callback_query: CallbackQuery):
    """Handles all incoming callback queries from inline buttons."""
    user_id = callback_query.from_user.id
    data = callback_query.data
    message = callback_query.message
    msg_id = message.id if message else None

    log_prefix = f"[User:{user_id} CB:'{data}']" # Consistent log prefix
    logging.info(f"{log_prefix} Received callback query. (Msg ID: {msg_id})")

    # Define page_to_refresh early, default to 0
    page_to_refresh = 0
    if message and message.reply_markup and message.reply_markup.inline_keyboard:
         # Attempt to determine the current page for context-aware refreshing
         for row in message.reply_markup.inline_keyboard:
             for btn in row:
                  if btn.callback_data.startswith("manage_sessions_") and "_" in btn.callback_data:
                       try:
                           page_str = btn.callback_data.split('_')[-1]
                           if page_str.isdigit():
                               page_to_refresh = int(page_str)
                               break # Found a valid page number
                       except Exception: pass # Ignore malformed callback data
             if page_to_refresh != 0: break

    try:
        # --- Navigation & Status Callbacks ---
        if data.startswith("manage_sessions_"):
            page = int(data.split("_")[-1])
            await callback_query.answer("Loading sessions...")
            await send_manage_sessions_page(client, user_id, page, msg_id)

        elif data == "status_callback":
            await callback_query.answer("Loading status...")
            await send_status_message(client, user_id, msg_id)

        elif data == "help_callback":
            await callback_query.answer("Loading help...")
            await send_help_message(client, user_id, msg_id)

        elif data == "refresh_status":
            await callback_query.answer("Refreshing status...")
            await send_status_message(client, user_id, msg_id)

        elif data == "clear_error":
            set_user_last_error(user_id, None)
            await callback_query.answer("Last error cleared.")
            await send_status_message(client, user_id, msg_id)

        # --- Listen/Stop Individual Session ---
        elif data.startswith("listen_s:") or data.startswith("stop_s:"):
            action, session_name = data.split(":", 1)
            # Use the detected page_to_refresh for session list refresh
            current_page = page_to_refresh
            log_prefix = f"[User:{user_id} CB:'{data}' Page:{current_page}]" # Update log prefix

            if action == "listen_s":
                logging.info(f"{log_prefix} Activating session '{session_name}'.")
                await callback_query.answer(f"Activating {session_name}...")

                session_string = get_session_string(user_id, session_name)
                if not session_string:
                    logging.warning(f"{log_prefix} Session string not found for '{session_name}' (deleted?).")
                    await callback_query.answer("❌ Session not found! Maybe it was deleted?", show_alert=True)
                    if msg_id: await send_manage_sessions_page(client, user_id, 0, msg_id) # Refresh page 0
                    return

                success, final_name, err_msg_technical = await start_session_client_temporary(
                    bot_client=client, session_string=session_string,
                    session_name_base=session_name, user_id=user_id,
                    duration_seconds=SESSION_LISTEN_DURATION
                )

                if not success:
                    user_friendly_error = map_error_to_user_message(err_msg_technical)
                    logging.warning(f"{log_prefix} Activation failed for '{session_name}'. UserMsg: '{user_friendly_error}', Tech: '{err_msg_technical}'")
                    await callback_query.answer(f"❌ Activation Failed: {user_friendly_error}", show_alert=True)
                else:
                    logging.info(f"{log_prefix} Activation initiated successfully for '{final_name}'.")
                    # No explicit success alert needed, UI refresh shows status

            elif action == "stop_s":
                logging.info(f"{log_prefix} Stopping session '{session_name}'.")
                await callback_query.answer(f"Stopping {session_name}...")
                await stop_session_logic(client, user_id, session_name, initiated_by="Button Stop")
                # No alert needed, refresh shows status

            # Refresh the session list view after listen/stop attempt
            await asyncio.sleep(0.2)
            if msg_id:
                 logging.debug(f"{log_prefix} Refreshing session list on page {current_page}.")
                 with suppress(MessageNotModified):
                      await send_manage_sessions_page(client, user_id, current_page, msg_id)
            else:
                 logging.warning(f"{log_prefix} Original message ID not found, sending new manage page 0.")
                 await send_manage_sessions_page(client, user_id, 0)

        # --- Start All Available Sessions ---
        elif data == "start_all_sessions":
            logging.info(f"{log_prefix} Starting all available sessions.")
            await callback_query.answer("Attempting to start available sessions...")
            info_msg = await client.send_message(user_id, "🔄 Preparing to start available sessions...")

            all_session_names = get_user_sessions(user_id)
            if not all_session_names:
                await info_msg.edit_text("ℹ️ You have no stored sessions to start.")
                return

            sessions_to_attempt = []
            failed_to_fetch_string = []
            num_to_attempt = 0

            async with sessions_lock:
                active_names = set(active_sessions.get(user_id, {}).keys())
                active_count = len(active_names)
                available_slots = MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER - active_count

                if available_slots <= 0:
                    await info_msg.edit_text(f"ℹ️ Maximum active session limit ({MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER}) already reached. Cannot start more.")
                    return

                count = 0
                for name in all_session_names:
                    if count >= available_slots: break
                    if name not in active_names:
                        session_str = get_session_string(user_id, name)
                        if session_str:
                            sessions_to_attempt.append((name, session_str))
                            count += 1
                        else:
                            failed_to_fetch_string.append(name)

            num_to_attempt = len(sessions_to_attempt)
            if not sessions_to_attempt:
                msg_text = "✅ All stored sessions are already active or no more slots available."
                if failed_to_fetch_string:
                     msg_text += f"\n⚠️ Could not retrieve string for {len(failed_to_fetch_string)} session(s)."
                await info_msg.edit_text(msg_text)
                return

            await info_msg.edit_text(f"🚀 Attempting to start **{num_to_attempt}** session(s)...")
            start_tasks = [
                asyncio.create_task(start_session_client_temporary(
                    bot_client=client, session_string=s_str, session_name_base=name,
                    user_id=user_id, duration_seconds=SESSION_LISTEN_DURATION))
                for name, s_str in sessions_to_attempt
            ]
            results = await asyncio.gather(*start_tasks, return_exceptions=True)

            started_count = 0
            failed_tasks_details = []
            for i, result in enumerate(results):
                name_attempted, _ = sessions_to_attempt[i]
                if isinstance(result, Exception):
                    error_type = type(result).__name__
                    failed_tasks_details.append((name_attempted, f"Task Error: {error_type}"))
                    logging.error(f"{log_prefix} Start All Task Error for '{name_attempted}': {result}", exc_info=result)
                else:
                    success, _, err_msg_technical = result
                    if success: started_count += 1
                    else:
                        failed_tasks_details.append((name_attempted, map_error_to_user_message(err_msg_technical)))
                        logging.warning(f"{log_prefix} Start All failed for '{name_attempted}': Tech: {err_msg_technical}")

            summary = f"✅ **Start All Complete!**\n\n"
            summary += f"▶️ Started Successfully: **{started_count}**\n"
            if failed_tasks_details:
                summary += f"❌ Failed / Skipped: **{len(failed_tasks_details)}**\n"
                summary += "\n".join([f"  • `{name}` ({reason})" for name, reason in failed_tasks_details[:5]])
                if len(failed_tasks_details) > 5: summary += "\n  • ... (and others)"
            if failed_to_fetch_string:
                 summary += f"\n⚠️ Could not fetch string for: **{len(failed_to_fetch_string)}**"
            await info_msg.edit_text(summary)
            logging.info(f"{log_prefix} Start All finished. Started: {started_count}, Failed/Skipped: {len(failed_tasks_details)}")

            await send_manage_sessions_page(client, user_id, 0) # Refresh page 0

        # --- Stop All Active Sessions --- NEW BLOCK ---
        elif data == "stop_all_sessions":
             logging.info(f"{log_prefix} Stopping all active sessions.")
             await callback_query.answer("Attempting to stop all active sessions...")
             info_msg = await client.send_message(user_id, "🔄 Preparing to stop all active sessions...")

             active_names_to_stop = []
             async with sessions_lock:
                  # Get a copy of the names currently active for this user
                  active_names_to_stop = list(active_sessions.get(user_id, {}).keys())

             num_to_stop = len(active_names_to_stop)
             if num_to_stop == 0:
                 await info_msg.edit_text("ℹ️ No sessions are currently active to stop.")
                 logging.info(f"{log_prefix} No active sessions found to stop.")
                 # Refresh the manage page in case state changed race condition? Optional.
                 # if msg_id: await send_manage_sessions_page(client, user_id, page_to_refresh, msg_id)
                 return

             await info_msg.edit_text(f"⏹️ Attempting to stop **{num_to_stop}** active session(s)...")

             stop_tasks = [
                 asyncio.create_task(stop_session_logic(
                     bot_client=client, user_id=user_id, session_name_base=name,
                     initiated_by="Button Stop All" # Use a distinct initiator string
                 )) for name in active_names_to_stop
             ]
             results = await asyncio.gather(*stop_tasks, return_exceptions=True)

             stopped_count = 0
             failed_stops = [] # Store names that failed to stop cleanly
             for i, result in enumerate(results):
                  name_attempted = active_names_to_stop[i]
                  if isinstance(result, Exception):
                       failed_stops.append(name_attempted)
                       logging.error(f"{log_prefix} Stop All Task Error for '{name_attempted}': {result}", exc_info=result)
                  else:
                       # result from stop_session_logic: (success, display_name)
                       success, _ = result
                       if success:
                           stopped_count += 1
                       else:
                           # stop_session_logic returns False if stop failed or session was already gone (race condition)
                           failed_stops.append(name_attempted)
                           logging.warning(f"{log_prefix} Stop All: stop_session_logic reported failure or not found for '{name_attempted}'.")

             summary = f"✅ **Stop All Complete!**\n\n"
             summary += f"⏹️ Stopped Successfully: **{stopped_count}**\n"
             if failed_stops:
                  summary += f"❓ Not Found / Failed to Stop: **{len(failed_stops)}**"
                  # Could optionally list failed names if desired, e.g.:
                  # summary += f" ({', '.join([f'`{n}`' for n in failed_stops[:3]])}{', ...' if len(failed_stops) > 3 else ''})"

             await info_msg.edit_text(summary)
             logging.info(f"{log_prefix} Stop All finished. Stopped: {stopped_count}, Failed/Not Found: {len(failed_stops)}")

             # Always refresh the manage sessions page after stopping all
             # Use the page context if available, otherwise default to 0
             if msg_id: await send_manage_sessions_page(client, user_id, page_to_refresh, msg_id)
             else: await send_manage_sessions_page(client, user_id, 0)


        # --- Delete Session Flow ---
        elif data.startswith("delete_s:"):
            session_name_to_delete = data.split(":", 1)[1]
            logging.info(f"{log_prefix} Initiating delete for session '{session_name_to_delete}'.")
            await callback_query.answer()

            if not get_session_string(user_id, session_name_to_delete):
                 await callback_query.answer(f"❌ Session `{session_name_to_delete}` not found.", show_alert=True)
                 if msg_id: await send_manage_sessions_page(client, user_id, 0, msg_id)
                 return

            async with sessions_lock:
                if session_name_to_delete in active_sessions.get(user_id, {}):
                    await callback_query.answer("⚠️ Stop this session first before deleting!", show_alert=True)
                    return

            confirm_kb = generate_confirmation_keyboard(session_name_to_delete)
            confirm_text = f"❓ **Confirm Deletion**\n\nPermanently delete session `{session_name_to_delete}`?\n\n**This cannot be undone.**"
            if msg_id:
                try:
                    await client.edit_message_text(user_id, msg_id, confirm_text, reply_markup=confirm_kb)
                except MessageNotModified: pass
                except Exception as e:
                    logging.error(f"{log_prefix} Failed to edit message for delete confirmation: {e}", exc_info=True)
                    await client.send_message(user_id, confirm_text, reply_markup=confirm_kb)
            else:
                 logging.warning(f"{log_prefix} No message context for delete confirmation. Sending new.")
                 await client.send_message(user_id, confirm_text, reply_markup=confirm_kb)


        elif data.startswith("confirm_delete_s:"):
            session_name_to_delete = data.split(":", 1)[1]
            logging.info(f"{log_prefix} Confirmed delete for session '{session_name_to_delete}'.")
            await callback_query.answer(f"Deleting {session_name_to_delete}...")

            async with sessions_lock:
                if session_name_to_delete in active_sessions.get(user_id, {}):
                    await callback_query.answer("❗️ Session became active! Stop it first.", show_alert=True)
                    if msg_id: await send_manage_sessions_page(client, user_id, page_to_refresh, msg_id) # Use current page
                    return

            delete_success = delete_user_session_record(user_id, session_name_to_delete)
            if delete_success:
                 await callback_query.answer(f"✅ Session `{session_name_to_delete}` deleted.")
            else:
                 await callback_query.answer(f"❗️ Could not delete '{session_name_to_delete}'. Already deleted or DB error?", show_alert=True)

            # Refresh session list after delete attempt
            if msg_id: await send_manage_sessions_page(client, user_id, page_to_refresh, msg_id) # Use current page
            else: await send_manage_sessions_page(client, user_id, 0)

        # --- Unhandled Callbacks ---
        else:
            await callback_query.answer()
            logging.warning(f"{log_prefix} Received unhandled callback data.")

    # --- General Exception Handling for Callbacks ---
    except MessageNotModified:
        await callback_query.answer()
        logging.debug(f"{log_prefix} Caught MessageNotModified.")
    except PyroFloodWait as e:
        logging.warning(f"{log_prefix} Flood wait during callback processing: {e.value}s")
        await callback_query.answer(f"⏳ Telegram flood wait: {e.value}s. Try again shortly.", show_alert=True)
    except Exception as e:
        logging.exception(f"{log_prefix} CRITICAL error processing callback:")
        await callback_query.answer("❗️ An internal error occurred. Please try again later.", show_alert=True)


def register_callback_handlers(client: Client):
    """Adds the callback query handler defined in this file to the Pyrogram client."""
    client.add_handler(CallbackQueryHandler(handle_callback_query))
    logging.info("Registered callback query handler.")