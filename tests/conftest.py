from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from src import storage

_TEST_KEY = Fernet.generate_key().decode("ascii")


def _seed_default_account(conn: sqlite3.Connection) -> int:
    from src.accounts import insert_account_row

    return insert_account_row(
        conn,
        key=_TEST_KEY,
        label="TestDefault",
        api_id=11111,
        api_hash="aaaaaaaa",
        phone="+10000000001",
        session_string=None,
        is_active=True,
    )


@pytest.fixture
def encryption_key() -> str:
    return _TEST_KEY


@pytest.fixture
def make_db(tmp_path: Path) -> Callable[[], Path]:
    """Return a factory creating a fresh DB with one seeded account (id=1)."""
    counter = {"i": 0}

    def _factory() -> Path:
        counter["i"] += 1
        db = tmp_path / f"bot{counter['i']}.db"
        storage.init_db(db, migration_context={"encryption_key": _TEST_KEY})
        # Seed default account so account_id=1 references something concrete.
        with sqlite3.connect(str(db)) as conn:
            _seed_default_account(conn)
        return db

    return _factory


@pytest.fixture
def db(make_db: Callable[[], Path]) -> Path:
    return make_db()


@pytest.fixture
def account_id() -> int:
    return 1
