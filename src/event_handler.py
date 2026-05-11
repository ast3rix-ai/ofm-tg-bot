from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path
from typing import Any

from loguru import logger
from telethon import events

from src import storage
from src.notifier import Notifier
from src.telegram_client import BotClient

_TEXT_PREVIEW_CHARS = 80


def _media_type(message: Any) -> str | None:
    media = getattr(message, "media", None)
    if media is None:
        return None
    return type(media).__name__


def _text_preview(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= _TEXT_PREVIEW_CHARS:
        return text
    return text[:_TEXT_PREVIEW_CHARS] + "…"


class EventHandler:
    """Subscribes to incoming private DMs, persists them, dispatches to pipeline.

    In Phase 1 the pipeline is observation-only — messages are stored and
    logged. Per-chat asyncio locks are constructed but not yet contended.
    """

    def __init__(
        self,
        client: BotClient,
        db_path: Path,
        notifier: Notifier,
    ) -> None:
        self._client = client
        self._db_path = db_path
        self._notifier = notifier
        self._log = logger.bind(module=__name__)
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def get_lock(self, chat_id: int) -> asyncio.Lock:
        """Return the per-chat asyncio lock, creating it on first access."""
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def setup(self) -> None:
        """Register the Telethon incoming-DM handler."""
        tg = self._client.client

        @tg.on(events.NewMessage(incoming=True))
        async def _on_new_message(event: events.NewMessage.Event) -> None:
            await self._handle(event)

        self._log.info("Event handler registered")

    async def _handle(self, event: events.NewMessage.Event) -> None:
        try:
            if not getattr(event, "is_private", False):
                return

            sender = await event.get_sender()
            if sender is None:
                return
            if getattr(sender, "bot", False):
                return

            chat_id = int(event.chat_id)
            sender_id = int(getattr(sender, "id", chat_id))
            username = getattr(sender, "username", None)
            first_name = getattr(sender, "first_name", None)
            last_name = getattr(sender, "last_name", None)

            storage.upsert_contact(
                self._db_path,
                chat_id=chat_id,
                tg_user_id=sender_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )

            message = event.message
            text = getattr(message, "message", None) or None
            raw_json = json.dumps(message.to_dict(), default=str, ensure_ascii=False)

            inserted = storage.insert_message(
                self._db_path,
                chat_id=chat_id,
                tg_message_id=int(message.id),
                direction="in",
                sender_id=sender_id,
                text=text,
                media_type=_media_type(message),
                raw_json=raw_json,
            )

            self._log.info(
                "Incoming DM",
                chat_id=chat_id,
                sender_id=sender_id,
                username=username,
                text_preview=_text_preview(text),
                duplicate=not inserted,
            )

        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            self._log.error(
                "Event handler failed",
                error=str(exc),
                traceback=tb,
            )
            try:
                storage.log_event(
                    self._db_path,
                    "event_handler_error",
                    {"error": str(exc), "traceback": tb},
                )
            except Exception as log_exc:  # noqa: BLE001
                self._log.warning(
                    "Failed to persist error event",
                    error=str(log_exc),
                )
            await self._notifier.alert(
                f"Event handler failure: {exc}",
                severity="error",
                key="event_handler_failure",
            )
