from __future__ import annotations

import asyncio
import enum
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger

from src import storage
from src.config import RateLimitConfig

_log = logger.bind(module=__name__)

# rate_limit_state values written to `response_runs` — keep in sync with the
# Phase 5.5 spec.
REASON_ALLOWED = "allowed"
REASON_PER_CHAT_BUCKET = "per_chat_bucket_empty"
REASON_GLOBAL_BUCKET = "global_bucket_empty"
REASON_DAILY_CAP = "daily_cap_exceeded"
REASON_BREAKER_OPEN = "breaker_open"
REASON_ACCOUNT_RESTRICTED = "account_restricted"


class BreakerState(enum.Enum):
    """Circuit-breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class AcquireResult:
    """Outcome of a `RateLimiter.acquire` call."""

    allowed: bool
    reason: str | None
    retry_after_seconds: float | None
    is_probe: bool = False


class TokenBucket:
    """A monotonic-clock token bucket.

    Tokens refill continuously at `refill_per_sec` up to `capacity`. `level`
    peeks the current token count without consuming; `consume` removes one
    token (callers must check `level >= 1` first).
    """

    def __init__(self, *, refill_per_sec: float, capacity: float) -> None:
        self._refill = refill_per_sec
        self._capacity = capacity
        self._tokens = capacity
        self._last: float | None = None

    def level(self, now: float) -> float:
        """Return the current token count, applying refill since last touch."""
        if self._last is None:
            self._last = now
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill)
        self._last = now
        return self._tokens

    def consume(self, now: float) -> None:
        """Remove one token. Assumes `level(now) >= 1` was already checked."""
        self._tokens = self.level(now) - 1.0

    def time_until_token(self, now: float) -> float:
        """Seconds until at least one token is available."""
        level = self.level(now)
        if level >= 1.0:
            return 0.0
        if self._refill <= 0:
            return float("inf")
        return (1.0 - level) / self._refill


class RateLimiter:
    """Three-layer token-bucket limiter with daily caps and a circuit breaker.

    A single async lock serializes the whole `acquire` critical section. The
    section is microsecond-fast (in-memory arithmetic plus sub-millisecond
    SQLite reads), so per-chat locking would add complexity without benefit.
    """

    def __init__(
        self,
        db_path: Path,
        account_id: int,
        config: RateLimitConfig,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utc_day: Callable[[], str] | None = None,
    ) -> None:
        self._db_path = db_path
        self._account_id = account_id
        self._config = config
        self._clock = monotonic
        self._utc_day = utc_day or _default_utc_day
        self._log = _log

        self._per_chat_buckets: dict[int, TokenBucket] = {}
        self._global_bucket = TokenBucket(
            refill_per_sec=config.global_refill_per_sec,
            capacity=config.global_capacity,
        )
        self._lock = asyncio.Lock()

        # Circuit breaker state.
        self._state = BreakerState.CLOSED
        self._reason: str | None = None
        self._open_until: float | None = None  # monotonic; None = no auto-expiry
        self._last_open_duration: float | None = None
        self._probe_in_flight = False
        # (monotonic_time, flood_wait_seconds) pairs within the double-window.
        self._flood_events: list[tuple[float, float]] = []

        # Re-hydrate breaker from a persisted, un-cleared restriction so a
        # process restart does not silently re-enable a restricted account.
        if storage.get_active_account_restriction(db_path, account_id) is not None:
            self._state = BreakerState.OPEN
            self._reason = REASON_ACCOUNT_RESTRICTED
            self._open_until = None

    # ---------- breaker snapshot ----------

    @property
    def breaker_state(self) -> BreakerState:
        return self._state

    def snapshot(self) -> dict[str, object]:
        """Return a UI-friendly view of limiter + breaker + restriction state."""
        now = self._clock()
        reset_at: str | None = None
        if self._open_until is not None:
            reset_at = (
                datetime.now(UTC)
                + timedelta(seconds=max(0.0, self._open_until - now))
            ).isoformat()
        day = self._utc_day()
        global_count = storage.get_daily_global_count(
            self._db_path, account_id=self._account_id, utc_day=day
        )
        restriction = storage.get_active_account_restriction(
            self._db_path, self._account_id
        )
        cap = self._config.daily_global_cap
        return {
            "circuit_breaker": {
                "state": self._state.value,
                "reason": self._reason,
                "reset_at": reset_at,
                "probe_in_flight": self._probe_in_flight,
            },
            "global_bucket_level": round(self._global_bucket.level(now), 2),
            "global_bucket_capacity": self._config.global_capacity,
            "daily_global": {
                "count": global_count,
                "cap": cap,
                "pct_used": round(100.0 * global_count / cap, 1) if cap else 0.0,
            },
            "account_restricted": restriction is not None,
            "account_restriction": restriction,
        }

    # ---------- acquire ----------

    async def acquire(self, chat_id: int, is_new_chat: bool) -> AcquireResult:
        """Decide whether an outbound send to `chat_id` may proceed now."""
        async with self._lock:
            now = self._clock()

            # 1. Persisted account restriction — hard stop.
            if storage.get_active_account_restriction(
                self._db_path, self._account_id
            ) is not None:
                if not (
                    self._state is BreakerState.OPEN
                    and self._reason == REASON_ACCOUNT_RESTRICTED
                ):
                    self._open_locked(
                        REASON_ACCOUNT_RESTRICTED, duration=None, now=now
                    )
                return AcquireResult(
                    allowed=False,
                    reason=REASON_ACCOUNT_RESTRICTED,
                    retry_after_seconds=None,
                )

            # 2. Circuit breaker.
            is_probe = False
            if self._state is BreakerState.OPEN:
                if self._open_until is None or now < self._open_until:
                    retry = (
                        None if self._open_until is None
                        else max(0.0, self._open_until - now)
                    )
                    deny_reason = (
                        REASON_ACCOUNT_RESTRICTED
                        if self._reason == REASON_ACCOUNT_RESTRICTED
                        else REASON_BREAKER_OPEN
                    )
                    return AcquireResult(
                        allowed=False,
                        reason=deny_reason,
                        retry_after_seconds=retry,
                    )
                # Timer elapsed → move to half-open.
                self._transition_half_open_locked()
            if self._state is BreakerState.HALF_OPEN:
                if self._probe_in_flight:
                    return AcquireResult(
                        allowed=False,
                        reason=REASON_BREAKER_OPEN,
                        retry_after_seconds=None,
                    )
                is_probe = True

            # 3. Daily caps (read-through SQLite).
            day = self._utc_day()
            per_chat_count = storage.get_daily_send_count(
                self._db_path,
                account_id=self._account_id,
                chat_id=chat_id,
                utc_day=day,
            )
            if is_new_chat and per_chat_count >= self._config.new_chat_daily_cap:
                return AcquireResult(
                    allowed=False,
                    reason=REASON_DAILY_CAP,
                    retry_after_seconds=_seconds_until_utc_midnight(),
                )
            if per_chat_count >= self._config.daily_per_chat_cap:
                return AcquireResult(
                    allowed=False,
                    reason=REASON_DAILY_CAP,
                    retry_after_seconds=_seconds_until_utc_midnight(),
                )
            global_count = storage.get_daily_global_count(
                self._db_path, account_id=self._account_id, utc_day=day
            )
            if global_count >= self._config.daily_global_cap:
                return AcquireResult(
                    allowed=False,
                    reason=REASON_DAILY_CAP,
                    retry_after_seconds=_seconds_until_utc_midnight(),
                )

            # 4/5. Token buckets — check both before consuming either.
            per_chat_bucket = self._per_chat_bucket(chat_id)
            if per_chat_bucket.level(now) < 1.0:
                return AcquireResult(
                    allowed=False,
                    reason=REASON_PER_CHAT_BUCKET,
                    retry_after_seconds=per_chat_bucket.time_until_token(now),
                )
            if self._global_bucket.level(now) < 1.0:
                return AcquireResult(
                    allowed=False,
                    reason=REASON_GLOBAL_BUCKET,
                    retry_after_seconds=self._global_bucket.time_until_token(now),
                )

            # Allowed — consume tokens and write back daily counters.
            per_chat_bucket.consume(now)
            self._global_bucket.consume(now)
            storage.increment_daily_send_count(
                self._db_path,
                account_id=self._account_id,
                chat_id=chat_id,
                utc_day=day,
            )
            storage.increment_daily_global_count(
                self._db_path, account_id=self._account_id, utc_day=day
            )
            if is_probe:
                self._probe_in_flight = True
            return AcquireResult(
                allowed=True,
                reason=REASON_ALLOWED,
                retry_after_seconds=None,
                is_probe=is_probe,
            )

    # ---------- breaker transitions ----------

    async def record_flood_wait(self, flood_wait_seconds: float) -> None:
        """Record a `FloodWaitError`. Two within the window trip the breaker."""
        async with self._lock:
            now = self._clock()
            window = self._config.flood_double_window_seconds
            self._flood_events = [
                (t, s) for (t, s) in self._flood_events if now - t <= window
            ]
            self._flood_events.append((now, flood_wait_seconds))
            if len(self._flood_events) >= 2:
                worst = max(s for (_, s) in self._flood_events)
                duration = worst * self._config.flood_open_multiplier
                self._flood_events.clear()
                self._open_locked("flood_wait_x2", duration=duration, now=now)

    async def record_peer_flood(self) -> None:
        """Record a `PeerFloodError` — trips the breaker immediately."""
        async with self._lock:
            self._open_locked(
                "peer_flood",
                duration=self._config.peer_flood_open_seconds,
                now=self._clock(),
            )

    async def record_account_restriction(self) -> None:
        """Trip the breaker for a SpamBot restriction — open until manual reset."""
        async with self._lock:
            self._open_locked(
                REASON_ACCOUNT_RESTRICTED, duration=None, now=self._clock()
            )

    async def record_probe_result(self, *, success: bool) -> None:
        """Resolve a half-open probe send: success closes, failure re-opens."""
        async with self._lock:
            self._probe_in_flight = False
            if self._state is not BreakerState.HALF_OPEN:
                return
            if success:
                self._close_locked("probe_succeeded")
            else:
                prior = self._last_open_duration or 1.0
                self._open_locked(
                    "probe_failed", duration=prior * 2.0, now=self._clock()
                )

    async def reset_breaker(self) -> None:
        """Operator-driven recovery: close the breaker, clear restrictions."""
        async with self._lock:
            cleared = storage.clear_account_restrictions(
                self._db_path, self._account_id
            )
            self._flood_events.clear()
            self._probe_in_flight = False
            self._close_locked("manual_reset")
            self._log.info(
                "Circuit breaker manually reset",
                account_id=self._account_id,
                restrictions_cleared=cleared,
            )

    # ---------- internals ----------

    def _per_chat_bucket(self, chat_id: int) -> TokenBucket:
        bucket = self._per_chat_buckets.get(chat_id)
        if bucket is None:
            bucket = TokenBucket(
                refill_per_sec=self._config.per_chat_refill_per_sec,
                capacity=self._config.per_chat_capacity,
            )
            self._per_chat_buckets[chat_id] = bucket
        return bucket

    def _open_locked(
        self, reason: str, *, duration: float | None, now: float
    ) -> None:
        self._state = BreakerState.OPEN
        self._reason = reason
        self._open_until = None if duration is None else now + duration
        if duration is not None:
            self._last_open_duration = duration
        self._log.warning(
            "Circuit breaker opened",
            account_id=self._account_id,
            reason=reason,
            duration_seconds=duration,
        )
        storage.insert_circuit_breaker_event(
            self._db_path,
            account_id=self._account_id,
            event="opened",
            reason=reason,
            duration_seconds=duration,
        )

    def _transition_half_open_locked(self) -> None:
        self._state = BreakerState.HALF_OPEN
        self._probe_in_flight = False
        self._log.info(
            "Circuit breaker half-open — next send is a probe",
            account_id=self._account_id,
        )
        storage.insert_circuit_breaker_event(
            self._db_path,
            account_id=self._account_id,
            event="half_opened",
            reason=self._reason,
            duration_seconds=None,
        )

    def _close_locked(self, reason: str) -> None:
        self._state = BreakerState.CLOSED
        self._reason = None
        self._open_until = None
        self._log.info(
            "Circuit breaker closed", account_id=self._account_id, reason=reason
        )
        storage.insert_circuit_breaker_event(
            self._db_path,
            account_id=self._account_id,
            event="closed",
            reason=reason,
            duration_seconds=None,
        )


def _default_utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _seconds_until_utc_midnight() -> float:
    now = datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (tomorrow - now).total_seconds()
