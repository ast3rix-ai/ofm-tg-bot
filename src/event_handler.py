from __future__ import annotations

import asyncio
import json
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from telethon import events

from src import commands, storage
from src.notifier import Notifier
from src.signal_detector import run_signals
from src.telegram_client import BotClient

if TYPE_CHECKING:
    from src.classifier import Classifier
    from src.rate_limiter import RateLimiter
    from src.response_generator import ResponseGenerator
    from src.spambot_monitor import SpamBotMonitor

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
        operator_user_ids: frozenset[int] = frozenset(),
        rate_limiter: RateLimiter | None = None,
        spambot_monitor: SpamBotMonitor | None = None,
    ) -> None:
        self._client = client
        self._db_path = db_path
        self._notifier = notifier
        self._account_id = account_id
        self._classifier = classifier
        self._resurface_days = resurface_threshold_days
        self._operator_user_ids = operator_user_ids
        self._rate_limiter = rate_limiter
        self._spambot_monitor = spambot_monitor
        self._response_generator: ResponseGenerator | None = None
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

    def set_response_generator(
        self, response_generator: ResponseGenerator
    ) -> None:
        """Attach the response generator. Called by `BotManager` post-construction."""
        self._response_generator = response_generator

    async def setup(self) -> None:
        tg = self._client.client

        @tg.on(events.NewMessage(incoming=True))
        async def _on_new_message(event: events.NewMessage.Event) -> None:
            await self._handle(event)

        if self._spambot_monitor is not None:
            self._spambot_monitor.register(tg)

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

            if not inserted:
                return

            # Operator commands — run before classification, honoured only
            # from allow-listed operator accounts; from anyone else they are
            # treated as ordinary text (we do not leak their existence).
            is_operator = sender_id in self._operator_user_ids
            command = text.strip().lower() if text is not None else ""
            if is_operator and command.startswith("/breaker_reset"):
                await self._handle_breaker_reset(chat_id)
                return
            if is_operator and command.startswith("/reset"):
                await self._handle_reset(chat_id)
                return

            if self._classifier is None:
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

    async def _handle_reset(self, chat_id: int) -> None:
        """Wipe a chat's state on operator command and confirm in-chat."""
        await commands.handle_reset(
            db_path=self._db_path,
            account_id=self._account_id,
            chat_id=chat_id,
        )
        confirmation = "reset done ✓"
        try:
            async with self.get_lock(chat_id):
                sent = await self._client.send_message(chat_id, confirmation)
                tg_message_id = int(sent["tg_message_id"])
                storage.insert_message(
                    self._db_path,
                    account_id=self._account_id,
                    chat_id=chat_id,
                    tg_message_id=tg_message_id,
                    direction="out",
                    sender_id=0,
                    text=confirmation,
                    media_type=None,
                    raw_json='{"operator_command": "reset"}',
                )
                storage.insert_bot_sent_message(
                    self._db_path,
                    account_id=self._account_id,
                    chat_id=chat_id,
                    tg_message_id=tg_message_id,
                    response_run_id=None,
                )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Reset confirmation send failed",
                chat_id=chat_id,
                error=str(exc),
            )
        self._log.info("Operator /reset handled", chat_id=chat_id)

    async def _handle_breaker_reset(self, chat_id: int) -> None:
        """Close the circuit breaker on operator command and confirm in-chat."""
        if self._rate_limiter is None:
            self._log.warning(
                "Operator /breaker_reset received but no rate limiter wired",
                chat_id=chat_id,
            )
            return
        await commands.handle_breaker_reset(
            db_path=self._db_path,
            account_id=self._account_id,
            rate_limiter=self._rate_limiter,
        )
        confirmation = "breaker reset ✓"
        try:
            async with self.get_lock(chat_id):
                sent = await self._client.send_message(chat_id, confirmation)
                tg_message_id = int(sent["tg_message_id"])
                storage.insert_message(
                    self._db_path,
                    account_id=self._account_id,
                    chat_id=chat_id,
                    tg_message_id=tg_message_id,
                    direction="out",
                    sender_id=0,
                    text=confirmation,
                    media_type=None,
                    raw_json='{"operator_command": "breaker_reset"}',
                )
                storage.insert_bot_sent_message(
                    self._db_path,
                    account_id=self._account_id,
                    chat_id=chat_id,
                    tg_message_id=tg_message_id,
                    response_run_id=None,
                )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Breaker reset confirmation send failed",
                chat_id=chat_id,
                error=str(exc),
            )
        self._log.info("Operator /breaker_reset handled", chat_id=chat_id)

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

        # Dispatch the response generator. Failures here must never break
        # the event handler — they are caught, logged, and alerted.
        if self._response_generator is not None:
            try:
                triggering_id = storage.get_message_db_id(
                    self._db_path,
                    account_id=self._account_id,
                    chat_id=chat_id,
                    tg_message_id=int(payload["tg_message_id"]),
                    direction="in",
                )
                await self._response_generator.generate(
                    account_id=self._account_id,
                    chat_id=chat_id,
                    triggering_message_id=triggering_id,
                )
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                self._log.error(
                    "Response generation failed",
                    chat_id=chat_id,
                    error=str(exc),
                    traceback=tb,
                )
                try:
                    storage.insert_operator_alert(
                        self._db_path,
                        account_id=self._account_id,
                        chat_id=chat_id,
                        alert_type="response_generation_error",
                        severity="error",
                        message=f"Response generation failed: {exc}",
                        payload={"traceback": tb},
                    )
                except Exception as alert_exc:  # noqa: BLE001
                    self._log.warning(
                        "Failed to persist response error alert",
                        error=str(alert_exc),
                    )
