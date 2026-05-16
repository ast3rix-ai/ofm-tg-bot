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
    assert versions == [1, 2, 3, 4, 5, 6]

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
    assert [r["version"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert rows[1]["name"] == "contact_state_and_memory"
    assert rows[2]["name"] == "multi_account"
    assert rows[3]["name"] == "classifier_runs_and_alerts"
    assert rows[4]["name"] == "response_runs_and_bot_sent"
    assert rows[5]["name"] == "rate_limit_and_circuit_breaker"


def test_migration_004_adds_bot_enabled_and_alerts(
    tmp_path: Path, key: str
) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db, migration_context={"encryption_key": key})
    with sqlite3.connect(str(db)) as conn:
        cs_cols = {r[1] for r in conn.execute("PRAGMA table_info(contact_state)")}
        assert {
            "bot_enabled", "bootstrap_completed_at", "last_resurface_at"
        } <= cs_cols

        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "classifier_runs" in tables
        assert "operator_alerts" in tables


def test_phase3_db_is_upgraded_to_phase4(tmp_path: Path, key: str) -> None:
    db = tmp_path / "bot.db"
    # First apply 1+2+3 (run init_db); then drop just the Phase 4 changes
    # would be artificial — simulate "Phase 3 stop point" by removing the
    # version-4 row, the new columns, and the new tables, then re-running.
    storage.init_db(db, migration_context={"encryption_key": key})
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version >= 4")
        conn.execute("DROP TABLE classifier_runs")
        conn.execute("DROP TABLE operator_alerts")
        # Faking the column drop: SQLite can't drop columns easily; instead,
        # check that the migration is idempotent: re-running init_db should
        # restore the tables (it skips already-present columns).
    storage.init_db(db, migration_context={"encryption_key": key})
    with sqlite3.connect(str(db)) as conn:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "classifier_runs" in tables
    assert "operator_alerts" in tables


def test_migration_005_creates_response_tables(tmp_path: Path, key: str) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db, migration_context={"encryption_key": key})
    with sqlite3.connect(str(db)) as conn:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "bot_sent_messages" in tables
        assert "response_runs" in tables

        rr_cols = {r[1] for r in conn.execute("PRAGMA table_info(response_runs)")}
        assert {
            "account_id", "chat_id", "triggered_by_message_id", "persona_version",
            "attempts", "outcome", "gate_reason", "raw_attempts", "final_text",
            "latency_ms", "created_at",
        } <= rr_cols

        bsm_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(bot_sent_messages)")
        }
        assert {
            "account_id", "chat_id", "tg_message_id", "response_run_id",
            "created_at",
        } <= bsm_cols


def test_phase4_db_is_upgraded_to_phase5(tmp_path: Path, key: str) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db, migration_context={"encryption_key": key})
    # Simulate a "Phase 4 stop point": remove the version-5 row and tables,
    # then confirm a re-run of init_db restores them idempotently.
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version >= 5")
        conn.execute("DROP TABLE bot_sent_messages")
        conn.execute("DROP TABLE response_runs")
    storage.init_db(db, migration_context={"encryption_key": key})
    with sqlite3.connect(str(db)) as conn:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "bot_sent_messages" in tables
    assert "response_runs" in tables
    assert _applied_versions(db) == [1, 2, 3, 4, 5, 6]


def test_migration_006_creates_rate_limit_tables(
    tmp_path: Path, key: str
) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db, migration_context={"encryption_key": key})
    with sqlite3.connect(str(db)) as conn:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "daily_send_counters", "daily_global_counters",
            "circuit_breaker_events", "account_restrictions",
        } <= tables

        rr_cols = {r[1] for r in conn.execute("PRAGMA table_info(response_runs)")}
        assert {
            "rate_limit_state", "flood_wait_seconds",
            "circuit_breaker_tripped_at",
        } <= rr_cols

        bsm_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(bot_sent_messages)")
        }
        assert {
            "split_index", "total_chunks", "humanizer_metadata",
        } <= bsm_cols


def test_phase5_db_is_upgraded_to_phase6(tmp_path: Path, key: str) -> None:
    db = tmp_path / "bot.db"
    storage.init_db(db, migration_context={"encryption_key": key})
    # Simulate a "Phase 5 stop point": drop the version-6 row and tables,
    # then confirm a re-run of init_db restores them idempotently.
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version >= 6")
        conn.execute("DROP TABLE daily_send_counters")
        conn.execute("DROP TABLE daily_global_counters")
        conn.execute("DROP TABLE circuit_breaker_events")
        conn.execute("DROP TABLE account_restrictions")
    storage.init_db(db, migration_context={"encryption_key": key})
    with sqlite3.connect(str(db)) as conn:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "daily_send_counters", "daily_global_counters",
        "circuit_breaker_events", "account_restrictions",
    } <= tables
    assert _applied_versions(db) == [1, 2, 3, 4, 5, 6]


def test_migration_006_is_idempotent_on_columns(
    tmp_path: Path, key: str
) -> None:
    # Re-running init_db must not fail on the already-present ADD COLUMNs.
    db = tmp_path / "bot.db"
    storage.init_db(db, migration_context={"encryption_key": key})
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version >= 6")
    storage.init_db(db, migration_context={"encryption_key": key})
    assert _applied_versions(db) == [1, 2, 3, 4, 5, 6]


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
