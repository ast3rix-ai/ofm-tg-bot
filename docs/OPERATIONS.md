# Operations

This document covers day-to-day operation. Sections are added as features land.

## Starting and stopping the bot

```
.venv\Scripts\activate
python -m src.main
```

First-run prompts on stdin for the SMS code and 2FA password if set. Subsequent runs reuse the encrypted session at `data/session.enc` and skip auth.

Stop with `Ctrl+C` (SIGINT). The bot shuts down gracefully:

1. Watchdog stops.
2. Telethon disconnects.
3. The decrypted session is re-encrypted to `data/session.enc` and the temp directory `data/.session_tmp/` is removed.
4. A `Bot stopped` alert is sent to the operator chat.

If you see `data/.session_tmp/` left behind after a stop, the shutdown did not complete cleanly — re-check `logs/bot.log` for the trailing entries.

## Inspecting state

Three read-only views:

```
python scripts/inspect_db.py contacts
python scripts/inspect_db.py messages <chat_id> [--limit 30]
python scripts/inspect_db.py events  [--limit 50]
```

`events` includes heartbeats, reconnects, and any error events caught in the event handler. Useful for confirming the watchdog is alive and that nothing is silently failing.

The live log lives at `logs/bot.log` (current day) and gzip-rotated daily for 14 days. The current file is open by the bot — use `Get-Content -Tail 50 -Wait logs\bot.log` (PowerShell) or `tail -f` (Git Bash) to follow.

## Verifying the watchdog (simulated disconnect)

1. Start the bot.
2. Disable the network adapter, or block Telegram via firewall.
3. Within `HEARTBEAT_INTERVAL_SECONDS` (default 60s), `logs/bot.log` should show `Userbot disconnected — attempting reconnect.` followed by backoff messages (`1`, `2`, `4`, `8`, `16`, `32`, then `60` second waits).
4. Re-enable the network. The next reconnect attempt should succeed and an `Reconnected` log line should appear; a `reconnect` event is recorded in the DB.
5. To trigger the operator alert, leave the network off for >5 minutes. A `🚨 Userbot disconnected >5min` message should arrive in the notifier chat. Subsequent re-fires of the same alert key are suppressed for 60 seconds.

## Rotating the session encryption key

The Fernet key in `SESSION_ENCRYPTION_KEY` protects `data/session.enc` at rest. To rotate:

1. Stop the bot cleanly (so `session.enc` is freshly written with the *old* key).
2. Decrypt the session under the old key to a temporary file:
   ```
   .venv\Scripts\activate
   python -c "from pathlib import Path; from src.crypto import decrypt_file; decrypt_file(Path('data/session.enc'), Path('data/.session_tmp/userbot.session'), 'OLD_KEY')"
   ```
3. Generate a new key: `python scripts/generate_key.py`.
4. Re-encrypt under the new key:
   ```
   python -c "from pathlib import Path; from src.crypto import encrypt_file; encrypt_file(Path('data/.session_tmp/userbot.session'), Path('data/session.enc'), 'NEW_KEY')"
   ```
5. Delete the decrypted temp file: `rm data/.session_tmp/userbot.session`.
6. Replace `SESSION_ENCRYPTION_KEY` in `.env` with the new key.
7. Start the bot — confirm it connects without re-auth.

If you ever lose the encryption key, the session blob is unrecoverable. Recovery requires a fresh login (SMS code + 2FA), which Telegram will treat as a new device and may flag.

## When the bot won't start

- `ConfigError: missing ...` — fill the missing keys in `.env`. `.env.example` lists what's required.
- `Failed to decrypt session — wrong key or corrupt file.` — the `SESSION_ENCRYPTION_KEY` doesn't match the one used to write `data/session.enc`. If you have no backup of the old key, delete `data/session.enc` and re-auth.
- Notifier alerts not arriving — confirm the `NOTIFIER_BOT_TOKEN` is the right bot, `NOTIFIER_CHAT_ID` is the numeric chat ID (not the username), and that you have sent at least one message to the bot from that chat (Telegram bots cannot DM users who have never messaged them).
