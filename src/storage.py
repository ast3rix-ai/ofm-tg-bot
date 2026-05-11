from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict, Unpack

from loguru import logger

from src.migrations import MIGRATIONS, SCHEMA_MIGRATIONS_DDL

Direction = Literal["in", "out"]

_log = logger.bind(module=__name__)


class ContactStateFields(TypedDict, total=False):
    """Whitelisted, optional fields for `upsert_contact_state`.

    `flags` and `classifier_metadata` may be passed as a dict; they are
    JSON-serialized before insertion. All other fields are written verbatim.
    """

    category: str | None
    funnel_stage: str | None
    flags: dict[str, Any]
    last_classified_at: str | None
    last_classifier_confidence: float | None
    classifier_metadata: dict[str, Any]
    human_active: int
    human_active_until: str | None


class ContactMemoryFields(TypedDict, total=False):
    """Whitelisted, optional fields for `upsert_contact_memory`."""

    facts: dict[str, Any]
    summary: str
    summary_message_count: int
    last_summarized_at: str | None


_CONTACT_STATE_KEYS: frozenset[str] = frozenset(ContactStateFields.__annotations__)
_CONTACT_STATE_JSON_KEYS = frozenset({"flags", "classifier_metadata"})

_CONTACT_MEMORY_KEYS: frozenset[str] = frozenset(ContactMemoryFields.__annotations__)
_CONTACT_MEMORY_JSON_KEYS = frozenset({"facts"})


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path) -> None:
    """Initialize DB and apply all pending migrations. Idempotent.

    Steps:
        1. Open a connection, enable WAL + synchronous=NORMAL.
        2. Create `schema_migrations` if needed.
        3. For each Migration in version order: skip if already recorded,
           otherwise run inside an explicit transaction and record the row.

    Migration SQL is idempotent (`IF NOT EXISTS`), so a Phase 1 DB without
    `schema_migrations` rows is upgraded safely on first run.
    """
    with _connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.executescript(SCHEMA_MIGRATIONS_DDL)

        applied: set[int] = {
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }

        for migration in sorted(MIGRATIONS, key=lambda m: m.version):
            if migration.version in applied:
                _log.debug(
                    "Migration already applied",
                    version=migration.version,
                    name=migration.name,
                )
                continue
            # sqlite3.executescript() issues an implicit COMMIT before running,
            # so we can't wrap it in an explicit BEGIN/COMMIT in autocommit mode.
            # Idempotent DDL (`IF NOT EXISTS`) plus the `schema_migrations` row
            # being inserted only after the script succeeds keeps re-runs safe.
            conn.executescript(migration.sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at)"
                " VALUES (?, ?, ?)",
                (migration.version, migration.name, _utcnow_iso()),
            )
            _log.info(
                "Migration applied",
                version=migration.version,
                name=migration.name,
            )


def get_applied_migrations(db_path: Path) -> list[dict[str, Any]]:
    """Return all rows from `schema_migrations`, ascending by version."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT version, name, applied_at FROM schema_migrations"
            " ORDER BY version ASC"
        ).fetchall()
    return [dict(r) for r in rows]


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


def _normalize_partial(
    fields: dict[str, Any],
    allowed: frozenset[str],
    json_keys: frozenset[str],
    table: str,
) -> dict[str, Any]:
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(
            f"Unknown {table} fields: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    normalized: dict[str, Any] = {}
    for key, value in fields.items():
        if key in json_keys and isinstance(value, dict):
            normalized[key] = json.dumps(value, default=str, ensure_ascii=False)
        else:
            normalized[key] = value
    return normalized


def _build_upsert(
    table: str,
    chat_id: int,
    fields: dict[str, Any],
    now: str,
) -> tuple[str, list[Any]]:
    columns = ["chat_id", *fields.keys(), "updated_at"]
    placeholders = ", ".join("?" * len(columns))
    values: list[Any] = [chat_id, *fields.values(), now]
    update_pairs = [f"{k} = excluded.{k}" for k in (*fields.keys(), "updated_at")]
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(chat_id) DO UPDATE SET {', '.join(update_pairs)}"
    )
    return sql, values


def upsert_contact_state(
    db_path: Path,
    chat_id: int,
    **fields: Unpack[ContactStateFields],
) -> None:
    """Insert or partially-update `contact_state` for `chat_id`.

    Only the provided fields are written; existing column values are
    preserved. `updated_at` is set unconditionally to the current UTC time.
    Dict-valued `flags` / `classifier_metadata` are JSON-serialized.

    Raises:
        ValueError: If `fields` contains an unknown key.
        sqlite3.IntegrityError: If `chat_id` does not exist in `contacts`.
    """
    normalized = _normalize_partial(
        dict(fields), _CONTACT_STATE_KEYS, _CONTACT_STATE_JSON_KEYS, "contact_state"
    )
    now = _utcnow_iso()
    sql, values = _build_upsert("contact_state", chat_id, normalized, now)
    with _connect(db_path) as conn:
        conn.execute(sql, values)


def get_contact_state(db_path: Path, chat_id: int) -> dict[str, Any] | None:
    """Return the `contact_state` row for `chat_id`, or None.

    JSON columns (`flags`, `classifier_metadata`) are decoded into dicts.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM contact_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    if row is None:
        return None
    record = dict(row)
    for key in _CONTACT_STATE_JSON_KEYS:
        raw = record.get(key)
        if isinstance(raw, str):
            try:
                record[key] = json.loads(raw)
            except (TypeError, ValueError):
                record[key] = {}
    return record


def upsert_contact_memory(
    db_path: Path,
    chat_id: int,
    **fields: Unpack[ContactMemoryFields],
) -> None:
    """Insert or partially-update `contact_memory` for `chat_id`.

    Raises:
        ValueError: If `fields` contains an unknown key.
        sqlite3.IntegrityError: If `chat_id` does not exist in `contacts`.
    """
    normalized = _normalize_partial(
        dict(fields), _CONTACT_MEMORY_KEYS, _CONTACT_MEMORY_JSON_KEYS, "contact_memory"
    )
    now = _utcnow_iso()
    sql, values = _build_upsert("contact_memory", chat_id, normalized, now)
    with _connect(db_path) as conn:
        conn.execute(sql, values)


def get_contact_memory(db_path: Path, chat_id: int) -> dict[str, Any] | None:
    """Return the `contact_memory` row for `chat_id`, or None.

    The JSON `facts` column is decoded into a dict.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM contact_memory WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    if row is None:
        return None
    record = dict(row)
    raw = record.get("facts")
    if isinstance(raw, str):
        try:
            record["facts"] = json.loads(raw)
        except (TypeError, ValueError):
            record["facts"] = {}
    return record
