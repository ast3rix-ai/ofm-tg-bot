from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

MigrationContext = dict[str, Any]
"""Free-form dict passed by the runner into Python-backed migrations.

Conventional keys used by current migrations:

- `encryption_key`: Fernet key for credential encryption.
- `default_label`: label to use when seeding the default account.
- `default_api_id`, `default_api_hash`, `default_phone`: legacy `.env`
  credentials to backfill into the default account.
- `legacy_session_path`: path to the Phase 1 encrypted SQLite session blob.
"""


@dataclass(frozen=True)
class Migration:
    """A single forward-only schema/data migration.

    Either `sql` is a non-empty DDL script that is run via
    `sqlite3.executescript`, or `apply` is a Python callable that performs
    arbitrary work using a sqlite3 connection (used when a migration needs
    to read application context, e.g. to backfill encrypted credentials).
    Migrations must be idempotent — both forms.
    """

    version: int
    name: str
    sql: str = ""
    apply: Callable[[sqlite3.Connection, MigrationContext], None] | None = field(
        default=None
    )


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


def _apply_migration_003(
    conn: sqlite3.Connection, context: MigrationContext
) -> None:
    """Multi-account migration.

    Steps (each idempotent):
      1. Create `accounts` table.
      2. If accounts is empty and `.env` provided legacy creds, seed a
         "Default" account row encrypted with `encryption_key`. If a Phase 1
         `data/session.enc` file exists, attempt to convert its SQLite
         session contents into a Telethon `StringSession` and store as
         `session_blob_enc`; on conversion failure, leave session null and
         require re-auth via the UI.
      3. Add `account_id` columns to messages (NOT NULL DEFAULT 1) and
         events (nullable).
      4. Rebuild `contacts`, `contact_state`, `contact_memory` with
         composite primary key `(account_id, chat_id)` and FK to accounts.
      5. Recreate the indexes from migrations 001/002 plus the new
         per-account indexes.
    """
    # Local imports to avoid cycles at module-load time.
    from src import accounts as accounts_mod

    cur = conn.cursor()

    # ---- Step 1: accounts table.
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            label               TEXT NOT NULL UNIQUE,
            tg_api_id_enc       TEXT NOT NULL,
            tg_api_hash_enc     TEXT NOT NULL,
            tg_phone_enc        TEXT NOT NULL,
            session_blob_enc    TEXT,
            tg_user_id          INTEGER,
            tg_username         TEXT,
            is_active           INTEGER NOT NULL DEFAULT 0,
            created_at          TEXT NOT NULL,
            last_connected_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active);
        """
    )

    # ---- Step 2: backfill default account from context if accounts empty.
    row_count = cur.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    if row_count == 0:
        encryption_key = context.get("encryption_key")
        api_id = context.get("default_api_id")
        api_hash = context.get("default_api_hash")
        phone = context.get("default_phone")
        if encryption_key and api_id and api_hash and phone:
            label = context.get("default_label") or "Default"
            session_string = _maybe_convert_legacy_session(context)
            accounts_mod.insert_account_row(
                conn,
                key=encryption_key,
                label=label,
                api_id=int(api_id),
                api_hash=api_hash,
                phone=phone,
                session_string=session_string,
                is_active=True,
            )

    # ---- Step 3: add account_id to messages / events (idempotent).
    # SQLite refuses ADD COLUMN with a REFERENCES clause and a non-NULL
    # default while foreign keys are enabled. The connection is in
    # autocommit mode (isolation_level=None) so we can toggle the pragma
    # safely around the DDL.
    cur.execute("PRAGMA table_info(messages)")
    if "account_id" not in {r[1] for r in cur.fetchall()}:
        cur.execute("PRAGMA foreign_keys = OFF")
        try:
            cur.execute(
                "ALTER TABLE messages ADD COLUMN account_id INTEGER NOT NULL"
                " DEFAULT 1 REFERENCES accounts(id) ON DELETE CASCADE"
            )
        finally:
            cur.execute("PRAGMA foreign_keys = ON")
    cur.execute("PRAGMA table_info(events)")
    if "account_id" not in {r[1] for r in cur.fetchall()}:
        cur.execute(
            "ALTER TABLE events ADD COLUMN account_id INTEGER"
            " REFERENCES accounts(id) ON DELETE SET NULL"
        )

    # ---- Step 4: rebuild contacts / contact_state / contact_memory with
    # composite PK. Idempotent: if account_id is already a column, skip.
    cur.execute("PRAGMA table_info(contacts)")
    contact_cols = {r[1] for r in cur.fetchall()}
    if "account_id" not in contact_cols:
        cur.executescript(
            """
            CREATE TABLE contacts_new (
                account_id    INTEGER NOT NULL,
                chat_id       INTEGER NOT NULL,
                tg_user_id    INTEGER NOT NULL,
                username      TEXT,
                first_name    TEXT,
                last_name     TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at  TEXT NOT NULL,
                PRIMARY KEY (account_id, chat_id),
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );
            INSERT INTO contacts_new (
                account_id, chat_id, tg_user_id, username, first_name,
                last_name, first_seen_at, last_seen_at
            )
            SELECT 1, chat_id, tg_user_id, username, first_name,
                   last_name, first_seen_at, last_seen_at
            FROM contacts;
            DROP TABLE contacts;
            ALTER TABLE contacts_new RENAME TO contacts;
            """
        )

    cur.execute("PRAGMA table_info(contact_state)")
    if "account_id" not in {r[1] for r in cur.fetchall()}:
        cur.executescript(
            """
            CREATE TABLE contact_state_new (
                account_id                 INTEGER NOT NULL,
                chat_id                    INTEGER NOT NULL,
                category                   TEXT,
                funnel_stage               TEXT,
                flags                      TEXT NOT NULL DEFAULT '{}',
                last_classified_at         TEXT,
                last_classifier_confidence REAL,
                classifier_metadata        TEXT NOT NULL DEFAULT '{}',
                human_active               INTEGER NOT NULL DEFAULT 0,
                human_active_until         TEXT,
                updated_at                 TEXT NOT NULL,
                PRIMARY KEY (account_id, chat_id),
                FOREIGN KEY (account_id, chat_id)
                    REFERENCES contacts(account_id, chat_id) ON DELETE CASCADE
            );
            INSERT INTO contact_state_new (
                account_id, chat_id, category, funnel_stage, flags,
                last_classified_at, last_classifier_confidence,
                classifier_metadata, human_active, human_active_until, updated_at
            )
            SELECT 1, chat_id, category, funnel_stage, flags,
                   last_classified_at, last_classifier_confidence,
                   classifier_metadata, human_active, human_active_until, updated_at
            FROM contact_state;
            DROP TABLE contact_state;
            ALTER TABLE contact_state_new RENAME TO contact_state;
            """
        )

    cur.execute("PRAGMA table_info(contact_memory)")
    if "account_id" not in {r[1] for r in cur.fetchall()}:
        cur.executescript(
            """
            CREATE TABLE contact_memory_new (
                account_id            INTEGER NOT NULL,
                chat_id               INTEGER NOT NULL,
                facts                 TEXT NOT NULL DEFAULT '{}',
                summary               TEXT NOT NULL DEFAULT '',
                summary_message_count INTEGER NOT NULL DEFAULT 0,
                last_summarized_at    TEXT,
                updated_at            TEXT NOT NULL,
                PRIMARY KEY (account_id, chat_id),
                FOREIGN KEY (account_id, chat_id)
                    REFERENCES contacts(account_id, chat_id) ON DELETE CASCADE
            );
            INSERT INTO contact_memory_new (
                account_id, chat_id, facts, summary, summary_message_count,
                last_summarized_at, updated_at
            )
            SELECT 1, chat_id, facts, summary, summary_message_count,
                   last_summarized_at, updated_at
            FROM contact_memory;
            DROP TABLE contact_memory;
            ALTER TABLE contact_memory_new RENAME TO contact_memory;
            """
        )

    # ---- Step 5: indexes.
    cur.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_contacts_account
            ON contacts (account_id);
        CREATE INDEX IF NOT EXISTS idx_messages_account_chat
            ON messages (account_id, chat_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_account_created
            ON messages (account_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_contact_state_category
            ON contact_state (category);
        CREATE INDEX IF NOT EXISTS idx_contact_state_human_active
            ON contact_state (human_active);
        """
    )


def _maybe_convert_legacy_session(context: MigrationContext) -> str | None:
    """Convert a Phase 1 encrypted SQLite session blob to a StringSession.

    Returns the StringSession serialized form on success, or None if the
    legacy file is absent or conversion fails.
    """
    from pathlib import Path

    from loguru import logger

    legacy_path = context.get("legacy_session_path")
    encryption_key = context.get("encryption_key")
    if not legacy_path or not encryption_key:
        return None
    legacy = Path(legacy_path)
    if not legacy.exists():
        return None

    log = logger.bind(module="src.migrations")

    try:
        import tempfile

        from telethon.sessions import SQLiteSession, StringSession

        from src.crypto import decrypt_file

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "userbot.session"
            decrypt_file(legacy, tmp, encryption_key)
            # SQLiteSession takes the filename without ".session" suffix.
            base = str(tmp.with_suffix(""))
            sqlite_session = SQLiteSession(base)
            string_session = StringSession()
            string_session.set_dc(
                sqlite_session.dc_id,
                sqlite_session.server_address,
                sqlite_session.port,
            )
            string_session.auth_key = sqlite_session.auth_key
            sqlite_session.close()
            saved: str = string_session.save()
            return saved
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Failed to convert legacy session — account will require"
            " re-authentication via the UI",
            error=str(exc),
        )
        return None


MIGRATIONS: list[Migration] = [
    Migration(version=1, name="initial_schema", sql=_MIGRATION_001),
    Migration(version=2, name="contact_state_and_memory", sql=_MIGRATION_002),
    Migration(
        version=3,
        name="multi_account",
        apply=_apply_migration_003,
    ),
]


SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""
