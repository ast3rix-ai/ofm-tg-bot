from __future__ import annotations

import pytest

from src.output_validator import validate_response

# --- positive (rejected) cases, one per AI-tell pattern ---

_REJECTED: list[str] = [
    "as an AI i think u r great",
    "i cannot do that babe",
    "i'm an assistant here for u",
    "im a bot lol",
    "i'm here to help with anything",
    "feel free to ask me stuff",
    "let me know if u need anything",
    "happy to assist you today",
    "how may i assist you",
    "what's shakin",
    "whats shakin'",
    "ok love xoxo",
    "ok talk soon, cheers",
    "*smiles softly*",
    "the narrator pauses here",
    "i loved it — so much",
]


@pytest.mark.parametrize("text", _REJECTED)
def test_ai_tells_are_rejected(text: str) -> None:
    result = validate_response(text)
    assert result.valid is False
    assert result.reason is not None


# --- negative (accepted) cases ---

_ACCEPTED: list[str] = [
    "heyy whats up",
    "ngl i missed u 🥰",
    "depends what u want 😏",
    "haha stop it",
    "u free later tonight",
    "cheers is a fun bar tho",  # "cheers" not at the end → not a sign-off
]


@pytest.mark.parametrize("text", _ACCEPTED)
def test_natural_replies_are_accepted(text: str) -> None:
    result = validate_response(text)
    assert result.valid is True
    assert result.reason is None


def test_empty_string_rejected() -> None:
    assert validate_response("").valid is False


def test_whitespace_only_rejected() -> None:
    assert validate_response("   \n  \t ").valid is False


def test_emoji_only_accepted() -> None:
    assert validate_response("😏").valid is True


def test_too_long_rejected() -> None:
    result = validate_response("a" * 281)
    assert result.valid is False
    assert "too long" in (result.reason or "")


def test_length_at_limit_accepted() -> None:
    assert validate_response("a" * 280).valid is True


def test_double_newline_rejected() -> None:
    result = validate_response("first thought\n\nsecond paragraph")
    assert result.valid is False
    assert "double newline" in (result.reason or "")


def test_single_newline_accepted() -> None:
    assert validate_response("first line\nsecond line").valid is True


@pytest.mark.parametrize("quote", ['"', "'", "“", "‘"])
def test_starts_with_quote_rejected(quote: str) -> None:
    result = validate_response(f"{quote}hey there")
    assert result.valid is False
    assert "quote" in (result.reason or "")


def test_mixed_case_ai_tell_rejected() -> None:
    assert validate_response("As An Ai I must say").valid is False


def test_stage_direction_only_rejected() -> None:
    assert validate_response("  *tilts head*  ").valid is False
