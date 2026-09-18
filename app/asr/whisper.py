"""Local faster-whisper provider (fallback). Kept, never removed."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from app.asr.base import ASRProvider, ASRResult, ASRSegment, ASRWord
from app.config import settings

logger = logging.getLogger('asr-whisper')

_sem: threading.Semaphore | None = None
_lock = threading.Lock()


def _get_sem() -> threading.Semaphore:
    global _sem
    with _lock:
        if _sem is None:
            _sem = threading.Semaphore(max(1, settings.asr_concurrency))
        return _sem


class WhisperProvider(ASRProvider):
    name = 'whisper'

    # faster-whisper language codes; None = auto-detect.
    SUPPORTED_LANGUAGES = frozenset({
        'zh', 'en', 'ko', 'ja', 'es', 'fr', 'de', 'it', 'pt', 'ru', 'ar',
        'hi', 'th', 'vi', 'id', 'ms', 'tr', 'nl', 'pl', 'uk',
    })

    def transcribe(self, audio_path: Path, *, job_id: str | None = None,
                   language: str | None = None) -> ASRResult:
        from faster_whisper import WhisperModel

        if not audio_path.exists() or audio_path.stat().st_size <= 0:
            raise RuntimeError(f'audio missing/empty for Whisper: {audio_path}')
        try:
            os.environ.setdefault('OMP_NUM_THREADS', str(max(1, settings.omp_num_threads)))
            os.environ.setdefault('MKL_NUM_THREADS', str(max(1, settings.omp_num_threads)))
        except Exception:
            pass
        sem = _get_sem()
        model_name = settings.whisper_model
        requested = (language or settings.whisper_language or 'zh').strip().lower()
        forced: str | None = requested if requested in self.SUPPORTED_LANGUAGES else None
        if requested not in ('auto', '') and forced is None:
            logger.warning('whisper unsupported language %s, auto-detecting job_id=%s', requested, job_id)
        logger.info(
            'whisper start job_id=%s model=%s device=%s compute=%s lang=%s audio_size=%s',
            job_id, model_name, settings.whisper_device, settings.whisper_compute_type,
            forced or 'auto', audio_path.stat().st_size,
        )
        start = time.monotonic()
        if not sem.acquire(blocking=True, timeout=3600 * 6):
            raise RuntimeError('ASR concurrency slot unavailable (ASR_CONCURRENCY)')
        try:
            model = WhisperModel(
                model_name,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
                cpu_threads=max(1, settings.whisper_cpu_threads),
            )
            # Whisper prefers WAV; if given m4a it still works via PyAV, but
            # caller may pass a WAV specifically. Accept either.
            segments_iter, info = model.transcribe(
                str(audio_path),
                language=forced,
                task='transcribe',
                vad_filter=bool(settings.whisper_vad_filter),
                word_timestamps=bool(settings.whisper_word_timestamps),
            )
            detected = (getattr(info, 'language', None) or forced or 'zh').strip().lower()
            logger.info('whisper detected language=%s job_id=%s', detected, job_id)
            segments: list[ASRSegment] = []
            for seg in segments_iter:
                words: list[ASRWord] = []
                for w in getattr(seg, 'words', None) or []:
                    try:
                        words.append(ASRWord(
                            text=str(w.word),
                            start_ms=int(float(w.start) * 1000),
                            end_ms=int(float(w.end) * 1000),
                        ))
                    except (TypeError, ValueError, AttributeError):
                        continue
                try:
                    segments.append(ASRSegment(
                        start_ms=int(float(seg.start) * 1000),
                        end_ms=int(float(seg.end) * 1000),
                        text=str(seg.text or '').strip(),
                        words=words,
                    ))
                except (TypeError, ValueError):
                    continue
            if not segments:
                raise RuntimeError('ASR produced no segments (empty transcription)')
        finally:
            sem.release()
        elapsed = time.monotonic() - start
        logger.info('whisper done job_id=%s cues=%s elapsed=%.1fs', job_id, len(segments), elapsed)
        return ASRResult(
            provider='whisper', language=detected, segments=segments,
            recognition_seconds=elapsed,
        )
