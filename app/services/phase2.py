"""Phase 2 orchestration: zh -> VI translation -> Edge TTS -> voice.vi.wav timeline.

Idempotent resume via cue_states + workdir checkpoints. Timestamps are never
changed by translation; TTS adapts to subtitle timing.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.bilibili.srt import validate_srt
from app.config import settings
from app.db import SessionLocal
from app.models import CueState, Job
from app.storage.factory import get_storage
from app.translation.service import (
    build_vi_srt,
    estimate_speech_ms,
    parse_srt_cues,
    translate_cues,
    validate_vi_against_zh,
)

logger = logging.getLogger('phase2')

PHASE2_STATES = (
    'translating',
    'validating_translation',
    'generating_tts',
    'syncing_voice',
    'validating_voice',
)


def utcnow():
    from datetime import datetime, timezone

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
    logger.error('phase2 failed job_id=%s error=%s', job_id, message[:1000])


def _finish_render_ready(job_id: str) -> None:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = 'ready_for_render'
        job.current_stage = 'ready_for_render'
        job.progress = 100
        job.phase2_completed_at = utcnow()
        if job.phase2_started_at:
            start = job.phase2_started_at
            if start.tzinfo is None:
                from datetime import timezone

                start = start.replace(tzinfo=timezone.utc)
            job.phase2_seconds = (utcnow() - start).total_seconds()
        job.completed_at = utcnow()
        job.lease_owner = None
        job.lease_until = None
        job.error = None
    logger.info('phase2 ready_for_render job_id=%s', job_id)


def _zh_cues(job_id: str, workdir: Path) -> tuple[list[dict], Path]:
    """Resolve source.zh.srt locally (download from storage if needed)."""
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        local = Path(job.local_subtitle_path) if job.local_subtitle_path else None
        key = job.subtitle_storage_key
    if local and local.exists():
        zh_path = local
    elif key:
        zh_path = workdir / 'source.zh.srt'
        if not zh_path.exists():
            zh_path.write_bytes(get_storage().get_bytes(key))
    else:
        raise RuntimeError('PHASE2_NO_ZH: source.zh.srt not available (run Phase 1 first)')
    cues = parse_srt_cues(zh_path)
    if not cues:
        raise RuntimeError('PHASE2_NO_ZH: empty Chinese subtitles')
    return cues, zh_path


def _seed_cues(job_id: str, cues: list[dict]) -> None:
    with SessionLocal.begin() as db:
        for cue in cues:
            row = db.get(CueState, (job_id, cue['id']))
            if row is None:
                db.add(CueState(job_id=job_id, cue_id=cue['id'], start_ms=cue['start_ms'],
                                end_ms=cue['end_ms'], zh_text=cue['text'], translated=False,
                                tts_status='pending'))
            elif row.zh_text != cue['text'] or row.start_ms != cue['start_ms']:
                row.zh_text = cue['text']
                row.start_ms = cue['start_ms']
                row.end_ms = cue['end_ms']


def _mapping_from_db(job_id: str) -> dict[int, str]:
    with SessionLocal() as db:
        from sqlalchemy import select

        rows = db.execute(select(CueState).where(CueState.job_id == job_id)).scalars().all()
        return {r.cue_id: r.vi_text for r in rows if r.translated and (r.vi_text or '').strip()}


def _save_translations(job_id: str, mapping: dict[int, str]) -> None:
    from app.translation.service import compute_cps

    with SessionLocal.begin() as db:
        for cid, text in mapping.items():
            row = db.get(CueState, (job_id, cid))
            if row is None:
                continue
            if (row.vi_text or '') != text:
                # Text changed (re-translation/edit): stale TTS must regenerate.
                row.tts_path = None
                row.tts_duration_ms = None
                row.tts_status = 'needs_regeneration'
                row.tempo = None
            row.vi_text = text
            row.translated = True
            row.tts_status = row.tts_status or 'pending'
            row.estimated_speech_ms = estimate_speech_ms(text)
            dur = (row.end_ms - row.start_ms) if row.start_ms is not None and row.end_ms is not None else 0
            row.cps = compute_cps(text, dur) if dur > 0 else None
            row.error = None


def _compress_long_cues(cues: list[dict], mapping: dict[int, str], *, job_id: str | None = None,
                        cps_limit: float | None = None, max_rounds: int = 2) -> dict[int, str]:
    """Auto-compress cues with CPS > limit via the model (same chain). Timing untouched."""
    from app.translation.providers_toolnet import OpenAICompatibleProvider
    from app.translation.service import compute_cps

    if cps_limit is None:
        cps_limit = settings.cps_target
    by_id = {c['id']: c for c in cues}
    provider = OpenAICompatibleProvider()
    compressed = 0
    for _ in range(max_rounds):
        over = [cid for cid, text in mapping.items()
                if compute_cps(text, by_id[cid]['end_ms'] - by_id[cid]['start_ms']) > cps_limit]
        if not over:
            break
        logger.info('phase2 cps round job_id=%s over=%s', job_id, len(over))
        from concurrent.futures import ThreadPoolExecutor

        def _one(cid: int) -> tuple[int, str | None]:
            dur = by_id[cid]['end_ms'] - by_id[cid]['start_ms']
            try:
                shorter = provider.compress_text(cid, mapping[cid], dur)
                if compute_cps(shorter, dur) < compute_cps(mapping[cid], dur):
                    return cid, shorter
            except Exception as exc:  # noqa: BLE001 - keep original, warn later
                logger.warning('compression failed job_id=%s cue=%s error=%s', job_id, cid, str(exc)[:200])
            return cid, None

        with ThreadPoolExecutor(max_workers=6) as pool:
            for cid, shorter in pool.map(_one, over):
                if shorter:
                    mapping[cid] = shorter
                    compressed += 1
    still_long: list[int] = []
    with SessionLocal.begin() as db:
        for cid, text in mapping.items():
            row = db.get(CueState, (job_id, cid))
            dur = by_id[cid]['end_ms'] - by_id[cid]['start_ms']
            cps = compute_cps(text, dur)
            if row is not None:
                row.vi_text = text
                row.cps = cps
                row.estimated_speech_ms = estimate_speech_ms(text)
            if cps > cps_limit:
                still_long.append(cid)
                if row is not None:
                    row.timing_warning = True
    logger.info('phase2 cps job_id=%s compressed=%s still_over_22=%s', job_id, compressed, len(still_long))
    return mapping


def _get_tts_state(job_id: str, cue_id: int) -> dict | None:
    with SessionLocal() as db:
        row = db.get(CueState, (job_id, cue_id))
        if row and row.tts_duration_ms:
            return {'tts_duration_ms': row.tts_duration_ms}
        return None


def _save_tts_state(job_id: str, cue_id: int, info: dict) -> None:
    with SessionLocal.begin() as db:
        row = db.get(CueState, (job_id, cue_id))
        if row is None:
            return
        row.tts_path = info.get('tts_path')
        row.tts_duration_ms = info.get('tts_duration_ms')
        row.tempo = info.get('tempo', 1.0)
        row.timing_warning = bool(info.get('timing_warning'))
        row.tts_status = 'done' if info.get('tts_duration_ms') else 'failed'
        row.error = info.get('error')


def _video_duration(job_id: str, workdir: Path) -> float:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        local_video = Path(job.local_video_path) if job.local_video_path else None
        declared = job.duration_seconds
    if local_video and local_video.exists():
        from app.bilibili.audio import ffprobe_duration

        dur = ffprobe_duration(local_video)
        if dur and dur > 0:
            return dur
    if declared and declared > 0:
        return float(declared)
    raise RuntimeError('PHASE2_NO_VIDEO_DURATION')


def process_phase2(job_id: str, worker_id: str) -> None:
    from app.services.jobs import lease_renewer

    storage = get_storage()
    workdir = settings.work_dir / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        if job.phase2_started_at is None:
            job.phase2_started_at = utcnow()
        stage = job.current_stage

    try:
        with lease_renewer(job_id, worker_id):
            cues, zh_path = _zh_cues(job_id, workdir)
            _seed_cues(job_id, cues)
            logger.info('phase2 start job_id=%s stage=%s cues=%s', job_id, stage, len(cues))

            # ---- translating (0-35) ----
            if stage in {'translating'}:
                _set_stage(job_id, 'translating', 0)
                t0 = utcnow()
                with SessionLocal.begin() as db:
                    db.get(Job, job_id).translation_started_at = t0

                def _on_chunk(index: int, total: int) -> None:
                    with SessionLocal.begin() as db:
                        job = db.get(Job, job_id)
                        if job is not None:
                            # Completions arrive out of order; never move backwards.
                            job.progress = max(job.progress, int(35 * (index + 1) / max(1, total)))

                # Translate only cues missing vi_text (resume).
                existing = _mapping_from_db(job_id)
                todo = [c for c in cues if c['id'] not in existing]
                if todo:
                    mapping_new, tinfo = translate_cues(
                        todo, workdir, job_id=job_id, on_chunk=_on_chunk)
                    _save_translations(job_id, mapping_new)
                    batches, seconds = tinfo['batches'], tinfo['seconds']
                    primary_model, fallback_model = tinfo['primary_model'], tinfo['fallback_model']
                    primary_n, fallback_n = tinfo['primary_batches'], tinfo['fallback_batches']
                    retries_n = tinfo['retries']
                else:
                    from app.config import settings as _s
                    from app.translation.service import count_checkpoint_models

                    primary_model, fallback_model = _s.translation_model, _s.translation_fallback_model
                    # Recount totals from checkpoints (this run did no translation work).
                    _nb = (len(cues) + max(1, _s.translation_batch_size) - 1) // max(1, _s.translation_batch_size)
                    batches, seconds, retries_n = _nb, 0.0, 0
                    primary_n, fallback_n = count_checkpoint_models(workdir, _nb, primary_model)
                    logger.info('phase2 translation skipped (all cached) job_id=%s primary=%s fallback=%s',
                                job_id, primary_n, fallback_n)
                full_mapping = _mapping_from_db(job_id)
                if len(full_mapping) != len(cues):
                    missing = [c['id'] for c in cues if c['id'] not in full_mapping]
                    raise RuntimeError(f'TRANSLATION_INCOMPLETE: {len(missing)} cues missing')
                # ---- CPS check + auto compression (timestamps untouched) ----
                full_mapping = _compress_long_cues(cues, full_mapping, job_id=job_id)
                vi_text = build_vi_srt(cues, full_mapping)
                validate_vi_against_zh(zh_path, vi_text)
                vi_path = workdir / 'source.vi.srt'
                vi_path.write_text(vi_text if vi_text.endswith('\n') else vi_text + '\n', encoding='utf-8', newline='\n')
                key = storage.put_file(vi_path, f'jobs/{job_id}/source.vi.srt')
                t1 = utcnow()
                with SessionLocal.begin() as db:
                    job = db.get(Job, job_id)
                    job.translation_provider = settings.translation_provider
                    job.translation_model = primary_model
                    job.translation_batches = batches
                    job.translation_seconds = seconds
                    job.translation_primary_model = primary_model
                    job.translation_fallback_model = fallback_model
                    job.translation_primary_batches = primary_n
                    job.translation_fallback_batches = fallback_n
                    job.translation_failed_batches = 0
                    job.translation_retries = retries_n
                    job.translation_completed_at = t1
                    job.vi_cue_count = len(cues)
                    job.progress = 35
                logger.info('phase2 translated job_id=%s cues=%s batches=%s primary=%s fallback=%s seconds=%.1f',
                            job_id, len(cues), batches, primary_n, fallback_n, seconds)

            # ---- validating_translation ----
            _set_stage(job_id, 'validating_translation', 36)
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                vi_path = Path(job.vi_local_path) if job.vi_local_path else workdir / 'source.vi.srt'
            if not vi_path.exists():
                # Rebuild from DB (e.g. entering at later stage after restart).
                full_mapping = _mapping_from_db(job_id)
                vi_text = build_vi_srt(cues, full_mapping)
                vi_path = workdir / 'source.vi.srt'
                vi_path.write_text(vi_text, encoding='utf-8', newline='\n')
            validate_srt(vi_path)
            validate_vi_against_zh(zh_path, vi_path.read_text(encoding='utf-8'))
            with SessionLocal.begin() as db:
                job = db.get(Job, job_id)
                if not job.vi_local_path:
                    job.vi_local_path = str(vi_path)
                if not job.vi_storage_key:
                    job.vi_storage_key = storage.put_file(vi_path, f'jobs/{job_id}/source.vi.srt')
                job.vi_cue_count = len(cues)
                job.progress = 37
            logger.info('phase2 translation validated job_id=%s cues=%s', job_id, len(cues))

            # ---- generating_tts (35-80) ----
            _set_stage(job_id, 'generating_tts', 40)
            from app.tts.providers_edge import EdgeTTSProvider
            from app.tts.service import clip_wav, synthesize_cues

            tts_provider = EdgeTTSProvider()
            full_mapping = _mapping_from_db(job_id)
            total_cues = len(cues)

            def _on_tts(_cid: int) -> None:
                with SessionLocal.begin() as db:
                    job = db.get(Job, job_id)
                    if job is None:
                        return
                    from sqlalchemy import func, select

                    done = db.execute(
                        select(func.count()).select_from(CueState)
                        .where(CueState.job_id == job_id, CueState.tts_duration_ms.is_not(None))
                    ).scalar() or 0
                    job.progress = 40 + int(40 * done / max(1, total_cues))

            clips, warnings, tts_seconds, _ = synthesize_cues(
                cues, full_mapping, workdir, tts_provider, job_id=job_id,
                get_state=lambda cid: _get_tts_state(job_id, cid),
                save_state=lambda cid, info: _save_tts_state(job_id, cid, info),
                on_progress=_on_tts,
            )
            with SessionLocal.begin() as db:
                job = db.get(Job, job_id)
                job.tts_provider = tts_provider.name
                job.tts_voice = tts_provider.resolve_voice()
                job.tts_clip_count = clips
                job.tts_seconds = tts_seconds
                job.tts_timing_warnings = warnings
                job.progress = 80
            logger.info('phase2 tts done job_id=%s clips=%s warnings=%s seconds=%.1f', job_id, clips, warnings, tts_seconds)

            # ---- syncing_voice (80-95) ----
            _set_stage(job_id, 'syncing_voice', 82)
            from app.tts.voice import assemble_voice

            video_dur = _video_duration(job_id, workdir)
            voice_path = workdir / 'voice.vi.wav'

            def _get_wav(cid: int) -> Path:
                p = clip_wav(workdir, cid)
                if not p.exists():
                    raise RuntimeError(f'TTS_WAV_MISSING: cue {cid}')
                return p

            voice_path, voice_dur = assemble_voice(cues, _get_wav, video_dur, voice_path, job_id=job_id)

            # ---- validating_voice (95-100) ----
            _set_stage(job_id, 'validating_voice', 96)
            from app.bilibili.audio import ffprobe_duration

            check = ffprobe_duration(voice_path)
            if check is None or abs(check - video_dur) >= 0.5:
                raise RuntimeError(f'VOICE_DURATION_MISMATCH: voice={check} video={video_dur}')
            voice_key = storage.put_file(voice_path, f'jobs/{job_id}/voice.vi.wav')
            with SessionLocal.begin() as db:
                job = db.get(Job, job_id)
                job.voice_local_path = str(voice_path)
                job.voice_storage_key = voice_key
                job.voice_duration_seconds = check
                job.progress = 98
            logger.info('phase2 voice done job_id=%s voice=%.2fs video=%.2fs diff=%.3f',
                        job_id, check, video_dur, abs(check - video_dur))

            _finish_render_ready(job_id)
    except Exception as exc:
        _fail(job_id, exc)
        raise


def apply_vi_edits(job_id: str, edits: list[dict]) -> dict:
    """PATCH handler: update VI texts, rebuild SRT, invalidate affected TTS, requeue voice sync."""
    from sqlalchemy import select

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise RuntimeError('Job not found')
        if job.status not in {'ready_for_render', 'ready'} and job.vi_cue_count is None and not job.vi_local_path:
            raise RuntimeError('VI subtitles not ready for edit')
        rows = {r.cue_id: r for r in db.execute(select(CueState).where(CueState.job_id == job_id)).scalars().all()}
    if not rows:
        raise RuntimeError('No cue states for job (run Phase 2 first)')
    changed: list[int] = []
    with SessionLocal.begin() as db:
        for edit in edits:
            cid = int(edit['id'])
            text = str(edit['text'] or '').strip()
            if not text:
                raise RuntimeError(f'VI edit empty text for cue {cid}')
            row = db.get(CueState, (job_id, cid))
            if row is None:
                raise RuntimeError(f'VI edit unknown cue {cid}')
            if (row.vi_text or '') != text:
                row.vi_text = text
                row.translated = True
                row.estimated_speech_ms = estimate_speech_ms(text)
                row.tts_path = None
                row.tts_duration_ms = None
                row.tts_status = 'needs_regeneration'
                row.timing_warning = None
                row.error = None
                changed.append(cid)
    if not changed:
        return {'job_id': job_id, 'changed': [], 'status': 'unchanged'}
    # Rebuild VI SRT from DB.
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        workdir = settings.work_dir / job_id
        rows = db.execute(select(CueState).where(CueState.job_id == job_id)).scalars().all()
        mapping = {r.cue_id: r.vi_text for r in rows if (r.vi_text or '').strip()}
        cues = [{'id': r.cue_id, 'start_ms': r.start_ms, 'end_ms': r.end_ms} for r in rows]
    cues.sort(key=lambda c: c['id'])
    zh_path = Path(job.local_subtitle_path) if job.local_subtitle_path else None
    vi_text = build_vi_srt(cues, mapping)
    if zh_path and zh_path.exists():
        validate_vi_against_zh(zh_path, vi_text)
    else:
        validate_srt_text_lenient(vi_text)
    vi_path = workdir / 'source.vi.srt'
    vi_path.write_text(vi_text, encoding='utf-8', newline='\n')
    key = get_storage().put_file(vi_path, f'jobs/{job_id}/source.vi.srt')
    # Remove stale TTS artifacts for changed cues only.
    for cid in changed:
        for p in (workdir / 'tts' / f'{cid:06d}.mp3', workdir / 'tts' / f'{cid:06d}.wav'):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        job.vi_local_path = str(vi_path)
        job.vi_storage_key = key
        job.status = 'syncing_voice'
        job.current_stage = 'syncing_voice'
        job.progress = 82
        job.error = None
        job.lease_owner = None
        job.lease_until = None
    logger.info('phase2 vi edited job_id=%s changed=%s requeued=syncing_voice', job_id, changed)
    return {'job_id': job_id, 'changed': changed, 'status': 'syncing_voice'}


def validate_srt_text_lenient(text: str) -> int:
    from app.bilibili.srt import validate_srt_text

    return validate_srt_text(text)
