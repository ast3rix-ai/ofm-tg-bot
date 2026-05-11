from __future__ import annotations

import json
from pathlib import Path

from src import storage


def test_init_db_is_idempotent(db: Path) -> None:
    storage.init_db(db)
    storage.init_db(db)
    assert db.exists()


def test_insert_message_round_trip_and_duplicate(db: Path, account_id: int) -> None:
    inserted = storage.insert_message(
        db,
        account_id=account_id,
        chat_id=42,
        tg_message_id=1,
        direction="in",
        sender_id=42,
        text="hello",
        media_type=None,
        raw_json=json.dumps({"id": 1}),
    )
    assert inserted is True

    duplicate = storage.insert_message(
        db,
        account_id=account_id,
        chat_id=42,
        tg_message_id=1,
        direction="in",
        sender_id=42,
        text="hello",
        media_type=None,
        raw_json=json.dumps({"id": 1}),
    )
    assert duplicate is False


def test_insert_message_distinct_directions_allowed(db: Path, account_id: int) -> None:
    a = storage.insert_message(
        db,
        account_id=account_id,
        chat_id=1,
        tg_message_id=10,
        direction="in",
        sender_id=99,
        text="x",
        media_type=None,
        raw_json="{}",
    )
    b = storage.insert_message(
        db,
        account_id=account_id,
        chat_id=1,
        tg_message_id=10,
        direction="out",
        sender_id=99,
        text="y",
        media_type=None,
        raw_json="{}",
    )
    assert a is True
    assert b is True


def test_get_recent_messages_ordering(db: Path, account_id: int) -> None:
    for i in range(5):
        storage.insert_message(
            db,
            account_id=account_id,
            chat_id=7,
            tg_message_id=i,
            direction="in",
            sender_id=7,
            text=f"m{i}",
            media_type=None,
            raw_json="{}",
        )
    msgs = storage.get_recent_messages(db, account_id, 7, limit=3)
    assert [m["text"] for m in msgs] == ["m2", "m3", "m4"]
    all_msgs = storage.get_recent_messages(db, account_id, 7, limit=10)
    assert [m["text"] for m in all_msgs] == ["m0", "m1", "m2", "m3", "m4"]


def test_upsert_contact_updates_profile(db: Path, account_id: int) -> None:
    storage.upsert_contact(
        db,
        account_id=account_id,
        chat_id=1,
        tg_user_id=1,
        username="a",
        first_name="A",
        last_name=None,
    )
    storage.upsert_contact(
        db,
        account_id=account_id,
        chat_id=1,
        tg_user_id=1,
        username="b",
        first_name="B",
        last_name="Last",
    )

    contacts = storage.get_all_contacts(db, account_id=account_id)
    assert len(contacts) == 1
    assert contacts[0]["username"] == "b"
    assert contacts[0]["first_name"] == "B"
    assert contacts[0]["last_name"] == "Last"


def test_log_event_and_get_recent(db: Path, account_id: int) -> None:
    storage.log_event(db, "heartbeat", {"connected": True}, account_id=account_id)
    storage.log_event(db, "reconnect", {"attempts": 3}, account_id=account_id)

    events = storage.get_recent_events(db, limit=10, account_id=account_id)
    types = [e["event_type"] for e in events]
    assert types == ["reconnect", "heartbeat"]
    payload = json.loads(events[0]["payload_json"])
    assert payload == {"attempts": 3}


def test_contacts_message_count(db: Path, account_id: int) -> None:
    storage.upsert_contact(
        db,
        account_id=account_id,
        chat_id=5,
        tg_user_id=5,
        username=None,
        first_name=None,
        last_name=None,
    )
    for i in range(3):
        storage.insert_message(
            db,
            account_id=account_id,
            chat_id=5,
            tg_message_id=i,
            direction="in",
            sender_id=5,
            text="x",
            media_type=None,
            raw_json="{}",
        )
    contacts = storage.get_all_contacts(db, account_id=account_id)
    assert contacts[0]["message_count"] == 3
