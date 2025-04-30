# Telegram Session OTP Forwarder

[![GitHub Repo stars](https://img.shields.io/github/stars/vilas-error-404/session-to-otp?style=social)](https://github.com/vilas-error-404/session-to-otp)

A Telegram bot designed to manage user `.session` files (Pyrogram/Telethon), listen for OTP (One-Time Password) login codes sent to those sessions by Telegram, and forward them securely to the user.

**Disclaimer:** Managing session files grants significant access to Telegram accounts. Use this bot responsibly and **at your own risk**. Ensure the server hosting this bot is secure. The author is not responsible for any misuse or security breaches.

## Key Features

*   📁 **Session Upload:** Accepts archives (`.zip`, `.rar`, `.7z`, etc.) containing `.session` files.
*   🛡️ **Validation:** Attempts to validate sessions using both Pyrogram and Telethon to ensure compatibility and basic validity.
*   🔒 **Secure Storage:** Stores the *session string* (not the file itself) in a local SQLite database.
*   🎧 **OTP Listening:** Allows users to activate ("listen") specific sessions temporarily.
*   📨 **OTP Forwarding:** Automatically detects and forwards login codes sent by Telegram to the corresponding user.
*   ⏱️ **Auto-Stop:** Listening sessions automatically stop after a configurable duration (default: 30 minutes) to conserve resources.
*   🎛️ **Session Management:** Provides an inline keyboard interface (`/manage_sessions`) to:
    *   ▶️ Start listening on a session.
    *   ⏹️ Stop listening on an active session.
    *   🗑️ Delete a stored session string.
    *   🚀 Start all available inactive sessions (up to the concurrent limit).
    *   🛑 Stop all currently active listening sessions.
    *   Pagination for large numbers of sessions.
*   📊 **Status & Control:** Commands to check status (`/status`), get help (`/help`), and manually stop/delete sessions via commands (`/stop_session`, `/delete_session`).
*   ⚙️ **Configuration:** Uses environment variables (`.env` file) for critical settings (Bot Token, API ID/Hash).
*   🧹 **Background Cleanup:** Periodically cleans up temporary downloaded and extracted files.

## How it Works

1.  **Upload:** The user sends an archive file (e.g., `my_sessions.zip`) containing their `.session` file(s) to the bot in a private chat.
2.  **Extract & Validate:** The bot downloads the archive, extracts its contents to a temporary location, and scans for `.session` files. It then attempts to connect using each session file (trying both Pyrogram and Telethon methods) to validate it and extract the internal session string.
3.  **Store:** Valid session strings are stored securely in the bot's local SQLite database, associated with the user's Telegram ID and a chosen session name (derived from the filename).
4.  **Manage & Activate:** The user uses the `/manage_sessions` command to view their stored sessions. They can press the ▶️ button next to a session name.
5.  **Listen:** The bot starts a temporary, in-memory client (Pyrogram or Telethon) using the stored session string. This client connects to Telegram and actively listens *only* for messages from the official Telegram account (ID 777000).
6.  **Forward:** If the temporary client receives a message from Telegram containing a login code pattern, the bot extracts the code and sends it as a message to the user's chat with the bot.
7.  **Stop:** The listening session automatically stops after the configured duration (e.g., 30 minutes), or the user can manually stop it using the ⏹️ button or the `/stop_session` command. Deleting a session also implicitly stops it if active (though deletion requires it to be stopped first).

## Requirements

*   **Python:** 3.8 or higher recommended.
*   **Pip:** Python package installer.
*   **Telegram Bot Token:** Obtainable from [@BotFather](https://t.me/BotFather) on Telegram.
*   **Telegram API Credentials:**
    *   `API_ID`
    *   `API_HASH`
    *   Get these from [my.telegram.org](https://my.telegram.org/apps).
*   **`patoolib` Backends:** System-level archive utilities are needed for extraction. Install based on your OS:
    *   **Debian/Ubuntu:** `sudo apt update && sudo apt install unzip unrar p7zip-full`
    *   **Fedora/CentOS:** `sudo dnf install unzip unrar p7zip`
    *   **macOS (Homebrew):** `brew install unrar p7zip` (zip/unzip usually pre-installed)
    *   **Windows:** Ensure `7z.exe`, `rar.exe`, etc., are in your system's PATH or install them.

## Setup & Installation

1.  **Clone the Repository:**
    ```bash
    git clone https://github.com/vilas-error-404/session-to-otp.git
    cd session-to-otp
    ```

2.  **Create a Virtual Environment (Recommended):**
    ```bash
    python -m venv venv
    # Activate the environment
    # Linux/macOS:
    source venv/bin/activate
    # Windows:
    .\venv\Scripts\activate
    ```

3.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
    *(See `requirements.txt` below)*

4.  **Install `patoolib` Backends:** Make sure you have the necessary system packages installed (see Requirements section above).

5.  **Configure Environment Variables:**
    *   Create a file named `.env` in the project's root directory.
    *   Add your credentials to the `.env` file.

    **.env File Template:**
    ```dotenv
    # --- Telegram Bot Credentials ---
    BOT_TOKEN=YOUR_BOT_TOKEN_HERE

    # --- Telegram API Credentials ---
    API_ID=YOUR_API_ID_HERE
    API_HASH=YOUR_API_HASH_HERE

    # --- Optional Configuration (Defaults are in config.py) ---
    # MAX_CONCURRENT_ACTIVE_SESSIONS_PER_USER=10
    # SESSION_LISTEN_DURATION=1800 # 30 minutes in seconds
    # SESSIONS_PER_PAGE=10
    # BACKGROUND_CLEANUP_INTERVAL_SECONDS=7200 # 2 hours
    # BACKGROUND_CLEANUP_AGE_THRESHOLD_SECONDS=3600 # 1 hour
    ```

6.  **Run the Bot:**
    ```bash
    python main.py
    ```

## Usage

Interact with the bot primarily through Telegram commands and the inline buttons provided:

*   `/start`: Welcome message and main menu buttons.
*   `/help`: Shows instructions on how the bot works.
*   `/status`: Displays current bot status, including the number of stored sessions, active listeners, and the last recorded error.
*   `/manage_sessions`: The main command to view, start, stop, and delete your stored sessions via inline keyboards.
*   `/stop_session <SessionName>`: Manually stop listening on a specific active session.
*   `/delete_session <SessionName>`: Initiate the process to permanently delete a specific stored session (requires confirmation).
*   **Send Archive:** Send a `.zip`, `.rar`, `.7z`, etc., file containing `.session` files to the bot in a private chat to add them.
