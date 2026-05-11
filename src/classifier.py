from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from src import storage
from src.llm.client import LLMClient, LLMError
from src.llm.prompts import bootstrap_prompt, classifier_prompt
from src.notifier import Notifier
from src.signal_detector import SignalResult

_VALID_CATEGORIES = frozenset(
    {"cold", "warm", "hot", "negotiating", "paid", "post_purchase"}
)


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True)
class ClassifierResult:
    category: str
    confidence: float
    flags: dict[str, Any]
    reasoning: str
    extracted_facts: dict[str, Any]
    threat_detected: bool
    threat_details: str
    triggered_by: str
    latency_ms: int
    raw_output: str
    skipped_llm: bool = False


@dataclass(frozen=True)
class BootstrapResult:
    category: str
    funnel_stage_inferred: str
    confidence: float
    flags: dict[str, Any]
    summary: str
    extracted_facts: dict[str, Any]
    threat_detected: bool
    threat_details: str
    raw_output: str
    latency_ms: int
    message_count: int


def _strip_json(text: str) -> str:
    """Extract a JSON object from a model response — strip code fences if any."""
    t = text.strip()
    if t.startswith("```"):
        # Drop the opening fence (with optional language tag) and trailing fence.
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1 :]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _parse_classifier_output(raw: str) -> dict[str, Any]:
    """Parse + validate model output. Raises ValueError if unusable."""
    payload = json.loads(_strip_json(raw))
    if not isinstance(payload, dict):
        raise ValueError("classifier output is not a JSON object")
    category = str(payload.get("category", "")).strip().lower()
    if category not in _VALID_CATEGORIES:
        raise ValueError(f"invalid category: {category!r}")
    confidence_raw = payload.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"non-numeric confidence: {confidence_raw!r}"
        ) from exc
    confidence = max(0.0, min(1.0, confidence))
    flags_raw_in = payload.get("flags")
    if flags_raw_in is not None and not isinstance(flags_raw_in, dict):
        raise ValueError("flags is not an object")
    flags_raw = flags_raw_in or {}
    flags = {
        "timewaster": bool(flags_raw.get("timewaster", False)),
        "human_active": bool(flags_raw.get("human_active", False)),
    }
    facts_raw = payload.get("extracted_facts") or {}
    if not isinstance(facts_raw, dict):
        facts_raw = {}
    return {
        "category": category,
        "confidence": confidence,
        "flags": flags,
        "reasoning": str(payload.get("reasoning") or "").strip(),
        "extracted_facts": facts_raw,
        "threat_detected": bool(payload.get("threat_detected", False)),
        "threat_details": str(payload.get("threat_details") or "").strip(),
        "summary": str(payload.get("summary") or "").strip(),
        "funnel_stage_inferred": str(
            payload.get("funnel_stage_inferred") or ""
        ).strip(),
    }


class Classifier:
    """Hybrid classifier: rules-first fast path + LLM for ambiguous turns."""

    def __init__(
        self,
        *,
        db_path: Path,
        llm: LLMClient,
        notifier: Notifier,
        confidence_threshold: float,
        history_window: int = 30,
    ) -> None:
        self._db_path = db_path
        self._llm = llm
        self._notifier = notifier
        self._threshold = confidence_threshold
        self._history_window = history_window
        self._log = logger.bind(module=__name__)

    # ---------- new-message path ----------

    async def classify_new_message(
        self,
        *,
        account_id: int,
        chat_id: int,
        new_message: dict[str, Any],
        signal_result: SignalResult,
    ) -> ClassifierResult:
        prior_state = storage.get_contact_state(self._db_path, account_id, chat_id)
        recent = storage.get_recent_messages(
            self._db_path, account_id, chat_id, limit=self._history_window
        )
        memory = storage.get_contact_memory(self._db_path, account_id, chat_id)
        category_before: str | None = (
            prior_state.get("category") if prior_state else None
        )
        flags_before: dict[str, Any] = (
            prior_state.get("flags") or {} if prior_state else {}
        )
        has_prior_classification = bool(prior_state and category_before)

        triggered_by = "new_message"
        if signal_result.is_resurface and has_prior_classification:
            triggered_by = "resurface"

        # --- fast path: greeting-only on a fresh chat ---
        if (
            signal_result.is_greeting_only
            and not has_prior_classification
            and len(recent) <= 2
        ):
            result = ClassifierResult(
                category="cold",
                confidence=0.9,
                flags={"timewaster": False, "human_active": False},
                reasoning="Greeting-only opener on fresh chat (rule fast-path).",
                extracted_facts={},
                threat_detected=signal_result.is_threat,
                threat_details=signal_result.threat_details or "",
                triggered_by="rule:greeting",
                latency_ms=0,
                raw_output="",
                skipped_llm=True,
            )
            await self._persist(
                account_id=account_id,
                chat_id=chat_id,
                result=result,
                category_before=category_before,
                flags_before=flags_before,
                signal_result=signal_result,
                input_message_count=len(recent),
            )
            return result

        # --- LLM path ---
        prompt = classifier_prompt(
            recent_messages=recent,
            contact_memory=memory,
            triggered_by=triggered_by,
        )

        raw_output = ""
        latency_ms = 0
        parsed: dict[str, Any] | None = None
        parse_error: str | None = None
        for attempt in (1, 2):
            try:
                response = await self._llm.generate(
                    prompt, response_format="json", temperature=0.2, max_tokens=512
                )
                raw_output = response.text
                latency_ms = response.latency_ms
                parsed = _parse_classifier_output(raw_output)
                break
            except LLMError as exc:
                parse_error = str(exc)
                self._log.warning(
                    "Classifier LLM call failed",
                    error=str(exc),
                    chat_id=chat_id,
                    attempt=attempt,
                )
                break
            except (ValueError, json.JSONDecodeError) as exc:
                parse_error = str(exc)
                self._log.warning(
                    "Classifier output unparseable — retrying",
                    error=str(exc),
                    chat_id=chat_id,
                    attempt=attempt,
                    raw=raw_output[:300],
                )
                prompt = (
                    prompt
                    + "\n\nReminder: output ONLY a single JSON object matching"
                    " the schema. No prose. No code fences."
                )

        if parsed is None:
            storage.insert_operator_alert(
                self._db_path,
                account_id=account_id,
                chat_id=chat_id,
                alert_type="classifier_parse_failure",
                severity="error",
                message=f"Classifier failed: {parse_error or 'unknown error'}",
                payload={
                    "raw_output": raw_output[:2000],
                    "triggered_by": triggered_by,
                },
            )
            storage.insert_classifier_run(
                self._db_path,
                account_id=account_id,
                chat_id=chat_id,
                triggered_by=triggered_by,
                input_message_count=len(recent),
                category_before=category_before,
                category_after=None,
                confidence=None,
                flags_before=flags_before,
                flags_after=None,
                raw_llm_output=raw_output or None,
                latency_ms=latency_ms or None,
            )
            return ClassifierResult(
                category=category_before or "cold",
                confidence=0.0,
                flags=flags_before or {"timewaster": False, "human_active": False},
                reasoning=f"Classifier failed: {parse_error or 'unknown error'}",
                extracted_facts={},
                threat_detected=signal_result.is_threat,
                threat_details=signal_result.threat_details or "",
                triggered_by=triggered_by,
                latency_ms=latency_ms,
                raw_output=raw_output,
                skipped_llm=False,
            )

        result = ClassifierResult(
            category=parsed["category"],
            confidence=parsed["confidence"],
            flags=parsed["flags"],
            reasoning=parsed["reasoning"],
            extracted_facts=parsed["extracted_facts"],
            threat_detected=parsed["threat_detected"] or signal_result.is_threat,
            threat_details=parsed["threat_details"]
            or (signal_result.threat_details or ""),
            triggered_by=triggered_by,
            latency_ms=latency_ms,
            raw_output=raw_output,
            skipped_llm=False,
        )

        await self._persist(
            account_id=account_id,
            chat_id=chat_id,
            result=result,
            category_before=category_before,
            flags_before=flags_before,
            signal_result=signal_result,
            input_message_count=len(recent),
        )
        return result

    # ---------- bootstrap path ----------

    async def bootstrap_chat(
        self,
        *,
        account_id: int,
        chat_id: int,
        history_messages: list[dict[str, Any]],
    ) -> BootstrapResult:
        prompt = bootstrap_prompt(full_history=history_messages)
        try:
            response = await self._llm.generate(
                prompt, response_format="json", temperature=0.2, max_tokens=2048
            )
        except LLMError as exc:
            storage.insert_operator_alert(
                self._db_path,
                account_id=account_id,
                chat_id=chat_id,
                alert_type="bootstrap_failed",
                severity="error",
                message=f"Bootstrap LLM unavailable: {exc}",
                payload={"history_count": len(history_messages)},
            )
            raise

        raw_output = response.text
        try:
            parsed = _parse_classifier_output(raw_output)
        except (ValueError, json.JSONDecodeError) as exc:
            storage.insert_operator_alert(
                self._db_path,
                account_id=account_id,
                chat_id=chat_id,
                alert_type="bootstrap_failed",
                severity="error",
                message=f"Bootstrap output unparseable: {exc}",
                payload={"raw_output": raw_output[:2000]},
            )
            raise

        now = _utcnow_iso()
        storage.upsert_contact_state(
            self._db_path,
            account_id=account_id,
            chat_id=chat_id,
            category=parsed["category"],
            funnel_stage=parsed["funnel_stage_inferred"] or None,
            flags=parsed["flags"],
            last_classified_at=now,
            last_classifier_confidence=parsed["confidence"],
            classifier_metadata={
                "triggered_by": "bootstrap",
                "raw": parsed,
            },
            bot_enabled=0,
            bootstrap_completed_at=now,
        )
        storage.upsert_contact_memory(
            self._db_path,
            account_id=account_id,
            chat_id=chat_id,
            facts=parsed["extracted_facts"] or {},
            summary=parsed["summary"] or "",
            summary_message_count=len(history_messages),
            last_summarized_at=now,
        )
        storage.insert_classifier_run(
            self._db_path,
            account_id=account_id,
            chat_id=chat_id,
            triggered_by="bootstrap",
            input_message_count=len(history_messages),
            category_before=None,
            category_after=parsed["category"],
            confidence=parsed["confidence"],
            flags_before=None,
            flags_after=parsed["flags"],
            raw_llm_output=raw_output,
            latency_ms=response.latency_ms,
        )
        if parsed["threat_detected"]:
            await self._fire_threat_alert(
                account_id=account_id,
                chat_id=chat_id,
                details=parsed["threat_details"] or "(no details)",
                source="bootstrap",
            )

        return BootstrapResult(
            category=parsed["category"],
            funnel_stage_inferred=parsed["funnel_stage_inferred"],
            confidence=parsed["confidence"],
            flags=parsed["flags"],
            summary=parsed["summary"],
            extracted_facts=parsed["extracted_facts"],
            threat_detected=parsed["threat_detected"],
            threat_details=parsed["threat_details"],
            raw_output=raw_output,
            latency_ms=response.latency_ms,
            message_count=len(history_messages),
        )

    # ---------- helpers ----------

    async def _persist(
        self,
        *,
        account_id: int,
        chat_id: int,
        result: ClassifierResult,
        category_before: str | None,
        flags_before: dict[str, Any],
        signal_result: SignalResult,
        input_message_count: int,
    ) -> None:
        now = _utcnow_iso()
        state_fields: dict[str, Any] = {
            "category": result.category,
            "flags": result.flags,
            "last_classified_at": now,
            "last_classifier_confidence": result.confidence,
            "classifier_metadata": {
                "triggered_by": result.triggered_by,
                "reasoning": result.reasoning,
                "skipped_llm": result.skipped_llm,
            },
        }
        if signal_result.is_resurface:
            state_fields["last_resurface_at"] = now
        existing_state = storage.get_contact_state(
            self._db_path, account_id, chat_id
        )
        if existing_state is None:
            # No prior row — chat seen live from message 1; bot is enabled by default.
            state_fields["bot_enabled"] = 1

        storage.upsert_contact_state(
            self._db_path,
            account_id=account_id,
            chat_id=chat_id,
            **state_fields,
        )

        # Merge extracted_facts into existing memory facts.
        if result.extracted_facts:
            mem = storage.get_contact_memory(
                self._db_path, account_id, chat_id
            ) or {}
            merged_facts: dict[str, Any] = dict(mem.get("facts") or {})
            merged_facts.update(result.extracted_facts)
            storage.upsert_contact_memory(
                self._db_path,
                account_id=account_id,
                chat_id=chat_id,
                facts=merged_facts,
            )

        storage.insert_classifier_run(
            self._db_path,
            account_id=account_id,
            chat_id=chat_id,
            triggered_by=result.triggered_by,
            input_message_count=input_message_count,
            category_before=category_before,
            category_after=result.category,
            confidence=result.confidence,
            flags_before=flags_before,
            flags_after=result.flags,
            raw_llm_output=result.raw_output or None,
            latency_ms=result.latency_ms or None,
        )

        if result.threat_detected:
            await self._fire_threat_alert(
                account_id=account_id,
                chat_id=chat_id,
                details=result.threat_details or "(no details)",
                source=result.triggered_by,
            )

        if (
            not result.skipped_llm
            and result.confidence < self._threshold
        ):
            storage.insert_operator_alert(
                self._db_path,
                account_id=account_id,
                chat_id=chat_id,
                alert_type="low_confidence",
                severity="warn",
                message=(
                    f"Low classifier confidence ({result.confidence:.2f}) "
                    f"for category {result.category!r}"
                ),
                payload={"reasoning": result.reasoning},
            )

    async def _fire_threat_alert(
        self,
        *,
        account_id: int,
        chat_id: int,
        details: str,
        source: str,
    ) -> None:
        # Dedupe: 1 alert per chat per hour, regardless of source.
        since = (datetime.now(UTC) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        recent_count = storage.count_recent_alerts(
            self._db_path,
            account_id=account_id,
            chat_id=chat_id,
            alert_type="threat_detected",
            since_iso=since,
        )
        if recent_count > 0:
            self._log.info(
                "Threat already alerted within last hour — skipping dup",
                chat_id=chat_id,
            )
            return
        storage.insert_operator_alert(
            self._db_path,
            account_id=account_id,
            chat_id=chat_id,
            alert_type="threat_detected",
            severity="error",
            message=f"Threat detected ({source}): {details}",
            payload={"source": source, "details": details},
        )
        await self._notifier.alert(
            f"🚨 Threat detected on chat {chat_id}: {details}",
            severity="error",
            key=f"threat_{account_id}_{chat_id}",
        )
