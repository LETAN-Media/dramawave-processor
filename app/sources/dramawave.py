"""Multi-Provider API Video Source."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
import json

from app.clients.drama_source_api import DramaApiError
from app.clients.drama_source_api import get_series as api_get_series
from app.clients.drama_source_api import list_episodes as api_list_episodes
from app.clients.drama_source_api import resolve_playback as api_resolve_playback
from app.clients.drama_source_api import search as api_search
from app.sources.base import (
    DownloadResult,
    EpisodeInfo,
    EpisodePlayback,
    SeriesInfo,
    SourceError,
    VideoSourceProvider,
)
import urllib.request
import urllib.error

logger = logging.getLogger('drama-source')


def _headers_dict(headers: dict | None) -> dict:
    if not headers:
        return {}
    return {k: v for k, v in headers.items() if v}


class DramaWaveSource(VideoSourceProvider):
    name = 'dramawave_multi'

    def can_handle(self, url: str) -> bool:
        return True

    def resolve_series(self, url: str) -> SeriesInfo:
        series_id = url
        try:
            res = api_get_series(series_id)
            return SeriesInfo(
                provider='multi',
                provider_series_id=res['canonical_series_id'],
                title=res.get('canonical_title') or '',
                description=res.get('description') or '',
                cover_url=res.get('cover_url') or '',
                episode_count=res.get('episode_count') or 0,
                source_url=url,
                metadata=res
            )
        except DramaApiError as exc:
            raise SourceError(exc.code, exc.message) from exc

    def list_episodes(self, series: SeriesInfo) -> list[EpisodeInfo]:
        try:
            res = api_list_episodes(series.provider_series_id)
            eps = []
            for e in res.get('episodes', []):
                # multi-provider endpoints return `episodes: [{episode_number, sources: [...]}]`
                num = e['episode_number']
                sources = e.get('sources', [])
                free = any(s.get('status') == 'free' for s in sources)
                eps.append(EpisodeInfo(
                    provider_episode_id=str(num),
                    episode_number=num,
                    title=f"Episode {num}",
                    duration=None,
                    locked=not free,
                    source_url=series.source_url,
                    metadata=e
                ))
            return eps
        except DramaApiError as exc:
            raise SourceError(exc.code, exc.message) from exc

    def resolve_episode(self, episode: EpisodeInfo, quality: str | None = None) -> EpisodePlayback:
        if episode.locked:
            raise SourceError('LOCKED', 'Episode is locked')
        try:
            pb = api_resolve_playback(series_id=episode.source_url, episode_number=episode.episode_number, quality=quality or 'best')
            return EpisodePlayback(
                episode_id=str(episode.episode_number),
                playback_type=pb.get('type') or 'hls',
                playback_url=pb['url'],
                headers=pb.get('headers') or {},
                quality=pb.get('quality') or '',
                master_url=pb.get('master_url'),
                audio_url=pb.get('audio_url'),
                audio_language=pb.get('audio_language'),
                metadata=pb,
            )
        except DramaApiError as exc:
            raise SourceError(exc.code, exc.message) from exc

    def resolve_playback_with_meta(self, series_id: str, episode_number: int, quality: str = 'best') -> dict:
        try:
            return api_resolve_playback(series_id=series_id, episode_number=episode_number, quality=quality)
        except DramaApiError as exc:
            raise SourceError(exc.code, exc.message) from exc

    def download_episode(self, playback: EpisodePlayback, output_path: Path) -> DownloadResult:
        t0 = time.monotonic()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        part = output_path.with_suffix(output_path.suffix + '.part')
        if part.exists():
            part.unlink()

        headers = _headers_dict(playback.headers)
        header_blob = ''.join(f'{k}: {v}\r\n' for k, v in headers.items())

        if playback.playback_type == 'mp4':
            # Fast MP4 streaming download via HTTP or FFmpeg. 
            # We use FFmpeg to ensure generic processing (it handles headers well)
            cmd = ['ffmpeg', '-y', '-v', 'error', '-headers', header_blob,
                   '-i', playback.playback_url, '-c', 'copy', '-f', 'mp4', str(part)]
            mode = 'mp4'
        else: # hls
            # If audio_url is separate and different from playback_url
            if playback.audio_url and playback.audio_url != playback.playback_url:
                cmd = ['ffmpeg', '-y', '-v', 'error',
                       '-headers', header_blob, '-i', playback.playback_url,
                       '-headers', header_blob, '-i', playback.audio_url,
                       '-map', '0:v:0', '-map', '1:a:0',
                       '-c', 'copy', '-shortest', '-f', 'mp4', str(part)]
                mode = 'hls (video+audio)'
            else:
                cmd = ['ffmpeg', '-y', '-v', 'error', '-headers', header_blob,
                       '-i', playback.playback_url, '-c', 'copy', '-f', 'mp4', str(part)]
                mode = 'hls (single)'

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except FileNotFoundError as exc:
            raise SourceError('DOWNLOAD_FAILED', 'ffmpeg missing') from exc
            
        if proc.returncode != 0 or not part.exists() or part.stat().st_size <= 0:
            if part.exists(): part.unlink()
            raise SourceError('DOWNLOAD_FAILED', (proc.stderr or '')[-300:])
            
        details = _probe_details(part, output_path)
        part.rename(output_path)
        secs = time.monotonic() - t0
        logger.info('download ok file=%s size=%s seconds=%.1f quality=%s mode=%s',
                    output_path.name, output_path.stat().st_size, secs,
                    details.get('quality'), mode)
                    
        return DownloadResult(path=output_path, size_bytes=output_path.stat().st_size,
                              quality=details.get('quality') or playback.quality,
                              playback_type=playback.playback_type, download_seconds=secs)


def _probe_details(part: Path, final_path: Path) -> dict:
    import json as _json

    proc = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json',
                           '-show_streams', '-show_format', str(part)],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        if part.exists(): part.unlink()
        raise SourceError('FFPROBE_FAILED', (proc.stderr or '')[-300:])
    try:
        payload = _json.loads(proc.stdout or '{}')
    except ValueError as exc:
        raise SourceError('FFPROBE_FAILED', f'parse: {exc}')
    videos = [s for s in (payload.get('streams') or []) if s.get('codec_type') == 'video']
    audios = [s for s in (payload.get('streams') or []) if s.get('codec_type') == 'audio']
    if not videos or not audios:
        if part.exists(): part.unlink()
        raise SourceError('FFPROBE_FAILED', 'missing video/audio stream')
    v = videos[0]
    w, h = int(v.get('width') or 0), int(v.get('height') or 0)
    fps = None
    try:
        num, den = (v.get('avg_frame_rate') or '0/1').split('/')
        fps = round(float(num) / float(den or 1), 2) or None
    except (ValueError, ZeroDivisionError):
        pass
    fmt = payload.get('format') or {}
    return {'codec': v.get('codec_name'), 'width': w or None, 'height': h or None,
            'fps': fps, 'duration': float(fmt['duration']) if fmt.get('duration') else None,
            'size': int(fmt['size']) if fmt.get('size') else None,
            'quality': f'{min(w, h)}p' if w and h else None}

def get_provider(url: str) -> VideoSourceProvider:
    return DramaWaveSource()
