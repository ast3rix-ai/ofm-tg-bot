# Architecture

The bot is a single-process async Python application with eight logical components. Components are layered: lower components have no knowledge of upper ones.

## Components

### 1. Telegram client layer (`src/telegram_client.py`)
Thin wrapper around Telethon. Handles connection, session encryption at rest, reconnection. Knows nothing about classification, persona, or response generation. Exposes: connect, disconnect, send_message, mark_read, send_typing, event subscription.

### 2. Storage (`src/storage.py`)
SQLite. Tables: `contacts`, `messages`, `events`, plus phase-additive tables (`contact_state`, `contact_memory`, etc.) added as phases land. All access via module functions. No ORM.

### 3. Event handler (`src/event_handler.py`)
Subscribes to incoming DM events from the Telegram client. For each message: persist, then dispatch to the pipeline. Holds per-chat asyncio locks. Catches and logs all exceptions; never lets the handler die silently.

### 4. Signal detector (Phase 4+)
Fast deterministic pattern matcher. Runs on every inbound message before the classifier. Detects events that force state transitions regardless of LLM judgment: explicit price asks, payment screenshots, slurs, prolonged silence on outbound. Cheap, regex- and keyword-based.

### 5. Classifier (Phase 4+)
Hybrid: rules-first for high-frequency obvious cases (menu requests, price requests, common openers), LLM for ambiguous cases. Outputs category + confidence. Below-threshold confidence routes to operator handoff. Re-runs on each turn.

### 6. Response generator (Phase 5+)
LLM call. Inputs: persona document, distilled contact memory, rolling message window (~30), category-specific instruction. Output: reply text, possibly multi-part. Output validation rejects AI-tell patterns and triggers re-roll.

### 7. Humanization layer (Phase 6+)
Sits between response generator and Telegram client. Splits long replies into 2-4 messages with inter-message delays. Inserts occasional typos at human rates. Computes typing-indicator duration from reply length. Randomizes total response latency based on time of day and category (cold leads: minutes-to-hours; hot leads: seconds-to-minutes).

### 8. Routing rules + handoff (Phase 7+)
Category → action map. Some categories auto-respond; some queue for operator review in a control chat; some are ignored. Operator can take over any chat from the control chat, which sets a `human_active` flag suppressing bot replies until cleared.

### 9. Control interface (Phase 3, expanded Phase 7+)
Local FastAPI app + minimal HTML frontend. Account management (login persistence, swap between accounts), live log viewing, contact inspection, manual chat override. Runs on `localhost` only.

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
