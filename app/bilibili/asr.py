"""Chinese ASR via faster-whisper -> strict SRT."""

import logging
import os
import threading
import time
from pathlib import Path

from app.bilibili.srt import normalize_segments_to_srt, write_srt
from app.config import settings

logger = logging.getLogger('bilibili-asr')

_asr_semaphore: threading.Semaphore | None = None
_asr_lock = threading.Lock()


def _get_semaphore() -> threading.Semaphore:
    global _asr_semaphore
    with _asr_lock:
        if _asr_semaphore is None:
            _asr_semaphore = threading.Semaphore(max(1, settings.asr_concurrency))
        return _asr_semaphore


def _apply_cpu_env() -> None:
    try:
        threads = str(max(1, settings.omp_num_threads))
        os.environ.setdefault('OMP_NUM_THREADS', threads)
        os.environ.setdefault('MKL_NUM_THREADS', threads)
    except Exception:
        pass


def transcribe_audio_to_srt(
    audio_path: Path,
    workdir: Path,
    *,
    job_id: str | None = None,
    bvid: str | None = None,
    cid: str | None = None,
) -> tuple[Path, str, int, str]:
    """Transcribe Chinese audio to strict SRT.

    Returns (srt_path, language, cue_count, detected_language_info).
    Raises on failure (caller marks job failed, no mock).
    """
    from faster_whisper import WhisperModel

    if not audio_path.exists() or audio_path.stat().st_size <= 0:
        raise RuntimeError(f'audio file missing/empty for ASR: {audio_path}')
    workdir.mkdir(parents=True, exist_ok=True)
    _apply_cpu_env()
    sem = _get_semaphore()
    model_name = settings.whisper_model
    device = settings.whisper_device
    compute_type = settings.whisper_compute_type
    language = settings.whisper_language or 'zh'

    logger.info(
        'asr start job_id=%s bvid=%s cid=%s model=%s device=%s compute=%s lang=%s vad=%s word_ts=%s',
        job_id, bvid, cid, model_name, device, compute_type, language,
        settings.whisper_vad_filter, settings.whisper_word_timestamps,
    )
    started = time.monotonic()
    acquired = sem.acquire(blocking=True, timeout=3600 * 6)
    if not acquired:
        raise RuntimeError('ASR concurrency slot unavailable (ASR_CONCURRENCY)')
    try:
        model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            cpu_threads=max(1, settings.whisper_cpu_threads),
        )
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            task='transcribe',
            vad_filter=bool(settings.whisper_vad_filter),
            word_timestamps=bool(settings.whisper_word_timestamps),
        )
        detected_lang = getattr(info, 'language', language) or language
        detected_prob = getattr(info, 'language_probability', None)
        logger.info(
            'asr model loaded job_id=%s detected_language=%s prob=%s duration=%s',
            job_id, detected_lang, detected_prob, getattr(info, 'duration', None),
        )
        segments: list[dict] = []
        for seg in segments_iter:
            words = []
            for w in getattr(seg, 'words', None) or []:
                try:
                    words.append({'start': float(w.start), 'end': float(w.end), 'word': str(w.word)})
                except (TypeError, ValueError, AttributeError):
                    continue
            try:
                segments.append({
                    'start': float(seg.start),
                    'end': float(seg.end),
                    'text': str(seg.text or ''),
                    'words': words,
                })
            except (TypeError, ValueError):
                continue
        if not segments:
            raise RuntimeError('ASR produced no segments (empty transcription)')
    finally:
        sem.release()

    elapsed = time.monotonic() - started
    srt = normalize_segments_to_srt(
        segments,
        min_duration_ms=settings.srt_min_duration_ms,
        max_chars_per_line=settings.srt_max_chars_per_line,
        max_lines=settings.srt_max_lines,
    )
    if not srt.strip():
        raise RuntimeError('ASR produced empty SRT after normalization')
    out = workdir / 'source.zh.srt'
    cue_count = write_srt(out, srt)  # validates by re-parsing; raises INVALID_SRT_FORMAT
    logger.info(
        'asr done job_id=%s bvid=%s cid=%s cues=%s elapsed=%.1fs detected=%s srt=%s',
        job_id, bvid, cid, cue_count, elapsed, detected_lang, out,
    )
    return out, 'zh', cue_count, str(detected_lang)
