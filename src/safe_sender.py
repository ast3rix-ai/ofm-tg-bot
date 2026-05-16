from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerFloodError,
    UserPrivacyRestrictedError,
)

from src import storage
from src.humanizer import Humanizer
from src.rate_limiter import BreakerState, RateLimiter
from src.telegram_client import BotClient

_log = logger.bind(module=__name__)

# Telethon errors that mean "this chat can't be written to" — never retried,
# never trip the breaker (they are not abuse signals).
_PERMANENT_SEND_ERRORS = (UserPrivacyRestrictedError, ChatWriteForbiddenError)

SleepFn = Callable[[float], Awaitable[None]]
LockProvider = Callable[[int], asyncio.Lock]

STATUS_SENT = "sent"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_SEND_FAILED = "send_failed"


@dataclass(frozen=True)
class SendResult:
    """Outcome of a full `SafeSender.send` sequence."""

    status: str  # 'sent' | 'rate_limited' | 'send_failed'
    chunks_sent: int
    total_duration_ms: int
    rate_limit_state: str | None = None
    flood_wait_seconds: int | None = None
    circuit_breaker_tripped_at: int | None = None


class SafeSender:
    """Orchestrates the protected, humanized outbound send path.

    Composes the rate limiter, humanizer, and flood-safe Telegram send into a
    single `send` entry point that `ResponseGenerator` calls in place of the
    raw `telegram_client.send_message`.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        account_id: int,
        telegram_client: BotClient,
        rate_limiter: RateLimiter,
        humanizer: Humanizer,
        get_lock: LockProvider,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._db_path = db_path
        self._account_id = account_id
        self._client = telegram_client
        self._rate_limiter = rate_limiter
        self._humanizer = humanizer
        self._get_lock = get_lock
        self._sleep = sleep
        self._log = logger.bind(module=__name__, account_id=account_id)

    async def send(
        self, chat_id: int, text: str, run_id: int, is_new_chat: bool
    ) -> SendResult:
        """Rate-limit, humanize, and send a reply under the per-chat lock."""
        start = time.monotonic()
        async with self._get_lock(chat_id):
            # --- Rate limiting / circuit breaker ---
            acquire = await self._rate_limiter.acquire(chat_id, is_new_chat)
            if not acquire.allowed:
                storage.update_response_run(
                    self._db_path,
                    run_id,
                    rate_limit_state=acquire.reason,
                )
                self._log.info(
                    "Send gated by rate limiter",
                    chat_id=chat_id,
                    reason=acquire.reason,
                )
                return SendResult(
                    status=STATUS_RATE_LIMITED,
                    chunks_sent=0,
                    total_duration_ms=_elapsed_ms(start),
                    rate_limit_state=acquire.reason,
                )

            breaker_open_before = (
                self._rate_limiter.breaker_state is not BreakerState.CLOSED
            )

            # --- Read receipt after a human "reading" delay ---
            inbound_text = self._last_inbound_text(chat_id)
            read_delay = self._humanizer.read_delay(inbound_text)
            await self._sleep(read_delay)
            try:
                await self._client.mark_read(chat_id)
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "mark_read failed", chat_id=chat_id, error=str(exc)
                )

            # --- Split + send chunks ---
            chunks = self._humanizer.split_message(text)
            total = len(chunks)
            chunks_sent = 0
            first_flood_seconds: int | None = None
            send_failed = False

            for index, chunk in enumerate(chunks):
                inter_delay_ms = 0
                if index > 0:
                    inter_delay = self._humanizer.inter_message_delay()
                    inter_delay_ms = int(inter_delay * 1000)
                    await self._sleep(inter_delay)

                typing_duration = self._humanizer.typing_duration(chunk)
                await self._hold_typing(chat_id, typing_duration)

                typo = self._humanizer.maybe_typo(chunk)
                outcome = await self._flood_safe_send(chat_id, typo.text)
                if outcome.flood_seconds is not None and first_flood_seconds is None:
                    first_flood_seconds = outcome.flood_seconds
                if outcome.tg_message_id is None:
                    send_failed = True
                    break

                chunks_sent += 1
                self._record_sent(
                    chat_id=chat_id,
                    run_id=run_id,
                    tg_message_id=outcome.tg_message_id,
                    text=typo.text,
                    split_index=index,
                    total_chunks=total,
                    metadata={
                        "typing_duration_ms": int(typing_duration * 1000),
                        "inter_delay_ms": inter_delay_ms,
                        "had_typo": typo.had_typo,
                        "was_correction": False,
                    },
                )

                # --- Optional `*correction` follow-up ---
                if typo.correction is not None:
                    await self._sleep(self._humanizer.correction_delay())
                    corr_typing = self._humanizer.typing_duration(typo.correction)
                    await self._hold_typing(chat_id, corr_typing)
                    corr = await self._flood_safe_send(chat_id, typo.correction)
                    if corr.tg_message_id is not None:
                        chunks_sent += 1
                        self._record_sent(
                            chat_id=chat_id,
                            run_id=run_id,
                            tg_message_id=corr.tg_message_id,
                            text=typo.correction,
                            split_index=index,
                            total_chunks=total,
                            metadata={
                                "typing_duration_ms": int(corr_typing * 1000),
                                "inter_delay_ms": 0,
                                "had_typo": False,
                                "was_correction": True,
                            },
                        )

            # --- Resolve a half-open probe ---
            if acquire.is_probe:
                await self._rate_limiter.record_probe_result(
                    success=not send_failed
                )

            breaker_open_after = (
                self._rate_limiter.breaker_state is not BreakerState.CLOSED
            )
            tripped_at: int | None = None
            if breaker_open_after and not breaker_open_before:
                tripped_at = int(time.time())

            storage.update_response_run(
                self._db_path,
                run_id,
                rate_limit_state=acquire.reason,
                flood_wait_seconds=first_flood_seconds,
                circuit_breaker_tripped_at=tripped_at,
            )

            status = STATUS_SEND_FAILED if send_failed else STATUS_SENT
            return SendResult(
                status=status,
                chunks_sent=chunks_sent,
                total_duration_ms=_elapsed_ms(start),
                rate_limit_state=acquire.reason,
                flood_wait_seconds=first_flood_seconds,
                circuit_breaker_tripped_at=tripped_at,
            )

    # ---------- helpers ----------

    async def _hold_typing(self, chat_id: int, duration: float) -> None:
        """Display the typing indicator for `duration` seconds."""
        try:
            async with self._client.typing_action(chat_id):
                await self._sleep(duration)
        except Exception as exc:  # noqa: BLE001
            # A failed typing indicator must never abort the send.
            self._log.warning(
                "typing action failed", chat_id=chat_id, error=str(exc)
            )
            await self._sleep(duration)

    async def _flood_safe_send(
        self, chat_id: int, text: str
    ) -> _SendOutcome:
        """Send one message, handling flood / abuse errors per the spec.

        Single retry on `FloodWaitError`; `PeerFloodError` trips the breaker
        immediately with no retry; privacy/permission errors fail silently.
        """
        try:
            sent = await self._client.send_message(chat_id, text)
            return _SendOutcome(tg_message_id=int(sent["tg_message_id"]))
        except FloodWaitError as exc:
            seconds = int(getattr(exc, "seconds", 0) or 0)
            self._log.warning(
                "FloodWaitError — backing off for single retry",
                chat_id=chat_id,
                seconds=seconds,
            )
            await self._rate_limiter.record_flood_wait(float(seconds))
            await self._sleep(seconds + _flood_jitter())
            try:
                sent = await self._client.send_message(chat_id, text)
                return _SendOutcome(
                    tg_message_id=int(sent["tg_message_id"]),
                    flood_seconds=seconds,
                )
            except FloodWaitError as exc2:
                seconds2 = int(getattr(exc2, "seconds", 0) or 0)
                self._log.error(
                    "Second FloodWaitError within retry — not retrying again",
                    chat_id=chat_id,
                    seconds=seconds2,
                )
                await self._rate_limiter.record_flood_wait(float(seconds2))
                return _SendOutcome(tg_message_id=None, flood_seconds=seconds)
        except PeerFloodError:
            self._log.error(
                "PeerFloodError — tripping breaker, surfacing to operator",
                chat_id=chat_id,
            )
            await self._rate_limiter.record_peer_flood()
            storage.insert_operator_alert(
                self._db_path,
                account_id=self._account_id,
                chat_id=chat_id,
                alert_type="peer_flood",
                severity="critical",
                message=(
                    "PeerFloodError from Telegram — spam-pattern throttling "
                    "detected; outbound halted for 6h."
                ),
                payload=None,
            )
            return _SendOutcome(tg_message_id=None)
        except _PERMANENT_SEND_ERRORS as exc:
            self._log.warning(
                "Permanent send error — skipping chat",
                chat_id=chat_id,
                error=type(exc).__name__,
            )
            return _SendOutcome(tg_message_id=None)

    def _record_sent(
        self,
        *,
        chat_id: int,
        run_id: int,
        tg_message_id: int,
        text: str,
        split_index: int,
        total_chunks: int,
        metadata: dict[str, object],
    ) -> None:
        """Persist one sent chunk to `messages` + `bot_sent_messages`."""
        storage.insert_message(
            self._db_path,
            account_id=self._account_id,
            chat_id=chat_id,
            tg_message_id=tg_message_id,
            direction="out",
            sender_id=0,
            text=text,
            media_type=None,
            raw_json=(
                f'{{"bot_sent": true, "response_run_id": {run_id}, '
                f'"split_index": {split_index}}}'
            ),
        )
        storage.insert_bot_sent_message(
            self._db_path,
            account_id=self._account_id,
            chat_id=chat_id,
            tg_message_id=tg_message_id,
            response_run_id=run_id,
            split_index=split_index,
            total_chunks=total_chunks,
            humanizer_metadata=metadata,
        )

    def _last_inbound_text(self, chat_id: int) -> str:
        """Return the text of the most recent inbound message, or ''."""
        recent = storage.get_recent_messages(
            self._db_path, self._account_id, chat_id, limit=20
        )
        for message in reversed(recent):
            if message.get("direction") == "in":
                return str(message.get("text") or "")
        return ""


@dataclass(frozen=True)
class _SendOutcome:
    """Result of a single `_flood_safe_send` attempt."""

    tg_message_id: int | None
    flood_seconds: int | None = None


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _flood_jitter() -> float:
    """Extra random backoff added on top of a FloodWait, per the spec."""
    import random

    return random.uniform(1.0, 3.0)
