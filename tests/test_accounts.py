from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from src import accounts as accounts_mod
from src.accounts import AccountsError


def test_create_and_list(db: Path, encryption_key: str) -> None:
    # Conftest seeds one account already.
    initial = accounts_mod.list_accounts(db)
    assert len(initial) == 1

    new_id = accounts_mod.create_account(
        db, encryption_key,
        label="Alpha", api_id=42, api_hash="hash-a", phone="+10000000001",
    )
    assert new_id > 1
    rows = accounts_mod.list_accounts(db)
    assert {a.label for a in rows} == {"TestDefault", "Alpha"}
    # Credential fields not decrypted by list_accounts.
    alpha = next(a for a in rows if a.label == "Alpha")
    assert alpha.api_id is None and alpha.api_hash is None and alpha.phone is None
    assert alpha.has_session is False


def test_get_account_with_credentials(db: Path, encryption_key: str) -> None:
    new_id = accounts_mod.create_account(
        db, encryption_key,
        label="Beta", api_id=99, api_hash="hash-b", phone="+10000000002",
    )
    acc = accounts_mod.get_account(
        db, encryption_key, new_id, with_credentials=True
    )
    assert acc is not None
    assert acc.api_id == 99
    assert acc.api_hash == "hash-b"
    assert acc.phone == "+10000000002"


def test_duplicate_label_rejected(db: Path, encryption_key: str) -> None:
    accounts_mod.create_account(
        db, encryption_key,
        label="Dup", api_id=1, api_hash="x", phone="+1",
    )
    with pytest.raises(AccountsError, match="label already exists"):
        accounts_mod.create_account(
            db, encryption_key,
            label="Dup", api_id=2, api_hash="y", phone="+2",
        )


def test_set_active_account_is_exclusive(db: Path, encryption_key: str) -> None:
    a_id = accounts_mod.create_account(
        db, encryption_key, label="A", api_id=1, api_hash="x", phone="+1"
    )
    b_id = accounts_mod.create_account(
        db, encryption_key, label="B", api_id=2, api_hash="y", phone="+2"
    )

    accounts_mod.set_active_account(db, a_id)
    rows = {a.id: a for a in accounts_mod.list_accounts(db)}
    assert rows[a_id].is_active is True
    assert rows[b_id].is_active is False
    # Default seed account also no longer active.
    assert rows[1].is_active is False

    accounts_mod.set_active_account(db, b_id)
    rows = {a.id: a for a in accounts_mod.list_accounts(db)}
    assert rows[a_id].is_active is False
    assert rows[b_id].is_active is True


def test_set_active_unknown_raises(db: Path) -> None:
    with pytest.raises(AccountsError, match="No account with id"):
        accounts_mod.set_active_account(db, 9999)


def test_session_blob_round_trip(db: Path, encryption_key: str) -> None:
    new_id = accounts_mod.create_account(
        db, encryption_key, label="Sess", api_id=1, api_hash="x", phone="+1"
    )
    assert accounts_mod.read_session_blob(db, encryption_key, new_id) is None

    accounts_mod.update_session_blob(
        db, encryption_key, new_id, "some_telethon_string_session"
    )
    blob = accounts_mod.read_session_blob(db, encryption_key, new_id)
    assert blob == "some_telethon_string_session"

    listed = next(
        a for a in accounts_mod.list_accounts(db) if a.id == new_id
    )
    assert listed.has_session is True


def test_wrong_key_fails_to_decrypt(db: Path, encryption_key: str) -> None:
    new_id = accounts_mod.create_account(
        db, encryption_key,
        label="Cred", api_id=7, api_hash="x", phone="+1",
    )
    from cryptography.fernet import Fernet
    other_key = Fernet.generate_key().decode("ascii")
    with pytest.raises(AccountsError, match="wrong encryption key"):
        accounts_mod.get_account(
            db, other_key, new_id, with_credentials=True
        )


def test_update_metadata_partial(db: Path, encryption_key: str) -> None:
    new_id = accounts_mod.create_account(
        db, encryption_key, label="M", api_id=1, api_hash="x", phone="+1"
    )
    accounts_mod.update_account_metadata(
        db, new_id, tg_user_id=12345, tg_username="me",
    )
    acc = accounts_mod.get_account(db, encryption_key, new_id)
    assert acc is not None
    assert acc.tg_user_id == 12345
    assert acc.tg_username == "me"


def test_delete_cascades(make_db: Callable[[], Path], encryption_key: str) -> None:
    db = make_db()
    from src import storage

    storage.upsert_contact(
        db, account_id=1, chat_id=1, tg_user_id=1,
        username=None, first_name=None, last_name=None,
    )
    storage.insert_message(
        db, account_id=1, chat_id=1, tg_message_id=1, direction="in",
        sender_id=1, text="x", media_type=None, raw_json="{}",
    )
    storage.upsert_contact_state(db, account_id=1, chat_id=1, category="cold")

    accounts_mod.delete_account(db, 1)
    assert storage.get_all_contacts(db, account_id=1) == []
    assert storage.get_recent_messages(db, 1, 1) == []
    assert storage.get_contact_state(db, 1, 1) is None


def test_get_active_account(db: Path, encryption_key: str) -> None:
    active = accounts_mod.get_active_account(db, encryption_key)
    assert active is not None
    assert active.id == 1
    assert active.label == "TestDefault"
    assert active.api_id == 11111
