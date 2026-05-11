# Operations

This document covers day-to-day operation. Sections are added as features land.

## Starting and stopping the bot

```
.venv\Scripts\activate
python -m src.main
```

This starts the bot host (UI + lifecycle manager). The process does **not** auto-connect to Telegram until an account is marked active.

Open `http://127.0.0.1:8765` in a browser. From there:

- **First run after Phase 2 upgrade.** If your `.env` has `TG_API_ID` / `TG_API_HASH` / `TG_PHONE`, those are imported as the "Default" account during migration 003 and marked active. If `UI_AUTO_ACTIVATE=true` (the default) and the legacy session was successfully converted, the bot reconnects automatically within ~5 seconds — green badge in the top-right.
- **If the legacy session couldn't be converted**, the Default account exists but has no session blob. Click **View** → **Start Auth** to re-authenticate (one-time).
- **Adding a new account.** Click **+ Add Account**, fill in label / API ID / API hash / phone, save. The form redirects you to the auth wizard. Click **Start Auth**, then enter the SMS code from Telegram when prompted, then the 2FA password if your account has one. The page transitions to the green "running" state on success.
- **Swapping accounts.** On `/accounts`, click **Activate** on a different row. The previously-active account is deactivated automatically before the new one starts.
- **Inspecting a chat.** Navigate to **Chats**, pick the right account in the dropdown, click a contact, view the last 100 messages.

Stop with `Ctrl+C` in the terminal that runs `python -m src.main`. The bot:

1. Deactivates the active account (watchdog → Telethon disconnect → final session save → set `is_active=0`).
2. Shuts down uvicorn.
3. Fires a `Bot host stopped` alert if a notifier is configured.

Sessions live encrypted inside `accounts.session_blob_enc` only — there is no longer a `data/session.enc` file or a `data/.session_tmp/` directory. A clean shutdown will not leave plaintext credentials on disk.

## Inspecting state

Read-only views:

```
python scripts/inspect_db.py contacts
python scripts/inspect_db.py messages <chat_id> [--limit 30]
python scripts/inspect_db.py events     [--limit 50]
python scripts/inspect_db.py state      <chat_id>
python scripts/inspect_db.py memory     <chat_id>
python scripts/inspect_db.py migrations
```

`state` and `memory` will print `(no state for chat <id>)` / `(no memory for chat <id>)` until Phase 4 begins writing to those tables.

`events` includes heartbeats, reconnects, and any error events caught in the event handler. Useful for confirming the watchdog is alive and that nothing is silently failing.

The live log lives at `logs/bot.log` (current day) and gzip-rotated daily for 14 days. The current file is open by the bot — use `Get-Content -Tail 50 -Wait logs\bot.log` (PowerShell) or `tail -f` (Git Bash) to follow.

## Database

Schema lives in `src/migrations.py` as a forward-only `MIGRATIONS` list. Each entry is `(version, name, sql)`. `init_db()` applies any pending migrations on every start; the `schema_migrations` table records which versions have already been applied.

Inspect applied migrations:

```
python scripts/inspect_db.py migrations
```

**No down migrations.** Migrations are forward-only by design — there is no rollback path in code. If a migration is wrong and you need to roll back, restore `data/bot.db` from a backup (a plain file copy is sufficient when the bot is stopped). Treat each schema change as an irreversible commit; review the migration SQL carefully before merging.

When adding a new migration:

1. Append a new `Migration(version=N, name="...", sql="...")` to `MIGRATIONS` in `src/migrations.py`.
2. Use `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` so re-runs are safe.
3. Do not alter previously-published migrations — they have already executed on real databases.
4. Run `pytest tests/test_migrations.py` to confirm fresh and upgrade paths both work.

Foreign keys are enforced. Rows in `contact_state` / `contact_memory` must reference an existing `contacts.chat_id` (the event handler upserts the contact before any pipeline step writes state, so this is automatic in normal operation).

## First connect to an existing account (bootstrap)

When the bot activates an account that has existing dialogs without `bootstrap_completed_at`, it enters the `bootstrapping` state. The footer badge turns yellow with progress `(done/total)`. For each existing chat the bot pulls up to `BOOTSTRAP_HISTORY_MESSAGES` messages (or the messages from the last `BOOTSTRAP_HISTORY_DAYS` days — whichever is larger), persists them, and runs `Classifier.bootstrap_chat`. Concurrency is capped at `BOOTSTRAP_MAX_CONCURRENT`.

**Bootstrapped chats have `bot_enabled = 0`.** The bot must not reply until the operator flips the toggle in `Chats → <contact> → State → bot: OFF/ON`. Chats seen live from message 1 have `bot_enabled = 1` by default.

After bootstrap, the bot runs an **unread catchup** for every dialog with `unread_count > 0` (messages missed while the bot was offline). Each unread message is classified the same way as a live one. State transitions to `running` once both phases finish.

## Reviewing classifier output

`/chats?account_id=<id>` shows category per contact. Click a contact to open the chat detail — the right rail shows the latest classifier output, confidence, flags, and a collapsible **Classifier runs** section with the last 10 runs (timestamp, trigger source, category-before → category-after, latency, raw LLM output).

Classifier runs include rule-based fast paths (`triggered_by = "rule:greeting"`) — those have zero latency and an empty raw output.

## Acknowledging operator alerts

`/alerts` lists every `operator_alerts` row. Types so far:

- `threat_detected` — signal-detector or classifier flagged a real-world threat. Notifier fires immediately too (deduped 1/chat/hour). Red row.
- `low_confidence` — classifier confidence under `CLASSIFIER_CONFIDENCE_THRESHOLD`. Routing in Phase 7 will use these to route to human handoff.
- `classifier_parse_failure` — LLM output couldn't be parsed twice. State left untouched; review raw output via the alert's payload.
- `bootstrap_failed` — bootstrap LLM call failed or output was malformed. The chat stays unbootstrapped; re-running the bot retries it.

Use the **Ack** button to mark an alert resolved. Acked alerts stay in the table for audit; they just disappear from the "only un-acknowledged" filter.

## Tuning the confidence threshold

`CLASSIFIER_CONFIDENCE_THRESHOLD` in `.env` controls when `low_confidence` alerts fire. Default 0.6. Raise to be stricter (more handoffs), lower to be looser. Change takes effect on next bot start.

## What to do if a chat is mis-classified

The classifier re-runs on every new inbound message, so misclassifications self-correct as the conversation continues. If you need to force a correction now:

1. Open the chat detail page.
2. Note the current category and the `bot_enabled` state.
3. (Phase 7 will add a manual "re-run classifier" button. For now, the next inbound message triggers a re-run automatically.)

If the bot is mis-replying (Phase 5+), flip `bot_enabled` off and handle the chat by hand.

## Verifying the watchdog (simulated disconnect)

1. Start the bot.
2. Disable the network adapter, or block Telegram via firewall.
3. Within `HEARTBEAT_INTERVAL_SECONDS` (default 60s), `logs/bot.log` should show `Userbot disconnected — attempting reconnect.` followed by backoff messages (`1`, `2`, `4`, `8`, `16`, `32`, then `60` second waits).
4. Re-enable the network. The next reconnect attempt should succeed and an `Reconnected` log line should appear; a `reconnect` event is recorded in the DB.
5. To trigger the operator alert, leave the network off for >5 minutes. A `🚨 Userbot disconnected >5min` message should arrive in the notifier chat. Subsequent re-fires of the same alert key are suppressed for 60 seconds.

## Account credential rotation

To rotate an account's `api_id` / `api_hash` / `phone`:

1. Stop the bot.
2. Delete the row via the UI **or** with a SQL update (see below).
3. Add the account again via the UI with the new credentials.
4. Re-authenticate.

There is intentionally no in-UI "edit credentials" — credential changes on Telegram's side usually require re-authenticating anyway, and editing the encrypted blob piecemeal invites partial-update bugs.

## Deleting an account

The **Delete** button on the accounts list **cascades**: every contact, message, conversation state, and memory row for that account is removed. Events for the deleted account have their `account_id` set to NULL (the event history is preserved as system-level rows). Operational logs in `logs/bot.log` are untouched.

If you want to keep the data, do not delete the account — set it inactive instead and leave the row in place.

## Rotating the session encryption key

The Fernet key in `SESSION_ENCRYPTION_KEY` protects every encrypted column in `accounts` (`tg_api_id_enc`, `tg_api_hash_enc`, `tg_phone_enc`, `session_blob_enc`). To rotate without losing sessions:

1. Stop the bot.
2. Run the rotation helper (writes new ciphertexts in place under a transaction):

   ```
   .venv\Scripts\activate
   python -c "
   from pathlib import Path
   from src.accounts import list_accounts, get_account, _fernet, _connect
   OLD = 'OLD_KEY'
   NEW = 'NEW_KEY'
   fo, fn = _fernet(OLD), _fernet(NEW)
   db = Path('data/bot.db')
   with _connect(db) as conn:
       rows = conn.execute('SELECT id, tg_api_id_enc, tg_api_hash_enc, tg_phone_enc, session_blob_enc FROM accounts').fetchall()
       conn.execute('BEGIN')
       for r in rows:
           def re(s):
               if s is None: return None
               return fn.encrypt(fo.decrypt(s.encode())).decode()
           conn.execute('UPDATE accounts SET tg_api_id_enc=?, tg_api_hash_enc=?, tg_phone_enc=?, session_blob_enc=? WHERE id=?',
               (re(r[1]), re(r[2]), re(r[3]), re(r[4]), r[0]))
       conn.execute('COMMIT')
   print('rotated', len(rows), 'accounts')
   "
   ```

3. Replace `SESSION_ENCRYPTION_KEY` in `.env` with the new key.
4. Start the bot — it should reconnect without re-auth.

If you lose the encryption key, the encrypted columns are unrecoverable: every account must be re-added and re-authenticated.

## When the bot won't start

- `ConfigError: missing ...` — fill the missing keys in `.env`. `.env.example` lists what's required.
- `Failed to decrypt session — wrong key or corrupt file.` — the `SESSION_ENCRYPTION_KEY` doesn't match the one used to write `data/session.enc`. If you have no backup of the old key, delete `data/session.enc` and re-auth.
- Notifier alerts not arriving — confirm the `NOTIFIER_BOT_TOKEN` is the right bot, `NOTIFIER_CHAT_ID` is the numeric chat ID (not the username), and that you have sent at least one message to the bot from that chat (Telegram bots cannot DM users who have never messaged them).
