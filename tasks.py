# --- File: tasks.py ---

import asyncio
import os
import time
import logging
import shutil
from contextlib import suppress # Useful for ignoring errors during cleanup attempts

# Import constants from config
from config import ( BACKGROUND_CLEANUP_INTERVAL_SECONDS, BACKGROUND_CLEANUP_AGE_THRESHOLD_SECONDS,
                     TEMP_DOWNLOAD_DIR, TEMP_EXTRACT_DIR_BASE )

async def periodic_cleanup_task():
    """
    A background task that periodically scans temporary directories (downloads, extractions)
    and removes files or folders older than a specified threshold.

    This task runs indefinitely until cancelled.
    """
    interval_seconds = BACKGROUND_CLEANUP_INTERVAL_SECONDS
    age_threshold_seconds = BACKGROUND_CLEANUP_AGE_THRESHOLD_SECONDS
    download_dir = TEMP_DOWNLOAD_DIR
    extract_base_dir = TEMP_EXTRACT_DIR_BASE

    # Initial log indicating the task has started and its parameters
    logging.info(f"[CleanupTask] Background periodic cleanup task started.")
    logging.info(f"[CleanupTask] Parameters: Interval={interval_seconds}s, AgeThreshold={age_threshold_seconds}s")
    logging.info(f"[CleanupTask] Target Dirs: Downloads='{download_dir}', ExtractsBase='{extract_base_dir}'")

    while True:
        try:
            # Wait for the specified interval before running the next cleanup cycle
            await asyncio.sleep(interval_seconds)

            logging.info("[CleanupTask] Starting cleanup cycle...")
            current_time = time.time()
            items_removed = 0
            errors_encountered = 0

            # --- 1. Clean TEMP_DOWNLOAD_DIR ---
            # This directory should primarily contain individual downloaded archive files.
            if os.path.isdir(download_dir):
                logging.debug(f"[CleanupTask] Scanning download directory: {download_dir}")
                # Use suppress to ignore potential errors during listdir (e.g., dir deleted mid-scan)
                with suppress(FileNotFoundError, PermissionError):
                    for item_name in os.listdir(download_dir):
                        item_path = os.path.join(download_dir, item_name)
                        try:
                            # Ensure it's a file before checking modification time and deleting
                            if os.path.isfile(item_path):
                                file_mtime = os.path.getmtime(item_path)
                                file_age = current_time - file_mtime

                                if file_age > age_threshold_seconds:
                                    logging.info(f"[CleanupTask] Removing old downloaded file (Age: {file_age:.0f}s): {item_path}")
                                    os.remove(item_path)
                                    items_removed += 1
                                else:
                                    logging.debug(f"[CleanupTask] Keeping recent downloaded file (Age: {file_age:.0f}s): {item_path}")
                            elif os.path.isdir(item_path):
                                # Directories are not expected here, but log if found
                                logging.warning(f"[CleanupTask] Found unexpected directory in download dir: {item_path}")
                                # Optional: Decide whether to delete old directories here too.
                                # dir_mtime = os.path.getmtime(item_path)
                                # if (current_time - dir_mtime) > age_threshold_seconds:
                                #     logging.warning(f"[CleanupTask] Removing old unexpected directory: {item_path}")
                                #     shutil.rmtree(item_path, ignore_errors=True)
                                #     items_removed += 1
                        except FileNotFoundError:
                            # File might have been removed by another process between listdir and stat/remove
                            logging.debug(f"[CleanupTask] File disappeared during cleanup scan: {item_path}")
                            continue # Move to the next item
                        except (OSError, PermissionError) as e:
                            logging.warning(f"[CleanupTask] Error removing item from download dir '{item_path}': {e}")
                            errors_encountered += 1
                        except Exception as e_gen: # Catch any other unexpected errors
                            logging.error(f"[CleanupTask] Unexpected error processing item in download dir '{item_path}': {e_gen}", exc_info=False)
                            errors_encountered += 1
            else:
                logging.warning(f"[CleanupTask] Download directory not found, skipping its cleanup: {download_dir}")

            # --- 2. Clean TEMP_EXTRACT_DIR_BASE ---
            # This directory contains subdirectories, each for a specific extraction operation (e.g., user_id_timestamp).
            if os.path.isdir(extract_base_dir):
                logging.debug(f"[CleanupTask] Scanning extraction base directory: {extract_base_dir}")
                with suppress(FileNotFoundError, PermissionError):
                    for item_name in os.listdir(extract_base_dir): # Items here should be directories
                        item_path = os.path.join(extract_base_dir, item_name)
                        try:
                            # We expect directories named like 'user_id_timestamp'
                            if os.path.isdir(item_path):
                                dir_mtime = os.path.getmtime(item_path) # Use directory's mtime
                                dir_age = current_time - dir_mtime

                                if dir_age > age_threshold_seconds:
                                    logging.info(f"[CleanupTask] Removing old extraction directory (Age: {dir_age:.0f}s): {item_path}")
                                    # Use shutil.rmtree for directories; ignore_errors=True is safer in cleanup tasks
                                    shutil.rmtree(item_path, ignore_errors=True)
                                    items_removed += 1
                                else:
                                     logging.debug(f"[CleanupTask] Keeping recent extraction directory (Age: {dir_age:.0f}s): {item_path}")

                            elif os.path.isfile(item_path):
                                # Files directly in the base directory are unexpected. Clean them up if old.
                                file_mtime = os.path.getmtime(item_path)
                                if (current_time - file_mtime) > age_threshold_seconds:
                                    logging.warning(f"[CleanupTask] Removing unexpected old file in extract base directory: {item_path}")
                                    with suppress(OSError): # Suppress errors during removal of unexpected files
                                        os.remove(item_path)
                                    items_removed += 1
                        except FileNotFoundError:
                             logging.debug(f"[CleanupTask] Extraction directory disappeared during cleanup scan: {item_path}")
                             continue
                        except (OSError, PermissionError) as e:
                            logging.warning(f"[CleanupTask] Error removing item from extract base '{item_path}': {e}")
                            errors_encountered += 1
                        except Exception as e_gen:
                             logging.error(f"[CleanupTask] Unexpected error processing item in extract base '{item_path}': {e_gen}", exc_info=False)
                             errors_encountered += 1
            else:
                logging.warning(f"[CleanupTask] Extraction base directory not found, skipping its cleanup: {extract_base_dir}")

            # Log summary of the cleanup cycle
            if items_removed > 0 or errors_encountered > 0:
                 logging.info(f"[CleanupTask] Cleanup cycle finished. Items removed: {items_removed}, Errors encountered: {errors_encountered}")
            else:
                 logging.info("[CleanupTask] Cleanup cycle finished. No old items found to remove.")

        except asyncio.CancelledError:
            # This is expected during graceful shutdown
            logging.info("[CleanupTask] Task received cancellation request. Exiting loop.")
            break # Exit the while loop

        except Exception as e:
            # Catch-all for unexpected errors within the main loop (e.g., issues with time.time()?)
            logging.critical(f"[CleanupTask] CRITICAL Error in main cleanup loop: {e}", exc_info=True)
            # Sleep for a short fallback interval to avoid tight error loops if something is fundamentally broken
            await asyncio.sleep(60) # Sleep 1 minute before retrying after a critical error

    logging.info("[CleanupTask] Background periodic cleanup task stopped.")