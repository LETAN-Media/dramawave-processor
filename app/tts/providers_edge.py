"""Edge TTS provider (default Vietnamese voice)."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
from pathlib import Path

from app.config import settings
from app.tts.base import TTSClip, TTSProvider

logger = logging.getLogger('tts-edge')

FEMALE = 'vi-VN-HoaiMyNeural'
MALE = 'vi-VN-NamMinhNeural'


def resolve_voice_name() -> str:
    raw = (settings.tts_voice or '').strip()
    if raw.lower() == 'male':
        return MALE
    if raw.lower() in {'female', ''}:
        return FEMALE
    return raw


def ffprobe_ms(path: Path) -> int:
    proc = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'ffprobe failed for {path.name}: {(proc.stderr or "")[:300]}')
    return int(round(float(proc.stdout.strip()) * 1000))


class EdgeTTSProvider(TTSProvider):
    name = 'edge'
    _loop_lock = threading.Lock()

    def resolve_voice(self) -> str:
        return resolve_voice_name()

    def synthesize(self, cue_id: int, text: str, out_mp3: Path) -> TTSClip:
        import edge_tts

        text = (text or '').strip()
        if not text:
            raise RuntimeError(f'TTS_EMPTY_TEXT: cue {cue_id}')
        voice = resolve_voice_name()
        out_mp3.parent.mkdir(parents=True, exist_ok=True)

        async def _run() -> None:
            communicate = edge_tts.Communicate(text, voice, rate=settings.tts_rate, volume=settings.tts_volume)
            await communicate.save(str(out_mp3))

        # edge-tts is async; worker threads have no running loop.
        try:
            asyncio.run(_run())
        except RuntimeError:
            # Already inside a loop (tests): run in a fresh thread.
            err: list[Exception] = []

            def _target() -> None:
                try:
                    asyncio.run(_run())
                except Exception as exc:  # noqa: BLE001
                    err.append(exc)

            thread = threading.Thread(target=_target, daemon=True)
            thread.start()
            thread.join(timeout=settings.tts_timeout + 60)
            if thread.is_alive():
                raise RuntimeError(f'TTS_TIMEOUT: cue {cue_id}')
            if err:
                raise err[0]
        if not out_mp3.exists() or out_mp3.stat().st_size <= 0:
            raise RuntimeError(f'TTS_EMPTY_AUDIO: cue {cue_id}')
        duration_ms = ffprobe_ms(out_mp3)
        if duration_ms <= 0:
            raise RuntimeError(f'TTS_ZERO_DURATION: cue {cue_id}')
        return TTSClip(cue_id=cue_id, mp3_path=out_mp3, duration_ms=duration_ms)
