from __future__ import annotations

from pathlib import Path

from src import commands, storage
from src.config import RateLimitConfig
from src.rate_limiter import BreakerState, RateLimiter


def _seed_full_chat(db: Path, account_id: int, chat_id: int = 1) -> None:
    storage.upsert_contact(
        db, account_id=account_id, chat_id=chat_id, tg_user_id=chat_id,
        username="u", first_name="U", last_name=None,
    )
    storage.upsert_contact_state(
        db, account_id=account_id, chat_id=chat_id,
        category="hot", bot_enabled=1,
    )
    storage.upsert_contact_memory(
        db, account_id=account_id, chat_id=chat_id,
        facts={"name": "larry"}, summary="chatted a bit",
    )
    storage.insert_message(
        db, account_id=account_id, chat_id=chat_id, tg_message_id=1,
        direction="in", sender_id=chat_id, text="hi", media_type=None,
        raw_json="{}",
    )
    storage.insert_classifier_run(
        db, account_id=account_id, chat_id=chat_id, triggered_by="new_message",
        input_message_count=1, category_before=None, category_after="hot",
        confidence=0.9, flags_before=None, flags_after={}, raw_llm_output="{}",
        latency_ms=10,
    )
    run_id = storage.insert_response_run(
        db, account_id=account_id, chat_id=chat_id, triggered_by_message_id=1,
        persona_version="v1", attempts=1, outcome="sent", gate_reason=None,
        raw_attempts=[], final_text="hey", latency_ms=20,
    )
    storage.insert_bot_sent_message(
        db, account_id=account_id, chat_id=chat_id, tg_message_id=99,
        response_run_id=run_id,
    )


async def test_reset_wipes_state_and_memory(db: Path, account_id: int) -> None:
    _seed_full_chat(db, account_id)
    assert storage.get_contact_state(db, account_id, 1) is not None
    assert storage.get_contact_memory(db, account_id, 1) is not None

    await commands.handle_reset(db_path=db, account_id=account_id, chat_id=1)

    assert storage.get_contact_state(db, account_id, 1) is None
    assert storage.get_contact_memory(db, account_id, 1) is None


async def test_reset_preserves_audit_trail(db: Path, account_id: int) -> None:
    _seed_full_chat(db, account_id)
    await commands.handle_reset(db_path=db, account_id=account_id, chat_id=1)

    messages = storage.get_recent_messages(db, account_id, 1)
    assert len(messages) == 1

    classifier_runs = storage.get_classifier_runs(
        db, account_id=account_id, chat_id=1
    )
    assert len(classifier_runs) == 1

    response_runs = storage.get_response_runs(db, account_id=account_id, chat_id=1)
    assert len(response_runs) == 1

    bot_sent = storage.get_bot_sent_tg_message_ids(db, account_id, 1)
    assert 99 in bot_sent


async def test_reset_logs_event(db: Path, account_id: int) -> None:
    _seed_full_chat(db, account_id)
    await commands.handle_reset(db_path=db, account_id=account_id, chat_id=1)

    events = storage.get_recent_events(db, account_id=account_id)
    reset_events = [e for e in events if e["event_type"] == "operator_reset"]
    assert len(reset_events) == 1


async def test_reset_on_unknown_chat_is_noop(db: Path, account_id: int) -> None:
    # No seeding — resetting a chat with no state must not raise.
    await commands.handle_reset(db_path=db, account_id=account_id, chat_id=777)
    assert storage.get_contact_state(db, account_id, 777) is None


async def test_breaker_reset_closes_breaker(db: Path, account_id: int) -> None:
    limiter = RateLimiter(db, account_id, RateLimitConfig())
    await limiter.record_peer_flood()
    assert limiter.breaker_state is BreakerState.OPEN

    await commands.handle_breaker_reset(
        db_path=db, account_id=account_id, rate_limiter=limiter
    )

    assert limiter.breaker_state is BreakerState.CLOSED


async def test_breaker_reset_clears_restriction_and_logs(
    db: Path, account_id: int
) -> None:
    storage.insert_account_restriction(
        db, account_id=account_id, restriction_type="account_limited",
        raw_body="limited",
    )
    limiter = RateLimiter(db, account_id, RateLimitConfig())
    assert limiter.breaker_state is BreakerState.OPEN

    await commands.handle_breaker_reset(
        db_path=db, account_id=account_id, rate_limiter=limiter
    )

    assert storage.get_active_account_restriction(db, account_id) is None
    events = storage.get_recent_events(db, account_id=account_id)
    assert any(e["event_type"] == "operator_breaker_reset" for e in events)
