from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Runtime configuration loaded from environment variables.

    Attributes:
        tg_api_id: Telegram API ID from my.telegram.org.
        tg_api_hash: Telegram API hash from my.telegram.org.
        tg_phone: Phone number of the operated account in E.164 format.
        session_encryption_key: Fernet key for session-at-rest encryption.
        notifier_bot_token: Token of the separate regular bot used for alerts.
        notifier_chat_id: Chat ID that receives operator alerts.
        heartbeat_interval_seconds: Watchdog heartbeat cadence in seconds.
        log_level: Console log level (DEBUG / INFO / WARNING / ERROR).
        project_root: Repository root directory.
        data_dir: Directory for persistent data (DB, encrypted session).
        logs_dir: Directory for log files.
        session_tmp_dir: Directory for the decrypted session at runtime.
        db_path: Absolute path of the SQLite database file.
        encrypted_session_path: Absolute path of the encrypted session blob.
        decrypted_session_path: Absolute path of the decrypted session file.
    """

    tg_api_id: int
    tg_api_hash: str
    tg_phone: str
    session_encryption_key: str
    notifier_bot_token: str
    notifier_chat_id: int
    heartbeat_interval_seconds: int
    log_level: str
    project_root: Path
    data_dir: Path
    logs_dir: Path
    session_tmp_dir: Path
    db_path: Path
    encrypted_session_path: Path
    decrypted_session_path: Path


_REQUIRED_STRING_KEYS = (
    "TG_API_HASH",
    "TG_PHONE",
    "SESSION_ENCRYPTION_KEY",
    "NOTIFIER_BOT_TOKEN",
)

_REQUIRED_INT_KEYS = (
    "TG_API_ID",
    "NOTIFIER_CHAT_ID",
)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_config(env_file: Path | None = None) -> Config:
    """Load and validate configuration from a `.env` file and the environment.

    Args:
        env_file: Optional explicit path to a `.env` file. Defaults to
            `<project_root>/.env`.

    Returns:
        A fully populated, frozen `Config` instance.

    Raises:
        ConfigError: If any required key is missing or unparseable.
    """
    root = _project_root()
    path = env_file if env_file is not None else root / ".env"
    load_dotenv(path, override=False)

    missing: list[str] = []
    invalid: list[str] = []

    string_values: dict[str, str] = {}
    for key in _REQUIRED_STRING_KEYS:
        raw = os.environ.get(key, "").strip()
        if not raw:
            missing.append(key)
        else:
            string_values[key] = raw

    int_values: dict[str, int] = {}
    for key in _REQUIRED_INT_KEYS:
        raw = os.environ.get(key, "").strip()
        if not raw:
            missing.append(key)
            continue
        try:
            int_values[key] = int(raw)
        except ValueError:
            invalid.append(f"{key} (not an integer: {raw!r})")

    heartbeat_raw = os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "60").strip() or "60"
    try:
        heartbeat = int(heartbeat_raw)
    except ValueError:
        invalid.append(f"HEARTBEAT_INTERVAL_SECONDS (not an integer: {heartbeat_raw!r})")
        heartbeat = 60

    log_level = (os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO").upper()

    if missing or invalid:
        parts: list[str] = []
        if missing:
            parts.append("missing: " + ", ".join(sorted(missing)))
        if invalid:
            parts.append("invalid: " + ", ".join(sorted(invalid)))
        raise ConfigError(
            "Configuration error — " + "; ".join(parts)
            + f". Populate {path} (see .env.example)."
        )

    data_dir = root / "data"
    logs_dir = root / "logs"
    session_tmp_dir = data_dir / ".session_tmp"

    return Config(
        tg_api_id=int_values["TG_API_ID"],
        tg_api_hash=string_values["TG_API_HASH"],
        tg_phone=string_values["TG_PHONE"],
        session_encryption_key=string_values["SESSION_ENCRYPTION_KEY"],
        notifier_bot_token=string_values["NOTIFIER_BOT_TOKEN"],
        notifier_chat_id=int_values["NOTIFIER_CHAT_ID"],
        heartbeat_interval_seconds=heartbeat,
        log_level=log_level,
        project_root=root,
        data_dir=data_dir,
        logs_dir=logs_dir,
        session_tmp_dir=session_tmp_dir,
        db_path=data_dir / "bot.db",
        encrypted_session_path=data_dir / "session.enc",
        decrypted_session_path=session_tmp_dir / "userbot.session",
    )
