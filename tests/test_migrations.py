from __future__ import annotations

import sqlite3
from pathlib import Path

from src import storage
from src.migrations import MIGRATIONS, SCHEMA_MIGRATIONS_DDL


def _applied_versions(db: Path) -> list[int]:
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    return [r[0] for r in rows]


def test_fresh_db_applies_all_migrations(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db)
    versions = _applied_versions(db)
    assert versions == [m.version for m in MIGRATIONS]


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db)
    storage.init_db(db)
    versions = _applied_versions(db)
    assert versions == [m.version for m in MIGRATIONS]


def test_phase1_db_is_upgraded(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(MIGRATIONS[0].sql)
        conn.executescript(SCHEMA_MIGRATIONS_DDL)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at)"
            " VALUES (1, 'initial_schema', '2026-01-01T00:00:00.000Z')"
        )
        conn.commit()

    storage.init_db(db)
    versions = _applied_versions(db)
    assert versions == [1, 2]

    with sqlite3.connect(str(db)) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"contacts", "messages", "events", "contact_state", "contact_memory"} <= names


def test_get_applied_migrations_returns_metadata(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db)
    rows = storage.get_applied_migrations(db)
    assert [r["version"] for r in rows] == [1, 2]
    assert rows[1]["name"] == "contact_state_and_memory"
    assert rows[0]["applied_at"]


def test_migration_smoke_schema_present(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db)
    with sqlite3.connect(str(db)) as conn:
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "idx_contact_state_category" in indexes
    assert "idx_contact_state_human_active" in indexes
