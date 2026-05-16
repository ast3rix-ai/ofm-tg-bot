# Architecture

The bot is a single-process async Python application with eight logical components. Components are layered: lower components have no knowledge of upper ones.

## Components

### 1. Telegram client layer (`src/telegram_client.py`)
Thin wrapper around Telethon. Handles connection, session encryption at rest, reconnection. Knows nothing about classification, persona, or response generation. Exposes: connect, disconnect, send_message, mark_read, send_typing, event subscription.

### 2. Storage (`src/storage.py`, `src/migrations.py`)
SQLite. Schema is applied via a forward-only versioned migration list in `src/migrations.py`; `init_db()` is idempotent and safe to run against an upgrading database. The `schema_migrations` table records which versions have been applied. Foreign keys are enforced (`PRAGMA foreign_keys = ON` on every connection).

Tables in the current schema:

- **`contacts`, `messages`, `events`** — Phase 1 surface area. Incoming DMs persist into `messages` (`UNIQUE(chat_id, tg_message_id, direction)` for idempotent replays); contacts upsert on every inbound; operational events (heartbeats, reconnects, handler errors) accumulate in `events`.
- **`contact_state`** — Phase 2 surface area, populated from Phase 4+. One row per chat. Holds the current classifier output (`category`, `funnel_stage`, `last_classifier_confidence`, raw `classifier_metadata`), behavioral flags (`flags` JSON, `human_active`, `human_active_until`), and an `updated_at` timestamp. Drives routing decisions and operator-takeover suppression.
- **`contact_memory`** — Phase 2 surface area, populated from Phase 4+. One row per chat. Holds the distilled per-contact `facts` (JSON), the rolling `summary`, the `summary_message_count` watermark, and `last_summarized_at`. Survives context-window truncation in the response generator.

All access goes through module-level functions; there is no ORM. JSON columns are decoded into dicts on read and serialized on write by the `upsert_*` helpers, which accept only whitelisted field names (typed via `TypedDict` + PEP 692 `Unpack`).

### 3. Event handler (`src/event_handler.py`)
Subscribes to incoming DM events from the Telegram client. For each message: persist, then dispatch to the pipeline. Holds per-chat asyncio locks. Catches and logs all exceptions; never lets the handler die silently.

### 4. Signal detector (`src/signal_detector.py`)
Fast deterministic pattern matcher. Runs on every inbound message before the classifier. Each detector is independent and pure (regex/keyword on text, plus a DB lookup for `detect_resurface`). Output is a frozen `SignalResult`. Used by the classifier's fast path (greeting-only → skip LLM) and by the threat-alert path (signal-detector threats fire alerts even if the LLM classification is benign). Conservative keyword sets — false negatives are acceptable, false positives create alert fatigue.

### 5. Classifier (`src/classifier.py`)
Hybrid: rules-first fast path for greeting-only openers on fresh chats (no LLM call), LLM call otherwise. Output validated against a fixed JSON schema and the closed category set in `docs/CATEGORIES.md`; parse failures retry once with a stricter prompt then fall back to recording an `operator_alerts` row with type `classifier_parse_failure`, leaving prior `contact_state` untouched. Below-threshold confidence emits a `low_confidence` alert. Threats from either the signal detector or the classifier fire `notifier.alert()` synchronously and write a `threat_detected` alert (deduped per chat per hour). Every run is recorded in `classifier_runs` for forensics.

### 5b. Backlog processor (`src/backlog.py`)
Orchestrates two catchup flows the `BotManager` runs on every account activation: (a) **initial bootstrap** — for chats without `bootstrap_completed_at`, pull `max(N messages, last N days)` of history, persist, run `Classifier.bootstrap_chat`; (b) **unread catchup** — for chats with `unread_count > 0`, fetch the unread tail and push through the normal signal-detector + classifier path. Concurrency bounded by per-phase semaphores. Progress visible via `BotManager.status().backlog`.

### 6. Response generator (`src/response_generator.py`, Phase 5+)
Runs after the classifier on every inbound message. Inputs: persona document, distilled contact memory, rolling message window (last 20), category-specific instruction overlay. Output: a single reply text (no multi-part — humanization is Phase 6).

Flow per inbound message:

1. **Gate check (fast path, no LLM call).** Reads `contact_state`. The bot replies only when `bot_enabled = 1` AND `category != 'paid'` AND the `human_active` flag is false. A failing gate records a `response_runs` row with `outcome='gated'` and the `gate_reason` (`bot_disabled` | `category_paid` | `human_active`) and returns. A chat with no `contact_state` row falls back to `DEFAULT_BOT_ENABLED_NEW_CHATS`.
2. **Prompt build.** The persona file (`RESPONSE_PERSONA_PATH`) is read once, cached, and re-read when its mtime changes — edits land on the next reply without a restart. `response_runs.persona_version` stores the mtime ISO string so audits can correlate.
3. **Generate loop.** Up to `RESPONSE_MAX_RETRIES + 1` attempts. Each attempt is cleaned (whitespace + wrapping quotes stripped) and run through `output_validator`. The first valid attempt wins; rejected attempts are appended to `raw_attempts`.
4. **Send.** Acquires the per-chat asyncio lock (shared with the event handler), sends atomically via `telegram_client.send_message`, persists the outbound `messages` row, the `response_runs` row (`outcome='sent'`), and a `bot_sent_messages` row tying the Telegram message id to the run.

Outcomes: `sent`, `gated`, `validator_rejected_all_attempts` (all re-rolls failed — nothing sent), `failed` (LLM or send error). Every call writes exactly one `response_runs` audit row and never raises for ordinary failures.

### 6b. Output validator (`src/output_validator.py`, Phase 5+)
Deterministic, LLM-free check. Rejects AI-tell regex patterns (`as an AI`, `I cannot`, em-dashes, stage directions, sign-offs, …), replies over 280 chars, double-newline prose, and replies wrapped in quotes. Returns `ValidationResult(valid, reason)`.

### 6c. Operator commands (`src/commands.py`, Phase 5+)
`/reset` wipes a chat's `contact_state` + `contact_memory` so the next inbound message is treated as a brand-new conversation. The audit trail (`messages`, `classifier_runs`, `response_runs`, `bot_sent_messages`) is preserved. Honoured only from `OPERATOR_USER_IDS`; from anyone else `/reset` is ordinary text. Processed inline in the event handler before classification.

### 7. Humanization layer (Phase 6+)
Sits between response generator and Telegram client. Splits long replies into 2-4 messages with inter-message delays. Inserts occasional typos at human rates. Computes typing-indicator duration from reply length. Randomizes total response latency based on time of day and category (cold leads: minutes-to-hours; hot leads: seconds-to-minutes).

### 8. Routing rules + handoff (Phase 7+)
Category → action map. Some categories auto-respond; some queue for operator review in a control chat; some are ignored. Operator can take over any chat from the control chat, which sets a `human_active` flag suppressing bot replies until cleared.

### 9. Control interface (Phase 3, expanded Phase 7+)
Local FastAPI app served by uvicorn, mounted into the same process as the bot. Bound strictly to `127.0.0.1:8765` (configurable via `UI_PORT`). No authentication — single-operator local tool. Server-rendered Jinja2 templates with Tailwind + Alpine.js loaded via CDN; no frontend build step.

Routes:
- `/accounts` — list, add (`/accounts/new` form), activate / deactivate / delete, per-account detail page.
- `/accounts/<id>/auth` — auth wizard that drives `BotManager.activate()` in the background, polls `/system/status`, and prompts for phone code / 2FA password via Alpine forms.
- `/chats?account_id=<id>` — contacts list. `/chats/<chat_id>?account_id=<id>` — chat detail with last 100 messages and Phase 4-populated state/memory side panels.
- `/logs` + `/logs/stream` — live SSE tail of `logs/bot.log`.
- `/system/status` — JSON snapshot of bot state, active account, applied migrations, uptime.

The web layer never imports Telethon directly — it drives `BotManager`, which owns the lifecycle of the single active `BotClient`.

## Activation state machine (Phase 4+)

```
idle → starting → awaiting_code → awaiting_password → bootstrapping → running
                                                                    ↘ error
running → stopping → idle
```

`bootstrapping` covers both initial-bootstrap and unread-catchup phases. The live event handler is registered before bootstrap starts; new live messages during this window are persisted but their classification is deferred onto an in-memory pending list, drained by `EventHandler.flush_pending()` once `bootstrapping → running`.

## LLM layer

`src/llm/client.py` wraps `ollama.AsyncClient`. `LLMClient.generate()` retries connection-level failures (tenacity, exponential backoff, configurable max retries); model-side errors surface as `LLMError` without retry. `LLMClient.ping()` is the health check shown in `/system/status` and the UI footer. Model selection lives in `docs/MODEL_SELECTION.md` and is set via `LLM_MODEL` in `.env` — no hard-coding anywhere in the code.

Prompts (`src/llm/prompts.py`) are centralized as pure functions returning strings. Each prompt embeds the category taxonomy and a fixed JSON output schema; few-shot examples are synthetic until Phase 8 supplies real samples (marked `TODO(phase-8)`).

## Multi-account data model (Phase 3+)

Migration 003 makes the bot multi-account at the data layer:

- `accounts` — one row per operated Telegram account. Holds Fernet-encrypted `api_id`, `api_hash`, `phone`, and `session_blob` (Telethon `StringSession` text). Exactly one row is `is_active=1` at a time, enforced by `accounts.set_active_account()`.
- All Phase 1/2 row tables (`contacts`, `messages`, `events`, `contact_state`, `contact_memory`) gain an `account_id` column with FKs back to `accounts(id)`.
- `contacts`, `contact_state`, `contact_memory` use a composite PK `(account_id, chat_id)`, allowing the same Telegram `chat_id` to exist across different operated accounts.
- `messages.account_id` and (transitively) `contacts.account_id` cascade-delete on account removal; `events.account_id` is nullable for system-wide rows and `ON DELETE SET NULL`.

The Phase 2 → Phase 3 upgrade preserves data: existing rows are copied into the rebuilt tables with `account_id = <default-id>`, and the `.env`'s legacy `TG_API_ID`/`TG_API_HASH`/`TG_PHONE` become the seed for that default account.

## Session lifecycle (Phase 3+)

Sessions live only inside the Fernet-encrypted `accounts.session_blob_enc` column. There is no longer a `.session` file on disk and no plaintext temp file. `BotClient` uses Telethon's `StringSession`, and any session change triggers `accounts.update_session_blob()` via the `on_session_update` callback.

This eliminates the Phase 1 risk of a loose decrypted session file being left behind on abnormal exit.

## Data flow (steady state, Phase 7+ complete)

```
incoming DM
    │
    ▼
event_handler ──► storage.insert_message
    │
    ▼
signal_detector ──► forced state transitions (if any)
    │
    ▼
classifier ──► category + confidence
    │
    ├─ low confidence ──► handoff queue
    │
    ▼
routing_rules ──► action decision
    │
    ├─ ignore ──► done
    ├─ handoff ──► operator alert
    │
    ▼
response_generator ──► reply text
    │
    ▼
output_validator ──► reject + re-roll if AI-tells
    │
    ▼
humanization ──► split + delays + typos
    │
    ▼
telegram_client.send_typing → wait → send_message
    │
    ▼
storage.insert_message (outbound)
```

## Memory model

Three layers, all per-contact:

1. **Rolling window** — last ~30 messages, raw, fed into LLM context.
2. **Distilled memory** — structured facts extracted by the LLM and updated over time: name, claimed interests, money signals, prior purchases, kinks mentioned, pet names, ghosting patterns. Survives context truncation. Stored as JSON in `contact_memory.facts`.
3. **Conversation summary** — regenerated every N messages, replaces older history when the rolling window overflows. Stored in `contact_memory.summary`.

## Reliability

- Watchdog (`src/watchdog.py`) heartbeats every 60s, attempts reconnect on disconnect with exponential backoff, alerts operator after sustained failure.
- All exceptions in event handlers caught, logged, alerted. Handler re-subscribes if it died.
- Notifier (`src/notifier.py`) uses a separate regular Telegram bot (not the userbot) so notifications survive userbot-account problems.
- Rate-limited alerts (1/min per alert key) prevent notification spam.

## Account safety posture

- Outbound rate cap (Phase 5+): max N messages per hour across all chats, configurable.
- No simultaneous typing in many chats — staggered.
- No replies between configured "sleep hours" unless the chat is flagged hot.
- Session encrypted at rest; multi-account support designed so each account = isolated session file + isolated DB row.
