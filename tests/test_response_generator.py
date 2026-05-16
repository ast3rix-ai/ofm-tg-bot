from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from src import storage
from src.llm.client import LLMError, LLMResponse
from src.response_generator import ResponseGenerator
from src.safe_sender import (
    STATUS_RATE_LIMITED,
    STATUS_SENT,
    SendResult,
)

_PERSONA = "# Persona\nYou are Sophia. Casual, lowercase, short.\n"


def _persona_file(tmp_path: Path) -> Path:
    p = tmp_path / "persona.md"
    p.write_text(_PERSONA, encoding="utf-8")
    return p


def _llm(*texts: str) -> AsyncMock:
    """Mock LLM whose `generate` yields `texts` in order across calls."""
    llm = AsyncMock()
    responses = [
        LLMResponse(text=t, tokens_in=1, tokens_out=1, latency_ms=5, model="m")
        for t in texts
    ]
    llm.generate = AsyncMock(side_effect=responses)
    return llm


def _safe_sender(
    *, status: str = STATUS_SENT, rate_limit_state: str | None = "allowed"
) -> AsyncMock:
    """Mock SafeSender returning a fixed `SendResult`."""
    sender = AsyncMock()
    sender.send = AsyncMock(
        return_value=SendResult(
            status=status,
            chunks_sent=1 if status == STATUS_SENT else 0,
            total_duration_ms=10,
            rate_limit_state=rate_limit_state,
        )
    )
    return sender


def _seed(db: Path, account_id: int, chat_id: int = 1, **state: Any) -> None:
    storage.upsert_contact(
        db, account_id=account_id, chat_id=chat_id, tg_user_id=chat_id,
        username="u", first_name="U", last_name=None,
    )
    if state:
        storage.upsert_contact_state(
            db, account_id=account_id, chat_id=chat_id, **state
        )


def _make_generator(
    db: Path, tmp_path: Path, llm: AsyncMock, safe_sender: AsyncMock,
    retries: int = 2,
) -> ResponseGenerator:
    return ResponseGenerator(
        db_path=db,
        llm=llm,
        safe_sender=safe_sender,
        persona_path=_persona_file(tmp_path),
        max_retries=retries,
        temperature=0.85,
        max_tokens=200,
    )


# ---------- gate cases ----------


async def test_gate_bot_disabled(db: Path, account_id: int, tmp_path: Path) -> None:
    _seed(db, account_id, category="cold", bot_enabled=0)
    llm = _llm()
    sender = _safe_sender()
    gen = _make_generator(db, tmp_path, llm, sender)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "gated"
    assert result.gate_reason == "bot_disabled"
    llm.generate.assert_not_called()
    sender.send.assert_not_called()
    runs = storage.get_response_runs(db, account_id=account_id, chat_id=1)
    assert runs and runs[0]["outcome"] == "gated"
    assert runs[0]["gate_reason"] == "bot_disabled"


async def test_gate_category_paid(db: Path, account_id: int, tmp_path: Path) -> None:
    _seed(db, account_id, category="paid", bot_enabled=1)
    llm = _llm()
    sender = _safe_sender()
    gen = _make_generator(db, tmp_path, llm, sender)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "gated"
    assert result.gate_reason == "category_paid"
    llm.generate.assert_not_called()
    sender.send.assert_not_called()


async def test_gate_human_active(db: Path, account_id: int, tmp_path: Path) -> None:
    _seed(
        db, account_id, category="cold", bot_enabled=1,
        flags={"timewaster": False, "human_active": True},
    )
    llm = _llm()
    sender = _safe_sender()
    gen = _make_generator(db, tmp_path, llm, sender)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "gated"
    assert result.gate_reason == "human_active"
    sender.send.assert_not_called()


# ---------- happy path ----------


async def test_happy_path_delegates_to_safe_sender(
    db: Path, account_id: int, tmp_path: Path
) -> None:
    _seed(db, account_id, category="cold", bot_enabled=1)
    storage.insert_message(
        db, account_id=account_id, chat_id=1, tg_message_id=1, direction="in",
        sender_id=1, text="hey", media_type=None, raw_json="{}",
    )
    llm = _llm("heyy whats up 🥰")
    sender = _safe_sender()
    gen = _make_generator(db, tmp_path, llm, sender)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "sent"
    assert result.attempts == 1
    assert result.final_text == "heyy whats up 🥰"

    sender.send.assert_awaited_once()
    call = sender.send.await_args
    assert call.args[0] == 1                       # chat_id
    assert call.args[1] == "heyy whats up 🥰"       # text
    assert isinstance(call.args[2], int)           # run_id
    assert isinstance(call.args[3], bool)          # is_new_chat

    runs = storage.get_response_runs(db, account_id=account_id, chat_id=1)
    assert runs[0]["outcome"] == "sent"
    assert runs[0]["final_text"] == "heyy whats up 🥰"
    # run_id passed to SafeSender matches the persisted row.
    assert call.args[2] == runs[0]["id"]


async def test_quotes_stripped_before_send(
    db: Path, account_id: int, tmp_path: Path
) -> None:
    _seed(db, account_id, category="warm", bot_enabled=1)
    llm = _llm('"heyy cutie"')
    sender = _safe_sender()
    gen = _make_generator(db, tmp_path, llm, sender)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "sent"
    assert result.final_text == "heyy cutie"
    assert sender.send.await_args.args[1] == "heyy cutie"


async def test_rate_limited_run_is_gated(
    db: Path, account_id: int, tmp_path: Path
) -> None:
    _seed(db, account_id, category="cold", bot_enabled=1)
    llm = _llm("heyy")
    sender = _safe_sender(
        status=STATUS_RATE_LIMITED, rate_limit_state="daily_cap_exceeded"
    )
    gen = _make_generator(db, tmp_path, llm, sender)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "gated"
    assert result.gate_reason == "daily_cap_exceeded"
    runs = storage.get_response_runs(db, account_id=account_id, chat_id=1)
    assert runs[0]["outcome"] == "gated"


# ---------- re-roll ----------


async def test_reroll_on_ai_tell(db: Path, account_id: int, tmp_path: Path) -> None:
    _seed(db, account_id, category="cold", bot_enabled=1)
    llm = _llm("as an ai i think ur cute", "haha ur cute")
    sender = _safe_sender()
    gen = _make_generator(db, tmp_path, llm, sender)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "sent"
    assert result.attempts == 2
    assert result.final_text == "haha ur cute"
    assert llm.generate.await_count == 2

    runs = storage.get_response_runs(db, account_id=account_id, chat_id=1)
    assert runs[0]["attempts"] == 2
    assert len(runs[0]["raw_attempts"]) == 2


async def test_all_rerolls_rejected(
    db: Path, account_id: int, tmp_path: Path
) -> None:
    _seed(db, account_id, category="cold", bot_enabled=1)
    # max_retries=2 → 3 attempts, all AI-tells.
    llm = _llm("as an ai 1", "i cannot 2", "feel free to 3")
    sender = _safe_sender()
    gen = _make_generator(db, tmp_path, llm, sender, retries=2)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "validator_rejected_all_attempts"
    assert result.attempts == 3
    assert result.final_text is None
    sender.send.assert_not_called()

    runs = storage.get_response_runs(db, account_id=account_id, chat_id=1)
    assert runs[0]["outcome"] == "validator_rejected_all_attempts"
    assert len(runs[0]["raw_attempts"]) == 3


# ---------- send / LLM errors ----------


async def test_send_error_records_failed(
    db: Path, account_id: int, tmp_path: Path
) -> None:
    _seed(db, account_id, category="cold", bot_enabled=1)
    llm = _llm("heyy")
    sender = AsyncMock()
    sender.send = AsyncMock(side_effect=RuntimeError("boom"))
    gen = _make_generator(db, tmp_path, llm, sender)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "failed"
    runs = storage.get_response_runs(db, account_id=account_id, chat_id=1)
    assert runs[0]["outcome"] == "failed"


async def test_send_failed_status_records_failed(
    db: Path, account_id: int, tmp_path: Path
) -> None:
    _seed(db, account_id, category="cold", bot_enabled=1)
    llm = _llm("heyy")
    sender = AsyncMock()
    sender.send = AsyncMock(
        return_value=SendResult(
            status="send_failed", chunks_sent=0, total_duration_ms=5
        )
    )
    gen = _make_generator(db, tmp_path, llm, sender)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "failed"


async def test_llm_error_records_failed(
    db: Path, account_id: int, tmp_path: Path
) -> None:
    _seed(db, account_id, category="cold", bot_enabled=1)
    llm = AsyncMock()
    llm.generate = AsyncMock(side_effect=LLMError("ollama down"))
    sender = _safe_sender()
    gen = _make_generator(db, tmp_path, llm, sender)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "failed"
    sender.send.assert_not_called()


# ---------- new chat with no state row ----------


async def test_no_state_row_uses_default_enabled(
    db: Path, account_id: int, tmp_path: Path
) -> None:
    _seed(db, account_id)  # contact only, no contact_state
    llm = _llm("hey u")
    sender = _safe_sender()
    gen = _make_generator(db, tmp_path, llm, sender)

    result = await gen.generate(
        account_id=account_id, chat_id=1, triggering_message_id=None
    )
    assert result.outcome == "sent"
    sender.send.assert_awaited_once()


# ---------- persona mtime reload ----------


async def test_persona_reload_on_mtime_change(
    db: Path, account_id: int, tmp_path: Path
) -> None:
    _seed(db, account_id, category="cold", bot_enabled=1)
    persona = _persona_file(tmp_path)
    llm = _llm("one", "two")
    gen = ResponseGenerator(
        db_path=db, llm=llm, safe_sender=_safe_sender(),
        persona_path=persona, max_retries=2, temperature=0.85, max_tokens=200,
    )
    await gen.generate(account_id=account_id, chat_id=1, triggering_message_id=None)
    v1 = storage.get_response_runs(db, account_id=account_id, chat_id=1)[0][
        "persona_version"
    ]

    import os
    import time

    time.sleep(0.01)
    new_mtime = time.time() + 100
    persona.write_text(_PERSONA + "\nedited\n", encoding="utf-8")
    os.utime(persona, (new_mtime, new_mtime))

    await gen.generate(account_id=account_id, chat_id=1, triggering_message_id=None)
    v2 = storage.get_response_runs(db, account_id=account_id, chat_id=1)[0][
        "persona_version"
    ]
    assert v1 != v2
