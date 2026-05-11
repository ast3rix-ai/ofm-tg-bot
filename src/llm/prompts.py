from __future__ import annotations

import json
from typing import Any

CATEGORY_TAXONOMY = """\
Categories (exactly one per chat — funnel stage):
- cold: greetings/small talk, no buying signals yet
- warm: engaged, complimenting/flirting, no explicit ask
- hot: explicit buying signals — asks about content, price, menu
- negotiating: specific offer being discussed, price on the table
- paid: payment confirmed (screenshot, "sent", tip received)
- post_purchase: delivery done, upsell window open

Flags (zero or more — orthogonal to category):
- timewaster: chats without intent to buy; explicit "I don't pay" type
- human_active: operator has taken over this chat

Threats (not a category): doxing references, real-world threats, self-harm
mentions toward the model or customer. Detected via `threat_detected: true`
plus `threat_details`. Does NOT change the category.
"""


_CLASSIFIER_OUTPUT_SCHEMA = """\
Respond with ONE JSON object — no prose, no markdown fences, no comments.
Schema:
{
  "category": "<one of: cold | warm | hot | negotiating | paid | post_purchase>",
  "confidence": <float 0.0–1.0>,
  "flags": { "timewaster": <bool>, "human_active": <bool> },
  "reasoning": "<one short sentence — why this category>",
  "extracted_facts": { "<key>": "<value>", ... },
  "threat_detected": <bool>,
  "threat_details": "<string, empty if no threat>"
}
"""


_BOOTSTRAP_OUTPUT_SCHEMA = """\
Respond with ONE JSON object — no prose, no markdown fences, no comments.
Schema:
{
  "category": "<one of: cold | warm | hot | negotiating | paid | post_purchase>",
  "funnel_stage_inferred": "<short string — finer-grained guess>",
  "confidence": <float 0.0–1.0>,
  "flags": { "timewaster": <bool>, "human_active": <bool> },
  "summary": "<2-4 sentences: relationship history, outstanding obligations, stated preferences>",
  "extracted_facts": { "<key>": "<value>", ... },
  "reasoning": "<one short sentence>",
  "threat_detected": <bool>,
  "threat_details": "<string, empty if no threat>"
}
"""


# TODO(phase-8): replace synthetic examples with real OF DM samples once
# operator provides them.
_CLASSIFIER_FEWSHOTS: list[dict[str, Any]] = [
    {
        "messages": [{"dir": "in", "text": "hi"}],
        "output": {
            "category": "cold", "confidence": 0.92,
            "flags": {"timewaster": False, "human_active": False},
            "reasoning": "Bare greeting, no signal yet.",
            "extracted_facts": {},
            "threat_detected": False, "threat_details": "",
        },
    },
    {
        "messages": [
            {"dir": "in", "text": "hey love, how much for a custom?"},
        ],
        "output": {
            "category": "hot", "confidence": 0.95,
            "flags": {"timewaster": False, "human_active": False},
            "reasoning": "Direct pricing inquiry on customs.",
            "extracted_facts": {"interest": "custom video"},
            "threat_detected": False, "threat_details": "",
        },
    },
    {
        "messages": [
            {"dir": "in", "text": "we've been chatting for an hour, "
             "I just like talking to pretty girls, not gonna spend"},
        ],
        "output": {
            "category": "cold", "confidence": 0.83,
            "flags": {"timewaster": True, "human_active": False},
            "reasoning": "Explicit non-buying intent.",
            "extracted_facts": {"non_buyer": True},
            "threat_detected": False, "threat_details": "",
        },
    },
    {
        "messages": [
            {"dir": "out", "text": "ok bb that's $50 for the 5min custom"},
            {"dir": "in", "text": "sent!"},
        ],
        "output": {
            "category": "paid", "confidence": 0.9,
            "flags": {"timewaster": False, "human_active": False},
            "reasoning": "Customer confirms payment after agreed price.",
            "extracted_facts": {"last_purchase_cents": 5000,
                                "last_purchase_kind": "custom video"},
            "threat_detected": False, "threat_details": "",
        },
    },
]


def _format_history(messages: list[dict[str, Any]], max_chars: int = 6000) -> str:
    """Render a message list as a transcript suitable for the prompt.

    Trims oldest messages first if the rendered length exceeds `max_chars`.
    """
    lines: list[str] = []
    for m in messages:
        direction = m.get("direction") or m.get("dir") or "in"
        prefix = "FAN" if direction == "in" else "ME"
        text = (m.get("text") or "").strip()
        media = m.get("media_type")
        suffix = f" [{media}]" if media and not text else (
            f" [+{media}]" if media else ""
        )
        if not text and not media:
            continue
        lines.append(f"{prefix}: {text}{suffix}")
    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered
    while lines and len("\n".join(lines)) > max_chars:
        lines.pop(0)
    return "[...older messages trimmed...]\n" + "\n".join(lines)


def _fewshot_block() -> str:
    parts: list[str] = []
    for i, ex in enumerate(_CLASSIFIER_FEWSHOTS, 1):
        transcript = _format_history(
            [{"direction": m["dir"], "text": m["text"]} for m in ex["messages"]]
        )
        out = json.dumps(ex["output"], ensure_ascii=False)
        parts.append(f"### Example {i}\nTranscript:\n{transcript}\n\nOutput:\n{out}")
    return "\n\n".join(parts)


def classifier_prompt(
    *,
    recent_messages: list[dict[str, Any]],
    contact_memory: dict[str, Any] | None,
    triggered_by: str,
) -> str:
    """Build the classifier prompt for a single inbound-classification turn."""
    memory_block = "(no memory yet)"
    if contact_memory:
        facts = contact_memory.get("facts") or {}
        summary = (contact_memory.get("summary") or "").strip()
        memory_block = "Distilled facts: " + json.dumps(
            facts, ensure_ascii=False, default=str
        )
        if summary:
            memory_block += f"\nSummary so far: {summary}"

    transcript = _format_history(recent_messages)
    if not transcript:
        transcript = "(no prior messages)"

    return f"""You classify Telegram DMs to an OnlyFans creator.
You must respond in JSON only.

{CATEGORY_TAXONOMY}

{_CLASSIFIER_OUTPUT_SCHEMA}

## Few-shot examples
{_fewshot_block()}

## Now classify
This call was triggered by: {triggered_by}

Contact memory:
{memory_block}

Transcript (oldest first):
{transcript}

JSON:"""


def bootstrap_prompt(*, full_history: list[dict[str, Any]]) -> str:
    """Build the prompt for first-time history ingestion."""
    transcript = _format_history(full_history, max_chars=10000)
    if not transcript:
        transcript = "(no prior messages)"
    return f"""You are reading the full DM history between an OnlyFans creator and a fan.
Summarize the relationship and classify the current funnel stage.
Respond in JSON only.

{CATEGORY_TAXONOMY}

{_BOOTSTRAP_OUTPUT_SCHEMA}

Transcript (oldest first):
{transcript}

JSON:"""


def summary_update_prompt(
    *, current_summary: str, new_messages: list[dict[str, Any]]
) -> str:
    """Build the prompt for rolling-summary regeneration (Phase 5 will call this)."""
    transcript = _format_history(new_messages)
    return f"""You maintain a running 2-4 sentence summary of an OnlyFans DM thread.
Update the summary with the new messages. Preserve concrete facts (names,
money signals, prior purchases, stated preferences). Drop pleasantries.
Respond with the updated summary as plain text — no JSON.

Previous summary:
{current_summary or "(empty)"}

New messages:
{transcript}

Updated summary:"""
