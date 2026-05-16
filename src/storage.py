from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict, Unpack

from loguru import logger

from src.migrations import (
    MIGRATIONS,
    SCHEMA_MIGRATIONS_DDL,
    MigrationContext,
)

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
    bot_enabled: int
    bootstrap_completed_at: str | None
    last_resurface_at: str | None


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


def init_db(
    db_path: Path, migration_context: MigrationContext | None = None
) -> None:
    """Initialize DB and apply all pending migrations. Idempotent.

    Args:
        db_path: SQLite database location.
        migration_context: Optional dict passed into Python-backed
            migrations (see `migrations.MigrationContext`). Required for
            migration 003 to backfill legacy `.env` credentials.

    Migration SQL is idempotent (`IF NOT EXISTS`), and Python migrations
    are expected to no-op when their precondition is not met, so re-running
    `init_db` on an up-to-date database is safe.
    """
    context: MigrationContext = migration_context or {}
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
            # so explicit BEGIN/COMMIT around it errors. Idempotent DDL plus
            # inserting the `schema_migrations` row only after success keeps
            # re-runs safe even if a migration fails midway.
            if migration.apply is not None:
                migration.apply(conn, context)
            else:
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
    *,
    account_id: int,
    chat_id: int,
    tg_user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> None:
    """Insert or update a contact row scoped to `account_id`.

    Profile fields and `last_seen_at` are refreshed every call;
    `first_seen_at` is preserved on update. The primary key is the
    composite `(account_id, chat_id)`.
    """
    now = _utcnow_iso()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO contacts (
                account_id, chat_id, tg_user_id, username, first_name, last_name,
                first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, chat_id) DO UPDATE SET
                tg_user_id   = excluded.tg_user_id,
                username     = excluded.username,
                first_name   = excluded.first_name,
                last_name    = excluded.last_name,
                last_seen_at = excluded.last_seen_at
            """,
            (
                account_id, chat_id, tg_user_id, username, first_name,
                last_name, now, now,
            ),
        )


def insert_message(
    db_path: Path,
    *,
    account_id: int,
    chat_id: int,
    tg_message_id: int,
    direction: Direction,
    sender_id: int,
    text: str | None,
    media_type: str | None,
    raw_json: str,
) -> bool:
    """Insert a message row. Returns False if it already exists.

    Idempotency is enforced by `UNIQUE(chat_id, tg_message_id, direction)`,
    so re-processing the same Telethon event is safe.
    """
    now = _utcnow_iso()
    with _connect(db_path) as conn:
        try:
            conn.execute(
                """
                INSERT INTO messages (
                    account_id, chat_id, tg_message_id, direction, sender_id,
                    text, media_type, raw_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
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


def log_event(
    db_path: Path,
    event_type: str,
    payload: dict[str, Any],
    account_id: int | None = None,
) -> None:
    """Persist an operational event. `account_id` may be None for system-wide events."""
    now = _utcnow_iso()
    payload_json = json.dumps(payload, default=str, ensure_ascii=False)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO events (event_type, payload_json, created_at, account_id)"
            " VALUES (?, ?, ?, ?)",
            (event_type, payload_json, now, account_id),
        )


def get_recent_messages(
    db_path: Path,
    account_id: int,
    chat_id: int,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Return the `limit` most recent messages for `(account_id, chat_id)`, oldest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, account_id, chat_id, tg_message_id, direction, sender_id,
                   text, media_type, created_at
            FROM messages
            WHERE account_id = ? AND chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (account_id, chat_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_all_contacts(
    db_path: Path, account_id: int | None = None
) -> list[dict[str, Any]]:
    """Return contacts with message counts, most recently seen first.

    If `account_id` is None, returns contacts across all accounts (used by
    inspection tooling). Production callers should always scope by account.
    """
    base_sql = """
        SELECT c.account_id, c.chat_id, c.tg_user_id, c.username,
               c.first_name, c.last_name,
               c.first_seen_at, c.last_seen_at,
               COALESCE(m.msg_count, 0) AS message_count,
               cs.category AS category,
               cs.human_active AS human_active
        FROM contacts c
        LEFT JOIN (
            SELECT account_id, chat_id, COUNT(*) AS msg_count
            FROM messages GROUP BY account_id, chat_id
        ) m ON m.account_id = c.account_id AND m.chat_id = c.chat_id
        LEFT JOIN contact_state cs
            ON cs.account_id = c.account_id AND cs.chat_id = c.chat_id
    """
    params: tuple[Any, ...]
    if account_id is None:
        sql = base_sql + " ORDER BY c.last_seen_at DESC"
        params = ()
    else:
        sql = base_sql + " WHERE c.account_id = ? ORDER BY c.last_seen_at DESC"
        params = (account_id,)
    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_recent_events(
    db_path: Path,
    limit: int = 50,
    account_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return the `limit` most recent events, newest first.

    If `account_id` is provided, only that account's events (plus
    system-wide events with `account_id IS NULL`) are returned.
    """
    with _connect(db_path) as conn:
        if account_id is None:
            rows = conn.execute(
                """
                SELECT id, event_type, payload_json, created_at, account_id
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, event_type, payload_json, created_at, account_id
                FROM events
                WHERE account_id = ? OR account_id IS NULL
                ORDER BY id DESC
                LIMIT ?
                """,
                (account_id, limit),
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


def _build_account_chat_upsert(
    table: str,
    account_id: int,
    chat_id: int,
    fields: dict[str, Any],
    now: str,
) -> tuple[str, list[Any]]:
    columns = ["account_id", "chat_id", *fields.keys(), "updated_at"]
    placeholders = ", ".join("?" * len(columns))
    values: list[Any] = [account_id, chat_id, *fields.values(), now]
    update_pairs = [f"{k} = excluded.{k}" for k in (*fields.keys(), "updated_at")]
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(account_id, chat_id) DO UPDATE SET {', '.join(update_pairs)}"
    )
    return sql, values


def upsert_contact_state(
    db_path: Path,
    *,
    account_id: int,
    chat_id: int,
    **fields: Unpack[ContactStateFields],
) -> None:
    """Insert or partially-update `contact_state` for `(account_id, chat_id)`.

    Only the provided fields are written; existing column values are
    preserved. `updated_at` is set unconditionally. Dict-valued `flags`
    and `classifier_metadata` are JSON-serialized.

    Raises:
        ValueError: If `fields` contains an unknown key.
        sqlite3.IntegrityError: If `(account_id, chat_id)` does not exist
            in `contacts`.
    """
    normalized = _normalize_partial(
        dict(fields), _CONTACT_STATE_KEYS, _CONTACT_STATE_JSON_KEYS, "contact_state"
    )
    now = _utcnow_iso()
    sql, values = _build_account_chat_upsert(
        "contact_state", account_id, chat_id, normalized, now
    )
    with _connect(db_path) as conn:
        conn.execute(sql, values)


def get_contact_state(
    db_path: Path, account_id: int, chat_id: int
) -> dict[str, Any] | None:
    """Return the `contact_state` row, JSON-decoded, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM contact_state WHERE account_id = ? AND chat_id = ?",
            (account_id, chat_id),
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
    *,
    account_id: int,
    chat_id: int,
    **fields: Unpack[ContactMemoryFields],
) -> None:
    """Insert or partially-update `contact_memory` for `(account_id, chat_id)`."""
    normalized = _normalize_partial(
        dict(fields), _CONTACT_MEMORY_KEYS, _CONTACT_MEMORY_JSON_KEYS, "contact_memory"
    )
    now = _utcnow_iso()
    sql, values = _build_account_chat_upsert(
        "contact_memory", account_id, chat_id, normalized, now
    )
    with _connect(db_path) as conn:
        conn.execute(sql, values)


def get_last_inbound_at(
    db_path: Path, account_id: int, chat_id: int
) -> str | None:
    """Return the `created_at` of the most recent inbound message, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT created_at FROM messages"
            " WHERE account_id = ? AND chat_id = ? AND direction = 'in'"
            " ORDER BY id DESC LIMIT 1",
            (account_id, chat_id),
        ).fetchone()
    return None if row is None else str(row[0])


def insert_classifier_run(
    db_path: Path,
    *,
    account_id: int,
    chat_id: int,
    triggered_by: str,
    input_message_count: int,
    category_before: str | None,
    category_after: str | None,
    confidence: float | None,
    flags_before: dict[str, Any] | None,
    flags_after: dict[str, Any] | None,
    raw_llm_output: str | None,
    latency_ms: int | None,
) -> int:
    now = _utcnow_iso()
    flags_before_json = (
        json.dumps(flags_before, default=str, ensure_ascii=False)
        if flags_before is not None else None
    )
    flags_after_json = (
        json.dumps(flags_after, default=str, ensure_ascii=False)
        if flags_after is not None else None
    )
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO classifier_runs (
                account_id, chat_id, triggered_by, input_message_count,
                category_before, category_after, confidence,
                flags_before, flags_after, raw_llm_output, latency_ms,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, chat_id, triggered_by, input_message_count,
                category_before, category_after, confidence,
                flags_before_json, flags_after_json, raw_llm_output,
                latency_ms, now,
            ),
        )
    return int(cur.lastrowid or 0)


def get_classifier_runs(
    db_path: Path, *, account_id: int, chat_id: int, limit: int = 10
) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM classifier_runs"
            " WHERE account_id = ? AND chat_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (account_id, chat_id, limit),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for k in ("flags_before", "flags_after"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except (TypeError, ValueError):
                    pass
        out.append(d)
    return out


def insert_operator_alert(
    db_path: Path,
    *,
    account_id: int,
    chat_id: int | None,
    alert_type: str,
    severity: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> int:
    now = _utcnow_iso()
    payload_json = (
        json.dumps(payload, default=str, ensure_ascii=False)
        if payload is not None else None
    )
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO operator_alerts (
                account_id, chat_id, alert_type, severity, message,
                payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, chat_id, alert_type, severity, message,
                payload_json, now,
            ),
        )
    return int(cur.lastrowid or 0)


def acknowledge_operator_alert(db_path: Path, alert_id: int) -> None:
    now = _utcnow_iso()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE operator_alerts SET acknowledged = 1, acknowledged_at = ?"
            " WHERE id = ?",
            (now, alert_id),
        )


def list_operator_alerts(
    db_path: Path,
    *,
    account_id: int | None = None,
    only_unacknowledged: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if account_id is not None:
        where.append("account_id = ?")
        params.append(account_id)
    if only_unacknowledged:
        where.append("acknowledged = 0")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    sql = (
        "SELECT * FROM operator_alerts"
        + where_sql
        + " ORDER BY id DESC LIMIT ?"
    )
    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        raw = d.get("payload")
        if isinstance(raw, str):
            try:
                d["payload"] = json.loads(raw)
            except (TypeError, ValueError):
                pass
        out.append(d)
    return out


def count_recent_alerts(
    db_path: Path,
    *,
    account_id: int,
    chat_id: int | None,
    alert_type: str,
    since_iso: str,
) -> int:
    where = "account_id = ? AND alert_type = ? AND created_at >= ?"
    params: list[Any] = [account_id, alert_type, since_iso]
    if chat_id is None:
        where += " AND chat_id IS NULL"
    else:
        where += " AND chat_id = ?"
        params.append(chat_id)
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM operator_alerts WHERE {where}", params
        ).fetchone()
    return int(row[0])


def chats_needing_bootstrap(
    db_path: Path, account_id: int
) -> list[int]:
    """Return chat_ids that have messages but no bootstrap_completed_at marker."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT c.chat_id
            FROM contacts c
            LEFT JOIN contact_state s
              ON s.account_id = c.account_id AND s.chat_id = c.chat_id
            WHERE c.account_id = ?
              AND (s.bootstrap_completed_at IS NULL)
            """,
            (account_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def get_contact_memory(
    db_path: Path, account_id: int, chat_id: int
) -> dict[str, Any] | None:
    """Return the `contact_memory` row, JSON-decoded, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM contact_memory WHERE account_id = ? AND chat_id = ?",
            (account_id, chat_id),
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


def delete_contact_state(db_path: Path, account_id: int, chat_id: int) -> None:
    """Delete the `contact_state` row for `(account_id, chat_id)` if present."""
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM contact_state WHERE account_id = ? AND chat_id = ?",
            (account_id, chat_id),
        )


def delete_contact_memory(db_path: Path, account_id: int, chat_id: int) -> None:
    """Delete the `contact_memory` row for `(account_id, chat_id)` if present."""
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM contact_memory WHERE account_id = ? AND chat_id = ?",
            (account_id, chat_id),
        )


def get_message_db_id(
    db_path: Path,
    *,
    account_id: int,
    chat_id: int,
    tg_message_id: int,
    direction: Direction,
) -> int | None:
    """Return the `messages.id` primary key for a Telegram message, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM messages"
            " WHERE account_id = ? AND chat_id = ? AND tg_message_id = ?"
            " AND direction = ?",
            (account_id, chat_id, tg_message_id, direction),
        ).fetchone()
    return None if row is None else int(row[0])


def insert_response_run(
    db_path: Path,
    *,
    account_id: int,
    chat_id: int,
    triggered_by_message_id: int | None,
    persona_version: str,
    attempts: int,
    outcome: str,
    gate_reason: str | None,
    raw_attempts: list[Any],
    final_text: str | None,
    latency_ms: int,
) -> int:
    """Insert a `response_runs` audit row. Returns the new row id."""
    now = _utcnow_iso()
    raw_json = json.dumps(raw_attempts, default=str, ensure_ascii=False)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO response_runs (
                account_id, chat_id, triggered_by_message_id, persona_version,
                attempts, outcome, gate_reason, raw_attempts, final_text,
                latency_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, chat_id, triggered_by_message_id, persona_version,
                attempts, outcome, gate_reason, raw_json, final_text,
                latency_ms, now,
            ),
        )
    return int(cur.lastrowid or 0)


def insert_bot_sent_message(
    db_path: Path,
    *,
    account_id: int,
    chat_id: int,
    tg_message_id: int,
    response_run_id: int | None,
) -> None:
    """Tag an outbound message as bot-sent. Idempotent on the unique key."""
    now = _utcnow_iso()
    with _connect(db_path) as conn:
        try:
            conn.execute(
                """
                INSERT INTO bot_sent_messages (
                    account_id, chat_id, tg_message_id, response_run_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (account_id, chat_id, tg_message_id, response_run_id, now),
            )
        except sqlite3.IntegrityError:
            pass


def get_response_runs(
    db_path: Path, *, account_id: int, chat_id: int, limit: int = 10
) -> list[dict[str, Any]]:
    """Return the `limit` most recent `response_runs` rows, newest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM response_runs"
            " WHERE account_id = ? AND chat_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (account_id, chat_id, limit),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        raw = d.get("raw_attempts")
        if isinstance(raw, str):
            try:
                d["raw_attempts"] = json.loads(raw)
            except (TypeError, ValueError):
                d["raw_attempts"] = []
        out.append(d)
    return out


def get_bot_sent_tg_message_ids(
    db_path: Path, account_id: int, chat_id: int
) -> set[int]:
    """Return the set of `tg_message_id`s the bot sent in this chat."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT tg_message_id FROM bot_sent_messages"
            " WHERE account_id = ? AND chat_id = ?",
            (account_id, chat_id),
        ).fetchall()
    return {int(r[0]) for r in rows}


def get_last_response_run_at(db_path: Path, account_id: int) -> str | None:
    """Return the `created_at` of the most recent response run, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT created_at FROM response_runs"
            " WHERE account_id = ? ORDER BY id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
    return None if row is None else str(row[0])


def count_response_runs_by_outcome(
    db_path: Path, *, account_id: int, outcome: str, since_iso: str
) -> int:
    """Count response runs with `outcome`, created at or after `since_iso`."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM response_runs"
            " WHERE account_id = ? AND outcome = ? AND created_at >= ?",
            (account_id, outcome, since_iso),
        ).fetchone()
    return int(row[0])
