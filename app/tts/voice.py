"""Build voice.vi.wav: silent base = video duration, overlay each TTS clip at cue.start_ms.

Uses stdlib wave/array + ffmpeg decode (no 1000-input filter graphs). Silence gaps preserved.
"""

from __future__ import annotations

import logging
import subprocess
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config import settings

logger = logging.getLogger('voice-timeline')


def _decode_any_to_pcm(path: Path, sample_rate: int) -> bytes:
    """Decode mp3/wav to mono s16le raw PCM at sample_rate."""
    proc = subprocess.run(
        ['ffmpeg', '-y', '-v', 'error', '-i', str(path),
         '-ac', '1', '-ar', str(sample_rate), '-c:a', 'pcm_s16le', '-f', 's16le', '-'],
        capture_output=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'ffmpeg decode failed for {path.name}: {(proc.stderr or b"")[-500:]}')
    return proc.stdout


def _mix_add(base: bytearray, pcm: bytes, offset_samples: int) -> None:
    """Saturating add of s16 mono pcm into base at sample offset."""
    import array

    if not pcm:
        return
    end = offset_samples + len(pcm) // 2
    if offset_samples < 0:
        cut = -offset_samples * 2
        pcm = pcm[cut:]
        offset_samples = 0
    if end * 2 > len(base):
        pcm = pcm[:len(base) - offset_samples * 2]
    if not pcm:
        return
    # audioop.add wraps on overflow; do manual saturating add in chunks.
    view = memoryview(base)[offset_samples * 2:offset_samples * 2 + len(pcm)]
    a = array.array('h')
    a.frombytes(view.tobytes())
    b = array.array('h')
    b.frombytes(pcm[:len(a) * 2])
    for i in range(len(a)):
        s = a[i] + b[i]
        a[i] = 32767 if s > 32767 else (-32768 if s < -32768 else s)
    view[:] = a.tobytes()


def assemble_voice(
    cues: list[dict],
    get_wav,
    video_duration_s: float,
    out_path: Path,
    *,
    job_id: str | None = None,
    sample_rate: int = 44100,
) -> tuple[Path, float]:
    """Assemble timeline. get_wav(cue_id) -> Path to fitted wav. Returns (path, duration_s)."""
    import time

    t0 = time.monotonic()
    total_samples = int(video_duration_s * sample_rate)
    logger.info('voice assemble start job_id=%s cues=%s duration=%.2fs sr=%s', job_id, len(cues), video_duration_s, sample_rate)
    base = bytearray(total_samples * 2)

    def _load(cue: dict) -> tuple[int, bytes]:
        wav = get_wav(cue['id'])
        pcm = _decode_any_to_pcm(Path(wav), sample_rate)
        offset = int(cue['start_ms'] / 1000 * sample_rate)
        return offset, pcm

    with ThreadPoolExecutor(max_workers=8) as pool:
        loaded = list(pool.map(_load, cues))
    for (offset, pcm), cue in zip(loaded, cues):
        _mix_add(base, pcm, offset)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(base))

    # Verify with ffprobe.
    proc = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(out_path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'ffprobe voice failed: {(proc.stderr or "")[:300]}')
    voice_dur = float(proc.stdout.strip())
    diff = abs(voice_dur - video_duration_s)
    logger.info('voice assemble done job_id=%s voice=%.2fs video=%.2fs diff=%.3fs seconds=%.1f',
                job_id, voice_dur, video_duration_s, diff, time.monotonic() - t0)
    if diff >= 0.5:
        raise RuntimeError(f'VOICE_DURATION_MISMATCH: voice={voice_dur:.2f}s video={video_duration_s:.2f}s diff={diff:.2f}s')
    return out_path, voice_dur
