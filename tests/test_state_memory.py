from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import storage


def _seed_contact(db: Path, account_id: int, chat_id: int = 100) -> int:
    storage.upsert_contact(
        db,
        account_id=account_id,
        chat_id=chat_id,
        tg_user_id=chat_id,
        username=f"u{chat_id}",
        first_name="T",
        last_name=None,
    )
    return chat_id


def test_get_state_returns_none_for_missing(db: Path, account_id: int) -> None:
    assert storage.get_contact_state(db, account_id, 999) is None


def test_state_upsert_partial_creates_with_defaults(db: Path, account_id: int) -> None:
    chat_id = _seed_contact(db, account_id)
    storage.upsert_contact_state(
        db, account_id=account_id, chat_id=chat_id, category="cold"
    )
    state = storage.get_contact_state(db, account_id, chat_id)
    assert state is not None
    assert state["category"] == "cold"
    assert state["funnel_stage"] is None
    assert state["flags"] == {}
    assert state["classifier_metadata"] == {}
    assert state["human_active"] == 0


def test_state_upsert_partial_update_preserves_other_fields(
    db: Path, account_id: int
) -> None:
    chat_id = _seed_contact(db, account_id)
    storage.upsert_contact_state(
        db, account_id=account_id, chat_id=chat_id, category="cold", human_active=1
    )
    storage.upsert_contact_state(
        db, account_id=account_id, chat_id=chat_id, category="warm"
    )
    state = storage.get_contact_state(db, account_id, chat_id)
    assert state is not None
    assert state["category"] == "warm"
    assert state["human_active"] == 1


def test_state_json_fields_round_trip(db: Path, account_id: int) -> None:
    chat_id = _seed_contact(db, account_id)
    flags = {"abusive": False, "vip": True, "tags": ["a", "b"]}
    meta = {"raw": {"category": "warm", "confidence": 0.83}}
    storage.upsert_contact_state(
        db, account_id=account_id, chat_id=chat_id,
        flags=flags, classifier_metadata=meta,
    )
    state = storage.get_contact_state(db, account_id, chat_id)
    assert state is not None
    assert state["flags"] == flags
    assert state["classifier_metadata"] == meta


def test_state_unknown_field_rejected(db: Path, account_id: int) -> None:
    chat_id = _seed_contact(db, account_id)
    with pytest.raises(ValueError, match="Unknown contact_state fields"):
        storage.upsert_contact_state(
            db, account_id=account_id, chat_id=chat_id, bogus="oops"  # type: ignore[call-arg]
        )


def test_state_fk_enforced(db: Path, account_id: int) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        storage.upsert_contact_state(
            db, account_id=account_id, chat_id=12345, category="cold"
        )


def test_memory_round_trip(db: Path, account_id: int) -> None:
    chat_id = _seed_contact(db, account_id)
    assert storage.get_contact_memory(db, account_id, chat_id) is None

    facts = {"name": "alex", "kinks": ["a", "b"], "spent_cents": 4200}
    storage.upsert_contact_memory(
        db, account_id=account_id, chat_id=chat_id,
        facts=facts,
        summary="ongoing chat about meeting up",
        summary_message_count=24,
    )
    memory = storage.get_contact_memory(db, account_id, chat_id)
    assert memory is not None
    assert memory["facts"] == facts
    assert memory["summary"] == "ongoing chat about meeting up"
    assert memory["summary_message_count"] == 24


def test_memory_partial_update(db: Path, account_id: int) -> None:
    chat_id = _seed_contact(db, account_id)
    storage.upsert_contact_memory(
        db, account_id=account_id, chat_id=chat_id, summary="first"
    )
    storage.upsert_contact_memory(
        db, account_id=account_id, chat_id=chat_id, summary_message_count=10
    )
    memory = storage.get_contact_memory(db, account_id, chat_id)
    assert memory is not None
    assert memory["summary"] == "first"
    assert memory["summary_message_count"] == 10


def test_memory_unknown_field_rejected(db: Path, account_id: int) -> None:
    chat_id = _seed_contact(db, account_id)
    with pytest.raises(ValueError, match="Unknown contact_memory fields"):
        storage.upsert_contact_memory(
            db, account_id=account_id, chat_id=chat_id, junk=1  # type: ignore[call-arg]
        )


def test_memory_fk_enforced(db: Path, account_id: int) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        storage.upsert_contact_memory(
            db, account_id=account_id, chat_id=99999, summary="hi"
        )
