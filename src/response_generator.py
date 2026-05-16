from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src import storage
from src.llm.client import LLMClient, LLMError
from src.llm.prompts import response_prompt
from src.output_validator import validate_response
from src.safe_sender import (
    STATUS_RATE_LIMITED,
    STATUS_SENT,
    SafeSender,
)

_QUOTE_CHARS = "\"'“”‘’"


@dataclass(frozen=True)
class ResponseResult:
    """Outcome of a single response-generation pass for one inbound message."""

    outcome: str  # 'sent' | 'gated' | 'failed' | 'validator_rejected_all_attempts'
    gate_reason: str | None
    attempts: int
    final_text: str | None
    response_run_id: int
    sent_tg_message_id: int | None
    latency_ms: int


def _clean_output(text: str) -> str:
    """Strip surrounding whitespace and quote characters from model output."""
    t = text.strip()
    while t and t[0] in _QUOTE_CHARS:
        t = t[1:]
    while t and t[-1] in _QUOTE_CHARS:
        t = t[:-1]
    return t.strip()


class ResponseGenerator:
    """Generates and sends an in-persona reply to an inbound message.

    Runs after the classifier on every inbound message. Gates on
    `bot_enabled`, category, and the `human_active` flag; generates with
    AI-tell validation and re-roll; sends atomically under a per-chat lock.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        llm: LLMClient,
        safe_sender: SafeSender,
        persona_path: Path,
        max_retries: int,
        temperature: float,
        max_tokens: int,
        default_bot_enabled_new_chats: int = 1,
        history_window: int = 20,
        new_chat_age_days: int = 7,
    ) -> None:
        self._db_path = db_path
        self._llm = llm
        self._safe_sender = safe_sender
        self._persona_path = persona_path
        self._max_retries = max(0, int(max_retries))
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._default_bot_enabled = default_bot_enabled_new_chats
        self._history_window = history_window
        self._new_chat_age_days = new_chat_age_days
        self._log = logger.bind(module=__name__)

        self._persona_text: str | None = None
        self._persona_mtime: float | None = None

    # ---------- persona loading ----------

    def _persona_version(self) -> str:
        """Return the persona doc's mtime as an ISO string, or 'unknown'."""
        try:
            mtime = self._persona_path.stat().st_mtime
        except OSError:
            return "unknown"
        return datetime.fromtimestamp(mtime, tz=UTC).isoformat()

    def _load_persona(self) -> tuple[str, str]:
        """Return `(persona_text, persona_version)`, re-reading on mtime change.

        Raises:
            FileNotFoundError: If the persona file does not exist.
        """
        mtime = self._persona_path.stat().st_mtime
        if self._persona_text is None or self._persona_mtime != mtime:
            self._persona_text = self._persona_path.read_text(encoding="utf-8")
            self._persona_mtime = mtime
            self._log.info(
                "Persona loaded", path=str(self._persona_path), mtime=mtime
            )
        text = self._persona_text
        assert text is not None
        version = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
        return text, version

    # ---------- main entry ----------

    async def generate(
        self,
        *,
        account_id: int,
        chat_id: int,
        triggering_message_id: int | None,
    ) -> ResponseResult:
        """Generate and send a reply for one inbound message.

        Args:
            account_id: Operated account id.
            chat_id: Telegram chat id.
            triggering_message_id: `messages.id` (DB pk) of the inbound
                message that triggered this run, or None if unknown.

        Returns:
            A `ResponseResult` describing the outcome. Never raises for
            ordinary failures — send/LLM errors are recorded as outcomes.
        """
        start = time.monotonic()

        # --- Gate check (fast path, no LLM call) ---
        state = storage.get_contact_state(self._db_path, account_id, chat_id)
        if state is None:
            bot_enabled = self._default_bot_enabled
            category: str | None = None
            flags: dict[str, Any] = {}
            human_active_col = 0
        else:
            bot_enabled = int(state.get("bot_enabled") or 0)
            category = state.get("category")
            flags = state.get("flags") or {}
            human_active_col = int(state.get("human_active") or 0)

        gate_reason: str | None = None
        if not bot_enabled:
            gate_reason = "bot_disabled"
        elif category == "paid":
            gate_reason = "category_paid"
        elif bool(flags.get("human_active")) or human_active_col:
            gate_reason = "human_active"

        if gate_reason is not None:
            run_id = storage.insert_response_run(
                self._db_path,
                account_id=account_id,
                chat_id=chat_id,
                triggered_by_message_id=triggering_message_id,
                persona_version=self._persona_version(),
                attempts=0,
                outcome="gated",
                gate_reason=gate_reason,
                raw_attempts=[],
                final_text=None,
                latency_ms=int((time.monotonic() - start) * 1000),
            )
            self._log.info(
                "Response gated", chat_id=chat_id, gate_reason=gate_reason
            )
            return ResponseResult(
                outcome="gated",
                gate_reason=gate_reason,
                attempts=0,
                final_text=None,
                response_run_id=run_id,
                sent_tg_message_id=None,
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        # --- Build prompt ---
        try:
            persona_text, persona_version = self._load_persona()
        except OSError as exc:
            return self._record_failed(
                account_id=account_id,
                chat_id=chat_id,
                triggering_message_id=triggering_message_id,
                persona_version=self._persona_version(),
                raw_attempts=[],
                attempts=0,
                start=start,
                reason=f"persona load failed: {exc}",
            )

        recent = storage.get_recent_messages(
            self._db_path, account_id, chat_id, limit=self._history_window
        )
        memory = storage.get_contact_memory(self._db_path, account_id, chat_id)
        prompt = response_prompt(
            persona_text=persona_text,
            contact_memory=memory,
            recent_messages=recent,
            category=category,
        )

        # --- Generate loop (1 + max_retries attempts) ---
        raw_attempts: list[dict[str, Any]] = []
        final_text: str | None = None
        attempts = 0
        for attempt in range(1, self._max_retries + 2):
            attempts = attempt
            try:
                response = await self._llm.generate(
                    prompt,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
            except LLMError as exc:
                raw_attempts.append(
                    {"attempt": attempt, "error": str(exc)}
                )
                self._log.warning(
                    "Response LLM call failed", chat_id=chat_id, error=str(exc)
                )
                return self._record_failed(
                    account_id=account_id,
                    chat_id=chat_id,
                    triggering_message_id=triggering_message_id,
                    persona_version=persona_version,
                    raw_attempts=raw_attempts,
                    attempts=attempts,
                    start=start,
                    reason=f"LLM error: {exc}",
                )

            candidate = _clean_output(response.text)
            verdict = validate_response(candidate)
            raw_attempts.append(
                {
                    "attempt": attempt,
                    "text": candidate,
                    "valid": verdict.valid,
                    "reason": verdict.reason,
                }
            )
            if verdict.valid:
                final_text = candidate
                break
            self._log.info(
                "Response rejected by validator — re-rolling",
                chat_id=chat_id,
                attempt=attempt,
                reason=verdict.reason,
            )

        if final_text is None:
            run_id = storage.insert_response_run(
                self._db_path,
                account_id=account_id,
                chat_id=chat_id,
                triggered_by_message_id=triggering_message_id,
                persona_version=persona_version,
                attempts=attempts,
                outcome="validator_rejected_all_attempts",
                gate_reason=None,
                raw_attempts=raw_attempts,
                final_text=None,
                latency_ms=int((time.monotonic() - start) * 1000),
            )
            self._log.warning(
                "All response attempts rejected by validator",
                chat_id=chat_id,
                attempts=attempts,
            )
            return ResponseResult(
                outcome="validator_rejected_all_attempts",
                gate_reason=None,
                attempts=attempts,
                final_text=None,
                response_run_id=run_id,
                sent_tg_message_id=None,
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        # --- Insert the audit row, then hand off to SafeSender ---
        # The run row is created before sending so SafeSender can attach
        # rate-limit/flood state to it; its outcome is finalized afterwards.
        run_id = storage.insert_response_run(
            self._db_path,
            account_id=account_id,
            chat_id=chat_id,
            triggered_by_message_id=triggering_message_id,
            persona_version=persona_version,
            attempts=attempts,
            outcome="pending",
            gate_reason=None,
            raw_attempts=raw_attempts,
            final_text=final_text,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

        is_new_chat = self._is_new_chat(account_id, chat_id)
        try:
            send_result = await self._safe_sender.send(
                chat_id, final_text, run_id, is_new_chat
            )
        except Exception as exc:  # noqa: BLE001
            self._log.error(
                "Response send failed", chat_id=chat_id, error=str(exc)
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            storage.update_response_run(
                self._db_path, run_id, outcome="failed", latency_ms=latency_ms
            )
            return ResponseResult(
                outcome="failed",
                gate_reason=None,
                attempts=attempts,
                final_text=None,
                response_run_id=run_id,
                sent_tg_message_id=None,
                latency_ms=latency_ms,
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        if send_result.status == STATUS_SENT:
            outcome = "sent"
            gate_reason = None
        elif send_result.status == STATUS_RATE_LIMITED:
            outcome = "gated"
            gate_reason = send_result.rate_limit_state
        else:  # STATUS_SEND_FAILED
            outcome = "failed"
            gate_reason = None
        storage.update_response_run(
            self._db_path,
            run_id,
            outcome=outcome,
            gate_reason=gate_reason,
            latency_ms=latency_ms,
        )
        self._log.info(
            "Response dispatched",
            chat_id=chat_id,
            attempts=attempts,
            outcome=outcome,
            chunks_sent=send_result.chunks_sent,
        )
        return ResponseResult(
            outcome=outcome,
            gate_reason=gate_reason,
            attempts=attempts,
            final_text=final_text,
            response_run_id=run_id,
            sent_tg_message_id=None,
            latency_ms=latency_ms,
        )

    def _is_new_chat(self, account_id: int, chat_id: int) -> bool:
        """True if the contact's first message is within `new_chat_age_days`."""
        first_seen = storage.get_contact_first_seen_at(
            self._db_path, account_id, chat_id
        )
        if first_seen is None:
            return True
        try:
            parsed = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        age_days = (datetime.now(UTC) - parsed).total_seconds() / 86400.0
        return age_days < self._new_chat_age_days

    # ---------- helpers ----------

    def _record_failed(
        self,
        *,
        account_id: int,
        chat_id: int,
        triggering_message_id: int | None,
        persona_version: str,
        raw_attempts: list[dict[str, Any]],
        attempts: int,
        start: float,
        reason: str,
    ) -> ResponseResult:
        latency_ms = int((time.monotonic() - start) * 1000)
        run_id = storage.insert_response_run(
            self._db_path,
            account_id=account_id,
            chat_id=chat_id,
            triggered_by_message_id=triggering_message_id,
            persona_version=persona_version,
            attempts=attempts,
            outcome="failed",
            gate_reason=None,
            raw_attempts=[*raw_attempts, {"failure": reason}],
            final_text=None,
            latency_ms=latency_ms,
        )
        return ResponseResult(
            outcome="failed",
            gate_reason=None,
            attempts=attempts,
            final_text=None,
            response_run_id=run_id,
            sent_tg_message_id=None,
            latency_ms=latency_ms,
        )
