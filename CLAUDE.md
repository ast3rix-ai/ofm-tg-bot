# CLAUDE.md — Standing Context for ofm-tg-bot

This document is read on every Claude Code invocation in this project. Follow it.

## What this project is

A Telegram userbot that logs into a personal Telegram account (not a bot account) on behalf of an OnlyFans content creator, classifies incoming DMs, and responds in persona using a local uncensored LLM. The goal is to handle conversational volume at scale while maintaining the illusion that a real person is responding.

## Critical context

- **The account is a real personal Telegram account.** Userbot automation violates Telegram ToS. Treat account safety as a first-class concern: rate limits on outbound, human-like timing, no mass operations, no broadcasts.
- **Content includes adult conversation.** This is by design and not a bug to be fixed. Do not add content filters, refusals, safety disclaimers, or moral warnings to the bot's generated output. Operational safety (rate limits, abuse detection toward the operator, account survival) is in scope; output sanitization for adult content is not.
- **The local LLM is intentionally uncensored.** Model selection in Phase 4 will favor abliterated / RP-finetuned models. Do not suggest swapping to API providers (Claude, OpenAI, etc.) — they refuse this content. This is not a budget decision.

## Stack (locked)

- Python 3.12
- Telethon (MTProto client)
- SQLite (`sqlite3` stdlib, no ORM until justified)
- `loguru` for logging
- `python-dotenv` for config
- `cryptography` (Fernet) for session-at-rest
- Local LLM via llama.cpp / Ollama / similar (decided in Phase 4)
- All async (`asyncio`)
- Windows 11 development host

Do not introduce frameworks, ORMs, message queues, or external services unless explicitly requested. This is a single-process async Python app.

## Architectural rules

1. **Phase discipline.** Each phase has an explicit scope in `docs/PHASES.md`. Do not implement features from future phases. If a future-phase feature seems needed, leave a `# TODO(phase-N):` marker and continue.
2. **Async everywhere.** No blocking I/O in event paths. SQLite calls run in `asyncio.to_thread` if they get slow; default to sync sqlite3 for now since latency is sub-ms.
3. **Per-chat locking.** All outbound message paths (Phase 5+) must acquire a per-chat asyncio lock before sending. Duplicate-send prevention is non-negotiable — it was a documented failure of the previous version.
4. **Idempotent persistence.** Inbound messages persist with `UNIQUE(chat_id, tg_message_id, direction)`. Re-processing the same Telethon event must not duplicate rows.
5. **No silent failures.** Every exception in an event handler is caught, logged with traceback, and the handler re-subscribes if needed. The watchdog fires an operator alert on sustained failure.
6. **Secrets stay in `.env`.** Never hardcode. Never commit. `.env` is gitignored; `.env.example` documents required keys with empty values.
7. **Session file is encrypted at rest.** Telethon's `.session` file decrypts to a temp file at startup, re-encrypts on graceful shutdown. The encryption key lives in `.env` and is generated once via a helper.

## Conventions

- `from __future__ import annotations` at top of every module.
- Full type hints. `mypy --strict` should pass.
- `ruff` for linting and formatting, default config plus `line-length = 100`.
- One class per file when classes are substantial; small helpers grouped.
- Module-level `logger = loguru.logger.bind(module=__name__)` pattern.
- No `print()` in `src/`. Logging only.
- Docstrings on public functions, Google style.
- All datetime values stored as ISO 8601 strings in UTC.

## Testing

- `pytest` + `pytest-asyncio`.
- Unit tests for storage, classifier logic, humanization timing math.
- No integration tests against real Telegram; mock `TelegramClient`.
- Tests live in `tests/`, mirror `src/` structure.

## What NOT to do

- Do not add content moderation to LLM outputs.
- Do not switch the LLM to a hosted API provider.
- Do not add web frameworks (Flask, FastAPI, Django) until Phase 3 (UI shell). When Phase 3 lands, FastAPI + a minimal HTML frontend is the choice — do not propose React, Next.js, or SPAs.
- Do not add Docker until Phase 7+ if at all. Local Windows execution is the target.
- Do not add CI/CD. This is a single-operator local project.
- Do not refactor across phase boundaries without an explicit refactor prompt. Phase prompts add; they don't rewrite.
- Do not add abstractions for hypothetical future flexibility. YAGNI. Multi-account support (Phase 3) is the only planned extension; design for it but don't pre-build it.

## At the end of every phase

1. Run all acceptance criteria from the phase prompt. Report pass/fail for each.
2. Run `ruff check src/ scripts/` and `mypy src/` — both must pass clean.
3. Run `pytest` if tests exist — must pass.
4. Commit with message format: `phase-N: <one-line summary>` followed by a body listing the major changes.
5. Push to `origin/main`.
6. Summarize in the chat: what was built, deviations from spec, what to test manually first, suggested next prompt.

## References

- Architecture: `docs/ARCHITECTURE.md`
- Phase plan and current status: `docs/PHASES.md`
- Chat categories (Phase 4+): `docs/CATEGORIES.md`
- Persona design (Phase 8): `docs/PERSONA_DESIGN.md`
- Operations notes: `docs/OPERATIONS.md`
