from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src import storage
from src.signal_detector import (
    contains_payment_screenshot,
    detect_resurface,
    detect_threat,
    is_greeting_only,
    is_price_inquiry,
    run_signals,
)


def test_price_inquiry_positives() -> None:
    for s in (
        "how much for a custom?",
        "PRICE list please",
        "what are your rates",
        "send me the menu",
        "$50 ok?",
        "can I get the PPV?",
    ):
        assert is_price_inquiry(s), s


def test_price_inquiry_negatives() -> None:
    for s in (
        "hi babe",
        "you look amazing",
        "i had a long day",
        "",
        None,
        "🙂",
    ):
        assert not is_price_inquiry(s)


def test_greeting_only_positives() -> None:
    for s in ("hi", "hey", "hello babe", "yo", "good morning", "ahoj"):
        assert is_greeting_only(s), s


def test_greeting_only_negatives() -> None:
    for s in (
        "hi how much",
        "",
        "hey, what's the menu?",
        "hi! lets talk about that custom we discussed",
        "hello? are you there??",
        None,
    ):
        assert not is_greeting_only(s)


def test_payment_screenshot_heuristic() -> None:
    photo_no_caption = {"media_type": "MessageMediaPhoto", "text": ""}
    assert contains_payment_screenshot(photo_no_caption)

    photo_with_sent = {"media_type": "MessageMediaPhoto", "text": "sent!"}
    assert contains_payment_screenshot(photo_with_sent)

    photo_random = {"media_type": "MessageMediaPhoto", "text": "look at my dog"}
    assert not contains_payment_screenshot(photo_random)

    no_photo = {"media_type": None, "text": "sent"}
    assert not contains_payment_screenshot(no_photo)


def test_threat_patterns() -> None:
    detected, details = detect_threat("i know where you live")
    assert detected
    assert details is not None and "doxing" in details

    detected, details = detect_threat("i want to die")
    assert detected
    assert details is not None and "self-harm" in details

    # Sexual content but not a threat-of-violence pattern.
    detected, _ = detect_threat("you're so hot babe")
    assert not detected

    # Empty string.
    detected, _ = detect_threat("")
    assert not detected


def test_resurface_detection(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet
    db = tmp_path / "bot.db"
    storage.init_db(
        db, migration_context={"encryption_key": Fernet.generate_key().decode("ascii")}
    )

    # Seed an account and a prior inbound 20 days ago.
    import sqlite3
    with sqlite3.connect(str(db)) as conn:
        from src.accounts import insert_account_row
        insert_account_row(
            conn,
            key=Fernet.generate_key().decode("ascii"),
            label="X", api_id=1, api_hash="x", phone="+1",
            session_string=None, is_active=True,
        )

    storage.upsert_contact(
        db, account_id=1, chat_id=42, tg_user_id=42,
        username=None, first_name=None, last_name=None,
    )

    old = (datetime.now(UTC) - timedelta(days=20)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    # Insert a message with backdated created_at via direct SQL.
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO messages (account_id, chat_id, tg_message_id, direction,"
            " sender_id, text, media_type, raw_json, created_at)"
            " VALUES (1, 42, 1, 'in', 42, 'old', NULL, '{}', ?)",
            (old,),
        )

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert detect_resurface(
        db_path=db, account_id=1, chat_id=42, now_iso=now, threshold_days=14
    )
    assert not detect_resurface(
        db_path=db, account_id=1, chat_id=42, now_iso=now, threshold_days=30
    )


def test_resurface_no_history(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet
    db = tmp_path / "bot.db"
    storage.init_db(
        db, migration_context={"encryption_key": Fernet.generate_key().decode("ascii")}
    )
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert not detect_resurface(
        db_path=db, account_id=1, chat_id=999, now_iso=now, threshold_days=14
    )


def test_run_signals_aggregates(db: Path, account_id: int) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    msg = {"text": "hey, how much for a custom?", "media_type": None}
    result = run_signals(
        msg, db_path=db, account_id=account_id, chat_id=1, now_iso=now
    )
    assert result.is_price_inquiry is True
    assert result.is_greeting_only is False
    assert result.is_threat is False
    assert result.any_signal is True
