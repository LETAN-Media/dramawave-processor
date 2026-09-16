"""Extract mono 16kHz WAV from downloaded video without loading into RAM."""

import json
import logging
import subprocess
from pathlib import Path

from app.config import settings

logger = logging.getLogger('bilibili-audio')


def ffprobe_duration(path: Path) -> float | None:
    cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or '{}')
        dur = (payload.get('format') or {}).get('duration')
        return float(dur) if dur is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def extract_compressed_audio(
    video_path: Path,
    workdir: Path,
    *,
    job_id: str | None = None,
    bvid: str | None = None,
    cid: str | None = None,
) -> Path:
    """Lightweight AAC mono 16kHz 48k for remote ASR upload.

    ffmpeg -i original.mp4 -vn -ac 1 -ar 16000 -c:a aac -b:a 48k audio.m4a
    Streams from disk, no full RAM load. Much smaller than WAV (~5x).
    """
    if not video_path.exists():
        raise RuntimeError(f'video file missing for audio extraction: {video_path}')
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / 'audio.m4a'
    if out.exists() and out.stat().st_size > 0:
        logger.info('compressed audio exists, reuse job_id=%s bvid=%s cid=%s file=%s', job_id, bvid, cid, out)
        return out
    cmd = [
        'ffmpeg', '-y',
        '-i', str(video_path),
        '-vn',
        '-ac', str(settings.audio_channels),
        '-ar', str(settings.audio_sample_rate),
        '-c:a', 'aac',
        '-b:a', '48k',
        str(out),
    ]
    logger.info(
        'compressed audio extract start job_id=%s bvid=%s cid=%s sr=%s ch=%s',
        job_id, bvid, cid, settings.audio_sample_rate, settings.audio_channels,
    )
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except FileNotFoundError as exc:
        raise RuntimeError('ffmpeg not available for audio extraction') from exc
    if proc.returncode != 0:
        err = (proc.stderr or '').strip()[-3000:]
        raise RuntimeError(f'ffmpeg compressed audio extraction failed: {err}')
    if not out.exists() or out.stat().st_size <= 0:
        raise RuntimeError('ffmpeg produced empty audio.m4a')
    audio_dur = ffprobe_duration(out)
    video_dur = ffprobe_duration(video_path)
    logger.info(
        'compressed audio extract done job_id=%s bvid=%s cid=%s file=%s size=%s audio_duration=%s video_duration=%s',
        job_id, bvid, cid, out, out.stat().st_size, audio_dur, video_dur,
    )
    return out


def extract_audio(
    video_path: Path,
    workdir: Path,
    *,
    job_id: str | None = None,
    bvid: str | None = None,
    cid: str | None = None,
) -> Path:
    """Run ffmpeg -vn -ac 1 -ar 16000 -c:a pcm_s16le. Streams from disk."""
    if not video_path.exists():
        raise RuntimeError(f'video file missing for audio extraction: {video_path}')
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / 'audio.wav'
    # Resume: if wav already exists and looks valid, reuse.
    if out.exists() and out.stat().st_size > 0:
        logger.info('audio exists, reuse job_id=%s bvid=%s cid=%s file=%s', job_id, bvid, cid, out)
        return out
    cmd = [
        'ffmpeg', '-y',
        '-i', str(video_path),
        '-vn',
        '-ac', str(settings.audio_channels),
        '-ar', str(settings.audio_sample_rate),
        '-c:a', 'pcm_s16le',
        str(out),
    ]
    logger.info(
        'audio extract start job_id=%s bvid=%s cid=%s sr=%s ch=%s',
        job_id, bvid, cid, settings.audio_sample_rate, settings.audio_channels,
    )
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except FileNotFoundError as exc:
        raise RuntimeError('ffmpeg not available for audio extraction') from exc
    if proc.returncode != 0:
        err = (proc.stderr or '').strip()[-3000:]
        raise RuntimeError(f'ffmpeg audio extraction failed: {err}')
    if not out.exists() or out.stat().st_size <= 0:
        raise RuntimeError('ffmpeg produced empty audio.wav')
    audio_dur = ffprobe_duration(out)
    video_dur = ffprobe_duration(video_path)
    logger.info(
        'audio extract done job_id=%s bvid=%s cid=%s file=%s size=%s audio_duration=%s video_duration=%s',
        job_id, bvid, cid, out, out.stat().st_size, audio_dur, video_dur,
    )
    return out
