"""Video source provider abstraction.

DramaWave URLs -> DramaWaveSource (the only source).
Downstream (download/ASR/translation/TTS/worker) never cares about the platform.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


class SourceError(Exception):
    """Error with a stable machine-readable code (no secrets inside)."""

    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(f'{code}: {message}' if message else code)
        self.code = code


@dataclass
class SeriesInfo:
    provider: str
    provider_series_id: str
    title: str
    description: str = ''
    cover_url: str = ''
    episode_count: int | None = None
    source_url: str = ''
    metadata: dict = field(default_factory=dict)


@dataclass
class EpisodeInfo:
    provider_episode_id: str
    episode_number: int
    title: str = ''
    duration: float | None = None
    locked: bool = False
    source_url: str = ''
    metadata: dict = field(default_factory=dict)


@dataclass
class EpisodePlayback:
    episode_id: str
    playback_type: str  # 'mp4' | 'hls' | 'unknown'
    playback_url: str
    headers: dict = field(default_factory=dict)
    quality: str = ''
    metadata: dict = field(default_factory=dict)
    expires_at: str | None = None
    master_url: str | None = None  # HLS master (has audio groups; variant may be video-only)
    audio_url: str | None = None  # chosen audio rendition playlist
    audio_language: str | None = None


@dataclass
class DownloadResult:
    path: Path
    size_bytes: int
    quality: str
    playback_type: str
    download_seconds: float


class VideoSourceProvider(ABC):
    name: str = 'base'

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def resolve_series(self, url: str) -> SeriesInfo:
        raise NotImplementedError

    @abstractmethod
    def list_episodes(self, series: SeriesInfo) -> list[EpisodeInfo]:
        raise NotImplementedError

    @abstractmethod
    def resolve_episode(self, episode: EpisodeInfo, quality: str | None = None) -> EpisodePlayback:
        raise NotImplementedError

    @abstractmethod
    def download_episode(self, playback: EpisodePlayback, output_path: Path) -> DownloadResult:
        raise NotImplementedError
