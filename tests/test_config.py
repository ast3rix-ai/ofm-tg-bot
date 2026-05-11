from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from src.config import ConfigError, load_config

_REQUIRED_ENV = {
    "SESSION_ENCRYPTION_KEY": "x" * 44,
}

_LEGACY_ENV = {
    "TG_API_ID": "12345",
    "TG_API_HASH": "deadbeef",
    "TG_PHONE": "+10000000000",
}

_NOTIFIER_ENV = {
    "NOTIFIER_BOT_TOKEN": "bot:token",
    "NOTIFIER_CHAT_ID": "9999",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    for key in (
        "TG_API_ID",
        "TG_API_HASH",
        "TG_PHONE",
        "SESSION_ENCRYPTION_KEY",
        "NOTIFIER_BOT_TOKEN",
        "NOTIFIER_CHAT_ID",
        "HEARTBEAT_INTERVAL_SECONDS",
        "LOG_LEVEL",
        "UI_PORT",
        "UI_AUTO_ACTIVATE",
    ):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def _missing_env_file(tmp_path: Path) -> Path:
    return tmp_path / "absent.env"


def test_load_config_succeeds_with_only_required(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for k, v in _REQUIRED_ENV.items():
        clean_env.setenv(k, v)
    cfg = load_config(env_file=_missing_env_file(tmp_path))
    assert cfg.tg_api_id is None
    assert cfg.tg_api_hash is None
    assert cfg.tg_phone is None
    assert cfg.notifier_bot_token is None
    assert cfg.notifier_chat_id is None
    assert cfg.ui_port == 8765
    assert cfg.ui_auto_activate is True


def test_load_config_legacy_creds(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for k, v in {**_REQUIRED_ENV, **_LEGACY_ENV}.items():
        clean_env.setenv(k, v)
    cfg = load_config(env_file=_missing_env_file(tmp_path))
    assert cfg.tg_api_id == 12345
    assert cfg.tg_api_hash == "deadbeef"
    assert cfg.tg_phone == "+10000000000"


def test_load_config_with_notifier(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for k, v in {**_REQUIRED_ENV, **_NOTIFIER_ENV}.items():
        clean_env.setenv(k, v)
    cfg = load_config(env_file=_missing_env_file(tmp_path))
    assert cfg.notifier_bot_token == "bot:token"
    assert cfg.notifier_chat_id == 9999


def test_load_config_fails_without_encryption_key(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(env_file=_missing_env_file(tmp_path))
    assert "SESSION_ENCRYPTION_KEY" in str(exc.value)


def test_load_config_invalid_chat_id_reported(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for k, v in _REQUIRED_ENV.items():
        clean_env.setenv(k, v)
    clean_env.setenv("NOTIFIER_CHAT_ID", "not-a-number")
    with pytest.raises(ConfigError) as exc:
        load_config(env_file=_missing_env_file(tmp_path))
    assert "NOTIFIER_CHAT_ID" in str(exc.value)


def test_load_config_ui_port_override(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for k, v in _REQUIRED_ENV.items():
        clean_env.setenv(k, v)
    clean_env.setenv("UI_PORT", "9000")
    clean_env.setenv("UI_AUTO_ACTIVATE", "false")
    cfg = load_config(env_file=_missing_env_file(tmp_path))
    assert cfg.ui_port == 9000
    assert cfg.ui_auto_activate is False
