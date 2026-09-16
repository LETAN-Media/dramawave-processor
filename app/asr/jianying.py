"""JianYing/CapCut remote ASR via `jianying-subtitle` CLI.

Privacy: audio is uploaded to ByteDance cloud. Gate with ALLOW_REMOTE_ASR.
No secrets logged. CLI output SRT is re-parsed into internal ASRResult,
then normalized again by the project's strict formatter (never dump raw).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app.asr.base import ASRProvider, ASRResult, ASRSegment, ASRWord
from app.config import settings

logger = logging.getLogger('asr-jianying')

_sem: threading.Semaphore | None = None
_lock = threading.Lock()


def _get_sem() -> threading.Semaphore:
    global _sem
    with _lock:
        if _sem is None:
            _sem = threading.Semaphore(max(1, settings.jianying_concurrency))
        return _sem


def jianying_available() -> bool:
    """Local availability check only (CLI present). No remote call."""
    if not settings.jianying_enabled:
        return False
    return shutil.which(settings.jianying_cli) is not None


def _is_retryable(msg: str) -> bool:
    m = (msg or '').lower()
    markers = (
        'timeout', 'timed out', 'connection', 'temporary', 'try again',
        '5xx', '500', '502', '503', '504', 'network', 'econnreset',
        'socket', 'upload', 'poll',
    )
    return any(k in m for k in markers)


def _parse_jianying_json(payload) -> list[ASRSegment]:
    """Accept JianYing JSON (list of {text,startMs,endMs,words}) in any shape."""
    if isinstance(payload, dict):
        for key in ('segments', 'data', 'result', 'utterances'):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise RuntimeError('INVALID_JIANYING_RESULT: JSON is not a segment list')
    segments: list[ASRSegment] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        text = str(item.get('text', '') or '').strip()
        try:
            start = int(item.get('startMs', item.get('start_ms', item.get('start', 0))) or 0)
            end = int(item.get('endMs', item.get('end_ms', item.get('end', 0))) or 0)
        except (TypeError, ValueError):
            continue
        # Support seconds-based floats.
        if end <= 1000 and ('start' in item or 'end' in item) and 'startMs' not in item:
            try:
                start = int(float(item.get('start', 0)) * 1000)
                end = int(float(item.get('end', 0)) * 1000)
            except (TypeError, ValueError):
                pass
        if not text or not end > start:
            continue
        words: list[ASRWord] = []
        for w in item.get('words') or []:
            if not isinstance(w, dict):
                continue
            wt = str(w.get('text', w.get('word', '')) or '').strip()
            if not wt:
                continue
            try:
                ws = int(w.get('startMs', w.get('start_ms', w.get('start', start))) or start)
                we = int(w.get('endMs', w.get('end_ms', w.get('end', ws))) or ws)
            except (TypeError, ValueError):
                continue
            if we <= ws:
                continue
            words.append(ASRWord(text=wt, start_ms=ws, end_ms=we))
        segments.append(ASRSegment(start_ms=start, end_ms=end, text=text, words=words))
    if not segments:
        raise RuntimeError('INVALID_JIANYING_RESULT: no usable segments')
    return segments


def parse_jianying_srt_file(path: Path) -> list[ASRSegment]:
    """Parse raw JianYing SRT back into internal segments (for re-normalization)."""
    text = path.read_text(encoding='utf-8')
    blocks = [b.strip() for b in text.replace('\r\n', '\n').split('\n\n') if b.strip()]
    segments: list[ASRSegment] = []
    for block in blocks:
        lines = block.split('\n')
        if len(lines) < 3:
            continue
        tc = lines[1].strip()
        if '-->' not in tc:
            continue
        try:
            left, right = [p.strip() for p in tc.split('-->')]

            def _ms(v: str) -> int:
                v = v.replace('.', ',')
                h, m, rest = v.split(':')
                s, ms = rest.split(',')
                return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms.ljust(3, '0')[:3])

            s_ms, e_ms = _ms(left), _ms(right)
            body = ' '.join(lines[2:]).strip()
            if body and e_ms > s_ms:
                segments.append(ASRSegment(start_ms=s_ms, end_ms=e_ms, text=body))
        except (ValueError, IndexError):
            continue
    if not segments:
        raise RuntimeError('INVALID_JIANYING_RESULT: SRT has no parseable cues')
    return segments


class JianYingProvider(ASRProvider):
    name = 'jianying'

    def transcribe(self, audio_path: Path, *, job_id: str | None = None) -> ASRResult:
        if not settings.allow_remote_asr:
            raise RuntimeError('REMOTE_ASR_DISABLED: ALLOW_REMOTE_ASR=false')
        if not settings.jianying_enabled:
            raise RuntimeError('JIANYING_DISABLED')
        if not audio_path.exists() or audio_path.stat().st_size <= 0:
            raise RuntimeError(f'audio missing/empty for JianYing: {audio_path}')
        cli = settings.jianying_cli
        if shutil.which(cli) is None:
            raise RuntimeError(f'JIANYING_CLI_MISSING: {cli} not on PATH')

        audio_size = audio_path.stat().st_size
        max_retries = max(0, settings.jianying_max_retries)
        timeout = max(60, settings.jianying_upload_timeout + settings.jianying_process_timeout)
        sem = _get_sem()
        if not sem.acquire(blocking=True, timeout=timeout + 300):
            raise RuntimeError('JIANYING_CONCURRENCY_BUSY')

        last_err: Exception | None = None
        t_upload_start = time.monotonic()
        try:
            for attempt in range(max_retries + 1):
                out_json = audio_path.parent / f'jianying.{attempt}.json'
                cmd = [cli, str(audio_path), '-o', str(out_json)]
                logger.info(
                    'jianying start job_id=%s audio_size=%s attempt=%s/%s',
                    job_id, audio_size, attempt + 1, max_retries + 1,
                )
                start = time.monotonic()
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    last_err = RuntimeError(f'JIANYING_TIMEOUT after {timeout}s')
                    logger.warning('jianying timeout job_id=%s attempt=%s', job_id, attempt + 1)
                    if attempt < max_retries:
                        time.sleep(min(5, settings.jianying_poll_interval))
                        continue
                    raise last_err from exc
                elapsed = time.monotonic() - start
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout or '').strip()[-1500:]
                    last_err = RuntimeError(f'JIANYING_CLI_FAILED: {err}')
                    logger.warning('jianying cli failed job_id=%s attempt=%s error=%s', job_id, attempt + 1, err[:500])
                    if attempt < max_retries and _is_retryable(err):
                        time.sleep(min(5, settings.jianying_poll_interval))
                        continue
                    raise last_err
                try:
                    payload = json.loads(out_json.read_text(encoding='utf-8'))
                    segments = _parse_jianying_json(payload)
                except Exception as exc:
                    last_err = RuntimeError(f'INVALID_JIANYING_RESULT: {exc}')
                    logger.warning('jianying parse failed job_id=%s attempt=%s error=%s', job_id, attempt + 1, str(exc)[:500])
                    # Invalid format is not retryable unless retries remain for transient causes.
                    raise last_err from exc
                upload_seconds = elapsed  # CLI bundles upload+process; split measured at service level if needed
                logger.info(
                    'jianying done job_id=%s audio_size=%s cues=%s elapsed=%.1fs',
                    job_id, audio_size, len(segments), elapsed,
                )
                return ASRResult(
                    provider='jianying',
                    language='zh',
                    segments=segments,
                    upload_seconds=upload_seconds,
                    recognition_seconds=elapsed,
                )
        finally:
            sem.release()
        raise last_err or RuntimeError('JIANYING_FAILED')
