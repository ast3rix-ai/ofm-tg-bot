from __future__ import annotations

import re
from dataclasses import dataclass

# Patterns that betray a generated / out-of-character reply. Case-insensitive.
AI_TELL_PATTERNS: list[str] = [
    r"\bas an ai\b",
    r"\bi cannot\b",
    r"\bi'?m an? (ai|assistant|language model|chatbot|bot)\b",
    r"\bi'?m here to help\b",
    r"\bfeel free to\b",
    r"\blet me know if\b",
    r"\bhappy to assist\b",
    r"\bhow may i assist\b",
    r"\bwhat'?s shakin'?\b",
    r"\bxoxo\b",
    r"\bcheers\b\s*[,!.]?\s*$",            # sign-off
    r"^\s*\*[^*]+\*\s*$",                  # stage directions like *smiles*
    r"\bnarrator\b",
    r"—",                                  # em-dash, classic AI tell
]

_COMPILED: list[tuple[str, re.Pattern[str]]] = [
    (p, re.compile(p, re.IGNORECASE)) for p in AI_TELL_PATTERNS
]

_MAX_CHARS = 280
_QUOTE_CHARS = ("\"", "'", "“", "”", "‘", "’")


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of an AI-tell check on a generated reply."""

    valid: bool
    reason: str | None


def validate_response(text: str) -> ValidationResult:
    """Check generated text for AI-tells and formatting that breaks the illusion.

    Args:
        text: The candidate reply.

    Returns:
        A `ValidationResult`. `valid` is True only if no rejection rule fired.
    """
    if not text or not text.strip():
        return ValidationResult(valid=False, reason="empty response")

    if len(text) > _MAX_CHARS:
        return ValidationResult(
            valid=False, reason=f"too long ({len(text)} > {_MAX_CHARS} chars)"
        )

    if "\n\n" in text:
        return ValidationResult(valid=False, reason="contains double newline (prose)")

    if text.lstrip().startswith(_QUOTE_CHARS):
        return ValidationResult(
            valid=False, reason="starts with a quote character"
        )

    for raw, pattern in _COMPILED:
        if pattern.search(text):
            return ValidationResult(
                valid=False, reason=f"matched AI-tell pattern: {raw}"
            )

    return ValidationResult(valid=True, reason=None)
