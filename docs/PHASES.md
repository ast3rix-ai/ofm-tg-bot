# Phases

Each phase is independently shippable. Do not implement features from future phases. Mark phase completion by updating the `Status` field below.

## Phase 0 — Repo scaffolding and docs
**Status:** in progress
**Scope:** Directory skeleton, `.gitignore`, `CLAUDE.md`, `docs/`, git init, first push.
**Acceptance:** Repo exists locally and on GitHub, all docs present.

## Phase 1 — Foundation, watchdog, logging
**Status:** not started
**Scope:** Telethon connection, encrypted session, SQLite schema, structured logging, watchdog, notifier bot, inspection CLI.
**Out of scope:** any reply logic, classification, LLM, persona, marking read, typing indicators.
**Acceptance:** see Phase 1 prompt.

## Phase 2 — State store deepening
**Status:** not started
**Scope:** Additional tables for contact state (category, funnel stage, flags), distilled memory stub, conversation summary stub. Migration mechanism. Inspection CLI expanded.
**Out of scope:** anything that writes to these tables — populated in Phase 4+.

## Phase 3 — Control UI shell + multi-account
**Status:** not started
**Scope:** FastAPI local web app. Account management (add account, store credentials encrypted, list accounts, switch active account). Live log tail view. Contact list + chat history viewer. No write operations yet.
**Out of scope:** manual chat takeover, message sending from UI.

## Phase 4 — Signal detector + classifier
**Status:** not started
**Scope:** Local LLM integration (model selection happens here, after research). Rules-first signal detector. Hybrid classifier with confidence thresholds. Categories defined in `docs/CATEGORIES.md`. Populates `contact_state` table on every inbound.
**Out of scope:** outbound messaging.

## Phase 5 — Response generator
**Status:** not started
**Scope:** LLM-driven reply generation. Persona document loaded from disk. Rolling window + distilled memory + summary feeding the prompt. Output validation with AI-tell blacklist and re-roll. Sends replies via Telegram client. Per-chat lock enforcement.
**Out of scope:** humanization (sends are immediate and atomic in this phase — humanization is Phase 6).

## Phase 6 — Humanization layer
**Status:** not started
**Scope:** Message splitting, inter-message delays, typing indicators, response latency distributions by category and time of day, occasional realistic typos, read-receipt patterns.

## Phase 7 — Routing rules + handoff + control chat
**Status:** not started
**Scope:** Category → action map. Operator control chat (a normal Telegram chat with the notifier bot, or a dedicated chat). Handoff queue. `human_active` flag and lifecycle. UI extended with takeover and manual-send.

## Phase 8 — Persona depth + edge cases + tuning
**Status:** not started
**Scope:** Persona document iteration. Voice samples. Few-shot example curation. Abuse handling. Sleep hours. Rate-limit tuning. Performance work.

## Phase guidance

Phases can be paused at any point and shipped. Phase 1-3 produce a working observation + admin tool with no responding. Phase 4 adds intelligence without action. Phase 5+ closes the loop.
