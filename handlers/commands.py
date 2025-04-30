# --- File: handlers/commands.py ---

import logging


from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.handlers import MessageHandler # Import MessageHandler type hint
from pyrogram.errors import UserNotParticipant # Example specific error

# --- Internal Imports ---
# Import functions responsible for sending UI messages/keyboards
from .ui_helpers import send_help_message, send_status_message, send_manage_sessions_page

# Import core session logic (stop, check existence via db)
from sessions import stop_session_logic, active_sessions, sessions_lock # Import active state too
from database import get_session_string # For checking existence before actions


# === Command Handler Functions ===

# Decorators assume a 'client' instance will be available when registered
# Or they can be removed if using explicit client.add_handler in register function

async def start_command(client: Client, message: Message):
    """Handles the /start command."""
    user_id = message.chat.id
    logging.info(f"[User:{user_id}] Received /start command.")
    start_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❓ Help", callback_data="help_callback")],
        [InlineKeyboardButton("📁 Manage Sessions", callback_data="manage_sessions_0")] # Link to page 0
    ])
    # Welcome message text (adjust as needed)
    start_text = (
        "**Welcome!** 👋\n\n"
        "Send an archive (`.zip`, `.rar`, etc.) containing your Pyrogram or Telethon `.session` files to add them.\n\n"
        "Use `/manage_sessions` or the button below.\n\n"
        "▶️ Start listening for OTPs (active for 30 mins).\n"
        "⏹️ Stop listening.\n"
        "🗑️ Delete stored sessions.\n\n"
        "⚠️ **Security:** Sessions grant account access. Delete them when not needed."
    )
    try:
        await message.reply_text(
            start_text,
            reply_markup=start_kb,
            disable_web_page_preview=True
        )
    except Exception as e:
        logging.error(f"Error handling /start for user {user_id}: {e}")


async def help_command(client: Client, message: Message):
    """Handles the /help command."""
    user_id = message.chat.id
    logging.info(f"[User:{user_id}] Received /help command.")
    # Call the UI helper function to send the help message
    # Pass the main client instance ('client' from the handler)
    await send_help_message(client, user_id)


async def status_command(client: Client, message: Message):
    """Handles the /status command."""
    user_id = message.chat.id
    logging.info(f"[User:{user_id}] Received /status command.")
    # Call the UI helper function to send the status message
    # Pass the main client instance ('client' from the handler)
    await send_status_message(client, user_id)


async def manage_sessions_command(client: Client, message: Message):
    """Handles the /manage_sessions command."""
    user_id = message.chat.id
    logging.info(f"[User:{user_id}] Received /manage_sessions command.")
    # Call the UI helper function to show the first page of sessions
    # Pass the main client instance ('client' from the handler)
    await send_manage_sessions_page(client, user_id, page=0)


async def stop_session_command(client: Client, message: Message):
    """Handles the /stop_session <SessionName> command."""
    user_id = message.chat.id
    command_parts = message.text.split(maxsplit=1)

    if len(command_parts) < 2:
        logging.warning(f"[User:{user_id}] /stop_session command used without session name.")
        await message.reply_text("⚠️ Usage: `/stop_session <SessionName>`\nPlease provide the name of the session to stop.")
        return

    session_name_to_stop = command_parts[1].strip()
    logging.info(f"[User:{user_id}] Received /stop_session command for '{session_name_to_stop}'.")

    # Check if the session exists in the database (doesn't need to be active)
    # We check existence primarily to give better feedback ("not found" vs "not active")
    if not get_session_string(user_id, session_name_to_stop):
        logging.warning(f"[User:{user_id}] Attempted /stop_session for non-existent session '{session_name_to_stop}'.")
        await message.reply_text(f"❌ Session `{session_name_to_stop}` not found in your stored sessions.")
        return

    # Attempt to stop the session using the logic from sessions.py
    # Pass the main 'client' instance here if stop_session_logic needs it (e.g., for notifications, though likely not)
    # Current 'sessions.py' design passes bot_client to stop_session_logic, so we pass 'client' here.
    success, display_name = await stop_session_logic(client, user_id, session_name_to_stop, initiated_by="Cmd /stop")

    if success:
        logging.info(f"[User:{user_id}] Successfully stopped session '{display_name}' via /stop_session command.")
        await message.reply_text(f"✅ Session `{display_name}` stopped listening.")
    else:
        # Stop logic returns False if session wasn't active or failed to stop
        logging.info(f"[User:{user_id}] /stop_session '{session_name_to_stop}': Session was not active or stop failed (see session logs).")
        # Check if it's *currently* active after the failed attempt (unlikely but possible race condition)
        async with sessions_lock:
            is_still_active = session_name_to_stop in active_sessions.get(user_id, {})
        if is_still_active:
             await message.reply_text(f"❗️ Failed to stop session `{session_name_to_stop}`. Please check logs or try again.")
        else:
             await message.reply_text(f"ℹ️ Session `{session_name_to_stop}` was not actively listening.")


async def delete_session_command(client: Client, message: Message):
    """Handles the /delete_session <SessionName> command - initiates confirmation."""
    user_id = message.chat.id
    command_parts = message.text.split(maxsplit=1)

    if len(command_parts) < 2:
        logging.warning(f"[User:{user_id}] /delete_session command used without session name.")
        await message.reply_text("⚠️ Usage: `/delete_session <SessionName>`\nPlease provide the name of the session to delete.")
        return

    session_name_to_delete = command_parts[1].strip()
    logging.info(f"[User:{user_id}] Received /delete_session command for '{session_name_to_delete}'.")

    # 1. Check if the session exists in the database
    if not get_session_string(user_id, session_name_to_delete):
        logging.warning(f"[User:{user_id}] Attempted /delete_session for non-existent session '{session_name_to_delete}'.")
        await message.reply_text(f"❌ Session `{session_name_to_delete}` not found in your stored sessions.")
        return

    # 2. Check if the session is currently active (must be stopped before deletion)
    async with sessions_lock:
        if session_name_to_delete in active_sessions.get(user_id, {}):
            logging.warning(f"[User:{user_id}] Attempted /delete_session for active session '{session_name_to_delete}'.")
            await message.reply_text(f"⚠️ Session `{session_name_to_delete}` is currently active.\nPlease stop it first using `/stop_session {session_name_to_delete}` or via the '⏹️ Stop' button in `/manage_sessions` before deleting.")
            return

    # 3. If session exists and is not active, show confirmation dialog
    confirm_callback_data = f"confirm_delete_s:{session_name_to_delete}" # Same callback as button
    cancel_callback_data = "manage_sessions_0" # Go back to session list page 0

    confirmation_kb = InlineKeyboardMarkup([
        # Use Markdown V2 formatting carefully in button text if needed, or keep simple
        [InlineKeyboardButton(f"⚠️ YES, Delete '{session_name_to_delete}'", callback_data=confirm_callback_data)],
        [InlineKeyboardButton("🚫 Cancel", callback_data=cancel_callback_data)]
    ])

    confirmation_text = (
        f"❓ **Confirm Deletion**\n\n"
        f"Are you sure you want to permanently delete the session named:\n`{session_name_to_delete}`?\n\n"
        f"**This action cannot be undone.**"
    )
    try:
        await message.reply_text(confirmation_text, reply_markup=confirmation_kb)
    except Exception as e:
        logging.error(f"Error sending delete confirmation for '{session_name_to_delete}' to user {user_id}: {e}")
        await message.reply_text("❗️ An error occurred while trying to ask for deletion confirmation.")


# === Registration Function ===

def register_command_handlers(client: Client):
    """Adds all command handlers defined in this file to the Pyrogram client."""
    client.add_handler(MessageHandler(start_command, filters.command("start") & filters.private))
    client.add_handler(MessageHandler(help_command, filters.command("help") & filters.private))
    client.add_handler(MessageHandler(status_command, filters.command("status") & filters.private))
    client.add_handler(MessageHandler(manage_sessions_command, filters.command("manage_sessions") & filters.private))
    client.add_handler(MessageHandler(stop_session_command, filters.command("stop_session") & filters.private))
    client.add_handler(MessageHandler(delete_session_command, filters.command("delete_session") & filters.private))

    logging.info("Registered command handlers: /start, /help, /status, /manage_sessions, /stop_session, /delete_session")