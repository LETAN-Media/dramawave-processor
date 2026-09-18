"""Speech-block voice generation (SRT IMMUTABLE).

Groups consecutive VI cues into speech blocks (audio-only structure), synthesizes
one natural TTS turn per block, fits with atempo (pref <=1.15, max 1.25), places
each block at its absolute start_ms. source.vi.srt is never modified: hashed
before/after, job fails if it changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.models import Job, SpeechBlock
from app.storage.factory import get_storage
from app.translation.service import compute_cps
from app.tts.blocks import SpeechBlock as BlockSpec, build_blocks

logger = logging.getLogger('block-voice')

BLOCK_VOICE_STATES = ('block_voice',)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _set_stage(job_id: str, stage: str, progress: int) -> None:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise RuntimeError('Job disappeared')
        job.status = stage
        job.current_stage = stage
        job.progress = progress
        job.error = None


def _fail(job_id: str, exc: Exception) -> None:
    message = str(exc)[:5000]
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = 'failed'
        job.current_stage = 'failed'
        job.error = message
        job.lease_owner = None
        job.lease_until = None
    logger.error('block voice failed job_id=%s error=%s', job_id, message[:1000])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _vi_cues(job_id: str) -> tuple[list[dict], Path]:
    """Load VI cues from DB cue_states (falls back to source.vi.srt parse)."""
    from sqlalchemy import select

    from app.models import CueState

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        vi_local = Path(job.vi_local_path) if job and job.vi_local_path else None
        rows = list(db.execute(select(CueState).where(CueState.job_id == job_id).order_by(CueState.cue_id)).scalars().all())
    cues: list[dict] = []
    for r in rows:
        if r.translated and (r.vi_text or '').strip():
            cues.append({'id': r.cue_id, 'start_ms': r.start_ms, 'end_ms': r.end_ms, 'text': r.vi_text})
    if cues:
        return cues, vi_local
    # Fallback: parse the VI file directly (no DB dependency).
    if vi_local and vi_local.exists():
        from app.translation.service import parse_srt_cues

        parsed = parse_srt_cues(vi_local)
        return [{'id': c['id'], 'start_ms': c['start_ms'], 'end_ms': c['end_ms'], 'text': c['text']} for c in parsed], vi_local
    raise RuntimeError('BLOCK_VOICE_NO_VI: Vietnamese cues unavailable')


def _seed_blocks(job_id: str, specs: list[BlockSpec]) -> None:
    with SessionLocal.begin() as db:
        for spec in specs:
            row = db.get(SpeechBlock, (job_id, spec.block_id))
            payload_cues = json.dumps(spec.cue_ids, ensure_ascii=False)
            payload_texts = json.dumps(spec.subtitle_texts, ensure_ascii=False)
            if row is None:
                db.add(SpeechBlock(
                    job_id=job_id, block_id=spec.block_id,
                    start_ms=spec.start_ms, end_ms=spec.end_ms,
                    cue_ids=payload_cues, subtitle_texts=payload_texts,
                    tts_text=spec.tts_text, status='pending'))
            elif row.cue_ids != payload_cues:
                # Timeline changed (should not happen): reset this block.
                row.start_ms, row.end_ms = spec.start_ms, spec.end_ms
                row.cue_ids, row.subtitle_texts = payload_cues, payload_texts
                row.tts_text = spec.tts_text
                row.status, row.error = 'pending', None
                row.mp3_path, row.wav_path, row.tts_duration_ms = None, None, None


def _block_state(job_id: str, block_id: int) -> dict | None:
    with SessionLocal() as db:
        row = db.get(SpeechBlock, (job_id, block_id))
        if row and row.tts_duration_ms and row.wav_path and Path(row.wav_path).exists():
            return {'tts_duration_ms': row.tts_duration_ms}
        return None


def _save_block_state(job_id: str, block_id: int, info: dict) -> None:
    with SessionLocal.begin() as db:
        row = db.get(SpeechBlock, (job_id, block_id))
        if row is None:
            return
        for key in ('mp3_path', 'wav_path', 'tts_duration_ms', 'tempo', 'overflow_ms', 'tts_text', 'voice', 'rate', 'error'):
            if key in info:
                setattr(row, key, info[key])
        row.qa_class = info.get('qa_class', row.qa_class)
        row.manual_review_required = info.get('manual_review_required', row.manual_review_required)
        if info.get('tts_duration_ms'):
            row.status = 'done'
        elif info.get('error'):
            row.status = 'failed'


def block_mp3(workdir: Path, block_id: int) -> Path:
    d = workdir / 'tts_blocks'
    d.mkdir(parents=True, exist_ok=True)
    return d / f'{block_id:06d}.mp3'


def block_wav(workdir: Path, block_id: int) -> Path:
    d = workdir / 'tts_blocks'
    d.mkdir(parents=True, exist_ok=True)
    return d / f'{block_id:06d}.wav'


def _classify_block(spoken_ms: int | None, tempo: float | None, available_ms: int) -> tuple[str, int | None, int]:
    if spoken_ms is None or spoken_ms <= 0 or available_ms <= 0:
        return 'OVERFLOW', spoken_ms, max(0, (spoken_ms or 0) - max(0, available_ms))
    if spoken_ms <= available_ms:
        return 'FIT', spoken_ms, 0
    tempo = min(tempo if tempo and tempo > 0 else 1.0, settings.tts_max_tempo)
    final_ms = int(round(spoken_ms / tempo))
    if final_ms <= available_ms:
        return 'ADJUSTED_FIT', final_ms, 0
    overflow = final_ms - available_ms
    if overflow > settings.qa_severe_overflow_ms or overflow / available_ms > settings.qa_severe_overflow_ratio:
        return 'SEVERE_OVERFLOW', final_ms, overflow
    return 'OVERFLOW', final_ms, overflow


def synthesize_blocks(
    specs: list[BlockSpec],
    workdir: Path,
    voice: str,
    *,
    job_id: str | None = None,
    on_progress=None,
) -> tuple[int, int]:
    """Synthesize missing blocks in parallel. Returns (done, warnings)."""
    from app.tts.providers_edge import EdgeTTSProvider, ffprobe_ms
    from app.tts.service import decode_to_wav, fit_tempo

    provider = EdgeTTSProvider()
    pending = [s for s in specs if not _block_state(job_id, s.block_id)]
    logger.info('block tts start job_id=%s blocks=%s pending=%s voice=%s',
                job_id, len(specs), len(pending), voice)
    t0 = time.monotonic()
    warnings = 0
    failed = 0
    lock = threading.Lock()

    def _one(spec: BlockSpec) -> None:
        nonlocal warnings, failed
        mp3, wav = block_mp3(workdir, spec.block_id), block_wav(workdir, spec.block_id)
        available = spec.available_ms
        last_err: Exception | None = None
        for attempt in range(max(1, settings.tts_max_retries) + 1):
            try:
                if not mp3.exists() or mp3.stat().st_size <= 0:
                    clip = provider.synthesize(spec.block_id, spec.tts_text or _tts_text(job_id, spec.block_id), mp3, voice)
                    spoken = clip.duration_ms
                else:
                    from app.tts.providers_edge import ffprobe_ms

                    spoken = ffprobe_ms(mp3)
                tempo, warn = fit_tempo(spoken, available)
                decode_to_wav(mp3, wav, tempo=tempo, sample_rate=settings.tts_sample_rate)
                cls, final_ms, overflow = _classify_block(spoken, tempo, available)
                _save_block_state(job_id, spec.block_id, {
                    'mp3_path': str(mp3), 'wav_path': str(wav), 'tts_duration_ms': spoken,
                    'tempo': tempo, 'overflow_ms': overflow if cls in ('OVERFLOW', 'SEVERE_OVERFLOW') else 0,
                    'qa_class': cls, 'voice': voice, 'rate': settings.tts_rate, 'error': None})
                with lock:
                    warnings += 1 if cls in ('OVERFLOW', 'SEVERE_OVERFLOW') else 0
                if on_progress:
                    on_progress(spec.block_id)
                return
            except Exception as exc:  # noqa: BLE001 - retry single block only
                last_err = exc
                # Edge throttles bursty parallel requests ("No audio was received"):
                # back off progressively so a later attempt succeeds.
                time.sleep(min(5 * (attempt + 1), 20))
                logger.warning('block tts failed job_id=%s block=%s attempt=%s error=%s',
                               job_id, spec.block_id, attempt + 1, str(exc)[:200])
        with lock:
            failed += 1
        _save_block_state(job_id, spec.block_id, {'error': str(last_err)[:500]})

    with ThreadPoolExecutor(max_workers=max(1, settings.tts_concurrency)) as pool:
        list(pool.map(_one, pending))
    elapsed = time.monotonic() - t0
    logger.info('block tts done job_id=%s done=%s warnings=%s failed=%s seconds=%.1f',
                job_id, len(specs) - failed, warnings, failed, elapsed)
    if failed:
        raise RuntimeError(f'BLOCK_TTS_FAILED: {failed} blocks failed')
    return len(specs) - failed, warnings


def _tts_text(job_id: str, block_id: int) -> str:
    with SessionLocal() as db:
        row = db.get(SpeechBlock, (job_id, block_id))
        return (row.tts_text or '') if row else ''


def _overflow_block_ids(job_id: str) -> list[int]:
    from sqlalchemy import select

    with SessionLocal() as db:
        rows = list(db.execute(select(SpeechBlock).where(SpeechBlock.job_id == job_id)).scalars().all())
        return sorted(r.block_id for r in rows if r.qa_class in ('OVERFLOW', 'SEVERE_OVERFLOW'))


def _classify_all_blocks(job_id: str) -> dict:
    from sqlalchemy import select

    counts = {'FIT': 0, 'ADJUSTED_FIT': 0, 'OVERFLOW': 0, 'SEVERE_OVERFLOW': 0}
    max_overflow = 0
    with SessionLocal.begin() as db:
        rows = list(db.execute(select(SpeechBlock).where(SpeechBlock.job_id == job_id)).scalars().all())
        specs = {r.block_id: r for r in rows}
        for row in rows:
            avail = (row.end_ms - row.start_ms) if row.start_ms is not None and row.end_ms is not None else 0
            cls, final_ms, overflow = _classify_block(row.tts_duration_ms, row.tempo or 1.0, avail)
            row.qa_class = cls
            if cls in ('OVERFLOW', 'SEVERE_OVERFLOW'):
                max_overflow = max(max_overflow, overflow)
            counts[cls] += 1
    return {'counts': counts, 'max_overflow_ms': max_overflow}


def _compress_block_texts(job_id: str, specs: list[BlockSpec], block_ids: list[int]) -> list[int]:
    """AI-compress tts_text of overflow blocks (SRT untouched). Returns changed ids."""
    from app.translation.providers_toolnet import OpenAICompatibleProvider

    by_id = {s.block_id: s for s in specs}
    provider = OpenAICompatibleProvider()
    changed: list[int] = []
    lock = threading.Lock()

    def _chunk(chunk: list[int]) -> dict[int, str]:
        items = []
        for bid in chunk:
            with SessionLocal() as db:
                row = db.get(SpeechBlock, (job_id, bid))
                current = (row.tts_text or '') if row else ''
            if not current:
                continue
            dur = by_id[bid].end_ms - by_id[bid].start_ms
            items.append((bid, current, dur))
        if not items:
            return {}
        try:
            res, _used = provider.compress_batch(items, max_attempts=1)
        except Exception as exc:  # noqa: BLE001 - per-block fallback
            logger.warning('block batch compress failed job_id=%s error=%s', job_id, str(exc)[:200])
            res = {}
            for bid, text, dur in items:
                try:
                    res[bid] = provider.compress_text(bid, text, dur, max_attempts=1)
                except Exception as exc2:  # noqa: BLE001
                    logger.warning('block compress failed job_id=%s block=%s error=%s', job_id, bid, str(exc2)[:200])
        out: dict[int, str] = {}
        for bid, text, dur in items:
            shorter = res.get(bid)
            if shorter and compute_cps(shorter, dur) < compute_cps(text, dur):
                out[bid] = shorter
        return out

    chunks = [block_ids[i:i + 10] for i in range(0, len(block_ids), 10)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for partial in pool.map(_chunk, chunks):
            with SessionLocal.begin() as db:
                for bid, shorter in partial.items():
                    row = db.get(SpeechBlock, (job_id, bid))
                    if row is None:
                        continue
                    row.tts_text = shorter
                    changed.append(bid)
    # Invalidate TTS artifacts of changed blocks so they regenerate.
    for bid in changed:
        for p in (block_mp3(_workdir_of(job_id), bid), block_wav(_workdir_of(job_id), bid)):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        with SessionLocal.begin() as db:
            row = db.get(SpeechBlock, (job_id, bid))
            if row is not None:
                row.tts_duration_ms = None
                row.status = 'pending'
    logger.info('block compress improved job_id=%s blocks=%s', job_id, len(changed))
    return changed


def _workdir_of(job_id: str) -> Path:
    return settings.work_dir / job_id


def run_block_voice(job_id: str, worker_id: str) -> dict:
    from app.services.worker_util import lease_renewer as _lease_renewer, renew_job_lease, update_heartbeat
    def lease_renewer(job_id, worker_id):
        return _lease_renewer(job_id, worker_id, lambda: (renew_job_lease(job_id, worker_id, ('block_voice',)), update_heartbeat(worker_id, 'busy', job_id)))

    storage = get_storage()
    workdir = settings.work_dir / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    t_start = time.monotonic()
    tempos: list[float] = []
    try:
        with lease_renewer(job_id, worker_id):
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                if job is None:
                    raise RuntimeError('Job not found')
                vi_local = Path(job.vi_local_path) if job.vi_local_path else None
            if not vi_local or not vi_local.exists():
                raise RuntimeError('BLOCK_VOICE_NO_VI')
            srt_hash_before = _sha256(vi_local)
            cues, _ = _vi_cues(job_id)
            _set_stage(job_id, 'block_voice', 0)
            logger.info('block voice start job_id=%s cues=%s', job_id, len(cues))

            specs = build_blocks(
                cues,
                max_gap_ms=settings.voice_block_max_gap_ms,
                target_ms=settings.voice_block_target_ms,
                max_ms=settings.voice_block_max_ms,
            )
            _seed_blocks(job_id, specs)
            logger.info('block voice grouped job_id=%s blocks=%s avg_cues=%.1f avg_dur=%.0fms',
                        job_id, len(specs),
                        len(cues) / max(1, len(specs)),
                        sum(s.available_ms for s in specs) / max(1, len(specs)))

            from app.tts.providers_edge import resolve_voice_name

            voice = resolve_voice_name()

            def _on_tts(_bid: int) -> None:
                with SessionLocal.begin() as db:
                    job = db.get(Job, job_id)
                    if job is None:
                        return
                    from sqlalchemy import func, select

                    done = db.execute(
                        select(func.count()).select_from(SpeechBlock)
                        .where(SpeechBlock.job_id == job_id, SpeechBlock.tts_duration_ms.is_not(None))
                    ).scalar() or 0
                    job.progress = int(60 * done / max(1, len(specs)))

            # Initial synthesis + up to 2 QA rounds on overflow blocks.
            synthesize_blocks(specs, workdir, voice, job_id=job_id, on_progress=_on_tts)
            total_changed, total_regen = 0, 0
            for rnd in range(max(1, int(settings.qa_max_rounds))):
                stats = _classify_all_blocks(job_id)
                over_ids = _overflow_block_ids(job_id)
                logger.info('block voice round %s job_id=%s %s', rnd + 1, job_id, stats['counts'])
                if not over_ids:
                    break
                with SessionLocal.begin() as db:
                    job = db.get(Job, job_id)
                    if job is not None:
                        job.progress = 60 + int(20 * rnd / max(1, int(settings.qa_max_rounds)))
                changed = _compress_block_texts(job_id, specs, over_ids)
                total_changed += len(changed)
                if changed:
                    # Regen only changed blocks.
                    _regen_blocks(job_id, workdir, specs, changed, voice)
                    total_regen += len(changed)

            stats = _classify_all_blocks(job_id)
            manual_ids = set(_overflow_block_ids(job_id))
            with SessionLocal.begin() as db:
                from sqlalchemy import select

                rows = list(db.execute(select(SpeechBlock).where(SpeechBlock.job_id == job_id)).scalars().all())
                for row in rows:
                    row.manual_review_required = row.block_id in manual_ids
                    if row.tempo:
                        tempos.append(row.tempo)

            # SRT immutability guard.
            assert _sha256(vi_local) == srt_hash_before, 'VI SRT CHANGED DURING BLOCK VOICE'

            # Rebuild master timeline from blocks.
            from app.media.audio import ffprobe_duration
            from app.tts.voice import assemble_voice

            with SessionLocal() as db:
                job = db.get(Job, job_id)
                video_dur = None
                if job.local_video_path and Path(job.local_video_path).exists():
                    video_dur = ffprobe_duration(Path(job.local_video_path))
                video_dur = video_dur or job.duration_seconds
            if not video_dur:
                raise RuntimeError('BLOCK_VOICE_NO_VIDEO_DURATION')
            pseudo = [{'id': s.block_id, 'start_ms': s.start_ms, 'end_ms': s.end_ms} for s in specs]

            def _get_wav(bid: int) -> Path:
                p = block_wav(workdir, bid)
                if not p.exists():
                    raise RuntimeError(f'BLOCK_WAV_MISSING: block {bid}')
                return p

            with SessionLocal.begin() as db:
                db.get(Job, job_id).progress = 85
            voice_path = workdir / 'voice.vi.wav'
            voice_path, voice_dur = assemble_voice(pseudo, _get_wav, float(video_dur), voice_path, job_id=job_id)
            storage.put_file(voice_path, f'jobs/{job_id}/voice.vi.wav')

            report = build_block_report(job_id, specs, cues, voice_dur, float(video_dur), tempos, t_start,
                                        total_changed, total_regen)
            (workdir / 'block_voice_qa.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
            storage.put_file(workdir / 'block_voice_qa.json', f'jobs/{job_id}/block_voice_qa.json')
            with SessionLocal.begin() as db:
                job = db.get(Job, job_id)
                job.voice_local_path = str(voice_path)
                job.voice_storage_key = f'jobs/{job_id}/voice.vi.wav'
                job.voice_duration_seconds = voice_dur
                job.status = 'ready_for_render'
                job.current_stage = 'ready_for_render'
                job.progress = 100
                job.error = None
                job.lease_owner = None
                job.lease_until = None
            logger.info('block voice done job_id=%s %s', job_id, {k: v for k, v in report.items() if not isinstance(v, list)})
            return report
    except Exception as exc:
        _fail(job_id, exc)
        raise


def _regen_blocks(job_id: str, workdir: Path, specs: list[BlockSpec], bids: list[int], voice: str) -> None:
    by_id = {s.block_id: s for s in specs}

    def _on_progress(_bid: int) -> None:
        return None

    # Synthesize only the listed blocks: temporarily seed state so others skip.
    synthesize_selected(job_id, workdir, [by_id[b] for b in bids if b in by_id], voice)


def synthesize_selected(job_id: str, workdir: Path, specs: list[BlockSpec], voice: str) -> None:
    from app.tts.providers_edge import EdgeTTSProvider
    from app.tts.service import decode_to_wav, fit_tempo

    provider = EdgeTTSProvider()

    def _one(spec: BlockSpec) -> None:
        mp3, wav = block_mp3(workdir, spec.block_id), block_wav(workdir, spec.block_id)
        available = spec.available_ms
        # Fresh text (may have been compressed).
        with SessionLocal() as db:
            row = db.get(SpeechBlock, (job_id, spec.block_id))
            text = (row.tts_text or '') if row else spec.tts_text
        last_err: Exception | None = None
        for attempt in range(max(1, settings.tts_max_retries) + 1):
            try:
                if not mp3.exists() or mp3.stat().st_size <= 0:
                    clip = provider.synthesize(spec.block_id, text, mp3, voice)
                    spoken = clip.duration_ms
                else:
                    from app.tts.providers_edge import ffprobe_ms

                    spoken = ffprobe_ms(mp3)
                tempo, _warn = fit_tempo(spoken, available)
                decode_to_wav(mp3, wav, tempo=tempo, sample_rate=settings.tts_sample_rate)
                cls, final_ms, overflow = _classify_block(spoken, tempo, available)
                _save_block_state(job_id, spec.block_id, {
                    'mp3_path': str(mp3), 'wav_path': str(wav), 'tts_duration_ms': spoken,
                    'tempo': tempo, 'overflow_ms': overflow if cls in ('OVERFLOW', 'SEVERE_OVERFLOW') else 0,
                    'qa_class': cls, 'voice': voice, 'rate': settings.tts_rate, 'error': None})
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(min(5 * (attempt + 1), 20))
        _save_block_state(job_id, spec.block_id, {'error': str(last_err)[:500]})
        raise RuntimeError(f'BLOCK_TTS_FAILED {spec.block_id}: {last_err}')

    with ThreadPoolExecutor(max_workers=max(1, settings.tts_concurrency)) as pool:
        list(pool.map(_one, specs))


def build_block_report(job_id: str, specs: list[BlockSpec], cues: list[dict], voice_dur: float,
                       video_dur: float, tempos: list[float], t_start: float,
                       ai_changed: int, regen: int) -> dict:
    from sqlalchemy import select

    with SessionLocal() as db:
        rows = list(db.execute(select(SpeechBlock).where(SpeechBlock.job_id == job_id)).scalars().all())
    counts = {'FIT': 0, 'ADJUSTED_FIT': 0, 'OVERFLOW': 0, 'SEVERE_OVERFLOW': 0}
    cps_warn = 0
    max_overflow = 0
    manual: list[int] = []
    by_id = {r.block_id: r for r in rows}
    for r in rows:
        counts[r.qa_class or 'OVERFLOW'] = counts.get(r.qa_class or 'OVERFLOW', 0) + 1
        if r.manual_review_required:
            manual.append(r.block_id)
        if (r.overflow_ms or 0) > max_overflow:
            max_overflow = r.overflow_ms or 0
    # Subtitle CPS warnings (readability) — independent from voice overflow.
    for c in cues:
        if compute_cps(c['text'], c['end_ms'] - c['start_ms']) > settings.cps_target:
            cps_warn += 1
    # Crossing vs next block start (non-manual blocks must not cross).
    ordered = sorted(rows, key=lambda r: r.block_id)
    crossing = 0
    worst_cross = 0
    for i, r in enumerate(ordered):
        if i + 1 >= len(ordered) or r.manual_review_required:
            continue
        nxt = ordered[i + 1]
        avail_to_next = (nxt.start_ms - r.start_ms) if nxt.start_ms is not None and r.start_ms is not None else 0
        fitted = 0
        if r.tts_duration_ms:
            fitted = int(round(r.tts_duration_ms / max(0.5, min(r.tempo or 1.0, settings.tts_max_tempo))))
        if fitted > avail_to_next > 0:
            crossing += 1
            worst_cross = max(worst_cross, fitted - avail_to_next)
    avg_tempo = round(sum(tempos) / len(tempos), 3) if tempos else 1.0
    avg_dur = sum(s.available_ms for s in specs) / max(1, len(specs))
    return {
        'Subtitle cues': len(cues),
        'Subtitle timecodes changed': 'NO',
        'Speech blocks': len(specs),
        'Average cues/block': round(len(cues) / max(1, len(specs)), 2),
        'Average block duration': f'{avg_dur:.0f} ms',
        'TTS voice': (rows[0].voice if rows and rows[0].voice else settings.tts_voice),
        'Natural fit': counts['FIT'],
        'Adjusted fit': counts['ADJUSTED_FIT'],
        'AI-compressed blocks': ai_changed,
        'Overflow blocks': counts['OVERFLOW'],
        'Severe overflow blocks': counts['SEVERE_OVERFLOW'],
        'Manual review blocks': len(manual),
        'manual_review_blocks': sorted(manual)[:50],
        'Subtitle CPS warnings': cps_warn,
        'Blocks crossing next block': crossing,
        'Maximum overflow': f'{max_overflow}ms',
        'Average speed adjustment': f'{avg_tempo}x',
        'Max speed': f'{settings.tts_max_tempo}x',
        'Regenerated block TTS': regen,
        'voice.vi.wav duration': round(voice_dur, 2),
        'video duration': round(video_dur, 2),
        'duration diff': round(abs(voice_dur - video_dur), 3),
        'qa_seconds': round(time.monotonic() - t_start, 1),
    }
