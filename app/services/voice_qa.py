"""FINAL VOICE QA: classify TTS fit, compress OVERFLOW cues via AI, regen TTS, reassemble.

Runs on the real job (no mock). Timestamps are never touched: compression only
rewrites text_vi, and source.vi.srt is rebuilt from DB + revalidated byte-for-byte
against source.zh.srt before any TTS regeneration counts.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.models import CueState, Job
from app.storage.factory import get_storage
from app.translation.service import build_vi_srt, compute_cps, parse_srt_cues, validate_vi_against_zh

logger = logging.getLogger('voice-qa')

QA_STATES = ('voice_qa',)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def classify_cue(spoken_ms: int | None, tempo: float | None, available_ms: int) -> tuple[str, int | None, int]:
    """Return (qa_class, final_tts_ms, overflow_ms).

    FIT: voice fits as-is. ADJUSTED_FIT: fits after rate/atempo (<= max).
    OVERFLOW: still exceeds end timestamp. SEVERE_OVERFLOW: >250ms or >10% over.
    """
    if spoken_ms is None or spoken_ms <= 0 or available_ms <= 0:
        return 'OVERFLOW', spoken_ms, max(0, (spoken_ms or 0) - max(0, available_ms))
    if spoken_ms <= available_ms:
        return 'FIT', spoken_ms, 0
    tempo = tempo if tempo and tempo > 0 else 1.0
    tempo = min(tempo, settings.tts_max_tempo)
    final_ms = int(round(spoken_ms / tempo))
    if final_ms <= available_ms:
        return 'ADJUSTED_FIT', final_ms, 0
    overflow = final_ms - available_ms
    if overflow > settings.qa_severe_overflow_ms or overflow / available_ms > settings.qa_severe_overflow_ratio:
        return 'SEVERE_OVERFLOW', final_ms, overflow
    return 'OVERFLOW', final_ms, overflow


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
    logger.error('voice qa failed job_id=%s error=%s', job_id, message[:1000])


def _load_rows(job_id: str) -> list[CueState]:
    from sqlalchemy import select

    with SessionLocal() as db:
        return list(db.execute(select(CueState).where(CueState.job_id == job_id).order_by(CueState.cue_id)).scalars().all())


def classify_all(job_id: str) -> dict:
    """Classify every cue from stored TTS data. Persists qa_class/final_tts_ms. Returns counts."""
    counts = {'FIT': 0, 'ADJUSTED_FIT': 0, 'OVERFLOW': 0, 'SEVERE_OVERFLOW': 0}
    max_overflow = 0
    worst: list[int] = []
    with SessionLocal.begin() as db:
        from sqlalchemy import select

        rows = list(db.execute(select(CueState).where(CueState.job_id == job_id)).scalars().all())
        for row in rows:
            avail = (row.end_ms - row.start_ms) if row.start_ms is not None and row.end_ms is not None else 0
            cls, final_ms, overflow = classify_cue(row.tts_duration_ms, row.tempo or 1.0, avail)
            row.qa_class = cls
            row.final_tts_ms = final_ms
            if cls in ('OVERFLOW', 'SEVERE_OVERFLOW'):
                row.timing_warning = True
                if overflow > max_overflow:
                    max_overflow = overflow
                    worst = [row.cue_id]
                elif overflow == max_overflow and max_overflow > 0:
                    worst.append(row.cue_id)
            counts[cls] += 1
    return {'counts': counts, 'max_overflow_ms': max_overflow, 'worst_cues': worst[:10]}


def _regen_single_cue(job_id: str, workdir: Path, cue: dict, text_vi: str) -> None:
    """Regenerate TTS for exactly one cue (delete artifacts, synth, fit, save state)."""
    from app.tts.providers_edge import EdgeTTSProvider
    from app.tts.service import clip_mp3, clip_wav, synthesize_cues

    for p in (clip_mp3(workdir, cue['id']), clip_wav(workdir, cue['id'])):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    with SessionLocal.begin() as db:
        row = db.get(CueState, (job_id, cue['id']))
        if row is not None:
            row.tts_path = None
            row.tts_duration_ms = None
            row.tts_status = 'needs_regeneration'
            row.tempo = None

    def _get(_cid: int) -> dict | None:
        return None  # force regeneration

    def _save(_cid: int, info: dict) -> None:
        with SessionLocal.begin() as db2:
            row = db2.get(CueState, (job_id, _cid))
            if row is None:
                return
            row.tts_path = info.get('tts_path')
            row.tts_duration_ms = info.get('tts_duration_ms')
            row.tempo = info.get('tempo', 1.0)
            row.timing_warning = bool(info.get('timing_warning'))
            row.tts_status = 'done' if info.get('tts_duration_ms') else 'failed'
            row.final_tts_ms = None  # recomputed in classify_all
            row.error = info.get('error')

    synthesize_cues([cue], {cue['id']: text_vi}, workdir, EdgeTTSProvider(), job_id=job_id,
                    get_state=_get, save_state=_save)


def _compress_and_regen(job_id: str, workdir: Path, cues: list[dict], overflow_ids: list[int]) -> tuple[int, int, int]:
    """One QA round: AI-compress overflow cues (batched 10/request), regen TTS for changed cues.

    Returns (changed, regenerated, still_over).
    """
    from app.translation.providers_toolnet import OpenAICompatibleProvider

    by_id = {c['id']: c for c in cues}
    provider = OpenAICompatibleProvider()
    improved: dict[int, str] = {}
    lock = threading.Lock()

    def _chunk(chunk: list[int]) -> dict[int, str]:
        t0 = time.monotonic()
        logger.info('qa chunk start job_id=%s cues=%s first=%s', job_id, len(chunk), chunk[0] if chunk else None)
        items = [(cid, _current_text(job_id, cid), by_id[cid]['end_ms'] - by_id[cid]['start_ms']) for cid in chunk]
        items = [(cid, t, d) for cid, t, d in items if t]
        if not items:
            return {}
        try:
            # QA compression: single attempt per model (per-cue fallback below preserves
            # robustness; translation batches keep full TRANSLATION_MAX_RETRIES).
            res, used = provider.compress_batch(items, max_attempts=1)
            logger.info('qa chunk batch ok job_id=%s cues=%s model=%s elapsed=%.1fs',
                        job_id, len(items), used, time.monotonic() - t0)
        except Exception as exc:  # noqa: BLE001 - per-cue fallback below
            logger.warning('qa batch compress failed job_id=%s cues=%s error=%s', job_id, len(items), str(exc)[:200])
            res = {}
            for cid, text, dur in items:
                try:
                    s = provider.compress_text(cid, text, dur, max_attempts=1)
                    res[cid] = s
                except Exception as exc2:  # noqa: BLE001
                    logger.warning('qa compress failed job_id=%s cue=%s error=%s', job_id, cid, str(exc2)[:200])
        out: dict[int, str] = {}
        for cid, text, dur in items:
            shorter = res.get(cid)
            if shorter and compute_cps(shorter, dur) < compute_cps(text, dur):
                out[cid] = shorter
        logger.info('qa chunk done job_id=%s improved=%s/%s elapsed=%.1fs', job_id, len(out), len(items), time.monotonic() - t0)
        return out

    chunks = [overflow_ids[i:i + 10] for i in range(0, len(overflow_ids), 10)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for partial in pool.map(_chunk, chunks):
            with lock:
                improved.update(partial)
    logger.info('qa compress improved job_id=%s cues=%s', job_id, len(improved))

    with SessionLocal.begin() as db:
        for cid, shorter in improved.items():
            row = db.get(CueState, (job_id, cid))
            if row is None:
                continue
            dur = by_id[cid]['end_ms'] - by_id[cid]['start_ms']
            row.vi_text = shorter
            row.translated = True
            row.cps = compute_cps(shorter, dur)
            from app.translation.service import estimate_speech_ms

            row.estimated_speech_ms = estimate_speech_ms(shorter)
    changed = sorted(improved)

    regenerated = 0
    if changed:
        def _regen(cid: int) -> bool:
            try:
                _regen_single_cue(job_id, workdir, by_id[cid], improved[cid])
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning('qa regen failed job_id=%s cue=%s error=%s', job_id, cid, str(exc)[:200])
                return False

        with ThreadPoolExecutor(max_workers=6) as pool:
            for ok in pool.map(_regen, changed):
                regenerated += 1 if ok else 0
    # Reclassify regenerated cues.
    stats = classify_all(job_id)
    over = stats['counts']['OVERFLOW'] + stats['counts']['SEVERE_OVERFLOW']
    return len(changed), regenerated, over


def _current_text(job_id: str, cue_id: int) -> str:
    with SessionLocal() as db:
        row = db.get(CueState, (job_id, cue_id))
        return (row.vi_text or '') if row else ''


def run_voice_qa(job_id: str, worker_id: str) -> dict:
    from app.services.worker_util import lease_renewer as _lease_renewer, renew_job_lease, update_heartbeat
    def lease_renewer(job_id, worker_id):
        return _lease_renewer(job_id, worker_id, lambda: (renew_job_lease(job_id, worker_id, ('voice_qa',)), update_heartbeat(worker_id, 'busy', job_id)))

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
                zh_local = Path(job.local_subtitle_path) if job.local_subtitle_path else None
            if not zh_local or not zh_local.exists():
                raise RuntimeError('VOICE_QA_NO_ZH')
            cues = parse_srt_cues(zh_local)
            _set_stage(job_id, 'voice_qa', 0)
            logger.info('voice qa start job_id=%s cues=%s', job_id, len(cues))

            # Baseline classification.
            stats = classify_all(job_id)
            logger.info('voice qa baseline job_id=%s %s', job_id, stats['counts'])

            # Overflow rounds (max qa_max_rounds): compress + regen.
            total_changed = 0
            total_regen = 0
            for rnd in range(max(1, int(settings.qa_max_rounds))):
                over_ids = _overflow_ids(job_id)
                if not over_ids:
                    break
                logger.info('voice qa round %s job_id=%s overflow=%s', rnd + 1, job_id, len(over_ids))
                with SessionLocal.begin() as db:
                    job = db.get(Job, job_id)
                    if job is not None:
                        job.progress = 5 + int(55 * rnd / max(1, int(settings.qa_max_rounds)))
                changed, regen, _ = _compress_and_regen(job_id, workdir, cues, over_ids)
                total_changed += changed
                total_regen += regen

            # Flag leftovers for manual review (explicit, never silent PASS).
            # Clear the flag for cues that now fit.
            stats = classify_all(job_id)
            manual_ids = set(_overflow_ids(job_id))
            with SessionLocal.begin() as db:
                from sqlalchemy import select

                rows = list(db.execute(select(CueState).where(CueState.job_id == job_id)).scalars().all())
                for row in rows:
                    row.manual_review_required = row.cue_id in manual_ids
                    if row.tempo:
                        tempos.append(row.tempo)

            # Rebuild VI SRT from DB (timestamps byte-identical) + validate.
            mapping = _mapping_from_db(job_id)
            if len(mapping) != len(cues):
                raise RuntimeError(f'VOICE_QA_INCOMPLETE: {len(mapping)}/{len(cues)} vi texts')
            from app.translation.service import build_vi_srt, validate_vi_against_zh

            vi_text = build_vi_srt(cues, mapping)
            try:
                validate_vi_against_zh(zh_local, vi_text)
            except RuntimeError as exc:
                raise RuntimeError(f'{exc}') from exc
            vi_path = workdir / 'source.vi.srt'
            vi_path.write_text(vi_text, encoding='utf-8', newline='\n')
            storage.put_file(vi_path, f'jobs/{job_id}/source.vi.srt')
            with SessionLocal.begin() as db:
                job = db.get(Job, job_id)
                job.vi_local_path = str(vi_path)
                job.vi_storage_key = f'jobs/{job_id}/source.vi.srt'
                job.progress = 70

            # Reassemble voice timeline + validate duration.
            from app.media.audio import ffprobe_duration
            from app.tts.service import clip_wav
            from app.tts.voice import assemble_voice

            with SessionLocal() as db:
                job = db.get(Job, job_id)
                video_dur = None
                if job.local_video_path and Path(job.local_video_path).exists():
                    video_dur = ffprobe_duration(Path(job.local_video_path))
                video_dur = video_dur or job.duration_seconds
            if not video_dur:
                raise RuntimeError('VOICE_QA_NO_VIDEO_DURATION')
            voice_path = workdir / 'voice.vi.wav'

            def _get_wav(cid: int) -> Path:
                p = clip_wav(workdir, cid)
                if not p.exists():
                    raise RuntimeError(f'TTS_WAV_MISSING: cue {cid}')
                return p

            with SessionLocal.begin() as db:
                db.get(Job, job_id).progress = 80
            voice_path, voice_dur = assemble_voice(cues, _get_wav, float(video_dur), voice_path, job_id=job_id)
            storage.put_file(voice_path, f'jobs/{job_id}/voice.vi.wav')

            report = build_report(job_id, cues, voice_dur, float(video_dur), tempos, t_start,
                                ai_changed=total_changed, regen=total_regen)
            (workdir / 'voice_qa.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
            storage.put_file(workdir / 'voice_qa.json', f'jobs/{job_id}/voice_qa.json')
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
            logger.info('voice qa done job_id=%s overflow=%s manual=%s', job_id,
                        report['TTS OVERFLOW'] + report['TTS SEVERE_OVERFLOW'], len(report['manual_review_cues']))
            return report
    except Exception as exc:
        _fail(job_id, exc)
        raise


def _overflow_ids(job_id: str) -> list[int]:
    from sqlalchemy import select

    with SessionLocal() as db:
        rows = list(db.execute(select(CueState).where(CueState.job_id == job_id)).scalars().all())
        return sorted(r.cue_id for r in rows if r.qa_class in ('OVERFLOW', 'SEVERE_OVERFLOW'))


def _mapping_from_db(job_id: str) -> dict[int, str]:
    from sqlalchemy import select

    with SessionLocal() as db:
        rows = list(db.execute(select(CueState).where(CueState.job_id == job_id)).scalars().all())
        return {r.cue_id: r.vi_text for r in rows if r.translated and (r.vi_text or '').strip()}


def build_report(job_id: str, cues: list[dict], voice_dur: float, video_dur: float,
                 tempos: list[float], t_start: float, ai_changed: int = 0, regen: int = 0) -> dict:
    from sqlalchemy import select

    with SessionLocal() as db:
        rows = list(db.execute(select(CueState).where(CueState.job_id == job_id)).scalars().all())
    counts = {'FIT': 0, 'ADJUSTED_FIT': 0, 'OVERFLOW': 0, 'SEVERE_OVERFLOW': 0}
    cps_ok = 0
    max_overflow = 0
    manual: list[int] = []
    for r in rows:
        counts[r.qa_class or 'OVERFLOW'] = counts.get(r.qa_class or 'OVERFLOW', 0) + 1
        if (r.cps or 0) <= settings.cps_target:
            cps_ok += 1
        if r.qa_class in ('OVERFLOW', 'SEVERE_OVERFLOW'):
            ov = max(0, (r.final_tts_ms or 0) - ((r.end_ms - r.start_ms) if r.end_ms and r.start_ms else 0))
            max_overflow = max(max_overflow, ov)
        if r.manual_review_required:
            manual.append(r.cue_id)
    # Crossing check: final fitted end vs next cue start (non-manual cues must not cross).
    by_id = {r.cue_id: r for r in rows}
    crossing = False
    for r in sorted(rows, key=lambda x: x.cue_id):
        nxt = by_id.get(r.cue_id + 1)
        if nxt is None or r.manual_review_required:
            continue
        if (r.final_tts_ms or 0) > (nxt.start_ms - r.start_ms):
            crossing = True
            break
    avg_tempo = round(sum(tempos) / len(tempos), 3) if tempos else 1.0
    return {
        'Total cues': len(cues),
        'CPS target': settings.cps_target,
        'CPS <=20': cps_ok,
        'CPS >20': len(cues) - cps_ok,
        'TTS FIT': counts['FIT'],
        'TTS ADJUSTED_FIT': counts['ADJUSTED_FIT'],
        'TTS OVERFLOW': counts['OVERFLOW'],
        'TTS SEVERE_OVERFLOW': counts['SEVERE_OVERFLOW'],
        'AI compressed cues': ai_changed,
        'Regenerated TTS': regen,
        'Manual review required': len(manual),
        'manual_review_cues': sorted(manual)[:50],
        'Maximum overflow': f'{max_overflow} ms',
        'Average speech speed adjustment': f'{avg_tempo}x',
        'Max atempo': f'{settings.tts_max_tempo}x',
        'Any audio crossing next cue': 'YES' if crossing else 'NO',
        'voice.vi.wav duration': round(voice_dur, 2),
        'video duration': round(video_dur, 2),
        'duration diff': round(abs(voice_dur - video_dur), 3),
        'qa_seconds': round(time.monotonic() - t_start, 1),
    }
