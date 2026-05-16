from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
from telethon import events

from src import storage
from src.notifier import Notifier
from src.rate_limiter import RateLimiter

_log = logger.bind(module=__name__)

# Telegram's official @SpamBot account.
SPAMBOT_USER_ID = 178220800

# Substrings (lowercased) seen in @SpamBot restriction notifications. Matching
# is deliberately loose; unknown bodies are logged so this list can grow.
_RESTRICTION_SUBSTRINGS: tuple[str, ...] = (
    "is now limited",
    "your account is limited",
    "account will be limited",
    "limited until",
    "you can only send messages to mutual contacts",
    "you can't send messages to people who are not in your contacts",
    "you cannot send messages to people who are not in your contacts",
    "i'm afraid you can't",
    "some limitations to your account",
)

# Substrings that indicate the account is explicitly *not* restricted.
_CLEAR_SUBSTRINGS: tuple[str, ...] = (
    "no limits are currently applied",
    "good news",
    "is free as a bird",
)

RESTRICTION_TYPE_LIMITED = "account_limited"


def parse_restriction(body: str) -> str | None:
    """Classify a @SpamBot message body.

    Returns a restriction type string if the body announces an active
    restriction, or None for the "no limits" message and any body that does
    not match a known restriction phrase.
    """
    text = (body or "").strip().lower()
    if not text:
        return None
    if any(sub in text for sub in _CLEAR_SUBSTRINGS):
        return None
    if any(sub in text for sub in _RESTRICTION_SUBSTRINGS):
        return RESTRICTION_TYPE_LIMITED
    return None


class SpamBotMonitor:
    """Listens for @SpamBot messages and trips the breaker on a restriction."""

    def __init__(
        self,
        *,
        db_path: Path,
        account_id: int,
        rate_limiter: RateLimiter,
        notifier: Notifier,
    ) -> None:
        self._db_path = db_path
        self._account_id = account_id
        self._rate_limiter = rate_limiter
        self._notifier = notifier
        self._log = logger.bind(module=__name__, account_id=account_id)

    def register(self, client: Any) -> None:
        """Attach the @SpamBot listener to a Telethon client."""

        @client.on(
            events.NewMessage(incoming=True, from_users=SPAMBOT_USER_ID)
        )
        async def _on_spambot_message(event: Any) -> None:
            await self.handle(event)

        self._log.info("SpamBot monitor registered")

    async def handle(self, event: Any) -> None:
        """Process one @SpamBot message."""
        try:
            body = getattr(event.message, "message", None) or ""
            restriction_type = parse_restriction(body)
            if restriction_type is None:
                self._log.info(
                    "SpamBot message — no restriction detected",
                    body_preview=body[:200],
                )
                return

            self._log.error(
                "Account restriction detected via @SpamBot",
                restriction_type=restriction_type,
                body_preview=body[:200],
            )
            storage.insert_account_restriction(
                self._db_path,
                account_id=self._account_id,
                restriction_type=restriction_type,
                raw_body=body,
            )
            await self._rate_limiter.record_account_restriction()
            storage.insert_operator_alert(
                self._db_path,
                account_id=self._account_id,
                chat_id=None,
                alert_type="account_restricted",
                severity="critical",
                message=(
                    "Telegram @SpamBot has restricted this account — all "
                    "outbound sending is halted until manually reset."
                ),
                payload={"raw_body": body},
            )
            await self._notifier.alert(
                "ACCOUNT RESTRICTED by @SpamBot — outbound sending halted.",
                severity="error",
                key="account_restricted",
            )
        except Exception as exc:  # noqa: BLE001
            self._log.error(
                "SpamBot monitor failed to process message", error=str(exc)
            )
