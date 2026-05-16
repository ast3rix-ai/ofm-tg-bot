from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from src import storage
from src.event_handler import EventHandler
from src.notifier import Notifier


class _FakeSender:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.bot = False
        self.username = "tester"
        self.first_name = "Test"
        self.last_name = None


class _FakeMessage:
    def __init__(self, message_id: int, text: str) -> None:
        self.id = message_id
        self.message = text
        self.media = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "message": self.message}


class _FakeEvent:
    def __init__(self, chat_id: int, sender: _FakeSender, message: _FakeMessage) -> None:
        self.is_private = True
        self.chat_id = chat_id
        self._sender = sender
        self.message = message

    async def get_sender(self) -> _FakeSender:
        return self._sender


def _telegram() -> AsyncMock:
    client = AsyncMock()
    client.send_message = AsyncMock(
        return_value={
            "tg_message_id": 7000,
            "created_at": "2026-05-16T00:00:00.000Z",
        }
    )
    return client


def _handler(
    db: Path,
    account_id: int,
    *,
    client: AsyncMock,
    classifier: AsyncMock,
    response_generator: AsyncMock,
    operator_ids: frozenset[int],
) -> EventHandler:
    handler = EventHandler(
        client=client,
        db_path=db,
        notifier=Notifier(token=None, chat_id=None),
        account_id=account_id,
        classifier=classifier,
        operator_user_ids=operator_ids,
    )
    handler.set_response_generator(response_generator)
    handler._accept_live = True
    return handler


async def test_operator_reset_skips_pipeline(db: Path, account_id: int) -> None:
    chat_id = 1
    storage.upsert_contact(
        db, account_id=account_id, chat_id=chat_id, tg_user_id=999,
        username="op", first_name="Op", last_name=None,
    )
    storage.upsert_contact_state(
        db, account_id=account_id, chat_id=chat_id, category="hot", bot_enabled=1,
    )

    client = _telegram()
    classifier = AsyncMock()
    response_generator = AsyncMock()
    handler = _handler(
        db, account_id, client=client, classifier=classifier,
        response_generator=response_generator, operator_ids=frozenset({999}),
    )

    event = _FakeEvent(chat_id, _FakeSender(999), _FakeMessage(10, "/reset"))
    await handler._handle(event)

    classifier.classify_new_message.assert_not_called()
    response_generator.generate.assert_not_called()
    client.send_message.assert_awaited_once()
    assert client.send_message.await_args.args[1] == "reset done ✓"

    # State + memory wiped, event logged.
    assert storage.get_contact_state(db, account_id, chat_id) is None
    events = storage.get_recent_events(db, account_id=account_id)
    assert any(e["event_type"] == "operator_reset" for e in events)


async def test_non_operator_reset_is_classified(db: Path, account_id: int) -> None:
    chat_id = 2
    client = _telegram()
    classifier = AsyncMock()
    response_generator = AsyncMock()
    handler = _handler(
        db, account_id, client=client, classifier=classifier,
        response_generator=response_generator, operator_ids=frozenset({999}),
    )

    # Sender 111 is NOT on the operator allow-list.
    event = _FakeEvent(chat_id, _FakeSender(111), _FakeMessage(20, "/reset"))
    await handler._handle(event)

    classifier.classify_new_message.assert_awaited_once()
    response_generator.generate.assert_awaited_once()
    # No reset confirmation leaked to a non-operator.
    client.send_message.assert_not_called()
