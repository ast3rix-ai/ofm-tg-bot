from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import storage

# --------------------- regex / keyword definitions ---------------------

_PRICE_PATTERNS = [
    r"\bhow\s+much\b",
    r"\bprice\s*(?:list)?\b",
    r"\bcost\b",
    r"\brate(?:s)?\b",
    r"\bmenu\b",
    r"\bppv\b",
    r"\bsub(?:scription)?\s+(?:cost|price)\b",
    r"\$\s?\d",
    r"€\s?\d",
    r"£\s?\d",
]
_PRICE_REGEX = re.compile("|".join(_PRICE_PATTERNS), re.IGNORECASE)

_GREETING_PATTERNS = [
    r"^\s*(hi|hey|hello|yo|hii+|heyy+|sup|wassup|wsp|good\s+(morning|afternoon|evening))",
    r"^\s*(hi|hey|hello)\s+(babe|baby|love|gorgeous|beautiful|sexy|cutie)",
    r"^\s*ahoj\b",  # Slovak greeting
    r"^\s*čau\b",
]
_GREETING_REGEX = re.compile("|".join(_GREETING_PATTERNS), re.IGNORECASE)

# Payment-confirmation captions on photo attachments.
_PAYMENT_CAPTION_PATTERNS = [
    r"\bsent\b",
    r"\bpaid\b",
    r"\bdone\b",
    r"\btip(?:ped)?\b",
    r"\bhere\s+you\s+go\b",
]
_PAYMENT_CAPTION_REGEX = re.compile("|".join(_PAYMENT_CAPTION_PATTERNS), re.IGNORECASE)

# Conservative threat keywords. False negatives are acceptable;
# false positives are not.
#
# Categories:
#   - Real-world doxing / location threats: "I know where you live", explicit
#     real-name/address claims.
#   - Self-harm directed at the model OR by the customer (e.g. suicide ideation).
#   - Direct violent threats.
# We avoid generic profanity, sexual aggression that's part of consensual RP
# context, and ambiguous "kill it" idioms. These are policed elsewhere.
_THREAT_PATTERNS: list[tuple[str, str]] = [
    (r"\bi\s+know\s+where\s+you\s+live\b", "explicit doxing/location threat"),
    (r"\bcome\s+to\s+your\s+(house|apartment|place)\b", "physical-approach threat"),
    (r"\b(?:i\s+will|im\s+going\s+to|gonna)\s+kill\s+you\b",
     "explicit death threat"),
    (r"\bsuicid(?:e|al)\b", "self-harm reference"),
    (r"\bkill\s+myself\b", "self-harm reference"),
    (r"\bi\s+want\s+to\s+die\b", "self-harm reference"),
    (r"\brape\s+you\b", "sexual violence threat"),
    (r"\bdox(?:x|x?ed|xing)\b", "doxing reference"),
    (r"\byour\s+real\s+name\s+is\b", "identity-exposure threat"),
]
_THREAT_REGEXES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), label) for p, label in _THREAT_PATTERNS
]


# --------------------- result dataclass ---------------------


@dataclass(frozen=True)
class SignalResult:
    """Output of the deterministic signal pass on one inbound message."""

    is_price_inquiry: bool = False
    is_greeting_only: bool = False
    contains_payment_screenshot: bool = False
    is_resurface: bool = False
    is_threat: bool = False
    threat_details: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def any_signal(self) -> bool:
        return any(
            (
                self.is_price_inquiry,
                self.is_greeting_only,
                self.contains_payment_screenshot,
                self.is_resurface,
                self.is_threat,
            )
        )


# --------------------- individual detectors ---------------------


def is_price_inquiry(text: str | None) -> bool:
    if not text:
        return False
    return bool(_PRICE_REGEX.search(text))


def is_greeting_only(text: str | None) -> bool:
    """True when the text looks like an opener and nothing else.

    Heuristic: short (<= 25 chars after stripping), matches a greeting
    pattern, and contains no '?' or money-shaped tokens.
    """
    if not text:
        return False
    t = text.strip()
    if not t or len(t) > 25:
        return False
    if "?" in t or _PRICE_REGEX.search(t):
        return False
    return bool(_GREETING_REGEX.search(t))


def contains_payment_screenshot(message: dict[str, Any]) -> bool:
    """True if the message has a photo attachment AND a paymentish caption (or none)."""
    media_type = (message.get("media_type") or "").lower()
    if "photo" not in media_type:
        return False
    text = (message.get("text") or "").strip()
    if not text:
        return True
    return bool(_PAYMENT_CAPTION_REGEX.search(text))


def detect_threat(text: str | None) -> tuple[bool, str | None]:
    """Return (detected, joined label string) over the conservative threat set."""
    if not text:
        return (False, None)
    hits = [label for rx, label in _THREAT_REGEXES if rx.search(text)]
    if not hits:
        return (False, None)
    return (True, "; ".join(sorted(set(hits))))


def detect_resurface(
    *,
    db_path: Path,
    account_id: int,
    chat_id: int,
    now_iso: str,
    threshold_days: int,
) -> bool:
    """True if the previous inbound was more than `threshold_days` ago."""
    last = storage.get_last_inbound_at(db_path, account_id, chat_id)
    if last is None:
        return False
    try:
        previous = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return False
    try:
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except ValueError:
        now = datetime.now(UTC)
    gap_days = (now - previous).total_seconds() / 86400.0
    return gap_days > threshold_days


def run_signals(
    message: dict[str, Any],
    *,
    db_path: Path,
    account_id: int,
    chat_id: int,
    now_iso: str,
    resurface_threshold_days: int = 14,
) -> SignalResult:
    """Run all deterministic detectors for a single inbound message."""
    text = message.get("text")
    threat_hit, threat_details = detect_threat(text)
    return SignalResult(
        is_price_inquiry=is_price_inquiry(text),
        is_greeting_only=is_greeting_only(text),
        contains_payment_screenshot=contains_payment_screenshot(message),
        is_resurface=detect_resurface(
            db_path=db_path,
            account_id=account_id,
            chat_id=chat_id,
            now_iso=now_iso,
            threshold_days=resurface_threshold_days,
        ),
        is_threat=threat_hit,
        threat_details=threat_details,
        metadata={"detected_at": now_iso},
    )
