"""ASR service: provider selection + auto fallback + strict SRT output."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.asr.base import ASRResult
from app.asr.jianying import JianYingProvider
from app.asr.whisper import WhisperProvider
from app.bilibili.srt import normalize_segments_to_srt, write_srt
from app.config import settings

logger = logging.getLogger('asr-service')


def _write_strict_srt(workdir: Path, result: ASRResult) -> tuple[Path, int]:
    srt = normalize_segments_to_srt(
        result.to_normalizer_input(),
        min_duration_ms=settings.srt_min_duration_ms,
        max_chars_per_line=settings.srt_max_chars_per_line,
        max_lines=settings.srt_max_lines,
    )
    if not srt.strip():
        raise RuntimeError('INVALID_SRT_FORMAT: empty after normalization')
    out = workdir / 'source.zh.srt'
    cues = write_srt(out, srt)
    return out, cues


def transcribe_with_fallback(
    compressed_audio: Path | None,
    wav_audio: Path | None,
    workdir: Path,
    *,
    job_id: str | None = None,
) -> tuple[Path, str, int, str, bool, float, float | None, float | None]:
    """Run ASR per ASR_PROVIDER (jianying|whisper|auto) with fallback.

    Returns (srt_path, language, cues, provider_used, fallback_used,
             asr_seconds, upload_seconds, recognition_seconds).
    """
    mode = (settings.asr_provider or 'auto').strip().lower()
    if mode not in {'jianying', 'whisper', 'auto'}:
        mode = 'auto'
    workdir.mkdir(parents=True, exist_ok=True)
    use_remote = bool(settings.allow_remote_asr and settings.jianying_enabled)

    def _run_jianying() -> tuple[Path, int, float, float | None, float | None]:
        if compressed_audio is None or not compressed_audio.exists():
            raise RuntimeError('JIANYING_NO_AUDIO: compressed audio missing')
        t0 = time.monotonic()
        result = JianYingProvider().transcribe(compressed_audio, job_id=job_id)
        out, cues = _write_strict_srt(workdir, result)
        total = time.monotonic() - t0
        return out, cues, total, result.upload_seconds, result.recognition_seconds

    def _run_whisper() -> tuple[Path, int, float, float | None, float | None]:
        audio = wav_audio if (wav_audio and wav_audio.exists()) else compressed_audio
        if audio is None or not audio.exists():
            raise RuntimeError('WHISPER_NO_AUDIO: no audio available')
        t0 = time.monotonic()
        result = WhisperProvider().transcribe(audio, job_id=job_id)
        out, cues = _write_strict_srt(workdir, result)
        total = time.monotonic() - t0
        return out, cues, total, result.upload_seconds, result.recognition_seconds

    if mode == 'jianying':
        if not use_remote:
            logger.info('jianying requested but remote disabled, using whisper job_id=%s', job_id)
            out, cues, total, up, rec = _run_whisper()
            return out, 'zh', cues, 'whisper', True, total, up, rec
        out, cues, total, up, rec = _run_jianying()
        return out, 'zh', cues, 'jianying', False, total, up, rec

    if mode == 'whisper':
        out, cues, total, up, rec = _run_whisper()
        return out, 'zh', cues, 'whisper', False, total, up, rec

    # auto: try JianYing, fallback Whisper.
    if use_remote:
        try:
            out, cues, total, up, rec = _run_jianying()
            return out, 'zh', cues, 'jianying', False, total, up, rec
        except Exception as exc:
            logger.warning('JianYing ASR failed, falling back to faster-whisper job_id=%s error=%s', job_id, str(exc)[:800])
    else:
        logger.info('remote ASR disabled, using local whisper job_id=%s', job_id)
    out, cues, total, up, rec = _run_whisper()
    fallback = use_remote  # True when jianying was tried first
    return out, 'zh', cues, 'whisper', fallback, total, up, rec
