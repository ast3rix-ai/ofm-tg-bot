from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from telethon import TelegramClient
from telethon.sessions import StringSession

CodeProvider = Callable[[], Awaitable[str]]
PasswordProvider = Callable[[], Awaitable[str]]
SessionUpdateCallback = Callable[[str], None]


class BotClient:
    """Telethon wrapper using `StringSession` for in-DB session storage.

    The session lives only in memory and in the encrypted DB blob —
    never on disk as a `.session` file. Each call into the client that
    might mutate the session triggers a save via `on_session_update`.
    """

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        phone: str,
        session_string: str | None,
        on_session_update: SessionUpdateCallback,
        label: str,
    ) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._phone = phone
        self._on_session_update = on_session_update
        self._label = label
        self._session: StringSession = StringSession(session_string or "")
        self._client: TelegramClient | None = None
        self._log = logger.bind(module=__name__, account=label)
        self._last_saved_session_string = session_string or ""

    @property
    def client(self) -> TelegramClient:
        if self._client is None:
            raise RuntimeError("BotClient.start() has not been called.")
        return self._client

    def is_connected(self) -> bool:
        if self._client is None:
            return False
        return bool(self._client.is_connected())

    def _maybe_save_session(self) -> None:
        if self._client is None:
            return
        try:
            current = self._client.session.save()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("session.save() failed", error=str(exc))
            return
        if current and current != self._last_saved_session_string:
            self._last_saved_session_string = current
            try:
                self._on_session_update(current)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("Session persistence failed", error=str(exc))

    async def start(
        self,
        *,
        code_provider: CodeProvider | None = None,
        password_provider: PasswordProvider | None = None,
    ) -> None:
        """Connect and authenticate.

        If the session already contains credentials, this is a no-op
        beyond opening the network connection. Otherwise, Telethon will
        invoke the supplied providers to fetch the SMS code (and 2FA
        password if set). When the providers are absent, the client must
        already be authenticated.

        Raises:
            RuntimeError: If first-run auth is required and a provider
                wasn't supplied.
        """
        self._client = TelegramClient(self._session, self._api_id, self._api_hash)
        self._log.info("Connecting to Telegram")

        await self._client.connect()

        if not await self._client.is_user_authorized():
            self._log.info("First-run auth required")
            if code_provider is None:
                raise RuntimeError(
                    "Account is not authenticated and no code_provider was"
                    " supplied to BotClient.start()."
                )
            await self._client.send_code_request(self._phone)
            code = await code_provider()
            try:
                await self._client.sign_in(phone=self._phone, code=code)
            except Exception as exc:  # noqa: BLE001
                # Telethon raises SessionPasswordNeededError when 2FA is set.
                if exc.__class__.__name__ != "SessionPasswordNeededError":
                    raise
                if password_provider is None:
                    raise RuntimeError(
                        "Account requires a 2FA password but no"
                        " password_provider was supplied."
                    ) from exc
                password = await password_provider()
                await self._client.sign_in(password=password)

        self._maybe_save_session()
        me = await self._client.get_me()
        self._log.info(
            "Connected",
            user_id=getattr(me, "id", None),
            username=getattr(me, "username", None),
        )

    async def get_me_summary(self) -> dict[str, Any]:
        """Return a small dict summarizing the authenticated user."""
        if self._client is None:
            return {}
        me = await self._client.get_me()
        return {
            "id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None),
        }

    async def stop(self) -> None:
        """Disconnect and persist the final session."""
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Disconnect raised", error=str(exc))
        self._maybe_save_session()
        self._client = None

    async def run_until_disconnected(self) -> None:
        """Block until the underlying Telethon client disconnects."""
        if self._client is None:
            raise RuntimeError("BotClient.start() has not been called.")
        await self._client.run_until_disconnected()

    def save_session_now(self) -> None:
        """Force-persist the current session string. Safe to call repeatedly."""
        self._maybe_save_session()


async def auth_provider_from_queue(
    queue: asyncio.Queue[str], timeout: float = 300.0
) -> str:
    """Helper: wait up to `timeout` seconds for a value pushed onto `queue`.

    Used by the web layer to bridge HTTP POSTs of phone codes / 2FA
    passwords into the Telethon auth flow.
    """
    return await asyncio.wait_for(queue.get(), timeout=timeout)
