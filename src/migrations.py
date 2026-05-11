from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    """A single forward-only schema migration.

    Attributes:
        version: Monotonic integer applied in ascending order.
        name: Short human-readable identifier (snake_case).
        sql: One or more DDL statements, run by `sqlite3.executescript`.
            Statements must be idempotent (use `IF NOT EXISTS`) so the
            migration is safe to re-run on a DB that already contains the
            schema but is missing the version row (e.g. upgrading a
            Phase 1 database).
    """

    version: int
    name: str
    sql: str


_MIGRATION_001 = """
CREATE TABLE IF NOT EXISTS contacts (
    chat_id        INTEGER PRIMARY KEY,
    tg_user_id     INTEGER NOT NULL,
    username       TEXT,
    first_name     TEXT,
    last_name      TEXT,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    tg_message_id  INTEGER NOT NULL,
    direction      TEXT NOT NULL CHECK (direction IN ('in','out')),
    sender_id      INTEGER NOT NULL,
    text           TEXT,
    media_type     TEXT,
    raw_json       TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (chat_id, tg_message_id, direction)
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_created
    ON messages (chat_id, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_created
    ON messages (created_at);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_created
    ON events (created_at);
"""

_MIGRATION_002 = """
CREATE TABLE IF NOT EXISTS contact_state (
    chat_id                    INTEGER PRIMARY KEY,
    category                   TEXT,
    funnel_stage               TEXT,
    flags                      TEXT NOT NULL DEFAULT '{}',
    last_classified_at         TEXT,
    last_classifier_confidence REAL,
    classifier_metadata        TEXT NOT NULL DEFAULT '{}',
    human_active               INTEGER NOT NULL DEFAULT 0,
    human_active_until         TEXT,
    updated_at                 TEXT NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES contacts(chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_contact_state_category
    ON contact_state (category);

CREATE INDEX IF NOT EXISTS idx_contact_state_human_active
    ON contact_state (human_active);

CREATE TABLE IF NOT EXISTS contact_memory (
    chat_id                INTEGER PRIMARY KEY,
    facts                  TEXT NOT NULL DEFAULT '{}',
    summary                TEXT NOT NULL DEFAULT '',
    summary_message_count  INTEGER NOT NULL DEFAULT 0,
    last_summarized_at     TEXT,
    updated_at             TEXT NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES contacts(chat_id) ON DELETE CASCADE
);
"""

MIGRATIONS: list[Migration] = [
    Migration(version=1, name="initial_schema", sql=_MIGRATION_001),
    Migration(version=2, name="contact_state_and_memory", sql=_MIGRATION_002),
]


SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""
