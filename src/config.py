from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class RateLimitConfig:
    """Token-bucket, daily-cap, and circuit-breaker tuning.

    All numbers are defaults; the daily caps are env-overridable. The buckets
    and breaker timings are fixed here and tunable per-persona later.
    """

    per_chat_refill_per_sec: float = 0.5
    per_chat_capacity: float = 3.0
    global_refill_per_sec: float = 1.0
    global_capacity: float = 10.0
    daily_per_chat_cap: int = 40
    daily_global_cap: int = 200
    new_chat_daily_cap: int = 10
    new_chat_age_days: int = 7
    flood_double_window_seconds: float = 300.0
    flood_open_multiplier: float = 4.0
    peer_flood_open_seconds: float = 21600.0  # 6 hours


@dataclass(frozen=True)
class HumanizationConfig:
    """Timing/typo distributions for the humanization layer.

    All randomness in `Humanizer` is drawn through an injected `random.Random`
    so tests can seed and assert exact output; these are the distribution
    parameters.
    """

    chars_per_word: float = 5.0
    read_words_per_sec: float = 4.2
    read_jitter_min: float = 1.5
    read_jitter_max: float = 4.0
    read_clamp_min: float = 2.0
    read_clamp_max: float = 15.0
    typing_cpm_min: float = 200.0
    typing_cpm_max: float = 400.0
    typing_jitter_min: float = 0.4
    typing_jitter_max: float = 1.2
    typing_clamp_min: float = 1.2
    typing_clamp_max: float = 8.0
    inter_quick_prob: float = 0.7
    inter_quick_min: float = 0.8
    inter_quick_max: float = 3.0
    inter_pause_min: float = 3.0
    inter_pause_max: float = 8.0
    single_chunk_prob: float = 0.7
    split_prob: float = 0.6
    short_sentence_words: int = 5
    min_chunk_chars: int = 4
    typo_per_word_prob: float = 0.015
    typo_correction_prob: float = 0.30
    correction_delay_min: float = 2.0
    correction_delay_max: float = 5.0


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
    response_temperature: float
    response_max_tokens: int
    response_max_retries: int
    response_persona_path: Path
    operator_user_ids: frozenset[int]
    default_bot_enabled_new_chats: int
    rate_limits: RateLimitConfig
    humanization: HumanizationConfig
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

    response_temperature = _opt_float("RESPONSE_TEMPERATURE", 0.85)
    response_max_tokens = _opt_int("RESPONSE_MAX_TOKENS", 200)
    response_max_retries = _opt_int("RESPONSE_MAX_RETRIES", 2)
    persona_path_raw = (
        os.environ.get("RESPONSE_PERSONA_PATH", "").strip()
        or "personas/default/persona.md"
    )
    persona_path = Path(persona_path_raw)
    if not persona_path.is_absolute():
        persona_path = root / persona_path

    operator_ids: set[int] = set()
    operator_raw = os.environ.get("OPERATOR_USER_IDS", "").strip()
    if operator_raw:
        for raw_part in operator_raw.split(","):
            part = raw_part.strip()
            if not part:
                continue
            try:
                operator_ids.add(int(part))
            except ValueError:
                invalid.append(f"OPERATOR_USER_IDS (not an integer: {part!r})")

    default_bot_enabled_new_chats = _opt_int("DEFAULT_BOT_ENABLED_NEW_CHATS", 1)

    rate_limits = RateLimitConfig(
        daily_per_chat_cap=_opt_int("RATE_LIMIT_DAILY_PER_CHAT", 40),
        daily_global_cap=_opt_int("RATE_LIMIT_DAILY_GLOBAL", 200),
        new_chat_daily_cap=_opt_int("RATE_LIMIT_NEW_CHAT_DAILY", 10),
    )
    humanization = HumanizationConfig()

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
        response_temperature=response_temperature,
        response_max_tokens=response_max_tokens,
        response_max_retries=response_max_retries,
        response_persona_path=persona_path,
        operator_user_ids=frozenset(operator_ids),
        default_bot_enabled_new_chats=default_bot_enabled_new_chats,
        rate_limits=rate_limits,
        humanization=humanization,
        project_root=root,
        data_dir=data_dir,
        logs_dir=logs_dir,
        db_path=data_dir / "bot.db",
        legacy_encrypted_session_path=data_dir / "session.enc",
    )
