# --- File: main.py ---

import os
import asyncio
import logging
import shutil
from contextlib import suppress

# --- Pyrogram Imports ---
try:
    # Import Client and idle
    from pyrogram import Client, idle
except ImportError:
    print("Pyrogram is not installed. Please install it: pip install pyrogram TgCrypto")
    exit(1)

# --- Internal Imports ---
import config          # Bot configuration variables (Now includes shutdown timeouts)
import database        # Database initialization and functions
from sessions import active_sessions, sessions_lock, stop_session_logic # Shared state and shutdown logic
from tasks import periodic_cleanup_task # Background task

# Import handler registration functions
from handlers.commands import register_command_handlers
from handlers.messages import register_message_handlers
from handlers.callbacks import register_callback_handlers

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(module)s:%(lineno)d] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logging.getLogger('pyrogram').setLevel(logging.WARNING) # Reduce Pyrogram's default noise


# --- Startup Functions ---

def perform_startup_cleanup():
    """Cleans up temporary directories on bot startup."""
    logging.info("Performing startup cleanup of temporary directories...")
    dirs_to_clean = [config.TEMP_DOWNLOAD_DIR, config.TEMP_EXTRACT_DIR_BASE]
    for dir_path in dirs_to_clean:
        try:
            if os.path.isdir(dir_path):
                logging.info(f"Removing existing temporary directory: {dir_path}")
                shutil.rmtree(dir_path, ignore_errors=True)
            # Always ensure the directory exists after potential removal
            os.makedirs(dir_path, exist_ok=True)
            logging.info(f"Ensured temporary directory exists: {dir_path}")
        except Exception as e:
            logging.error(f"Error during startup cleanup of '{dir_path}': {e}", exc_info=True)
    logging.info("Startup cleanup finished.")

# --- Shutdown Functions ---

async def stop_all_active_sessions_shutdown(bot_client: Client):
    """Stops all currently running user sessions during shutdown."""
    sessions_to_stop = []
    async with sessions_lock:
        # Create a list of (user_id, session_name) to stop
        # Use list() to avoid modifying dict during iteration if errors occur mid-way
        for user_id, user_dict in list(active_sessions.items()):
            sessions_to_stop.extend([(user_id, name) for name in list(user_dict.keys())])

    if sessions_to_stop:
        logging.info(f"[Shutdown] Attempting to stop {len(sessions_to_stop)} active user sessions...")
        tasks = [
            asyncio.create_task(stop_session_logic(bot_client, uid, name, "Shutdown"))
            for uid, name in sessions_to_stop
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        stopped_count = sum(1 for r in results if isinstance(r, tuple) and r[0] is True)
        failed_count = len(results) - stopped_count
        logging.info(f"[Shutdown] Finished stopping active user sessions. Stopped: {stopped_count}, Failed/Not Found: {failed_count}")
        if failed_count > 0:
             failed_details = [sessions_to_stop[i][1] for i, r in enumerate(results) if not (isinstance(r, tuple) and r[0] is True)]
             logging.warning(f"[Shutdown] Failed to cleanly stop sessions: {failed_details}")
    else:
        logging.info("[Shutdown] No active user sessions were running.")


async def perform_shutdown(bot_client: Client, bg_cleanup_task: asyncio.Task | None):
    """Coordinates the graceful shutdown sequence."""
    print("\nInitiating graceful shutdown...")
    logging.info("--- Initiating Graceful Shutdown ---")

    # 1. Stop the Main Bot Client first (if connected)
    print("Stopping main bot client...")
    if bot_client.is_connected:
        try:
            await bot_client.stop()
            logging.info("[Shutdown] Main Pyrogram client stopped.")
            print("Main bot client stopped.")
        except Exception as stop_err:
             logging.exception(f"[Shutdown] Error stopping main bot client: {stop_err}")
             print(f"Error stopping main bot client: {stop_err}")
    else:
         logging.info("[Shutdown] Main Pyrogram client was already disconnected.")

    # 2. Stop Active User Sessions (with timeout from config) <<< UPDATED >>>
    print("Stopping active user sessions...")
    try:
        # Use timeout from config file
        await asyncio.wait_for(
            stop_all_active_sessions_shutdown(bot_client),
            timeout=config.SHUTDOWN_SESSION_STOP_TIMEOUT_SECONDS
        )
        print("Active session stopping process completed.")
        logging.info("[Shutdown] Active user session stopping process completed.")
    except asyncio.TimeoutError:
        logging.error(f"[Shutdown] Timed out ({config.SHUTDOWN_SESSION_STOP_TIMEOUT_SECONDS}s) waiting for active user sessions to stop gracefully.")
        print("Warning: Timed out waiting for all user sessions to stop.")
    except Exception as stop_err:
        logging.exception(f"[Shutdown] Error during active user session stopping: {stop_err}")
        print(f"Error stopping user sessions: {stop_err}")

    # 3. Cancel the Background Cleanup Task (with timeout from config) <<< UPDATED >>>
    if bg_cleanup_task and not bg_cleanup_task.done():
        print("Stopping background cleanup task...")
        logging.info("[Shutdown] Cancelling background cleanup task...")
        bg_cleanup_task.cancel()
        with suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception): # Suppress errors during await
             # Use timeout from config file
            await asyncio.wait_for(
                bg_cleanup_task,
                timeout=config.SHUTDOWN_BG_TASK_STOP_TIMEOUT_SECONDS
            )
        logging.info("[Shutdown] Background cleanup task cancellation processed.")
        print("Background cleanup task stop requested.")
    elif bg_cleanup_task:
        logging.info("[Shutdown] Background cleanup task was already finished.")
    else:
        logging.info("[Shutdown] Background cleanup task reference not found.")

    logging.info("--- Graceful Shutdown Sequence Finished ---")
    print("Shutdown complete.")


# --- Main Async Function ---

async def main():
    """Main asynchronous execution function."""
    # 1. Initialize Pyrogram Client (Define before registration)
    # Ensure the bot session is stored safely
    client = Client(
        name="session_otp_bot", # Name for the bot's own .session file
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workdir=os.getcwd() # Store bot session in current directory
    )
    logging.info(f"Pyrogram Client initialized (Name: {client.name}, Workdir: {client.workdir})")

    # 2. Register Handlers (Before starting the client)
    register_command_handlers(client)
    register_message_handlers(client)
    register_callback_handlers(client)
    logging.info("All handlers registered.")

    # 3. Start Background Cleanup Task
    cleanup_task = asyncio.create_task(periodic_cleanup_task())
    logging.info("Background cleanup task created and scheduled.")

    # --- Run the Bot and Keep Alive ---
    try:
        print("Starting bot...")
        await client.start()
        bot_info = client.me # Fetch bot info after starting
        print(f"Bot started as @{bot_info.username} (ID: {bot_info.id})")
        logging.info(f"Bot started successfully as @{bot_info.username} (ID: {bot_info.id}).")

        print("Bot is running. Press Ctrl+C to stop.")
        await idle() # Keep the script running until interrupted

        # This part is reached if idle() is somehow cancelled programmatically
        # Normal exit is via KeyboardInterrupt in the outer scope
        logging.info("idle() exited normally (unexpected). Proceeding to shutdown.")

    finally:
        logging.info("Initiating shutdown from main function's finally block...")
        await perform_shutdown(client, cleanup_task)


# --- Synchronous Execution Entry Point ---

if __name__ == "__main__":
    print("Starting Session OTP Bot...")

    # 1. Check Essential Configuration (Synchronous)
    if not config.check_critical_config():
        logging.critical("Essential configuration is missing. Please set environment variables.")
        exit(1)
    logging.info("Configuration check passed.")

    # 2. Initialize Database (Synchronous)
    try:
        database.initialize_database()
    except Exception as db_err:
        logging.critical(f"FATAL: Database initialization failed: {db_err}", exc_info=True)
        exit(1)
    logging.info("Database initialized successfully.")

    # 3. Perform Startup Cleanup (Synchronous)
    perform_startup_cleanup()

    # 4. Run the main asynchronous function
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        # This catches Ctrl+C if it happens *before* the main() try/finally starts
        # or if asyncio.run() itself is interrupted. The finally block inside main()
        # should ideally handle Ctrl+C during client operation.
        logging.info("Shutdown requested via Interrupt/SystemExit at top level.")
        print("\nShutdown signal received at top level.")
    except Exception as e:
        logging.critical("FATAL: Unhandled exception during asyncio.run(main):", exc_info=True)
        print(f"\nFATAL ERROR: {e}\n")
    finally:
        # This ensures a final log message even if errors occurred during shutdown itself
        logging.info("="*10 + " Bot Process Terminated " + "="*10)
        print("Bot process terminated.")