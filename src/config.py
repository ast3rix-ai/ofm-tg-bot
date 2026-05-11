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

    `tg_api_id`, `tg_api_hash`, and `tg_phone` are legacy fields used only
    to backfill the default account on first run after upgrading from
    Phase 2; from Phase 3 onward, credentials live encrypted inside the
    `accounts` table and are managed via the UI.
    """

    tg_api_id: int | None
    tg_api_hash: str | None
    tg_phone: str | None
    session_encryption_key: str
    notifier_bot_token: str | None
    notifier_chat_id: int | None
    heartbeat_interval_seconds: int
    log_level: str
    ui_port: int
    ui_auto_activate: bool
    ollama_host: str
    llm_model: str | None
    llm_timeout_seconds: int
    llm_max_retries: int
    classifier_confidence_threshold: float
    bootstrap_history_messages: int
    bootstrap_history_days: int
    bootstrap_max_concurrent: int
    backlog_max_concurrent: int
    resurface_dormant_days: int
    project_root: Path
    data_dir: Path
    logs_dir: Path
    db_path: Path
    legacy_encrypted_session_path: Path


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _parse_bool(raw: str, default: bool) -> bool:
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def load_config(env_file: Path | None = None) -> Config:
    """Load and validate configuration from a `.env` file and the environment.

    Only `SESSION_ENCRYPTION_KEY` is strictly required from Phase 3 onward.
    All other fields are optional and either default or are populated via
    the UI.
    """
    root = _project_root()
    path = env_file if env_file is not None else root / ".env"
    load_dotenv(path, override=False)

    missing: list[str] = []
    invalid: list[str] = []

    session_key = os.environ.get("SESSION_ENCRYPTION_KEY", "").strip()
    if not session_key:
        missing.append("SESSION_ENCRYPTION_KEY")

    api_id_raw = os.environ.get("TG_API_ID", "").strip()
    api_id: int | None = None
    if api_id_raw:
        try:
            api_id = int(api_id_raw)
        except ValueError:
            invalid.append(f"TG_API_ID (not an integer: {api_id_raw!r})")

    api_hash = os.environ.get("TG_API_HASH", "").strip() or None
    phone = os.environ.get("TG_PHONE", "").strip() or None

    notifier_token = os.environ.get("NOTIFIER_BOT_TOKEN", "").strip() or None

    notifier_chat_raw = os.environ.get("NOTIFIER_CHAT_ID", "").strip()
    notifier_chat: int | None
    if not notifier_chat_raw:
        notifier_chat = None
    else:
        try:
            notifier_chat = int(notifier_chat_raw)
        except ValueError:
            invalid.append(f"NOTIFIER_CHAT_ID (not an integer: {notifier_chat_raw!r})")
            notifier_chat = None

    heartbeat_raw = os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "60").strip() or "60"
    try:
        heartbeat = int(heartbeat_raw)
    except ValueError:
        invalid.append(f"HEARTBEAT_INTERVAL_SECONDS (not an integer: {heartbeat_raw!r})")
        heartbeat = 60

    log_level = (os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO").upper()

    ui_port_raw = os.environ.get("UI_PORT", "8765").strip() or "8765"
    try:
        ui_port = int(ui_port_raw)
    except ValueError:
        invalid.append(f"UI_PORT (not an integer: {ui_port_raw!r})")
        ui_port = 8765

    ui_auto_activate = _parse_bool(
        os.environ.get("UI_AUTO_ACTIVATE", ""), default=True
    )

    ollama_host = (
        os.environ.get("OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434"
    )
    llm_model = os.environ.get("LLM_MODEL", "").strip() or None

    def _opt_int(key: str, default: int) -> int:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            invalid.append(f"{key} (not an integer: {raw!r})")
            return default

    def _opt_float(key: str, default: float) -> float:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            invalid.append(f"{key} (not a number: {raw!r})")
            return default

    llm_timeout_seconds = _opt_int("LLM_TIMEOUT_SECONDS", 60)
    llm_max_retries = _opt_int("LLM_MAX_RETRIES", 2)
    classifier_threshold = _opt_float("CLASSIFIER_CONFIDENCE_THRESHOLD", 0.6)
    bootstrap_msgs = _opt_int("BOOTSTRAP_HISTORY_MESSAGES", 100)
    bootstrap_days = _opt_int("BOOTSTRAP_HISTORY_DAYS", 30)
    bootstrap_max_concurrent = _opt_int("BOOTSTRAP_MAX_CONCURRENT", 3)
    backlog_max_concurrent = _opt_int("BACKLOG_MAX_CONCURRENT", 5)
    resurface_dormant_days = _opt_int("RESURFACE_DORMANT_DAYS", 14)

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

    return Config(
        tg_api_id=api_id,
        tg_api_hash=api_hash,
        tg_phone=phone,
        session_encryption_key=session_key,
        notifier_bot_token=notifier_token,
        notifier_chat_id=notifier_chat,
        heartbeat_interval_seconds=heartbeat,
        log_level=log_level,
        ui_port=ui_port,
        ui_auto_activate=ui_auto_activate,
        ollama_host=ollama_host,
        llm_model=llm_model,
        llm_timeout_seconds=llm_timeout_seconds,
        llm_max_retries=llm_max_retries,
        classifier_confidence_threshold=classifier_threshold,
        bootstrap_history_messages=bootstrap_msgs,
        bootstrap_history_days=bootstrap_days,
        bootstrap_max_concurrent=bootstrap_max_concurrent,
        backlog_max_concurrent=backlog_max_concurrent,
        resurface_dormant_days=resurface_dormant_days,
        project_root=root,
        data_dir=data_dir,
        logs_dir=logs_dir,
        db_path=data_dir / "bot.db",
        legacy_encrypted_session_path=data_dir / "session.enc",
    )
