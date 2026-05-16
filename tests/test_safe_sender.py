from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any

from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserPrivacyRestrictedError,
)

from src import storage
from src.config import HumanizationConfig, RateLimitConfig
from src.humanizer import Humanizer
from src.rate_limiter import BreakerState, RateLimiter
from src.safe_sender import (
    STATUS_RATE_LIMITED,
    STATUS_SEND_FAILED,
    STATUS_SENT,
    SafeSender,
)


class _FakeTypingAction:
    async def __aenter__(self) -> _FakeTypingAction:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakeClient:
    """Minimal stand-in for `BotClient` recording sends and read receipts."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.read_calls: list[int] = []
        self.send_exceptions: list[BaseException | None] = []
        self._next_id = 9000

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        if self.send_exceptions:
            exc = self.send_exceptions.pop(0)
            if exc is not None:
                raise exc
        self._next_id += 1
        self.sent.append((chat_id, text))
        return {"tg_message_id": self._next_id, "created_at": "2026-05-16T0Z"}

    async def mark_read(self, chat_id: int) -> None:
        self.read_calls.append(chat_id)

    def typing_action(self, _chat_id: int) -> _FakeTypingAction:
        return _FakeTypingAction()


async def _no_sleep(_seconds: float) -> None:
    return None


def _locks() -> Any:
    store: dict[int, asyncio.Lock] = {}

    def get_lock(chat_id: int) -> asyncio.Lock:
        return store.setdefault(chat_id, asyncio.Lock())

    return get_lock


def _make_sender(
    db: Path,
    account_id: int,
    client: _FakeClient,
    *,
    rate_limiter: RateLimiter | None = None,
    humanizer: HumanizationConfig | None = None,
    seed: int = 1,
) -> tuple[SafeSender, RateLimiter]:
    limiter = rate_limiter or RateLimiter(db, account_id, RateLimitConfig())
    hcfg = humanizer or HumanizationConfig(single_chunk_prob=1.0)
    sender = SafeSender(
        db_path=db,
        account_id=account_id,
        telegram_client=client,  # type: ignore[arg-type]
        rate_limiter=limiter,
        humanizer=Humanizer(hcfg, random.Random(seed)),
        get_lock=_locks(),
        sleep=_no_sleep,
    )
    return sender, limiter


def _seed_run(db: Path, account_id: int, chat_id: int = 1) -> int:
    storage.upsert_contact(
        db, account_id=account_id, chat_id=chat_id, tg_user_id=chat_id,
        username="u", first_name="U", last_name=None,
    )
    storage.insert_message(
        db, account_id=account_id, chat_id=chat_id, tg_message_id=1,
        direction="in", sender_id=chat_id, text="hey there", media_type=None,
        raw_json="{}",
    )
    return storage.insert_response_run(
        db, account_id=account_id, chat_id=chat_id,
        triggered_by_message_id=None, persona_version="v1", attempts=1,
        outcome="pending", gate_reason=None, raw_attempts=[],
        final_text="hi", latency_ms=0,
    )


# ---------- happy path ----------


async def test_send_happy_path(db: Path, account_id: int) -> None:
    client = _FakeClient()
    sender, limiter = _make_sender(db, account_id, client)
    run_id = _seed_run(db, account_id)

    result = await sender.send(1, "heyy cutie", run_id, is_new_chat=False)

    assert result.status == STATUS_SENT
    assert result.chunks_sent == 1
    assert client.sent == [(1, "heyy cutie")]
    assert client.read_calls == [1]  # read receipt sent

    bot_sent = storage.get_bot_sent_tg_message_ids(db, account_id, 1)
    assert len(bot_sent) == 1
    out = [
        m for m in storage.get_recent_messages(db, account_id, 1)
        if m["direction"] == "out"
    ]
    assert out and out[0]["text"] == "heyy cutie"


async def test_send_records_humanizer_metadata(
    db: Path, account_id: int
) -> None:
    client = _FakeClient()
    sender, _ = _make_sender(db, account_id, client)
    run_id = _seed_run(db, account_id)

    await sender.send(1, "hello there", run_id, is_new_chat=False)

    rows = storage.get_response_runs(db, account_id=account_id, chat_id=1)
    assert rows[0]["rate_limit_state"] == "allowed"
    # bot_sent_messages carries the split/typing metadata.
    import sqlite3
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT split_index, total_chunks, humanizer_metadata"
            " FROM bot_sent_messages WHERE account_id=? AND chat_id=?",
            (account_id, 1),
        ).fetchone()
    assert row[0] == 0 and row[1] == 1
    assert "typing_duration_ms" in (row[2] or "")


# ---------- rate limiting ----------


async def test_send_gated_when_breaker_open(db: Path, account_id: int) -> None:
    client = _FakeClient()
    limiter = RateLimiter(db, account_id, RateLimitConfig())
    await limiter.record_peer_flood()  # trip the breaker
    sender, _ = _make_sender(db, account_id, client, rate_limiter=limiter)
    run_id = _seed_run(db, account_id)

    result = await sender.send(1, "heyy", run_id, is_new_chat=False)

    assert result.status == STATUS_RATE_LIMITED
    assert result.chunks_sent == 0
    assert client.sent == []
    rows = storage.get_response_runs(db, account_id=account_id, chat_id=1)
    assert rows[0]["rate_limit_state"] == "breaker_open"


# ---------- message splitting ----------


async def test_send_splits_into_multiple_chunks(
    db: Path, account_id: int
) -> None:
    client = _FakeClient()
    hcfg = HumanizationConfig(single_chunk_prob=0.0, split_prob=1.0)
    sender, _ = _make_sender(db, account_id, client, humanizer=hcfg, seed=7)
    run_id = _seed_run(db, account_id)

    text = "Hey there beautiful. How was your day today? I missed you lots."
    result = await sender.send(1, text, run_id, is_new_chat=False)

    assert result.status == STATUS_SENT
    assert len(client.sent) >= 2
    assert result.chunks_sent == len(client.sent)
    bot_sent = storage.get_bot_sent_tg_message_ids(db, account_id, 1)
    assert len(bot_sent) == len(client.sent)


# ---------- flood handling ----------


async def test_flood_wait_single_retry_then_succeeds(
    db: Path, account_id: int
) -> None:
    client = _FakeClient()
    client.send_exceptions = [FloodWaitError(request=None, capture=5)]
    sender, limiter = _make_sender(db, account_id, client)
    run_id = _seed_run(db, account_id)

    result = await sender.send(1, "heyy", run_id, is_new_chat=False)

    assert result.status == STATUS_SENT
    assert client.sent == [(1, "heyy")]  # retried once, succeeded
    assert limiter.breaker_state is BreakerState.CLOSED
    rows = storage.get_response_runs(db, account_id=account_id, chat_id=1)
    assert rows[0]["flood_wait_seconds"] == 5


async def test_double_flood_wait_trips_breaker(
    db: Path, account_id: int
) -> None:
    client = _FakeClient()
    client.send_exceptions = [
        FloodWaitError(request=None, capture=5),
        FloodWaitError(request=None, capture=8),
    ]
    sender, limiter = _make_sender(db, account_id, client)
    run_id = _seed_run(db, account_id)

    result = await sender.send(1, "heyy", run_id, is_new_chat=False)

    assert result.status == STATUS_SEND_FAILED
    assert client.sent == []  # never retried beyond the single allowed retry
    assert limiter.breaker_state is BreakerState.OPEN


async def test_peer_flood_trips_breaker_immediately(
    db: Path, account_id: int
) -> None:
    client = _FakeClient()
    client.send_exceptions = [PeerFloodError(request=None)]
    sender, limiter = _make_sender(db, account_id, client)
    run_id = _seed_run(db, account_id)

    result = await sender.send(1, "heyy", run_id, is_new_chat=False)

    assert result.status == STATUS_SEND_FAILED
    assert limiter.breaker_state is BreakerState.OPEN
    alerts = storage.list_operator_alerts(db, account_id=account_id)
    assert any(a["alert_type"] == "peer_flood" for a in alerts)


async def test_privacy_error_fails_without_tripping_breaker(
    db: Path, account_id: int
) -> None:
    client = _FakeClient()
    client.send_exceptions = [UserPrivacyRestrictedError(request=None)]
    sender, limiter = _make_sender(db, account_id, client)
    run_id = _seed_run(db, account_id)

    result = await sender.send(1, "heyy", run_id, is_new_chat=False)

    assert result.status == STATUS_SEND_FAILED
    assert limiter.breaker_state is BreakerState.CLOSED  # not an abuse signal


# ---------- typo correction ----------


async def test_typo_correction_sends_followup(
    db: Path, account_id: int
) -> None:
    client = _FakeClient()
    hcfg = HumanizationConfig(
        single_chunk_prob=1.0,
        typo_per_word_prob=1.0,
        typo_correction_prob=1.0,
    )
    sender, _ = _make_sender(db, account_id, client, humanizer=hcfg, seed=3)
    run_id = _seed_run(db, account_id)

    result = await sender.send(
        1, "hello gorgeous darling", run_id, is_new_chat=False
    )

    assert result.status == STATUS_SENT
    # One typo'd chunk plus one `*correction` follow-up.
    assert len(client.sent) == 2
    assert client.sent[1][1].startswith("*")


# ---------- half-open probe ----------


async def test_probe_send_success_closes_breaker(
    db: Path, account_id: int
) -> None:
    client = _FakeClient()
    clock_box = [1000.0]
    limiter = RateLimiter(
        db, account_id, RateLimitConfig(), monotonic=lambda: clock_box[0]
    )
    await limiter.record_flood_wait(5.0)
    await limiter.record_flood_wait(5.0)  # trips: open 20 s
    clock_box[0] += 100.0  # past the open window → half-open on next acquire
    sender, _ = _make_sender(db, account_id, client, rate_limiter=limiter)
    run_id = _seed_run(db, account_id)

    result = await sender.send(1, "heyy", run_id, is_new_chat=False)

    assert result.status == STATUS_SENT
    assert limiter.breaker_state is BreakerState.CLOSED
