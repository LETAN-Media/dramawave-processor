"""ASR provider abstraction. Future providers (Deepgram/OpenAI/FunASR/GPU) plug in here."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ASRWord:
    text: str
    start_ms: int
    end_ms: int


@dataclass
class ASRSegment:
    start_ms: int
    end_ms: int
    text: str
    words: list[ASRWord] = field(default_factory=list)


@dataclass
class ASRResult:
    provider: str  # 'jianying' | 'whisper'
    language: str  # 'zh'
    segments: list[ASRSegment]
    duration_ms: int | None = None
    upload_seconds: float | None = None
    recognition_seconds: float | None = None

    def to_normalizer_input(self) -> list[dict]:
        out: list[dict] = []
        for seg in self.segments:
            out.append({
                'start': seg.start_ms / 1000.0,
                'end': seg.end_ms / 1000.0,
                'text': seg.text,
                'words': [
                    {'start': w.start_ms / 1000.0, 'end': w.end_ms / 1000.0, 'word': w.text}
                    for w in seg.words
                ],
            })
        return out


class ASRProvider(ABC):
    name: str = 'base'

    @abstractmethod
    def transcribe(self, audio_path: Path, *, job_id: str | None = None) -> ASRResult:
        raise NotImplementedError
