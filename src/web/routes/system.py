from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends

from src import accounts as accounts_mod
from src import storage
from src.web.app import WebDeps, get_deps

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status")
async def status(deps: WebDeps = Depends(get_deps)) -> dict[str, Any]:
    bot_status = deps.bot_manager.status()
    active_account = None
    if bot_status["active_account_id"] is not None:
        acc = accounts_mod.get_account(
            deps.config.db_path,
            deps.config.session_encryption_key,
            int(bot_status["active_account_id"]),
        )
        if acc is not None:
            active_account = {
                "id": acc.id,
                "label": acc.label,
                "tg_user_id": acc.tg_user_id,
                "tg_username": acc.tg_username,
                "has_session": acc.has_session,
                "last_connected_at": acc.last_connected_at,
            }
    migrations = storage.get_applied_migrations(deps.config.db_path)
    return {
        "bot": bot_status,
        "active_account": active_account,
        "db": {"migrations": migrations},
        "uptime_seconds": max(0.0, time.monotonic() - deps.boot_monotonic),
    }
