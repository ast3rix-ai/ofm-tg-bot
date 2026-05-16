from __future__ import annotations

import random
import re
from dataclasses import dataclass

from src.config import HumanizationConfig

# Sentence boundary: punctuation followed by whitespace.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
# A "sentence" that is only punctuation (e.g. a lone "?") — never its own chunk.
_PUNCT_ONLY = re.compile(r"^[.!?\s]+$")


@dataclass(frozen=True)
class TypoResult:
    """Outcome of a `maybe_typo` call.

    `text` is what to send first (typo'd or clean). `correction` is a
    follow-up `*word` message to send afterward, or None.
    """

    text: str
    correction: str | None
    had_typo: bool


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class Humanizer:
    """Pure timing/typo helpers for the outbound humanization layer.

    Every random draw goes through the injected `random.Random`, so a seeded
    instance is fully deterministic and snapshot-testable.
    """

    def __init__(self, config: HumanizationConfig, rng: random.Random) -> None:
        self._config = config
        self._rng = rng

    # ---------- read receipts ----------

    def read_delay(self, inbound_text: str) -> float:
        """Seconds to wait before marking an inbound message read.

        Models a human reading the message at ~250 wpm before replying.
        """
        cfg = self._config
        chars = len(inbound_text or "")
        reading = chars / cfg.chars_per_word / cfg.read_words_per_sec
        jitter = self._rng.uniform(cfg.read_jitter_min, cfg.read_jitter_max)
        return _clamp(reading + jitter, cfg.read_clamp_min, cfg.read_clamp_max)

    # ---------- message splitting ----------

    def split_message(self, text: str) -> list[str]:
        """Split a reply into 1–3 chunks along sentence boundaries.

        Short sentences and lone punctuation are folded into the previous
        chunk; no chunk is ever shorter than `min_chunk_chars`.
        """
        cfg = self._config
        stripped = (text or "").strip()
        if not stripped:
            return [stripped]

        raw = [s for s in _SENTENCE_SPLIT.split(stripped) if s.strip()]
        sentences = self._merge_short(raw)
        if len(sentences) <= 1:
            return [stripped]

        n = len(sentences)
        if n <= 2:
            if self._rng.random() < cfg.single_chunk_prob:
                return [stripped]
            return self._enforce_min_chunk(sentences)

        # n >= 3
        if self._rng.random() >= cfg.split_prob:
            return [stripped]
        target = self._rng.choice([2, 3])
        chunks = self._balanced_chunks(sentences, target)
        return self._enforce_min_chunk(chunks)

    def _merge_short(self, sentences: list[str]) -> list[str]:
        cfg = self._config
        merged: list[str] = []
        for sentence in sentences:
            words = len(sentence.split())
            punct_only = bool(_PUNCT_ONLY.match(sentence))
            too_short = words < cfg.short_sentence_words
            fold = punct_only or (too_short and self._rng.random() < 0.5)
            if merged and fold:
                merged[-1] = f"{merged[-1]} {sentence}"
            else:
                merged.append(sentence)
        return merged

    @staticmethod
    def _balanced_chunks(sentences: list[str], target: int) -> list[str]:
        """Greedily group sentences into `target` length-balanced chunks."""
        target = min(target, len(sentences))
        total = sum(len(s) for s in sentences)
        per_chunk = total / target
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for i, sentence in enumerate(sentences):
            remaining = len(sentences) - i
            current.append(sentence)
            current_len += len(sentence)
            slots_left = target - len(chunks)
            if current_len >= per_chunk and slots_left > 1 and remaining > 1:
                chunks.append(" ".join(current))
                current = []
                current_len = 0
        if current:
            chunks.append(" ".join(current))
        return chunks

    def _enforce_min_chunk(self, chunks: list[str]) -> list[str]:
        """Merge any chunk shorter than `min_chunk_chars` into a neighbour."""
        minimum = self._config.min_chunk_chars
        result: list[str] = []
        for chunk in chunks:
            if result and len(chunk.strip()) < minimum:
                result[-1] = f"{result[-1]} {chunk}"
            else:
                result.append(chunk)
        # A too-short leading chunk folds forward instead.
        if len(result) > 1 and len(result[0].strip()) < minimum:
            result[1] = f"{result[0]} {result[1]}"
            result = result[1:]
        return result

    # ---------- typing ----------

    def typing_duration(self, text: str) -> float:
        """Seconds to hold the typing indicator for a chunk of `text`."""
        cfg = self._config
        cpm = self._rng.uniform(cfg.typing_cpm_min, cfg.typing_cpm_max)
        typing = len(text or "") / cpm * 60.0
        jitter = self._rng.uniform(cfg.typing_jitter_min, cfg.typing_jitter_max)
        return _clamp(typing + jitter, cfg.typing_clamp_min, cfg.typing_clamp_max)

    def inter_message_delay(self) -> float:
        """Seconds to pause between consecutive chunks (bimodal)."""
        cfg = self._config
        if self._rng.random() < cfg.inter_quick_prob:
            return self._rng.uniform(cfg.inter_quick_min, cfg.inter_quick_max)
        return self._rng.uniform(cfg.inter_pause_min, cfg.inter_pause_max)

    def correction_delay(self) -> float:
        """Seconds to wait before sending a `*correction` follow-up."""
        return self._rng.uniform(
            self._config.correction_delay_min,
            self._config.correction_delay_max,
        )

    # ---------- typos ----------

    def maybe_typo(self, text: str) -> TypoResult:
        """Maybe inject a single realistic typo into `text`.

        Each word (length ≥ 3) independently has a small typo probability;
        at most one typo is injected. When a typo is injected there is a
        chance of also returning a `*word` correction follow-up.
        """
        cfg = self._config
        words = text.split(" ")
        for i, word in enumerate(words):
            if len(word) < 3:
                continue
            if self._rng.random() >= cfg.typo_per_word_prob:
                continue
            typo_word = self._apply_typo(word)
            if typo_word == word:
                continue
            new_words = list(words)
            new_words[i] = typo_word
            correction: str | None = None
            if self._rng.random() < cfg.typo_correction_prob:
                correction = f"*{word}"
            return TypoResult(
                text=" ".join(new_words), correction=correction, had_typo=True
            )
        return TypoResult(text=text, correction=None, had_typo=False)

    def _apply_typo(self, word: str) -> str:
        """Apply one weighted typo transformation to a word."""
        roll = self._rng.random()
        if roll < 0.50 and len(word) >= 2:
            # Adjacent-character transposition.
            i = self._rng.randint(0, len(word) - 2)
            chars = list(word)
            chars[i], chars[i + 1] = chars[i + 1], chars[i]
            return "".join(chars)
        if roll < 0.75:
            # Doubled letter.
            i = self._rng.randint(0, len(word) - 1)
            return word[: i + 1] + word[i] + word[i + 1 :]
        # Missing letter.
        i = self._rng.randint(0, len(word) - 1)
        return word[:i] + word[i + 1 :]
