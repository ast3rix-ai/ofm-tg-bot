# ofm-tg-bot

A Telegram userbot for OnlyFans agency DM management — automated persona-based responses, classification, and routing for a personal Telegram account operated on behalf of a content creator. Built as a single-process async Python application using Telethon, SQLite, and a local uncensored LLM.

**Status:** under active development — currently Phase 1 (foundation, watchdog, logging). See [`docs/PHASES.md`](docs/PHASES.md) for the phase plan and [`CLAUDE.md`](CLAUDE.md) for development conventions that all contributions must follow.

## Setup

1. **Install Python 3.12 (or newer).** Windows 11 is the supported development host.

2. **Create the virtual environment and install dependencies.**
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -e ".[dev]"
   ```
   If you have [`uv`](https://github.com/astral-sh/uv) installed, `uv sync` works as well.

3. **Get Telegram API credentials.** Log into <https://my.telegram.org> with the phone number of the account you intend to operate, open "API development tools", and create an application. Record `api_id` and `api_hash`.

4. **Create a notifier bot.** This is a *separate* regular bot (not the userbot) used only for operator alerts. In Telegram, message `@BotFather`, run `/newbot`, follow the prompts, and record the token. Then send any message to the new bot from your operator account so it has a chat with you, and fetch your chat ID at
   `https://api.telegram.org/bot<TOKEN>/getUpdates` — the numeric `chat.id` of the message you just sent.

5. **Generate the session encryption key.**
   ```
   python scripts/generate_key.py
   ```
   The key is printed to stdout. Paste it into `.env` under `SESSION_ENCRYPTION_KEY=`.

6. **Populate `.env`.** Copy `.env.example` to `.env` and fill in every value.

7. **First run.**
   ```
   python -m src.main
   ```
   Telethon will prompt on stdin for the SMS login code, and for the 2FA password if one is set on the account. Subsequent runs reuse the encrypted session and skip the prompts.

8. **Inspecting the DB.** A read-only CLI is provided:
   ```
   python scripts/inspect_db.py contacts
   python scripts/inspect_db.py messages <chat_id> [--limit N]
   python scripts/inspect_db.py events [--limit N]
   ```

See [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for additional operational notes (watchdog verification, key rotation, etc.).
