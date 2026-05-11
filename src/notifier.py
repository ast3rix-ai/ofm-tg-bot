from __future__ import annotations

import asyncio
from typing import Literal

import aiohttp
from loguru import logger

Severity = Literal["info", "warn", "error"]

_EMOJI: dict[Severity, str] = {
    "info": "ℹ️",
    "warn": "⚠️",
    "error": "\U0001f6a8",
}

_RATE_LIMIT_SECONDS = 60.0


class Notifier:
    """Sends operator alerts via a separate regular Telegram Bot API account.

    Distinct from the userbot so that account-level problems on the userbot
    do not prevent alerts from reaching the operator.
    """

    def __init__(self, token: str | None, chat_id: int | None) -> None:
        self._token = token
        self._chat_id = chat_id
        self._enabled = token is not None and chat_id is not None
        self._url = (
            f"https://api.telegram.org/bot{token}/sendMessage" if self._enabled else ""
        )
        self._last_sent_at: dict[str, float] = {}
        self._log = logger.bind(module=__name__)
        if not self._enabled:
            self._log.warning(
                "Notifier disabled — operator alerts will not be sent"
            )

    async def alert(
        self,
        message: str,
        severity: Severity = "info",
        key: str | None = None,
    ) -> None:
        """Send an alert. Non-fatal — HTTP failures are logged, not raised.

        Args:
            message: Plain-text body of the alert.
            severity: Affects the leading emoji.
            key: If provided, suppress repeat alerts with the same key within
                a 60-second window.
        """
        if not self._enabled:
            return

        if key is not None:
            now = asyncio.get_event_loop().time()
            last = self._last_sent_at.get(key)
            if last is not None and (now - last) < _RATE_LIMIT_SECONDS:
                self._log.debug(
                    "Suppressed alert (rate-limit)",
                    alert_key=key,
                    severity=severity,
                )
                return
            self._last_sent_at[key] = now

        prefix = _EMOJI.get(severity, "")
        text = f"{prefix} {message}".strip()
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self._url, json=payload) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        self._log.warning(
                            "Notifier API returned error",
                            status=resp.status,
                            body=body[:500],
                        )
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            self._log.warning("Notifier send failed", error=str(exc))
