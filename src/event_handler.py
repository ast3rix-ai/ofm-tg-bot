from __future__ import annotations

import asyncio
import json
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from telethon import events

from src import storage
from src.notifier import Notifier
from src.signal_detector import run_signals
from src.telegram_client import BotClient

if TYPE_CHECKING:
    from src.classifier import Classifier

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


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class EventHandler:
    """Subscribes to incoming private DMs, persists, classifies via Classifier.

    During bootstrap (before `accept_live()` is called) new messages are
    persisted but classification is deferred onto an internal queue. After
    bootstrap completes, `flush_pending()` drains the queue.
    """

    def __init__(
        self,
        *,
        client: BotClient,
        db_path: Path,
        notifier: Notifier,
        account_id: int,
        classifier: Classifier | None = None,
        resurface_threshold_days: int = 14,
    ) -> None:
        self._client = client
        self._db_path = db_path
        self._notifier = notifier
        self._account_id = account_id
        self._classifier = classifier
        self._resurface_days = resurface_threshold_days
        self._log = logger.bind(module=__name__, account_id=account_id)
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._pending: list[dict[str, Any]] = []
        self._accept_live = False

    def get_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def setup(self) -> None:
        tg = self._client.client

        @tg.on(events.NewMessage(incoming=True))
        async def _on_new_message(event: events.NewMessage.Event) -> None:
            await self._handle(event)

        self._log.info("Event handler registered")

    async def flush_pending(self) -> None:
        """Move into 'accept live' mode and drain any queued messages."""
        self._accept_live = True
        if not self._pending:
            return
        self._log.info(
            "Flushing pending live messages",
            count=len(self._pending),
        )
        to_process = self._pending
        self._pending = []
        for payload in to_process:
            try:
                await self._classify_persisted(payload)
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "Pending classify failed",
                    error=str(exc),
                    chat_id=payload.get("chat_id"),
                )

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
                account_id=self._account_id,
                chat_id=chat_id,
                tg_user_id=sender_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )

            message = event.message
            text = getattr(message, "message", None) or None
            media = _media_type(message)
            raw_json = json.dumps(message.to_dict(), default=str, ensure_ascii=False)

            inserted = storage.insert_message(
                self._db_path,
                account_id=self._account_id,
                chat_id=chat_id,
                tg_message_id=int(message.id),
                direction="in",
                sender_id=sender_id,
                text=text,
                media_type=media,
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

            if not inserted or self._classifier is None:
                return

            payload = {
                "chat_id": chat_id,
                "tg_message_id": int(message.id),
                "sender_id": sender_id,
                "text": text,
                "media_type": media,
                "raw_json": raw_json,
                "direction": "in",
            }
            if not self._accept_live:
                self._pending.append(payload)
                return

            await self._classify_persisted(payload)

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
                    account_id=self._account_id,
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

    async def _classify_persisted(self, payload: dict[str, Any]) -> None:
        assert self._classifier is not None
        chat_id = int(payload["chat_id"])
        # Skip if the chat hasn't been bootstrapped yet — bootstrap should
        # have captured this message already as part of its history pull.
        state = storage.get_contact_state(
            self._db_path, self._account_id, chat_id
        )
        if state is not None and state.get("bootstrap_completed_at") is None:
            self._log.debug(
                "Skipping classification during bootstrap",
                chat_id=chat_id,
            )
            return

        signals = run_signals(
            {"text": payload.get("text"), "media_type": payload.get("media_type")},
            db_path=self._db_path,
            account_id=self._account_id,
            chat_id=chat_id,
            now_iso=_utcnow_iso(),
            resurface_threshold_days=self._resurface_days,
        )
        try:
            await self._classifier.classify_new_message(
                account_id=self._account_id,
                chat_id=chat_id,
                new_message=payload,
                signal_result=signals,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Classification failed",
                chat_id=chat_id,
                error=str(exc),
            )
