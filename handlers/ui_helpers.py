# --- File: handlers/ui_helpers.py ---

import logging
from contextlib import suppress

# --- Pyrogram Imports ---
from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified

# --- Internal Imports ---
from database import get_user_last_error, get_user_sessions # DB access needed for status/manage
from sessions import active_sessions, sessions_lock # Access active session state
from config import MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER, SESSIONS_PER_PAGE # UI constants


# === Keyboard Generation Helpers ===

def generate_status_keyboard() -> InlineKeyboardMarkup:
    """Generates the standard keyboard for the status message."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh_status"),
         InlineKeyboardButton("🗑️ Clear Last Error", callback_data="clear_error")],
        [InlineKeyboardButton("📁 Manage Sessions", callback_data="manage_sessions_0")] # Link to page 0
    ])

def generate_confirmation_keyboard(session_name: str) -> InlineKeyboardMarkup:
    """Generates the Yes/No keyboard for session deletion confirmation."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⚠️ YES, Delete '{session_name}'", callback_data=f"confirm_delete_s:{session_name}")],
        [InlineKeyboardButton("🚫 Cancel", callback_data="manage_sessions_0")] # Back to page 0
    ])

async def generate_session_list_keyboard(user_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Generates the text and keyboard for the session management page.
    Handles pagination and dynamically shows Start/Stop/Delete buttons, plus bulk actions.

    Returns:
        tuple: (message_text, inline_keyboard_markup | None)
    """
    session_names = get_user_sessions(user_id) # Get list of stored session names for the user

    if not session_names:
        text = "You haven't stored any valid session strings yet. Send an archive (`.zip`, `.rar`, etc.) containing `.session` files."
        # Keyboard with only a help button if no sessions exist
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❓ How to Add Sessions (Help)", callback_data="help_callback")]])
        return text, kb

    # --- Pagination Logic ---
    total_session_count = len(session_names)
    start_index = page * SESSIONS_PER_PAGE
    end_index = start_index + SESSIONS_PER_PAGE
    # Get the slice of session names for the current page
    page_session_names = session_names[start_index:end_index]
    total_pages = (total_session_count + SESSIONS_PER_PAGE - 1) // SESSIONS_PER_PAGE

    # --- Build Message Text ---
    text = f"**📁 Your Stored Sessions ({total_session_count} total) - Page {page + 1}/{total_pages}**\n\n"

    keyboard_rows = []
    active_count_page = 0 # Count active sessions displayed on THIS page
    active_count_global = 0 # Count total active sessions for THIS user
    active_names_global = set() # Store the actual names of active sessions

    # Acquire lock to safely read the global active_sessions state
    async with sessions_lock:
        user_active_data = active_sessions.get(user_id, {}) # Get this user's active sessions dict
        active_names_global = set(user_active_data.keys()) # Set of active base names for quick lookup
        active_count_global = len(active_names_global)

        # Iterate through sessions on the current page
        for session_name in page_session_names:
            is_active = session_name in active_names_global
            display_name = user_active_data.get(session_name, {}).get('phone', session_name) if is_active else session_name

            if is_active:
                active_count_page += 1
                action_icon = "⏹️" # Stop icon
                action_text = f"{action_icon} {display_name}"
                action_callback = f"stop_s:{session_name}"
                text += f"• `{display_name}` **(Listening)**\n"
            else:
                action_icon = "▶️" # Start icon
                action_text = f"{action_icon} {session_name}"
                action_callback = f"listen_s:{session_name}"
                text += f"• `{session_name}`\n"

            # Create buttons for this session row
            action_button = InlineKeyboardButton(action_text, callback_data=action_callback)
            delete_button = InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_s:{session_name}")
            buttons = [action_button, delete_button]
            keyboard_rows.append(buttons)

    # Add info about max limit if reached
    available_slots = MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER - active_count_global
    if total_session_count > 0 and available_slots <= 0:
         text += f"\nℹ️ Max active sessions ({MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER}) reached."

    # --- Bulk Action Buttons ---

    # Add 'Start All Available' Button Row if applicable
    if total_session_count > 0 and available_slots > 0 and active_count_global < total_session_count:
        start_all_button = InlineKeyboardButton(f"🚀 Start All Available (up to {available_slots} more)", callback_data="start_all_sessions")
        keyboard_rows.append([start_all_button])

    # --- NEW: Add 'Stop All Active' Button Row ---
    if active_count_global > 1: # Only show if MORE THAN ONE session is active
         stop_all_button = InlineKeyboardButton(f"⏹️ Stop All Active ({active_count_global})", callback_data="stop_all_sessions")
         keyboard_rows.append([stop_all_button])
    # --- End NEW Section ---

    # --- Pagination Buttons ---
    pagination_row = []
    if page > 0:
        pagination_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"manage_sessions_{page-1}"))
    if end_index < total_session_count:
        pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"manage_sessions_{page+1}"))

    if pagination_row:
        keyboard_rows.append(pagination_row)

    # --- Bottom Action Row ---
    keyboard_rows.append([InlineKeyboardButton("🔄 Refresh List", callback_data=f"manage_sessions_{page}")])
    keyboard_rows.append([InlineKeyboardButton("📊 Status", callback_data="status_callback"), InlineKeyboardButton("❓ Help", callback_data="help_callback")])

    # --- Final Keyboard Markup ---
    kb = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

    return text, kb


# === Message Sending Functions ===

async def send_help_message(client: Client, chat_id: int, msg_id: int | None = None):
    """Sends or edits the help message."""
    help_text = (
        "**❓ How This Bot Works**\n\n"
        "1.  **Add Sessions:** Send an archive (`.zip`, `.rar`, `.7z`, etc.) containing one or more `.session` files. The bot will validate them and store the internal session *string*.\n"
        "2.  **Manage:** Use `/manage_sessions` to view your stored sessions.\n"
        "3.  **Listen (▶️):** Click ▶️ next to a session name. The bot starts a temporary client using the stored string to listen for OTPs from Telegram (ID: 777000).\n"
        "4.  **Receive OTPs:** When the listening account gets a login code, the bot forwards it to you here.\n"
        "5.  **Auto-Stop:** Listening sessions automatically stop after 30 minutes to save resources.\n"
        "6.  **Manual Stop (⏹️):** You can stop listening early using the ⏹️ button or the `Stop All` button if multiple are active.\n" # Added mention here
        "7.  **Delete (🗑️):** Permanently removes the stored session string from the bot's database.\n\n"
        "**Key Points:**\n"
        "• Only archives are accepted (no direct `.session` files).\n"
        "• The bot stores session *strings*, not the original files.\n"
        "• Use `/status` to see active sessions and last errors.\n\n"
    )
    # Basic keyboard for help message
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Manage Sessions", callback_data="manage_sessions_0")]])

    try:
        if msg_id:
            await client.edit_message_text(chat_id, msg_id, help_text, reply_markup=kb, disable_web_page_preview=True)
            logging.debug(f"Edited help message for user {chat_id} (Msg ID: {msg_id})")
        else:
            await client.send_message(chat_id, help_text, reply_markup=kb, disable_web_page_preview=True)
            logging.debug(f"Sent new help message to user {chat_id}")
    except MessageNotModified:
        logging.debug(f"Help message content unchanged for user {chat_id}")
    except Exception as e:
        logging.error(f"Error sending/editing help message for user {chat_id}: {e}")


async def send_status_message(client: Client, chat_id: int, msg_id: int | None = None):
    """Sends or edits the status message."""
    last_error = get_user_last_error(chat_id)
    total_stored_sessions = len(get_user_sessions(chat_id))

    active_count = 0
    async with sessions_lock:
        active_count = len(active_sessions.get(chat_id, {}))

    status_text = (
        f"📊 **Bot Status**\n\n"
        f"💾 Stored Sessions: **{total_stored_sessions}**\n"
        f"👂 Active Listeners: **{active_count} / {MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER}** (max)\n\n"
        f"❗ Last Recorded Error: `{last_error if last_error else 'None'}`"
    )

    status_kb = generate_status_keyboard()

    try:
        if msg_id:
            await client.edit_message_text(chat_id, msg_id, status_text, reply_markup=status_kb)
            logging.debug(f"Edited status message for user {chat_id} (Msg ID: {msg_id})")
        else:
            await client.send_message(chat_id, status_text, reply_markup=status_kb)
            logging.debug(f"Sent new status message to user {chat_id}")
    except MessageNotModified:
        logging.debug(f"Status message content unchanged for user {chat_id}")
    except Exception as e:
        logging.error(f"Error sending/editing status message for user {chat_id}: {e}")


async def send_manage_sessions_page(client: Client, user_id: int, page: int = 0, msg_id: int | None = None):
    """Sends or edits the session management page for a specific user."""
    logging.debug(f"Generating manage sessions page {page} for user {user_id} (Msg ID: {msg_id})")

    # Generate the text and keyboard using the updated helper
    page_text, page_kb = await generate_session_list_keyboard(user_id, page)

    try:
        if msg_id:
            await client.edit_message_text(user_id, msg_id, page_text, reply_markup=page_kb, disable_web_page_preview=True)
            logging.debug(f"Edited manage sessions page {page} for user {user_id} (Msg ID: {msg_id})")
        else:
            await client.send_message(user_id, page_text, reply_markup=page_kb, disable_web_page_preview=True)
            logging.debug(f"Sent new manage sessions page {page} to user {user_id}")
    except MessageNotModified:
        logging.debug(f"Manage sessions page {page} content unchanged for user {user_id}")
    except Exception as e:
        logging.error(f"Error sending/editing manage sessions page {page} for user {user_id}: {e}")
        if msg_id:
             with suppress(Exception):
                 await client.send_message(user_id, page_text, reply_markup=page_kb, disable_web_page_preview=True)