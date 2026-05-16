from __future__ import annotations

import asyncio
from pathlib import Path

from src import storage
from src.config import RateLimitConfig
from src.rate_limiter import (
    REASON_ACCOUNT_RESTRICTED,
    REASON_BREAKER_OPEN,
    REASON_DAILY_CAP,
    REASON_GLOBAL_BUCKET,
    REASON_PER_CHAT_BUCKET,
    BreakerState,
    RateLimiter,
    TokenBucket,
)


class _Clock:
    """A manually-advanced monotonic clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _limiter(
    db: Path,
    account_id: int,
    *,
    clock: _Clock,
    config: RateLimitConfig | None = None,
    day: list[str] | None = None,
) -> RateLimiter:
    day_box = day if day is not None else ["2026-05-16"]
    return RateLimiter(
        db,
        account_id,
        config or RateLimitConfig(),
        monotonic=clock,
        utc_day=lambda: day_box[0],
    )


# ---------- TokenBucket ----------


def test_token_bucket_starts_full() -> None:
    bucket = TokenBucket(refill_per_sec=0.5, capacity=3.0)
    assert bucket.level(0.0) == 3.0


def test_token_bucket_consume_and_refill() -> None:
    bucket = TokenBucket(refill_per_sec=0.5, capacity=3.0)
    bucket.consume(0.0)
    bucket.consume(0.0)
    bucket.consume(0.0)
    assert bucket.level(0.0) == 0.0
    # 0.5 tokens/sec → 4 s later, 2 tokens.
    assert bucket.level(4.0) == 2.0


def test_token_bucket_capped_at_capacity() -> None:
    bucket = TokenBucket(refill_per_sec=1.0, capacity=3.0)
    bucket.consume(0.0)
    # Far in the future the bucket never exceeds capacity.
    assert bucket.level(10_000.0) == 3.0


# ---------- buckets via acquire ----------


async def test_acquire_allowed_when_fresh(db: Path, account_id: int) -> None:
    limiter = _limiter(db, account_id, clock=_Clock())
    result = await limiter.acquire(chat_id=5, is_new_chat=False)
    assert result.allowed is True
    assert result.reason == "allowed"


async def test_per_chat_bucket_empties_then_refills(
    db: Path, account_id: int
) -> None:
    clock = _Clock()
    limiter = _limiter(db, account_id, clock=clock)
    # Per-chat capacity is 3 — the 4th immediate send is denied.
    for _ in range(3):
        assert (await limiter.acquire(7, False)).allowed is True
    denied = await limiter.acquire(7, False)
    assert denied.allowed is False
    assert denied.reason == REASON_PER_CHAT_BUCKET

    # Refill 0.5/s → after 2 s one token is back.
    clock.advance(2.0)
    assert (await limiter.acquire(7, False)).allowed is True


async def test_global_bucket_empties_across_chats(
    db: Path, account_id: int
) -> None:
    clock = _Clock()
    limiter = _limiter(db, account_id, clock=clock)
    # Global capacity 10 — spread across distinct chats so per-chat buckets
    # (capacity 3) are not the limiting factor.
    allowed = 0
    for chat_id in range(20):
        if (await limiter.acquire(chat_id, False)).allowed:
            allowed += 1
    assert allowed == 10
    denied = await limiter.acquire(999, False)
    assert denied.reason == REASON_GLOBAL_BUCKET


# ---------- daily caps ----------


async def test_daily_per_chat_cap_enforced(db: Path, account_id: int) -> None:
    clock = _Clock()
    config = RateLimitConfig(
        daily_per_chat_cap=2, per_chat_capacity=100.0, global_capacity=100.0
    )
    limiter = _limiter(db, account_id, clock=clock, config=config)
    assert (await limiter.acquire(3, False)).allowed is True
    assert (await limiter.acquire(3, False)).allowed is True
    denied = await limiter.acquire(3, False)
    assert denied.allowed is False
    assert denied.reason == REASON_DAILY_CAP


async def test_daily_global_cap_enforced(db: Path, account_id: int) -> None:
    clock = _Clock()
    config = RateLimitConfig(
        daily_global_cap=2, per_chat_capacity=100.0, global_capacity=100.0
    )
    limiter = _limiter(db, account_id, clock=clock, config=config)
    assert (await limiter.acquire(1, False)).allowed is True
    assert (await limiter.acquire(2, False)).allowed is True
    denied = await limiter.acquire(3, False)
    assert denied.reason == REASON_DAILY_CAP


async def test_new_chat_subcap_enforced(db: Path, account_id: int) -> None:
    clock = _Clock()
    config = RateLimitConfig(
        new_chat_daily_cap=1,
        daily_per_chat_cap=100,
        per_chat_capacity=100.0,
        global_capacity=100.0,
    )
    limiter = _limiter(db, account_id, clock=clock, config=config)
    assert (await limiter.acquire(4, is_new_chat=True)).allowed is True
    denied = await limiter.acquire(4, is_new_chat=True)
    assert denied.allowed is False
    assert denied.reason == REASON_DAILY_CAP


async def test_daily_cap_resets_on_utc_rollover(
    db: Path, account_id: int
) -> None:
    clock = _Clock()
    day = ["2026-05-16"]
    config = RateLimitConfig(
        daily_per_chat_cap=1, per_chat_capacity=100.0, global_capacity=100.0
    )
    limiter = _limiter(db, account_id, clock=clock, config=config, day=day)
    assert (await limiter.acquire(8, False)).allowed is True
    assert (await limiter.acquire(8, False)).allowed is False
    # New UTC day — counters are keyed by day, so the cap resets.
    day[0] = "2026-05-17"
    assert (await limiter.acquire(8, False)).allowed is True


# ---------- circuit breaker ----------


async def test_breaker_trips_after_two_floods(db: Path, account_id: int) -> None:
    clock = _Clock()
    limiter = _limiter(db, account_id, clock=clock)
    await limiter.record_flood_wait(10.0)
    assert limiter.breaker_state is BreakerState.CLOSED
    await limiter.record_flood_wait(20.0)
    assert limiter.breaker_state is BreakerState.OPEN
    denied = await limiter.acquire(1, False)
    assert denied.reason == REASON_BREAKER_OPEN
    # Open duration is max(flood) * 4 = 80 s.
    assert denied.retry_after_seconds is not None
    assert 70.0 < denied.retry_after_seconds <= 80.0


async def test_two_floods_outside_window_do_not_trip(
    db: Path, account_id: int
) -> None:
    clock = _Clock()
    limiter = _limiter(db, account_id, clock=clock)
    await limiter.record_flood_wait(10.0)
    clock.advance(400.0)  # beyond the 300 s double-window
    await limiter.record_flood_wait(10.0)
    assert limiter.breaker_state is BreakerState.CLOSED


async def test_breaker_trips_on_peer_flood(db: Path, account_id: int) -> None:
    clock = _Clock()
    limiter = _limiter(db, account_id, clock=clock)
    await limiter.record_peer_flood()
    assert limiter.breaker_state is BreakerState.OPEN
    denied = await limiter.acquire(1, False)
    assert denied.reason == REASON_BREAKER_OPEN
    # Peer-flood opens for 6 hours.
    assert denied.retry_after_seconds is not None
    assert denied.retry_after_seconds > 21000.0


async def test_breaker_trips_on_account_restriction(
    db: Path, account_id: int
) -> None:
    clock = _Clock()
    limiter = _limiter(db, account_id, clock=clock)
    await limiter.record_account_restriction()
    assert limiter.breaker_state is BreakerState.OPEN
    clock.advance(1_000_000.0)  # no timer — never auto-recovers
    denied = await limiter.acquire(1, False)
    assert denied.reason == REASON_ACCOUNT_RESTRICTED


async def test_half_open_probe_success_closes_breaker(
    db: Path, account_id: int
) -> None:
    clock = _Clock()
    limiter = _limiter(db, account_id, clock=clock)
    await limiter.record_peer_flood()
    clock.advance(30_000.0)  # past the 6 h open window
    probe = await limiter.acquire(1, False)
    assert probe.allowed is True
    assert probe.is_probe is True
    assert limiter.breaker_state is BreakerState.HALF_OPEN
    # While the probe is in flight, other sends are denied.
    assert (await limiter.acquire(2, False)).reason == REASON_BREAKER_OPEN
    await limiter.record_probe_result(success=True)
    assert limiter.breaker_state is BreakerState.CLOSED
    assert (await limiter.acquire(3, False)).allowed is True


async def test_half_open_probe_failure_reopens_doubled(
    db: Path, account_id: int
) -> None:
    clock = _Clock()
    limiter = _limiter(db, account_id, clock=clock)
    await limiter.record_flood_wait(10.0)
    await limiter.record_flood_wait(10.0)  # trips: open for 40 s
    clock.advance(50.0)
    probe = await limiter.acquire(1, False)
    assert probe.is_probe is True
    await limiter.record_probe_result(success=False)
    assert limiter.breaker_state is BreakerState.OPEN
    denied = await limiter.acquire(2, False)
    # Re-opened with doubled duration (40 → 80 s).
    assert denied.retry_after_seconds is not None
    assert 70.0 < denied.retry_after_seconds <= 80.0


async def test_persisted_restriction_denies_on_construction(
    db: Path, account_id: int
) -> None:
    # A restriction row left by a prior process keeps the breaker open.
    storage.insert_account_restriction(
        db, account_id=account_id, restriction_type="account_limited",
        raw_body="your account is now limited",
    )
    limiter = _limiter(db, account_id, clock=_Clock())
    assert limiter.breaker_state is BreakerState.OPEN
    denied = await limiter.acquire(1, False)
    assert denied.reason == REASON_ACCOUNT_RESTRICTED


async def test_reset_breaker_clears_restriction(
    db: Path, account_id: int
) -> None:
    storage.insert_account_restriction(
        db, account_id=account_id, restriction_type="account_limited",
        raw_body="limited",
    )
    limiter = _limiter(db, account_id, clock=_Clock())
    await limiter.reset_breaker()
    assert limiter.breaker_state is BreakerState.CLOSED
    assert storage.get_active_account_restriction(db, account_id) is None
    assert (await limiter.acquire(1, False)).allowed is True


# ---------- concurrency ----------


async def test_concurrent_acquire_does_not_overconsume(
    db: Path, account_id: int
) -> None:
    clock = _Clock()
    limiter = _limiter(db, account_id, clock=clock)
    # 12 concurrent acquires on one chat — only the 3 bucket tokens may pass.
    results = await asyncio.gather(
        *(limiter.acquire(1, False) for _ in range(12))
    )
    assert sum(1 for r in results if r.allowed) == 3


async def test_snapshot_reports_breaker_and_utilization(
    db: Path, account_id: int
) -> None:
    clock = _Clock()
    limiter = _limiter(db, account_id, clock=clock)
    await limiter.acquire(1, False)
    snap = limiter.snapshot()
    assert snap["circuit_breaker"]["state"] == "closed"  # type: ignore[index]
    assert snap["daily_global"]["count"] == 1  # type: ignore[index]
    assert snap["account_restricted"] is False
