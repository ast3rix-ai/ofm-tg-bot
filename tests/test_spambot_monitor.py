from __future__ import annotations

from pathlib import Path

from src import storage
from src.config import RateLimitConfig
from src.notifier import Notifier
from src.rate_limiter import BreakerState, RateLimiter
from src.spambot_monitor import SpamBotMonitor, parse_restriction

# Representative @SpamBot notification bodies.
_LIMITED_BODY = (
    "I'm afraid your account is now limited until 21 Jan 2026. "
    "Some actions like sending messages to strangers will be unavailable."
)
_MUTUAL_BODY = (
    "Unfortunately, you can only send messages to mutual contacts for now."
)
_GOOD_NEWS_BODY = (
    "Good news, no limits are currently applied to your account. "
    "It is free as a bird!"
)
_UNRELATED_BODY = "Hello! I'm here to help you with Telegram. Use the buttons below."


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.message = text


class _FakeEvent:
    def __init__(self, text: str) -> None:
        self.message = _FakeMessage(text)


def _limiter(db: Path, account_id: int) -> RateLimiter:
    return RateLimiter(db, account_id, RateLimitConfig())


def _monitor(
    db: Path, account_id: int, limiter: RateLimiter
) -> SpamBotMonitor:
    return SpamBotMonitor(
        db_path=db,
        account_id=account_id,
        rate_limiter=limiter,
        notifier=Notifier(token=None, chat_id=None),
    )


# ---------- parser ----------


def test_parse_restriction_detects_limited() -> None:
    assert parse_restriction(_LIMITED_BODY) == "account_limited"


def test_parse_restriction_detects_mutual_contacts_limit() -> None:
    assert parse_restriction(_MUTUAL_BODY) == "account_limited"


def test_parse_restriction_good_news_is_not_a_restriction() -> None:
    assert parse_restriction(_GOOD_NEWS_BODY) is None


def test_parse_restriction_unrelated_message() -> None:
    assert parse_restriction(_UNRELATED_BODY) is None


def test_parse_restriction_empty() -> None:
    assert parse_restriction("") is None


# ---------- monitor handler ----------


async def test_handle_restriction_trips_breaker_and_records(
    db: Path, account_id: int
) -> None:
    limiter = _limiter(db, account_id)
    monitor = _monitor(db, account_id, limiter)

    await monitor.handle(_FakeEvent(_LIMITED_BODY))

    assert limiter.breaker_state is BreakerState.OPEN
    restriction = storage.get_active_account_restriction(db, account_id)
    assert restriction is not None
    assert restriction["restriction_type"] == "account_limited"
    assert restriction["raw_body"] == _LIMITED_BODY

    alerts = storage.list_operator_alerts(db, account_id=account_id)
    assert any(a["alert_type"] == "account_restricted" for a in alerts)


async def test_handle_good_news_is_a_noop(db: Path, account_id: int) -> None:
    limiter = _limiter(db, account_id)
    monitor = _monitor(db, account_id, limiter)

    await monitor.handle(_FakeEvent(_GOOD_NEWS_BODY))

    assert limiter.breaker_state is BreakerState.CLOSED
    assert storage.get_active_account_restriction(db, account_id) is None


async def test_handle_unknown_body_does_not_trip(
    db: Path, account_id: int
) -> None:
    limiter = _limiter(db, account_id)
    monitor = _monitor(db, account_id, limiter)

    await monitor.handle(_FakeEvent(_UNRELATED_BODY))

    assert limiter.breaker_state is BreakerState.CLOSED
    assert storage.get_active_account_restriction(db, account_id) is None
