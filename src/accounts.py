from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

_log = logger.bind(module=__name__)


class AccountsError(RuntimeError):
    """Raised on account-layer errors (encryption, duplicate label, etc.)."""


@dataclass(frozen=True)
class Account:
    """An operated Telegram account.

    The encrypted-only credential fields (`api_id`, `api_hash`, `phone`) are
    populated by `get_account()` for activation flows. List-style endpoints
    leave them as `None` to avoid decrypting on every page render.
    """

    id: int
    label: str
    is_active: bool
    tg_user_id: int | None
    tg_username: str | None
    created_at: str
    last_connected_at: str | None
    has_session: bool
    api_id: int | None = None
    api_hash: str | None = None
    phone: str | None = None


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _fernet(key: str) -> Fernet:
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise AccountsError(f"Invalid encryption key: {exc}") from exc


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _encrypt_text(f: Fernet, plaintext: str) -> str:
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def _decrypt_text(f: Fernet, ciphertext: str) -> str:
    try:
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise AccountsError(
            "Failed to decrypt account credential — wrong encryption key."
        ) from exc


def insert_account_row(
    conn: sqlite3.Connection,
    *,
    key: str,
    label: str,
    api_id: int,
    api_hash: str,
    phone: str,
    session_string: str | None,
    is_active: bool,
) -> int:
    """Insert a new accounts row using an existing connection.

    This is the migration-time helper. Production callers should use
    `create_account()` which opens its own connection.
    """
    f = _fernet(key)
    if is_active:
        conn.execute("UPDATE accounts SET is_active = 0")
    cur = conn.execute(
        """
        INSERT INTO accounts (
            label, tg_api_id_enc, tg_api_hash_enc, tg_phone_enc,
            session_blob_enc, is_active, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            label,
            _encrypt_text(f, str(api_id)),
            _encrypt_text(f, api_hash),
            _encrypt_text(f, phone),
            _encrypt_text(f, session_string) if session_string else None,
            1 if is_active else 0,
            _utcnow_iso(),
        ),
    )
    account_id = int(cur.lastrowid or 0)
    _log.info(
        "Account inserted",
        account_id=account_id,
        label=label,
        is_active=is_active,
        has_session=session_string is not None,
    )
    return account_id


def create_account(
    db_path: Path,
    key: str,
    *,
    label: str,
    api_id: int,
    api_hash: str,
    phone: str,
) -> int:
    """Create a new account with encrypted credentials, return its id.

    Raises:
        AccountsError: If `label` is already in use.
    """
    with _connect(db_path) as conn:
        try:
            return insert_account_row(
                conn,
                key=key,
                label=label,
                api_id=api_id,
                api_hash=api_hash,
                phone=phone,
                session_string=None,
                is_active=False,
            )
        except sqlite3.IntegrityError as exc:
            raise AccountsError(f"Account label already exists: {label!r}") from exc


def _row_to_account(row: sqlite3.Row, with_creds: tuple[Fernet, ...] | None) -> Account:
    api_id_dec: int | None = None
    api_hash_dec: str | None = None
    phone_dec: str | None = None
    if with_creds is not None:
        f = with_creds[0]
        api_id_dec = int(_decrypt_text(f, row["tg_api_id_enc"]))
        api_hash_dec = _decrypt_text(f, row["tg_api_hash_enc"])
        phone_dec = _decrypt_text(f, row["tg_phone_enc"])
    return Account(
        id=int(row["id"]),
        label=str(row["label"]),
        is_active=bool(row["is_active"]),
        tg_user_id=row["tg_user_id"],
        tg_username=row["tg_username"],
        created_at=str(row["created_at"]),
        last_connected_at=row["last_connected_at"],
        has_session=row["session_blob_enc"] is not None,
        api_id=api_id_dec,
        api_hash=api_hash_dec,
        phone=phone_dec,
    )


def list_accounts(db_path: Path) -> list[Account]:
    """Return all accounts, most recently created first.

    Credential fields are not decrypted by this call.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM accounts ORDER BY id ASC"
        ).fetchall()
    return [_row_to_account(r, None) for r in rows]


def get_account(
    db_path: Path, key: str, account_id: int, *, with_credentials: bool = False
) -> Account | None:
    """Fetch one account. Set `with_credentials=True` to decrypt creds."""
    creds: tuple[Fernet, ...] | None = (_fernet(key),) if with_credentials else None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_account(row, creds)


def set_active_account(db_path: Path, account_id: int) -> None:
    """Mark exactly one account active. Other rows have `is_active = 0`.

    Wrapped in a transaction so the invariant "at most one active row"
    holds even under concurrent calls (which shouldn't happen — the bot
    process is single-threaded — but it's cheap to be correct).
    """
    with _connect(db_path) as conn:
        conn.execute("BEGIN")
        try:
            conn.execute("UPDATE accounts SET is_active = 0")
            updated = conn.execute(
                "UPDATE accounts SET is_active = 1 WHERE id = ?", (account_id,)
            )
            if updated.rowcount == 0:
                raise AccountsError(f"No account with id {account_id}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def clear_active_account(db_path: Path) -> None:
    """Set `is_active = 0` on all accounts."""
    with _connect(db_path) as conn:
        conn.execute("UPDATE accounts SET is_active = 0")


def get_active_account(db_path: Path, key: str) -> Account | None:
    """Return the active account with decrypted credentials, or None."""
    f = _fernet(key)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE is_active = 1 LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return _row_to_account(row, (f,))


def update_session_blob(
    db_path: Path, key: str, account_id: int, session_string: str
) -> None:
    """Encrypt and persist the Telethon `StringSession` for `account_id`."""
    f = _fernet(key)
    enc = _encrypt_text(f, session_string)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE accounts SET session_blob_enc = ? WHERE id = ?",
            (enc, account_id),
        )


def read_session_blob(
    db_path: Path, key: str, account_id: int
) -> str | None:
    """Return the decrypted Telethon `StringSession`, or None if absent."""
    f = _fernet(key)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT session_blob_enc FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return _decrypt_text(f, row[0])


def update_account_metadata(
    db_path: Path,
    account_id: int,
    *,
    tg_user_id: int | None = None,
    tg_username: str | None = None,
    last_connected_at: str | None = None,
) -> None:
    """Update post-auth metadata. Only provided fields are written."""
    sets: list[str] = []
    params: list[object] = []
    if tg_user_id is not None:
        sets.append("tg_user_id = ?")
        params.append(tg_user_id)
    if tg_username is not None:
        sets.append("tg_username = ?")
        params.append(tg_username)
    if last_connected_at is not None:
        sets.append("last_connected_at = ?")
        params.append(last_connected_at)
    if not sets:
        return
    params.append(account_id)
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?",
            params,
        )


def delete_account(db_path: Path, account_id: int) -> None:
    """Delete an account row. CASCADEs to contacts, messages, state, memory.

    Events with this account_id retain it (FK is nullable but without CASCADE);
    callers can decide whether to also delete those.
    """
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
