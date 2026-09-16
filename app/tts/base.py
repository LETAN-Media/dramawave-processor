"""TTS provider abstraction. Ready for Google/Azure/OpenAI/ElevenLabs/FPT/Viettel."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TTSClip:
    cue_id: int
    mp3_path: Path
    duration_ms: int


class TTSProvider(ABC):
    name: str = 'base'

    @abstractmethod
    def synthesize(self, cue_id: int, text: str, out_mp3: Path) -> TTSClip:
        """Synthesize one cue to mp3. Retries handled by caller/service."""
        raise NotImplementedError

    def resolve_voice(self) -> str:
        return ''
