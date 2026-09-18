"""Phase 3 render: cover hardcoded subs + burn VI SRT + mix audio + final.vi.mp4.

CPU-friendly FFmpeg only. No AI inpainting, no cropping.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from app.config import settings

logger = logging.getLogger('render')


def _ffprobe_json(path: Path) -> dict:
    import json

    proc = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json',
                           '-show_streams', '-show_format', str(path)],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f'RENDER_FAILED: ffprobe failed for {path.name}')
    return json.loads(proc.stdout or '{}')


def video_info(path: Path) -> dict:
    payload = _ffprobe_json(path)
    videos = [s for s in (payload.get('streams') or []) if s.get('codec_type') == 'video']
    if not videos:
        raise RuntimeError('RENDER_FAILED: no video stream')
    v = videos[0]
    w, h = int(v.get('width') or 0), int(v.get('height') or 0)
    fps = None
    try:
        num, den = (v.get('avg_frame_rate') or '0/1').split('/')
        fps = round(float(num) / float(den or 1), 2) or None
    except (ValueError, ZeroDivisionError):
        pass
    fmt = payload.get('format') or {}
    return {'width': w, 'height': h, 'fps': fps,
            'duration': float(fmt['duration']) if fmt.get('duration') else None}


def _escape_sub_path(path: Path) -> str:
    # libass filter path escaping.
    return str(path).replace('\\', '/').replace(':', '\\:').replace("'", "\\'").replace(',', '\\,')


def render_final(original_mp4: Path, vi_srt: Path, voice_wav: Path, out_mp4: Path,
                 *, job_id: str | None = None) -> dict:
    """Render final.vi.mp4. Returns {duration, width, height, fps, size, seconds}."""
    t0 = time.monotonic()
    for p in (original_mp4, vi_srt, voice_wav):
        if not p.exists() or p.stat().st_size <= 0:
            raise RuntimeError(f'RENDER_FAILED: missing input {p.name}')
    info = video_info(original_mp4)
    w, h = info['width'], info['height']
    if not w or not h:
        raise RuntimeError('RENDER_FAILED: unknown resolution')
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_mp4.with_suffix('.rendering.mp4')
    if tmp.exists():
        tmp.unlink()

    vf = []
    if settings.subtitle_cover_enabled:
        ratio = min(0.4, max(0.05, settings.subtitle_cover_bottom_ratio))
        opacity = min(1.0, max(0.0, settings.subtitle_cover_opacity))
        box_h = int(h * ratio)
        vf.append(f'drawbox=x=0:y={h - box_h}:w={w}:h={box_h}:color=black@{opacity}:t=fill')
    font_size = max(12, int(min(w, h) / 22))
    margin_v = max(8, int(h * 0.035))
    style = (f"FontName=DejaVu Sans,FontSize={font_size},PrimaryColour=&H00FFFFFF,"
             f"OutlineColour=&H80000000,BorderStyle=1,Outline=2,Shadow=1,"
             f"Alignment=2,MarginV={margin_v}")
    vf.append(f"subtitles={_escape_sub_path(vi_srt)}:force_style='{style}'")

    cmd = ['ffmpeg', '-y', '-v', 'error',
           '-i', str(original_mp4),
           '-i', str(voice_wav),
           '-filter_complex',
           f'[0:v]{",".join(vf)}[v];'
           f'[0:a]volume={settings.original_audio_volume}[a0];'
           f'[1:a]volume={settings.vi_voice_volume}[a1];'
           f'[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0[a]',
           '-map', '[v]', '-map', '[a]',
           '-c:v', 'libx264', '-preset', settings.render_preset, '-crf', str(settings.render_crf),
           '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', 'faststart',
           '-shortest', str(tmp)]
    logger.info('render start job_id=%s vf=%s', job_id, ';'.join(vf)[:200])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    except FileNotFoundError as exc:
        raise RuntimeError('RENDER_FAILED: ffmpeg missing') from exc
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size <= 0:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f'RENDER_FAILED: {(proc.stderr or "")[-500:]}')
    result = validate_final(tmp, info)
    tmp.rename(out_mp4)
    result['seconds'] = time.monotonic() - t0
    logger.info('render done job_id=%s size=%s seconds=%.1f', job_id, out_mp4.stat().st_size, result['seconds'])
    return result


def validate_final(path: Path, reference: dict) -> dict:
    """ffprobe final file: streams, resolution, fps, duration<=1s, size>0."""
    payload = _ffprobe_json(path)
    videos = [s for s in (payload.get('streams') or []) if s.get('codec_type') == 'video']
    audios = [s for s in (payload.get('streams') or []) if s.get('codec_type') == 'audio']
    if not videos:
        raise RuntimeError('FINAL_VALIDATION_FAILED: no video stream')
    if not audios:
        raise RuntimeError('FINAL_VALIDATION_FAILED: no audio stream')
    v = videos[0]
    w, h = int(v.get('width') or 0), int(v.get('height') or 0)
    if reference.get('width') and (w, h) != (reference['width'], reference['height']):
        raise RuntimeError(f'FINAL_VALIDATION_FAILED: resolution {(w, h)} != {(reference["width"], reference["height"])}')
    fmt = payload.get('format') or {}
    dur = float(fmt['duration']) if fmt.get('duration') else None
    ref_dur = reference.get('duration')
    if dur is None or dur <= 0:
        raise RuntimeError('FINAL_VALIDATION_FAILED: bad duration')
    if ref_dur and abs(dur - ref_dur) > 1.0:
        raise RuntimeError(f'FINAL_VALIDATION_FAILED: duration {dur:.2f} vs {ref_dur:.2f}')
    size = int(fmt.get('size') or path.stat().st_size or 0)
    if size <= 0:
        raise RuntimeError('FINAL_VALIDATION_FAILED: empty file')
    fps = None
    try:
        num, den = (v.get('avg_frame_rate') or '0/1').split('/')
        fps = round(float(num) / float(den or 1), 2) or None
    except (ValueError, ZeroDivisionError):
        pass
    return {'duration': dur, 'width': w, 'height': h, 'fps': fps, 'size': size}
