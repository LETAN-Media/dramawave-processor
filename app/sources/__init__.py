"""Source providers (DramaWave is the only source)."""

from app.sources.base import (
    DownloadResult,
    EpisodeInfo,
    EpisodePlayback,
    SeriesInfo,
    SourceError,
    VideoSourceProvider,
)
from app.sources.dramawave import DramaWaveSource, get_provider

__all__ = [
    'DownloadResult',
    'DramaWaveSource',
    'EpisodeInfo',
    'EpisodePlayback',
    'SeriesInfo',
    'SourceError',
    'VideoSourceProvider',
    'get_provider',
]
