from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from src import storage

if TYPE_CHECKING:
    from src.rate_limiter import RateLimiter

_log = logger.bind(module=__name__)


async def handle_reset(*, db_path: Path, account_id: int, chat_id: int) -> None:
    """Full wipe of a chat's classifier/response state. Audit trail preserved.

    Deletes the `contact_state` and `contact_memory` rows for the chat so
    the next inbound message is treated as a brand-new conversation. The
    `messages`, `classifier_runs`, `response_runs`, and `bot_sent_messages`
    rows are intentionally left intact for audit.

    Args:
        db_path: SQLite database location.
        account_id: Operated account id.
        chat_id: Telegram chat id to reset.
    """
    storage.delete_contact_state(db_path, account_id, chat_id)
    storage.delete_contact_memory(db_path, account_id, chat_id)
    storage.log_event(
        db_path,
        "operator_reset",
        {"chat_id": chat_id, "account_id": account_id},
        account_id=account_id,
    )
    _log.info("Operator reset applied", chat_id=chat_id, account_id=account_id)


async def handle_breaker_reset(
    *, db_path: Path, account_id: int, rate_limiter: RateLimiter
) -> None:
    """Close the circuit breaker and clear any active account restriction.

    This is the operator's recovery path after a flood/peer-flood trip or a
    @SpamBot restriction. The action is audited via the `events` table.

    Args:
        db_path: SQLite database location.
        account_id: Operated account id.
        rate_limiter: The active account's rate limiter.
    """
    await rate_limiter.reset_breaker()
    storage.log_event(
        db_path,
        "operator_breaker_reset",
        {"account_id": account_id},
        account_id=account_id,
    )
    _log.info("Operator breaker reset applied", account_id=account_id)
