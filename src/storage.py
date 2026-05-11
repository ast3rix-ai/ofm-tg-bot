from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

Direction = Literal["in", "out"]

_SCHEMA = """
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


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path) -> None:
    """Create tables and indexes, enable WAL. Idempotent.

    Args:
        db_path: Filesystem path for the SQLite database.
    """
    with _connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.executescript(_SCHEMA)


def upsert_contact(
    db_path: Path,
    chat_id: int,
    tg_user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> None:
    """Insert or update a contact row.

    Profile fields and `last_seen_at` are refreshed every call;
    `first_seen_at` is preserved on update.
    """
    now = _utcnow_iso()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO contacts (
                chat_id, tg_user_id, username, first_name, last_name,
                first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                tg_user_id   = excluded.tg_user_id,
                username     = excluded.username,
                first_name   = excluded.first_name,
                last_name    = excluded.last_name,
                last_seen_at = excluded.last_seen_at
            """,
            (chat_id, tg_user_id, username, first_name, last_name, now, now),
        )


def insert_message(
    db_path: Path,
    *,
    chat_id: int,
    tg_message_id: int,
    direction: Direction,
    sender_id: int,
    text: str | None,
    media_type: str | None,
    raw_json: str,
) -> bool:
    """Insert a message row. Returns False if it already exists.

    Idempotency is enforced by the UNIQUE(chat_id, tg_message_id, direction)
    constraint, so re-processing the same Telethon event is safe.
    """
    now = _utcnow_iso()
    with _connect(db_path) as conn:
        try:
            conn.execute(
                """
                INSERT INTO messages (
                    chat_id, tg_message_id, direction, sender_id,
                    text, media_type, raw_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    tg_message_id,
                    direction,
                    sender_id,
                    text,
                    media_type,
                    raw_json,
                    now,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def log_event(db_path: Path, event_type: str, payload: dict[str, Any]) -> None:
    """Persist an operational event (heartbeat, reconnect, error, etc.)."""
    now = _utcnow_iso()
    payload_json = json.dumps(payload, default=str, ensure_ascii=False)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO events (event_type, payload_json, created_at) VALUES (?, ?, ?)",
            (event_type, payload_json, now),
        )


def get_recent_messages(
    db_path: Path, chat_id: int, limit: int = 30
) -> list[dict[str, Any]]:
    """Return the `limit` most recent messages for `chat_id`, oldest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, chat_id, tg_message_id, direction, sender_id,
                   text, media_type, created_at
            FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_all_contacts(db_path: Path) -> list[dict[str, Any]]:
    """Return all contacts with message counts, most recently seen first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT c.chat_id, c.tg_user_id, c.username, c.first_name, c.last_name,
                   c.first_seen_at, c.last_seen_at,
                   COALESCE(m.msg_count, 0) AS message_count
            FROM contacts c
            LEFT JOIN (
                SELECT chat_id, COUNT(*) AS msg_count FROM messages GROUP BY chat_id
            ) m ON m.chat_id = c.chat_id
            ORDER BY c.last_seen_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_events(db_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    """Return the `limit` most recent events, newest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, event_type, payload_json, created_at
            FROM events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
