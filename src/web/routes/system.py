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

    # Surface the rate-limit / circuit-breaker / restriction sections
    # explicitly so the control panel can render them without digging.
    rate = bot_status.get("rate_limiter")
    rate_limiting: dict[str, Any] | None = None
    if isinstance(rate, dict):
        rate_limiting = {
            "utilization": rate.get("daily_global"),
            "global_bucket_level": rate.get("global_bucket_level"),
            "global_bucket_capacity": rate.get("global_bucket_capacity"),
            "circuit_breaker": rate.get("circuit_breaker"),
            "account_restriction": {
                "restricted": rate.get("account_restricted", False),
                "detail": rate.get("account_restriction"),
            },
        }

    return {
        "bot": bot_status,
        "active_account": active_account,
        "rate_limiting": rate_limiting,
        "db": {"migrations": migrations},
        "uptime_seconds": max(0.0, time.monotonic() - deps.boot_monotonic),
    }
