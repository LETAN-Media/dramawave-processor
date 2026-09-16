"""Per-cue parallel TTS + timing fit + resume from unfinished cue."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config import settings
from app.tts.base import TTSProvider

logger = logging.getLogger('tts-service')


def tts_dir(workdir: Path) -> Path:
    d = workdir / 'tts'
    d.mkdir(parents=True, exist_ok=True)
    return d


def clip_mp3(workdir: Path, cue_id: int) -> Path:
    return tts_dir(workdir) / f'{cue_id:06d}.mp3'


def clip_wav(workdir: Path, cue_id: int) -> Path:
    return tts_dir(workdir) / f'{cue_id:06d}.wav'


def decode_to_wav(mp3_path: Path, wav_path: Path, tempo: float = 1.0, sample_rate: int = 44100) -> int:
    """Decode mp3 to mono s16 wav, applying atempo when tempo > 1. Returns duration ms."""
    filters = []
    if tempo > 1.0:
        # atempo supports 0.5..100; chain if needed (max 1.25 here anyway).
        filters = ['atempo={:.4f}'.format(min(max(tempo, 0.5), 100.0))]
    cmd = ['ffmpeg', '-y', '-i', str(mp3_path)]
    if filters:
        cmd += ['-filter:a', ','.join(filters)]
    cmd += ['-ac', '1', '-ar', str(sample_rate), '-c:a', 'pcm_s16le', str(wav_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f'ffmpeg decode failed for {mp3_path.name}: {(proc.stderr or "")[-500:]}')
    from app.tts.providers_edge import ffprobe_ms
    return ffprobe_ms(wav_path)


def fit_tempo(spoken_ms: int, available_ms: int) -> tuple[float, bool]:
    """Return (tempo, timing_warning). Never exceed TTS_MAX_TEMPO."""
    if available_ms <= 0:
        return 1.0, True
    if spoken_ms <= available_ms:
        return 1.0, False
    need = spoken_ms / available_ms
    if need <= settings.tts_max_tempo:
        return need, False
    return settings.tts_max_tempo, True


def synthesize_cues(
    cues: list[dict],
    mapping: dict[int, str],
    workdir: Path,
    provider: TTSProvider,
    *,
    job_id: str | None = None,
    get_state=None,
    save_state=None,
    on_progress=None,
) -> tuple[int, int, float, int]:
    """Synthesize missing cues in parallel. Returns (clips, warnings, seconds, failed).

    get_state(cue_id) -> dict|None with tts_duration_ms when already done.
    save_state(cue_id, info) persists per-cue progress for resume.
    """
    conc = max(1, settings.tts_concurrency)
    sr = settings.tts_sample_rate
    pending: list[dict] = []
    for cue in cues:
        cid = cue['id']
        mp3, wav = clip_mp3(workdir, cid), clip_wav(workdir, cid)
        st = get_state(cid) if get_state else None
        if st and st.get('tts_duration_ms') and mp3.exists() and wav.exists():
            continue
        pending.append(cue)

    logger.info('tts start job_id=%s cues=%s pending=%s voice=%s conc=%s',
                job_id, len(cues), len(pending), provider.resolve_voice(), conc)
    started = time.monotonic()
    warnings = 0
    failed = 0
    lock = threading.Lock()

    def _one(cue: dict) -> None:
        nonlocal warnings, failed
        cid = cue['id']
        text = mapping[cid]
        available = cue['end_ms'] - cue['start_ms']
        mp3, wav = clip_mp3(workdir, cid), clip_wav(workdir, cid)
        last_err: Exception | None = None
        for attempt in range(max(1, settings.tts_max_retries) + 1):
            try:
                if not mp3.exists() or mp3.stat().st_size <= 0:
                    clip = provider.synthesize(cid, text, mp3)
                    spoken = clip.duration_ms
                else:
                    from app.tts.providers_edge import ffprobe_ms
                    spoken = ffprobe_ms(mp3)
                tempo, warn = fit_tempo(spoken, available)
                decode_to_wav(mp3, wav, tempo=tempo, sample_rate=sr)
                if save_state:
                    save_state(cid, {'tts_path': str(wav), 'tts_duration_ms': spoken,
                                     'tempo': tempo, 'timing_warning': warn, 'error': None})
                with lock:
                    warnings += 1 if warn else 0
                    if warn:
                        logger.warning('tts timing warning job_id=%s cue=%s spoken=%sms avail=%sms tempo=%.2f',
                                       job_id, cid, spoken, available, tempo)
                if on_progress:
                    on_progress(cid)
                return
            except Exception as exc:  # noqa: BLE001 - retry single cue only
                last_err = exc
                logger.warning('tts cue failed job_id=%s cue=%s attempt=%s error=%s',
                               job_id, cid, attempt + 1, str(exc)[:300])
                time.sleep(min(2 * (attempt + 1), 6))
        with lock:
            failed += 1
        if save_state:
            save_state(cid, {'tts_path': None, 'tts_duration_ms': None,
                             'tempo': 1.0, 'timing_warning': True, 'error': str(last_err)[:500]})

    with ThreadPoolExecutor(max_workers=conc) as pool:
        list(pool.map(_one, pending))

    elapsed = time.monotonic() - started
    done = len(cues) - failed
    logger.info('tts done job_id=%s clips=%s warnings=%s failed=%s seconds=%.1f', job_id, done, warnings, failed, elapsed)
    if failed:
        raise RuntimeError(f'TTS_FAILED_CUES: {failed} cues failed')
    return done, warnings, elapsed, failed
