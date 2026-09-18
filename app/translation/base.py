"""Translation provider abstraction. Ready for OpenAI/Claude/Gemini/OpenAI-compatible."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TransCue:
    cue_id: int
    start_ms: int
    end_ms: int
    text: str  # source (Chinese) text


@dataclass
class TransContext:
    previous: list[tuple['TransCue', str]]  # [(cue, translated_vi)] tail for coherence
    glossary: dict[str, dict[str, str]]  # characters/relationships/pronouns sections
    style: str = 'AUTO'  # MODERN_DRAMA | XIANXIA | NINETIES | AUTO
    source_language: str = 'zh'  # short code of the source audio


class TranslationProvider(ABC):
    name: str = 'base'

    @abstractmethod
    def translate_batch(self, cues: list[TransCue], context: TransContext) -> dict[int, str]:
        """Translate one batch. Returns {cue_id: vietnamese_text}. Never reorders."""
        raise NotImplementedError
