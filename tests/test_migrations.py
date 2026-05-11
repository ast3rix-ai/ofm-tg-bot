from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from src import storage
from src.migrations import MIGRATIONS, SCHEMA_MIGRATIONS_DDL


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode("ascii")


def _applied_versions(db: Path) -> list[int]:
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    return [r[0] for r in rows]


def test_fresh_db_applies_all_migrations(tmp_path: Path, key: str) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db, migration_context={"encryption_key": key})
    versions = _applied_versions(db)
    assert versions == [m.version for m in MIGRATIONS]


def test_init_db_is_idempotent(tmp_path: Path, key: str) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db, migration_context={"encryption_key": key})
    storage.init_db(db, migration_context={"encryption_key": key})
    versions = _applied_versions(db)
    assert versions == [m.version for m in MIGRATIONS]


def test_phase1_db_is_upgraded(tmp_path: Path, key: str) -> None:
    db = tmp_path / "bot.db"
    with sqlite3.connect(str(db)) as conn:
        # Migration 001 is now ix=0 in MIGRATIONS, an SQL migration.
        sql = MIGRATIONS[0].sql
        conn.executescript(sql)
        conn.executescript(SCHEMA_MIGRATIONS_DDL)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at)"
            " VALUES (1, 'initial_schema', '2026-01-01T00:00:00.000Z')"
        )
        # Seed a contact + message so the migration 003 backfill must
        # carry them into the rebuilt tables.
        conn.execute(
            "INSERT INTO contacts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (501, 99, "alice", "A", None, "2026-01-01T00:00:00Z",
             "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO messages (chat_id, tg_message_id, direction, sender_id,"
            " text, media_type, raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (501, 1, "in", 99, "hi", None, "{}", "2026-01-01T00:00:00Z"),
        )
        conn.commit()

    storage.init_db(
        db,
        migration_context={
            "encryption_key": key,
            "default_label": "Default",
            "default_api_id": 12345,
            "default_api_hash": "hashhash",
            "default_phone": "+10000000000",
        },
    )
    versions = _applied_versions(db)
    assert versions == [1, 2, 3]

    with sqlite3.connect(str(db)) as conn:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"accounts", "contacts", "messages", "events",
                "contact_state", "contact_memory"} <= names

        # Default account inserted, active.
        accs = conn.execute(
            "SELECT id, label, is_active FROM accounts"
        ).fetchall()
        assert len(accs) == 1
        assert accs[0][1] == "Default"
        assert accs[0][2] == 1
        default_id = accs[0][0]

        # Existing contact now has account_id pointing at the default account.
        contact = conn.execute(
            "SELECT account_id, chat_id FROM contacts WHERE chat_id = 501"
        ).fetchone()
        assert contact == (default_id, 501)

        # Composite PK should accept the same chat_id under a different account.
        conn.execute(
            "INSERT INTO accounts (id, label, tg_api_id_enc, tg_api_hash_enc,"
            " tg_phone_enc, is_active, created_at)"
            " VALUES (NULL, 'Other', 'x', 'y', 'z', 0, '2026-01-01T00:00:00Z')"
        )
        new_id = conn.execute("SELECT max(id) FROM accounts").fetchone()[0]
        conn.execute(
            "INSERT INTO contacts (account_id, chat_id, tg_user_id, username,"
            " first_name, last_name, first_seen_at, last_seen_at)"
            " VALUES (?, 501, 1, 'b', 'B', NULL, '2026-01-01T00:00:00Z',"
            " '2026-01-01T00:00:00Z')",
            (new_id,),
        )


def test_get_applied_migrations_returns_metadata(tmp_path: Path, key: str) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db, migration_context={"encryption_key": key})
    rows = storage.get_applied_migrations(db)
    assert [r["version"] for r in rows] == [1, 2, 3]
    assert rows[1]["name"] == "contact_state_and_memory"
    assert rows[2]["name"] == "multi_account"


def test_migration_smoke_schema_present(tmp_path: Path, key: str) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db, migration_context={"encryption_key": key})
    with sqlite3.connect(str(db)) as conn:
        indexes = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "idx_contact_state_category" in indexes
    assert "idx_contact_state_human_active" in indexes
    assert "idx_accounts_active" in indexes


def test_migration_003_skips_backfill_without_creds(
    tmp_path: Path, key: str
) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db, migration_context={"encryption_key": key})
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert n == 0
