from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from src import storage
from src.classifier import Classifier
from src.signal_detector import run_signals

if TYPE_CHECKING:
    from src.telegram_client import BotClient


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class BootstrapReport:
    total_chats: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CatchupReport:
    total_chats: int = 0
    total_messages: int = 0
    succeeded: int = 0
    failed: int = 0
    duration_seconds: float = 0.0
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Progress:
    phase: str = "idle"  # idle | bootstrap | catchup
    total: int = 0
    done: int = 0
    failed: int = 0
    current_chat_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None


class BacklogProcessor:
    """First-time bootstrap and unread-message catchup orchestrator.

    Lives for the lifetime of one active account; created by `BotManager`
    after the client connects.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        client: BotClient,
        classifier: Classifier,
        account_id: int,
        history_messages: int,
        history_days: int,
        bootstrap_concurrency: int,
        catchup_concurrency: int,
        resurface_threshold_days: int,
    ) -> None:
        self._db_path = db_path
        self._client = client
        self._classifier = classifier
        self._account_id = account_id
        self._history_messages = max(1, history_messages)
        self._history_days = max(1, history_days)
        self._bootstrap_sem = asyncio.Semaphore(max(1, bootstrap_concurrency))
        self._catchup_sem = asyncio.Semaphore(max(1, catchup_concurrency))
        self._resurface_days = resurface_threshold_days
        self._log = logger.bind(module=__name__, account_id=account_id)
        self._progress = _Progress()

    def progress(self) -> dict[str, Any]:
        p = self._progress
        return {
            "phase": p.phase,
            "total": p.total,
            "done": p.done,
            "failed": p.failed,
            "current_chat_id": p.current_chat_id,
            "started_at": p.started_at,
            "finished_at": p.finished_at,
        }

    # ---------- bootstrap ----------

    async def run_initial_bootstrap(self) -> BootstrapReport:
        """Iterate dialogs lacking a bootstrap marker; classify each."""
        report = BootstrapReport()
        started = datetime.now(UTC)
        self._progress = _Progress(
            phase="bootstrap", started_at=_utcnow_iso()
        )

        try:
            dialogs = await self._collect_bootstrap_targets()
        except Exception as exc:  # noqa: BLE001
            self._log.error("Failed to enumerate dialogs", error=str(exc))
            self._progress.finished_at = _utcnow_iso()
            self._progress.phase = "idle"
            report.errors.append({"phase": "enumerate", "error": str(exc)})
            return report

        report.total_chats = len(dialogs)
        self._progress.total = report.total_chats

        if not dialogs:
            self._progress.finished_at = _utcnow_iso()
            self._progress.phase = "idle"
            report.duration_seconds = (
                datetime.now(UTC) - started
            ).total_seconds()
            return report

        async def _one(chat_id: int) -> None:
            async with self._bootstrap_sem:
                self._progress.current_chat_id = chat_id
                try:
                    success = await self._bootstrap_single(chat_id)
                    if success:
                        report.succeeded += 1
                    else:
                        report.skipped += 1
                except Exception as exc:  # noqa: BLE001
                    report.failed += 1
                    report.errors.append(
                        {"chat_id": chat_id, "error": str(exc)}
                    )
                    self._progress.failed += 1
                    self._log.warning(
                        "Bootstrap failed for chat",
                        chat_id=chat_id,
                        error=str(exc),
                    )
                finally:
                    self._progress.done += 1
                    self._progress.current_chat_id = None

        await asyncio.gather(*(_one(c) for c in dialogs), return_exceptions=False)

        report.duration_seconds = (datetime.now(UTC) - started).total_seconds()
        self._progress.finished_at = _utcnow_iso()
        self._progress.phase = "idle"
        return report

    async def _collect_bootstrap_targets(self) -> list[int]:
        """Return chat_ids of private dialogs that still need bootstrap."""
        chats: list[int] = []
        tg = self._client.client
        async for dialog in tg.iter_dialogs():
            if not getattr(dialog, "is_user", False):
                continue
            entity = getattr(dialog, "entity", None)
            if entity is None or getattr(entity, "bot", False):
                continue
            chats.append(int(dialog.id))

        # Skip chats that already have a bootstrap marker. Dialog enumeration
        # gives us the authoritative chat set from Telegram; we filter out any
        # whose contact_state already has a bootstrap_completed_at timestamp.
        with_marker: set[int] = set()
        for row in storage.get_all_contacts(self._db_path, account_id=self._account_id):
            state = storage.get_contact_state(
                self._db_path, self._account_id, int(row["chat_id"])
            )
            if state is not None and state.get("bootstrap_completed_at"):
                with_marker.add(int(row["chat_id"]))

        return [c for c in chats if c not in with_marker]

    async def _bootstrap_single(self, chat_id: int) -> bool:
        """Pull history, persist messages, run bootstrap classification."""
        tg = self._client.client
        try:
            entity = await tg.get_entity(chat_id)
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "get_entity failed", chat_id=chat_id, error=str(exc)
            )
            return False

        if getattr(entity, "bot", False):
            return False

        # Compute the "fetch up to" cutoff.
        cutoff = datetime.now(UTC) - timedelta(days=self._history_days)

        history: list[dict[str, Any]] = []
        async for msg in tg.iter_messages(entity, limit=self._history_messages):
            history.append(self._normalize_telethon_message(msg, chat_id))
            mdate = getattr(msg, "date", None)
            # Stop if we've passed the day cutoff AND we have at least the
            # message count requirement.
            if (
                mdate is not None
                and mdate < cutoff
                and len(history) >= min(20, self._history_messages // 5 + 1)
            ):
                break

        if not history:
            # Genuinely empty chat — live event handler will cover it.
            return False

        # iter_messages returns newest first; we want oldest-first transcripts.
        history.reverse()

        sender = entity
        storage.upsert_contact(
            self._db_path,
            account_id=self._account_id,
            chat_id=chat_id,
            tg_user_id=int(getattr(sender, "id", chat_id)),
            username=getattr(sender, "username", None),
            first_name=getattr(sender, "first_name", None),
            last_name=getattr(sender, "last_name", None),
        )

        for m in history:
            storage.insert_message(
                self._db_path,
                account_id=self._account_id,
                chat_id=chat_id,
                tg_message_id=int(m["tg_message_id"]),
                direction=m["direction"],
                sender_id=int(m["sender_id"]),
                text=m["text"],
                media_type=m["media_type"],
                raw_json=m["raw_json"],
            )

        await self._classifier.bootstrap_chat(
            account_id=self._account_id,
            chat_id=chat_id,
            history_messages=history,
        )
        return True

    # ---------- unread catchup ----------

    async def run_unread_catchup(self) -> CatchupReport:
        report = CatchupReport()
        started = datetime.now(UTC)
        self._progress = _Progress(
            phase="catchup", started_at=_utcnow_iso()
        )

        try:
            unread_chats: list[tuple[int, int]] = []
            tg = self._client.client
            async for dialog in tg.iter_dialogs():
                if not getattr(dialog, "is_user", False):
                    continue
                entity = getattr(dialog, "entity", None)
                if entity is None or getattr(entity, "bot", False):
                    continue
                unread = int(getattr(dialog, "unread_count", 0) or 0)
                if unread > 0:
                    unread_chats.append((int(dialog.id), unread))
        except Exception as exc:  # noqa: BLE001
            self._log.error("Catchup enumerate failed", error=str(exc))
            self._progress.finished_at = _utcnow_iso()
            self._progress.phase = "idle"
            report.errors.append({"phase": "enumerate", "error": str(exc)})
            return report

        report.total_chats = len(unread_chats)
        self._progress.total = len(unread_chats)

        if not unread_chats:
            self._progress.finished_at = _utcnow_iso()
            self._progress.phase = "idle"
            report.duration_seconds = (
                datetime.now(UTC) - started
            ).total_seconds()
            return report

        async def _one(chat_id: int, unread_count: int) -> None:
            async with self._catchup_sem:
                self._progress.current_chat_id = chat_id
                try:
                    n = await self._catchup_single(chat_id, unread_count)
                    report.succeeded += 1
                    report.total_messages += n
                except Exception as exc:  # noqa: BLE001
                    report.failed += 1
                    report.errors.append(
                        {"chat_id": chat_id, "error": str(exc)}
                    )
                    self._progress.failed += 1
                    self._log.warning(
                        "Catchup failed for chat",
                        chat_id=chat_id,
                        error=str(exc),
                    )
                finally:
                    self._progress.done += 1
                    self._progress.current_chat_id = None

        await asyncio.gather(
            *(_one(c, u) for c, u in unread_chats), return_exceptions=False
        )
        report.duration_seconds = (datetime.now(UTC) - started).total_seconds()
        self._progress.finished_at = _utcnow_iso()
        self._progress.phase = "idle"
        return report

    async def _catchup_single(self, chat_id: int, unread_count: int) -> int:
        tg = self._client.client
        try:
            entity = await tg.get_entity(chat_id)
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "get_entity failed (catchup)", chat_id=chat_id, error=str(exc)
            )
            return 0

        fetched: list[dict[str, Any]] = []
        # Pull the unread tail. Cap at history_messages to bound memory.
        async for msg in tg.iter_messages(
            entity, limit=min(unread_count, self._history_messages)
        ):
            if getattr(msg, "out", False):
                continue
            fetched.append(self._normalize_telethon_message(msg, chat_id))

        if not fetched:
            return 0

        fetched.reverse()
        sender = entity
        storage.upsert_contact(
            self._db_path,
            account_id=self._account_id,
            chat_id=chat_id,
            tg_user_id=int(getattr(sender, "id", chat_id)),
            username=getattr(sender, "username", None),
            first_name=getattr(sender, "first_name", None),
            last_name=getattr(sender, "last_name", None),
        )

        new_inserts = 0
        for m in fetched:
            inserted = storage.insert_message(
                self._db_path,
                account_id=self._account_id,
                chat_id=chat_id,
                tg_message_id=int(m["tg_message_id"]),
                direction=m["direction"],
                sender_id=int(m["sender_id"]),
                text=m["text"],
                media_type=m["media_type"],
                raw_json=m["raw_json"],
            )
            if inserted:
                new_inserts += 1
                # Run signals + classifier on the new message.
                signals = run_signals(
                    {"text": m["text"], "media_type": m["media_type"]},
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
                        new_message=m,
                        signal_result=signals,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log.warning(
                        "Catchup classify failed",
                        chat_id=chat_id,
                        error=str(exc),
                    )
        return new_inserts

    # ---------- shared helpers ----------

    @staticmethod
    def _normalize_telethon_message(
        msg: Any, chat_id: int
    ) -> dict[str, Any]:
        out: bool = bool(getattr(msg, "out", False))
        sender_id_attr = (
            getattr(msg, "sender_id", None)
            or getattr(msg, "from_id", None)
            or chat_id
        )
        if hasattr(sender_id_attr, "user_id"):
            sender_id_val = int(sender_id_attr.user_id)
        else:
            try:
                sender_id_val = int(sender_id_attr)
            except (TypeError, ValueError):
                sender_id_val = chat_id

        media = getattr(msg, "media", None)
        media_type = type(media).__name__ if media is not None else None

        text = getattr(msg, "message", None) or getattr(msg, "text", None) or None

        try:
            raw_dict = msg.to_dict()
            raw_json = json.dumps(raw_dict, default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            raw_json = "{}"

        return {
            "chat_id": chat_id,
            "tg_message_id": int(getattr(msg, "id", 0) or 0),
            "direction": "out" if out else "in",
            "sender_id": sender_id_val,
            "text": text,
            "media_type": media_type,
            "raw_json": raw_json,
        }
