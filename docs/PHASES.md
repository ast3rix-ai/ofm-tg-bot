# Phases

Each phase is independently shippable. Do not implement features from future phases. Mark phase completion by updating the `Status` field below.

## Phase 0 — Repo scaffolding and docs
**Status:** complete
**Scope:** Directory skeleton, `.gitignore`, `CLAUDE.md`, `docs/`, git init, first push.
**Acceptance:** Repo exists locally and on GitHub, all docs present.

## Phase 1 — Foundation, watchdog, logging
**Status:** complete
**Scope:** Telethon connection, encrypted session, SQLite schema, structured logging, watchdog, notifier bot, inspection CLI.
**Out of scope:** any reply logic, classification, LLM, persona, marking read, typing indicators.
**Acceptance:** see Phase 1 prompt.
**Note:** Notifier optional as of phase-1.1; populate `.env` to enable.

## Phase 2 — State store deepening
**Status:** complete
**Scope:** Additional tables for contact state (category, funnel stage, flags), distilled memory stub, conversation summary stub. Migration mechanism. Inspection CLI expanded.
**Out of scope:** anything that writes to these tables — populated in Phase 4+.

## Phase 3 — Control UI shell + multi-account
**Status:** complete
**Scope:** FastAPI local web app. Account management (add account, store credentials encrypted, list accounts, switch active account). Live log tail view. Contact list + chat history viewer. Migration 003 introduces `accounts` plus `account_id` on every other table.
**Out of scope:** manual chat takeover, message sending from UI.
**Note:** Phase 4 must assume the active-account model — the classifier writes rows scoped to `bot_manager.active_account_id`.

## Phase 4 — Signal detector + classifier
**Status:** complete
**Scope:** Local LLM integration via Ollama. Rules-first signal detector. Hybrid classifier with confidence threshold. Migration 004 adds `bot_enabled`, `bootstrap_completed_at`, `last_resurface_at`, and the `classifier_runs` + `operator_alerts` tables. Backlog processor handles initial bootstrap + unread catchup. Categories defined in `docs/CATEGORIES.md`. Populates `contact_state` and `contact_memory` on every classification.
**Out of scope:** outbound messaging.
**Note:** Phase 5 must read `bot_enabled` before generating any reply; the toggle is operator-controlled and defaults off for bootstrapped chats.

## Phase 5 — Response generator (MVP slice)
**Status:** complete
**Scope:** LLM-driven reply generation. Minimal hardcoded persona (`personas/default/persona.md`) loaded from disk with mtime-based hot-reload. Rolling window + distilled memory feeding the prompt. Output validation with AI-tell blacklist and re-roll. Atomic sends via Telegram client under a per-chat lock. Reply gating on `bot_enabled` / `category != 'paid'` / `human_active`. Operator `/reset` command. Outbound message tagging (`bot_sent_messages`) as Phase 7 groundwork. Migration 005 adds `response_runs` + `bot_sent_messages`.
**Out of scope:** humanization (sends are immediate and atomic in this phase — humanization is Phase 6); real persona depth (Phase 8); operator-takeover auto-detection and routing rules (Phase 7).

## Phase 6 — Humanization layer
**Status:** not started
**Scope:** Message splitting, inter-message delays, typing indicators, response latency distributions by category and time of day, occasional realistic typos, read-receipt patterns. Wraps the Phase 5 atomic `telegram_client.send_message`.

## Phase 7 — Routing rules + handoff + control chat
**Status:** not started
**Scope:** Category → action map. Operator control chat (a normal Telegram chat with the notifier bot, or a dedicated chat). Handoff queue. `human_active` flag and lifecycle. UI extended with takeover and manual-send.

## Phase 8 — Persona depth + edge cases + tuning
**Status:** not started
**Scope:** Persona document iteration. Voice samples. Few-shot example curation. Abuse handling. Sleep hours. Rate-limit tuning. Performance work.

## Phase guidance

Phases can be paused at any point and shipped. Phase 1-3 produce a working observation + admin tool with no responding. Phase 4 adds intelligence without action. Phase 5+ closes the loop.
