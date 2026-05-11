from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import accounts as accounts_mod
from src.accounts import AccountsError
from src.bot_manager import BotManager
from src.notifier import Notifier


@pytest.fixture
def manager(db: Path, encryption_key: str) -> BotManager:
    from src.llm.client import LLMClient

    notifier = Notifier(token=None, chat_id=None)
    llm = LLMClient(host="http://127.0.0.1:11434", model=None)
    return BotManager(
        db_path=db,
        encryption_key=encryption_key,
        notifier=notifier,
        llm=llm,
        heartbeat_interval_seconds=60,
    )


def _patch_bot_client_factory() -> Any:
    """Patch BotClient and its watchdog/event-handler so activate() returns fast."""
    bc = AsyncMock()
    bc.is_connected.return_value = True
    bc.get_me_summary = AsyncMock(
        return_value={"id": 1234, "username": "u", "first_name": "U"}
    )
    bc.start = AsyncMock()
    bc.stop = AsyncMock()
    bc.run_until_disconnected = AsyncMock()

    factory = AsyncMock()
    factory.return_value = bc
    return factory, bc


def test_status_initial(manager: BotManager) -> None:
    s = manager.status()
    assert s["state"] == "idle"
    assert s["active_account_id"] is None
    assert s["last_error"] is None
    assert s["is_connected"] is False


async def test_activate_unknown_account_errors(manager: BotManager) -> None:
    with pytest.raises(AccountsError):
        await manager.activate(9999)
    assert manager.status()["state"] == "error"
    assert "No account with id" in (manager.status()["last_error"] or "")


async def test_activate_then_deactivate_lifecycle(
    manager: BotManager, db: Path, encryption_key: str
) -> None:
    accounts_mod.update_session_blob(db, encryption_key, 1, "stub_session")

    with patch("src.bot_manager.BotClient") as ClientCls, \
         patch("src.bot_manager.EventHandler") as EvtCls, \
         patch("src.bot_manager.Watchdog") as WdCls:
        client = AsyncMock()
        client.is_connected = MagicMock(return_value=True)
        client.get_me_summary = AsyncMock(
            return_value={"id": 555, "username": "u"}
        )
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.run_until_disconnected = AsyncMock()
        ClientCls.return_value = client

        evt = AsyncMock()
        evt.setup = AsyncMock()
        EvtCls.return_value = evt

        wd = AsyncMock()
        wd.stop = AsyncMock()
        wd.run = AsyncMock()
        wd.attach_task = lambda *_a, **_k: None
        WdCls.return_value = wd

        await manager.activate(1)
        s = manager.status()
        assert s["state"] == "running"
        assert s["active_account_id"] == 1
        assert s["is_connected"] is True

        # Active flag persisted.
        listed = accounts_mod.list_accounts(db)
        assert next(a for a in listed if a.id == 1).is_active is True

        # Activating again should stop-then-start cleanly without error.
        await manager.activate(1)
        assert manager.status()["state"] == "running"

        await manager.deactivate()
        s = manager.status()
        assert s["state"] == "idle"
        assert s["active_account_id"] is None


async def test_activation_failure_sets_error_state(
    manager: BotManager, db: Path, encryption_key: str
) -> None:
    accounts_mod.update_session_blob(db, encryption_key, 1, "stub_session")

    with patch("src.bot_manager.BotClient") as ClientCls:
        client = AsyncMock()
        client.start = AsyncMock(side_effect=RuntimeError("boom"))
        client.stop = AsyncMock()
        ClientCls.return_value = client

        with pytest.raises(RuntimeError, match="boom"):
            await manager.activate(1)

    s = manager.status()
    assert s["state"] == "error"
    assert "boom" in (s["last_error"] or "")


def test_submit_code_returns_false_when_no_prompt(manager: BotManager) -> None:
    assert manager.submit_code("12345") is False
    assert manager.submit_password("pw") is False
