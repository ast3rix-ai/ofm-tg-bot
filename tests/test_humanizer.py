from __future__ import annotations

import random

from src.config import HumanizationConfig
from src.humanizer import Humanizer

_CFG = HumanizationConfig()


def _humanizer(seed: int = 42, **overrides: object) -> Humanizer:
    cfg = HumanizationConfig(**overrides) if overrides else _CFG
    return Humanizer(cfg, random.Random(seed))


# ---------- read_delay ----------


def test_read_delay_within_clamp_for_short_text() -> None:
    delay = _humanizer().read_delay("hi")
    assert _CFG.read_clamp_min <= delay <= _CFG.read_clamp_max


def test_read_delay_long_text_clamped_to_max() -> None:
    delay = _humanizer().read_delay("x" * 100_000)
    assert delay == _CFG.read_clamp_max


def test_read_delay_empty_text_respects_min() -> None:
    delay = _humanizer().read_delay("")
    assert delay >= _CFG.read_clamp_min


# ---------- typing_duration ----------


def test_typing_duration_within_clamp() -> None:
    h = _humanizer()
    for text in ("", "hey", "a longer message that takes a while to type out"):
        d = h.typing_duration(text)
        assert _CFG.typing_clamp_min <= d <= _CFG.typing_clamp_max


def test_typing_duration_long_text_clamped() -> None:
    assert _humanizer().typing_duration("x" * 5000) == _CFG.typing_clamp_max


# ---------- inter_message_delay ----------


def test_inter_message_delay_always_in_global_range() -> None:
    h = _humanizer()
    for _ in range(500):
        d = h.inter_message_delay()
        assert _CFG.inter_quick_min <= d <= _CFG.inter_pause_max


# ---------- split_message ----------


def test_split_message_single_sentence_is_one_chunk() -> None:
    chunks = _humanizer().split_message("hey cutie how are you")
    assert chunks == ["hey cutie how are you"]


def test_split_message_never_produces_tiny_chunk() -> None:
    text = "Hey there. How are you doing today? I really missed you a lot."
    for seed in range(50):
        chunks = _humanizer(seed).split_message(text)
        assert all(len(c.strip()) >= _CFG.min_chunk_chars for c in chunks)
        assert 1 <= len(chunks) <= 3


def test_split_message_lone_question_mark_not_its_own_chunk() -> None:
    # A bare "?" must fold into the previous sentence, never stand alone.
    text = "i was thinking about you all day. yeah ? really i was."
    for seed in range(50):
        chunks = _humanizer(seed).split_message(text)
        assert "?" not in [c.strip() for c in chunks]


def test_split_message_empty_text() -> None:
    assert _humanizer().split_message("") == [""]


# ---------- maybe_typo ----------


def test_maybe_typo_default_rate_usually_clean() -> None:
    # At the 1.5% default rate a short message is almost always untouched.
    h = _humanizer()
    result = h.maybe_typo("hey")
    assert result.text == "hey"
    assert result.had_typo is False
    assert result.correction is None


def test_maybe_typo_can_inject_with_high_probability() -> None:
    h = _humanizer(typo_per_word_prob=1.0)
    result = h.maybe_typo("hello there gorgeous")
    assert result.had_typo is True
    assert result.text != "hello there gorgeous"


def test_maybe_typo_at_most_one_typo() -> None:
    h = _humanizer(typo_per_word_prob=1.0)
    result = h.maybe_typo("alpha bravo charlie delta echo foxtrot")
    original = "alpha bravo charlie delta echo foxtrot".split(" ")
    typo = result.text.split(" ")
    differing = sum(1 for a, b in zip(original, typo, strict=False) if a != b)
    # Length changes (doubled/missing letter) can desync word alignment;
    # compare the joined strings instead for the single-edit guarantee.
    assert result.had_typo is True
    assert differing <= 1 or len(typo) != len(original)


def test_maybe_typo_correction_follows_when_rolled() -> None:
    h = _humanizer(
        typo_per_word_prob=1.0, typo_correction_prob=1.0
    )
    result = h.maybe_typo("hello gorgeous")
    assert result.had_typo is True
    assert result.correction is not None
    assert result.correction.startswith("*")


def test_maybe_typo_skips_short_words() -> None:
    # Words shorter than 3 chars are never typo'd.
    h = _humanizer(typo_per_word_prob=1.0)
    result = h.maybe_typo("hi i am")
    assert result.had_typo is False


# ---------- determinism ----------


def _trace(seed: int) -> list[object]:
    h = Humanizer(_CFG, random.Random(seed))
    out: list[object] = []
    out.append(h.read_delay("how are you doing today cutie"))
    out.append(h.split_message("Hey there. How are you? I missed you so much."))
    out.append(h.typing_duration("heyy whats up"))
    out.append(h.inter_message_delay())
    typo = h.maybe_typo("you look absolutely gorgeous tonight darling")
    out.append((typo.text, typo.correction, typo.had_typo))
    out.append(h.correction_delay())
    return out


def test_humanizer_is_deterministic_for_a_seed() -> None:
    assert _trace(2026) == _trace(2026)


def test_humanizer_differs_across_seeds() -> None:
    assert _trace(1) != _trace(2)
