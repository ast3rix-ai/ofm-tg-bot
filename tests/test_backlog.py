from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src import storage
from src.backlog import BacklogProcessor
from src.classifier import Classifier
from src.llm.client import LLMResponse
from src.notifier import Notifier


def _make_msg(
    mid: int, text: str, *, out: bool = False, media_type: str | None = None
) -> SimpleNamespace:
    m = SimpleNamespace(
        id=mid,
        message=text,
        text=text,
        out=out,
        sender_id=42,
        media=SimpleNamespace() if media_type else None,
        date=None,
    )

    if media_type and m.media is not None:
        m.media.__class__.__name__ = media_type

    def _to_dict() -> dict[str, object]:
        return {"id": mid, "message": text, "out": out}

    m.to_dict = _to_dict
    return m


def _make_dialog(chat_id: int, unread: int = 0) -> SimpleNamespace:
    entity = SimpleNamespace(
        id=chat_id, username="u", first_name="U", last_name=None, bot=False
    )
    return SimpleNamespace(
        id=chat_id, is_user=True, unread_count=unread, entity=entity
    )


class _FakeTG:
    """Minimal stand-in for telethon's TelegramClient."""

    def __init__(
        self,
        dialogs: list[SimpleNamespace],
        messages_by_chat: dict[int, list[SimpleNamespace]],
    ) -> None:
        self._dialogs = dialogs
        self._messages = messages_by_chat

    def iter_dialogs(self) -> AsyncIterator[SimpleNamespace]:
        async def _gen() -> AsyncIterator[SimpleNamespace]:
            for d in self._dialogs:
                yield d
        return _gen()

    async def get_entity(self, chat_id: int) -> SimpleNamespace:
        for d in self._dialogs:
            if d.id == chat_id:
                return d.entity
        raise LookupError(chat_id)

    def iter_messages(
        self, _entity: object, limit: int | None = None
    ) -> AsyncIterator[SimpleNamespace]:
        msgs = list(self._messages.get(getattr(_entity, "id", 0), []))
        if limit:
            msgs = msgs[:limit]

        async def _gen() -> AsyncIterator[SimpleNamespace]:
            for m in msgs:
                yield m
        return _gen()


def _llm_ok(payload: dict[str, object]) -> AsyncMock:
    llm = AsyncMock()
    llm.health = lambda: {"reachable": True, "model_loaded": True}
    llm.generate = AsyncMock(
        return_value=LLMResponse(
            text=json.dumps(payload), tokens_in=1, tokens_out=1,
            latency_ms=10, model="m",
        )
    )
    return llm


def _build_processor(db: Path, tg: _FakeTG, llm: AsyncMock) -> BacklogProcessor:
    notifier = Notifier(token=None, chat_id=None)
    classifier = Classifier(
        db_path=db, llm=llm, notifier=notifier, confidence_threshold=0.6
    )
    client = MagicMock()
    client.client = tg
    return BacklogProcessor(
        db_path=db,
        client=client,
        classifier=classifier,
        account_id=1,
        history_messages=50,
        history_days=30,
        bootstrap_concurrency=2,
        catchup_concurrency=2,
        resurface_threshold_days=14,
    )


async def test_bootstrap_iterates_only_unbootstrapped(
    db: Path, account_id: int
) -> None:
    # Mark chat A as already bootstrapped.
    storage.upsert_contact(
        db, account_id=account_id, chat_id=10, tg_user_id=42,
        username="u", first_name="U", last_name=None,
    )
    storage.upsert_contact_state(
        db, account_id=account_id, chat_id=10, category="warm",
        bot_enabled=0, bootstrap_completed_at="2026-01-01T00:00:00Z",
    )

    dialogs = [_make_dialog(10, 0), _make_dialog(20, 0)]
    messages = {
        10: [_make_msg(1, "hi")],
        20: [_make_msg(1, "hey"), _make_msg(2, "what's up")],
    }
    tg = _FakeTG(dialogs, messages)
    llm = _llm_ok({
        "category": "cold",
        "funnel_stage_inferred": "opener",
        "confidence": 0.85,
        "flags": {"timewaster": False, "human_active": False},
        "summary": "n/a",
        "extracted_facts": {},
        "reasoning": "x",
        "threat_detected": False, "threat_details": "",
    })
    proc = _build_processor(db, tg, llm)
    report = await proc.run_initial_bootstrap()

    # Only chat 20 should have been bootstrapped this round.
    assert report.total_chats == 1
    assert report.succeeded == 1
    state_20 = storage.get_contact_state(db, account_id, 20)
    assert state_20 is not None
    assert state_20["bootstrap_completed_at"] is not None


async def test_bootstrap_failure_does_not_break_run(
    db: Path, account_id: int
) -> None:
    dialogs = [_make_dialog(1, 0), _make_dialog(2, 0)]
    messages = {
        1: [_make_msg(1, "hi")],
        2: [_make_msg(1, "hey")],
    }
    tg = _FakeTG(dialogs, messages)

    # Make the LLM fail on the SECOND chat by raising on the second call.
    payload = {
        "category": "cold", "funnel_stage_inferred": "opener", "confidence": 0.9,
        "flags": {"timewaster": False, "human_active": False},
        "summary": "x", "extracted_facts": {},
        "reasoning": "x", "threat_detected": False, "threat_details": "",
    }
    llm = AsyncMock()
    llm.health = lambda: {"reachable": True, "model_loaded": True}
    llm.generate = AsyncMock(
        side_effect=[
            LLMResponse(
                text=json.dumps(payload), tokens_in=1, tokens_out=1,
                latency_ms=10, model="m",
            ),
            RuntimeError("boom"),
        ]
    )

    proc = _build_processor(db, tg, llm)
    report = await proc.run_initial_bootstrap()

    assert report.total_chats == 2
    assert report.succeeded == 1
    assert report.failed == 1
    assert report.errors


async def test_catchup_persists_idempotently(db: Path, account_id: int) -> None:
    dialogs = [_make_dialog(50, unread=2)]
    msgs = [_make_msg(1, "hi"), _make_msg(2, "still there?")]
    tg = _FakeTG(dialogs, {50: msgs})
    llm = _llm_ok({
        "category": "cold", "confidence": 0.92,
        "flags": {"timewaster": False, "human_active": False},
        "reasoning": "x", "extracted_facts": {},
        "threat_detected": False, "threat_details": "",
    })
    proc = _build_processor(db, tg, llm)

    report1 = await proc.run_unread_catchup()
    assert report1.total_messages >= 1

    # Re-run — UNIQUE constraint prevents duplicates.
    report2 = await proc.run_unread_catchup()
    assert report2.total_messages == 0

    all_msgs = storage.get_recent_messages(db, account_id, 50, limit=100)
    assert len(all_msgs) == len(msgs)
