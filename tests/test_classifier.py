from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src import storage
from src.classifier import Classifier
from src.llm.client import LLMError, LLMResponse
from src.notifier import Notifier
from src.signal_detector import SignalResult


def _llm_returning(payload: dict[str, object]) -> AsyncMock:
    llm = AsyncMock()
    llm.health = lambda: {"reachable": True, "model_loaded": True}
    llm.generate = AsyncMock(
        return_value=LLMResponse(
            text=json.dumps(payload),
            tokens_in=10,
            tokens_out=20,
            latency_ms=42,
            model="test-model",
        )
    )
    return llm


def _seed_chat(db: Path, account_id: int, chat_id: int = 1) -> None:
    storage.upsert_contact(
        db, account_id=account_id, chat_id=chat_id, tg_user_id=chat_id,
        username="u", first_name="U", last_name=None,
    )


def _signals(**overrides: object) -> SignalResult:
    defaults: dict[str, object] = {
        "is_price_inquiry": False,
        "is_greeting_only": False,
        "contains_payment_screenshot": False,
        "is_resurface": False,
        "is_threat": False,
        "threat_details": None,
    }
    defaults.update(overrides)
    return SignalResult(**defaults)  # type: ignore[arg-type]


async def test_greeting_fastpath_skips_llm(db: Path, account_id: int) -> None:
    _seed_chat(db, account_id)
    storage.insert_message(
        db, account_id=account_id, chat_id=1, tg_message_id=1, direction="in",
        sender_id=1, text="hi", media_type=None, raw_json="{}",
    )
    llm = _llm_returning({})
    notifier = Notifier(token=None, chat_id=None)
    clf = Classifier(db_path=db, llm=llm, notifier=notifier, confidence_threshold=0.6)

    result = await clf.classify_new_message(
        account_id=account_id, chat_id=1,
        new_message={"text": "hi"}, signal_result=_signals(is_greeting_only=True),
    )
    assert result.skipped_llm is True
    assert result.category == "cold"
    assert result.triggered_by == "rule:greeting"
    llm.generate.assert_not_called()

    runs = storage.get_classifier_runs(db, account_id=account_id, chat_id=1)
    assert runs and runs[0]["triggered_by"] == "rule:greeting"


async def test_llm_classification_persists_state_and_memory(
    db: Path, account_id: int
) -> None:
    _seed_chat(db, account_id)
    for i in range(2):
        storage.insert_message(
            db, account_id=account_id, chat_id=1, tg_message_id=i,
            direction="in", sender_id=1, text=f"msg{i}",
            media_type=None, raw_json="{}",
        )
    llm = _llm_returning({
        "category": "hot",
        "confidence": 0.91,
        "flags": {"timewaster": False, "human_active": False},
        "reasoning": "asks for menu",
        "extracted_facts": {"interest": "video"},
        "threat_detected": False,
        "threat_details": "",
    })
    notifier = Notifier(token=None, chat_id=None)
    clf = Classifier(db_path=db, llm=llm, notifier=notifier, confidence_threshold=0.6)

    result = await clf.classify_new_message(
        account_id=account_id, chat_id=1,
        new_message={"text": "menu?"},
        signal_result=_signals(is_price_inquiry=True),
    )
    assert result.category == "hot"
    assert result.confidence == 0.91

    state = storage.get_contact_state(db, account_id, 1)
    assert state is not None
    assert state["category"] == "hot"
    assert state["bot_enabled"] == 1  # first-time live → enabled by default

    memory = storage.get_contact_memory(db, account_id, 1)
    assert memory is not None
    assert memory["facts"].get("interest") == "video"


async def test_threat_detected_fires_alert(db: Path, account_id: int) -> None:
    _seed_chat(db, account_id)
    llm = _llm_returning({
        "category": "cold",
        "confidence": 0.85,
        "flags": {"timewaster": False, "human_active": False},
        "reasoning": "x",
        "extracted_facts": {},
        "threat_detected": True,
        "threat_details": "explicit threat keyword",
    })
    notifier = AsyncMock()
    notifier.alert = AsyncMock()
    clf = Classifier(db_path=db, llm=llm, notifier=notifier, confidence_threshold=0.6)

    await clf.classify_new_message(
        account_id=account_id, chat_id=1,
        new_message={"text": "..."}, signal_result=_signals(),
    )
    notifier.alert.assert_awaited()
    alerts = storage.list_operator_alerts(db, account_id=account_id)
    assert any(a["alert_type"] == "threat_detected" for a in alerts)


async def test_low_confidence_alert(db: Path, account_id: int) -> None:
    _seed_chat(db, account_id)
    llm = _llm_returning({
        "category": "warm",
        "confidence": 0.3,
        "flags": {"timewaster": False, "human_active": False},
        "reasoning": "ambiguous",
        "extracted_facts": {},
        "threat_detected": False,
        "threat_details": "",
    })
    notifier = Notifier(token=None, chat_id=None)
    clf = Classifier(db_path=db, llm=llm, notifier=notifier, confidence_threshold=0.6)

    await clf.classify_new_message(
        account_id=account_id, chat_id=1,
        new_message={"text": "hmm"}, signal_result=_signals(),
    )
    alerts = storage.list_operator_alerts(db, account_id=account_id)
    assert any(a["alert_type"] == "low_confidence" for a in alerts)


async def test_parse_failure_writes_alert(db: Path, account_id: int) -> None:
    _seed_chat(db, account_id)
    llm = AsyncMock()
    llm.health = lambda: {"reachable": True, "model_loaded": True}
    llm.generate = AsyncMock(
        return_value=LLMResponse(
            text="this is not json at all", tokens_in=1, tokens_out=1,
            latency_ms=10, model="x",
        )
    )
    notifier = Notifier(token=None, chat_id=None)
    clf = Classifier(db_path=db, llm=llm, notifier=notifier, confidence_threshold=0.6)

    result = await clf.classify_new_message(
        account_id=account_id, chat_id=1,
        new_message={"text": "anything"}, signal_result=_signals(),
    )
    assert result.confidence == 0.0
    alerts = storage.list_operator_alerts(db, account_id=account_id)
    assert any(
        a["alert_type"] == "classifier_parse_failure" for a in alerts
    )
    # State should NOT be corrupted by the parse-failure path.
    state = storage.get_contact_state(db, account_id, 1)
    assert state is None or state.get("category") is None


async def test_llm_error_writes_alert(db: Path, account_id: int) -> None:
    _seed_chat(db, account_id)
    llm = AsyncMock()
    llm.health = lambda: {"reachable": False, "model_loaded": False}
    llm.generate = AsyncMock(side_effect=LLMError("ollama down"))
    notifier = Notifier(token=None, chat_id=None)
    clf = Classifier(db_path=db, llm=llm, notifier=notifier, confidence_threshold=0.6)

    result = await clf.classify_new_message(
        account_id=account_id, chat_id=1,
        new_message={"text": "hi there i'd like to buy"},
        signal_result=_signals(),
    )
    assert result.confidence == 0.0
    alerts = storage.list_operator_alerts(db, account_id=account_id)
    assert any(
        a["alert_type"] == "classifier_parse_failure" for a in alerts
    )


async def test_bootstrap_persists_state_with_bot_disabled(
    db: Path, account_id: int
) -> None:
    _seed_chat(db, account_id)
    llm = _llm_returning({
        "category": "warm",
        "funnel_stage_inferred": "small-talk",
        "confidence": 0.78,
        "flags": {"timewaster": False, "human_active": False},
        "summary": "Chatted for a few days, casual.",
        "extracted_facts": {"age_claim": 28},
        "reasoning": "history shows banter, no buying",
        "threat_detected": False,
        "threat_details": "",
    })
    notifier = Notifier(token=None, chat_id=None)
    clf = Classifier(db_path=db, llm=llm, notifier=notifier, confidence_threshold=0.6)

    history = [
        {"direction": "in", "text": "hey"},
        {"direction": "out", "text": "hi"},
        {"direction": "in", "text": "how was your day"},
    ]
    result = await clf.bootstrap_chat(
        account_id=account_id, chat_id=1, history_messages=history
    )
    assert result.category == "warm"

    state = storage.get_contact_state(db, account_id, 1)
    assert state is not None
    assert state["category"] == "warm"
    assert state["bot_enabled"] == 0
    assert state["bootstrap_completed_at"] is not None

    runs = storage.get_classifier_runs(db, account_id=account_id, chat_id=1)
    assert runs and runs[0]["triggered_by"] == "bootstrap"


async def test_threat_dedup_within_hour(db: Path, account_id: int) -> None:
    _seed_chat(db, account_id)
    llm = _llm_returning({
        "category": "cold",
        "confidence": 0.85,
        "flags": {"timewaster": False, "human_active": False},
        "reasoning": "x",
        "extracted_facts": {},
        "threat_detected": True,
        "threat_details": "threat",
    })
    notifier = AsyncMock()
    notifier.alert = AsyncMock()
    clf = Classifier(db_path=db, llm=llm, notifier=notifier, confidence_threshold=0.6)

    await clf.classify_new_message(
        account_id=account_id, chat_id=1,
        new_message={"text": "x"}, signal_result=_signals(),
    )
    await clf.classify_new_message(
        account_id=account_id, chat_id=1,
        new_message={"text": "y"}, signal_result=_signals(),
    )
    threat_alerts = [
        a for a in storage.list_operator_alerts(db, account_id=account_id)
        if a["alert_type"] == "threat_detected"
    ]
    assert len(threat_alerts) == 1


@pytest.mark.parametrize(
    "bad",
    [
        '{"category": "not-a-cat", "confidence": 0.5}',
        '{"category": "hot", "confidence": "high"}',
        '{"category": "hot", "confidence": 0.5, "flags": []}',
    ],
)
async def test_parser_rejects_bad_outputs(db: Path, account_id: int, bad: str) -> None:
    from src.classifier import _parse_classifier_output
    with pytest.raises(ValueError):
        _parse_classifier_output(bad)
