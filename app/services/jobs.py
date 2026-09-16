import logging
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.bilibili.audio import extract_audio, extract_compressed_audio
from app.bilibili.downloader import download_video
from app.bilibili.resolver import resolve_bilibili_url
from app.bilibili.srt import validate_srt
from app.bilibili.subtitles import extract_chinese_subtitle
from app.config import settings
from app.db import SessionLocal
from app.models import Job, WorkerHeartbeat
from app.storage.factory import get_storage


logger = logging.getLogger('bilibili-worker')

# New Phase-1 flow + legacy 'extracting_subtitles' for backward compat.
ACTIVE_STATES = (
    'queued',
    'resolving',
    'downloading',
    'extracting_audio',
    'transcribing_chinese',
    'validating_srt',
    'extracting_subtitles',
    # Phase 2: translation + synchronized TTS.
    'translating',
    'validating_translation',
    'generating_tts',
    'syncing_voice',
    'validating_voice',
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_job(db: Session, source_url: str) -> Job:
    job = Job(source_url=source_url.strip(), status='queued', current_stage='queued', progress=0)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def update_heartbeat(worker_id: str, status: str, current_job_id: str | None = None) -> None:
    with SessionLocal.begin() as db:
        row = db.get(WorkerHeartbeat, worker_id)
        if row is None:
            row = WorkerHeartbeat(worker_id=worker_id, hostname=socket.gethostname())
            db.add(row)
        row.hostname = socket.gethostname()
        row.status = status
        row.current_job_id = current_job_id
        row.last_seen_at = utcnow()


def claim_next_job(worker_id: str) -> str | None:
    now = utcnow()
    with SessionLocal.begin() as db:
        stmt = (
            select(Job)
            .where(Job.status.in_(ACTIVE_STATES))
            .where(or_(Job.lease_until.is_(None), Job.lease_until < now, Job.lease_owner == worker_id))
            .order_by(Job.created_at.asc())
            .limit(1)
        )
        if not settings.database_url.startswith('sqlite'):
            stmt = stmt.with_for_update(skip_locked=True)
        job = db.execute(stmt).scalars().first()
        if job is None:
            return None
        job.lease_owner = worker_id
        job.lease_until = now + timedelta(seconds=settings.job_lease_seconds)
        job.attempts += 1
        if job.started_at is None:
            job.started_at = now
        return job.id


def renew_lease(job_id: str, worker_id: str) -> None:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job and job.lease_owner == worker_id and job.status in ACTIVE_STATES:
            job.lease_until = utcnow() + timedelta(seconds=settings.job_lease_seconds)


@contextmanager
def lease_renewer(job_id: str, worker_id: str):
    stop = threading.Event()

    def run() -> None:
        interval = max(5, min(settings.worker_heartbeat_seconds, settings.job_lease_seconds // 3))
        while not stop.wait(interval):
            try:
                renew_lease(job_id, worker_id)
                update_heartbeat(worker_id, 'busy', job_id)
            except Exception:
                pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)


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
    message = str(exc)
    if len(message) > 5000:
        message = message[:5000]
    bvid = cid = stage = None
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        bvid, cid, stage = job.bvid, job.cid, job.current_stage
        job.status = 'failed'
        job.current_stage = 'failed'
        job.error = message
        job.lease_owner = None
        job.lease_until = None
    logger.error('job failed job_id=%s stage=%s bvid=%s cid=%s error=%s', job_id, stage, bvid, cid, message[:2000])


def _finish(job_id: str) -> None:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = 'ready'
        job.current_stage = 'ready'
        job.progress = 100
        job.completed_at = utcnow()
        job.lease_owner = None
        job.lease_until = None
        job.error = None
        bvid, cid = job.bvid, job.cid
        sub_src, sub_lang, sub_cues = job.subtitle_source, job.subtitle_language, job.subtitle_cue_count
    logger.info(
        'job ready job_id=%s bvid=%s cid=%s subtitle_source=%s subtitle_language=%s subtitle_cues=%s',
        job_id, bvid, cid, sub_src, sub_lang, sub_cues,
    )


def _run_bilibili_subtitle_mode(resolved_url: str, bvid: str, cid: str | None, workdir: Path, job_id: str):
    """Legacy/fallback: Bilibili API -> yt-dlp discovery. Returns SubtitleResult."""
    return extract_chinese_subtitle(resolved_url, bvid, cid, workdir, job_id=job_id)


def process_job(job_id: str, worker_id: str) -> None:
    storage = get_storage()
    workdir = settings.work_dir / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    started_monotonic = time.monotonic()

    try:
        with lease_renewer(job_id, worker_id):
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                if job is None:
                    return
                source_url = job.source_url
                current_stage = job.current_stage

            logger.info('job start job_id=%s stage=%s worker=%s mode=%s', job_id, current_stage, worker_id, settings.subtitle_source_mode)

            # ---- Phase 2 dispatch (translation + TTS) ----
            if current_stage in {'translating', 'validating_translation', 'generating_tts', 'syncing_voice', 'validating_voice'}:
                from app.services.phase2 import process_phase2

                process_phase2(job_id, worker_id)
                return

            # ---- resolving (progress 10) ----
            if current_stage in {'queued', 'resolving'}:
                _set_stage(job_id, 'resolving', 10)
                logger.info('job resolving job_id=%s stage=resolving', job_id)
                resolved = resolve_bilibili_url(source_url)
                with SessionLocal.begin() as db:
                    job = db.get(Job, job_id)
                    job.resolved_url = resolved.resolved_url
                    job.bvid = resolved.bvid
                    job.cid = resolved.cid
                    job.title = resolved.title
                    job.author = resolved.author
                    job.cover_url = resolved.cover_url
                    job.duration_seconds = resolved.duration_seconds
                    job.progress = 10
                logger.info(
                    'job resolved job_id=%s stage=resolving bvid=%s cid=%s title=%s duration=%s',
                    job_id, resolved.bvid, resolved.cid,
                    (resolved.title or '')[:200], resolved.duration_seconds,
                )

            with SessionLocal() as db:
                job = db.get(Job, job_id)
                resolved_url = job.resolved_url or job.source_url
                bvid = job.bvid
                cid = job.cid
                video_storage_key = job.video_storage_key
                local_video_path = Path(job.local_video_path) if job.local_video_path else None

            if not bvid:
                raise RuntimeError('BVID missing after resolve')

            # ---- downloading (20-45) ----
            if not video_storage_key and (not local_video_path or not local_video_path.exists()):
                _set_stage(job_id, 'downloading', 20)
                logger.info('job downloading job_id=%s stage=downloading bvid=%s cid=%s', job_id, bvid, cid)
                video_path = download_video(resolved_url, workdir, job_id=job_id, bvid=bvid, cid=cid)
                size = video_path.stat().st_size if video_path.exists() else 0
                logger.info('job downloaded job_id=%s stage=downloading bvid=%s cid=%s file=%s size=%s', job_id, bvid, cid, video_path, size)
                remote_key = None
                if settings.persist_original_video:
                    remote_key = storage.put_file(video_path, f'jobs/{job_id}/original.mp4')
                    logger.info('job video persisted job_id=%s bvid=%s cid=%s key=%s', job_id, bvid, cid, remote_key)
                with SessionLocal.begin() as db:
                    job = db.get(Job, job_id)
                    job.local_video_path = str(video_path)
                    job.video_storage_key = remote_key
                    job.progress = 45
            else:
                logger.info(
                    'job download skipped (already present) job_id=%s bvid=%s cid=%s local=%s key=%s',
                    job_id, bvid, cid, local_video_path, video_storage_key,
                )
                with SessionLocal() as db:
                    job = db.get(Job, job_id)
                    video_path = Path(job.local_video_path) if job.local_video_path else None
                if video_path is None or not video_path.exists():
                    # Persisted remotely but no local copy: re-resolve local from workdir.
                    candidate = workdir / 'original.mp4'
                    alt = workdir / 'source.mp4'
                    if candidate.exists():
                        video_path = candidate
                    elif alt.exists():
                        video_path = alt
                    else:
                        raise RuntimeError('video marked done but local file missing')
                # Ensure progress reflects download done.
                with SessionLocal.begin() as db:
                    job = db.get(Job, job_id)
                    if job.progress < 45:
                        job.progress = 45

            with SessionLocal() as db:
                job = db.get(Job, job_id)
                subtitle_storage_key = job.subtitle_storage_key
                subtitle_source = job.subtitle_source
                local_subtitle = Path(job.local_subtitle_path) if job.local_subtitle_path else None

            if subtitle_storage_key or (subtitle_source in {'ASR', 'BILIBILI_API', 'BILIBILI'} and local_subtitle and local_subtitle.exists()):
                logger.info(
                    'job subtitle skipped (already done) job_id=%s bvid=%s cid=%s source=%s',
                    job_id, bvid, cid, subtitle_source,
                )
                _finish(job_id)
                return

            mode = (settings.subtitle_source_mode or 'asr').strip().lower()
            if mode not in {'asr', 'bilibili', 'auto'}:
                mode = 'asr'

            srt_path: Path | None = None
            srt_lang: str | None = None
            srt_cues: int | None = None
            srt_source: str | None = None

            if mode == 'bilibili':
                # Optional fallback/debug only.
                _set_stage(job_id, 'extracting_subtitles', 65)
                logger.info('job subtitle bilibili mode job_id=%s bvid=%s cid=%s', job_id, bvid, cid)
                subtitle = _run_bilibili_subtitle_mode(resolved_url, bvid, cid, workdir, job_id)
                srt_path, srt_lang, srt_cues, srt_source = subtitle.path, subtitle.language, subtitle.cue_count, subtitle.source
            else:
                # ---- extracting_audio (50): compressed m4a (small upload). ----
                # WAV is created lazily below only if Whisper fallback needs it.
                _set_stage(job_id, 'extracting_audio', 50)
                logger.info('job extracting audio job_id=%s stage=extracting_audio bvid=%s cid=%s', job_id, bvid, cid)

                if mode == 'auto':
                    # Try Bilibili first, fall back to ASR on NO_SUBTITLE.
                    try:
                        _set_stage(job_id, 'extracting_subtitles', 65)
                        subtitle = _run_bilibili_subtitle_mode(resolved_url, bvid, cid, workdir, job_id)
                        srt_path, srt_lang, srt_cues, srt_source = subtitle.path, subtitle.language, subtitle.cue_count, subtitle.source
                        logger.info('job subtitle auto: bilibili hit job_id=%s source=%s cues=%s', job_id, srt_source, srt_cues)
                    except RuntimeError as exc:
                        if 'NO_SUBTITLE_FOUND' not in str(exc):
                            raise
                        logger.info('job subtitle auto: bilibili miss, fallback ASR job_id=%s', job_id)

                if srt_path is None:
                    # ---- transcribing_chinese (55-90) via ASR provider service ----
                    from app.asr.service import transcribe_with_fallback

                    _set_stage(job_id, 'transcribing_chinese', 55)
                    asr_start = utcnow()
                    with SessionLocal.begin() as db:
                        job = db.get(Job, job_id)
                        job.asr_started_at = asr_start
                    logger.info('job transcribing job_id=%s stage=transcribing_chinese bvid=%s cid=%s provider=%s', job_id, bvid, cid, settings.asr_provider)
                    # Compressed m4a for JianYing (fast upload); WAV only if Whisper needs it.
                    compressed = extract_compressed_audio(video_path, workdir, job_id=job_id, bvid=bvid, cid=cid)
                    wav_path: Path | None = None
                    need_wav = (settings.asr_provider or 'auto').strip().lower() in {'whisper'} or not (
                        settings.allow_remote_asr and settings.jianying_enabled
                    )
                    if need_wav:
                        wav_path = extract_audio(video_path, workdir, job_id=job_id, bvid=bvid, cid=cid)
                    try:
                        out_path, lang, cues, provider, fallback_used, asr_secs, up_secs, rec_secs = transcribe_with_fallback(
                            compressed, wav_path, workdir, job_id=job_id,
                        )
                    except Exception:
                        # Lazy WAV fallback: JianYing failed and whisper needs WAV.
                        if wav_path is None:
                            wav_path = extract_audio(video_path, workdir, job_id=job_id, bvid=bvid, cid=cid)
                            out_path, lang, cues, provider, fallback_used, asr_secs, up_secs, rec_secs = transcribe_with_fallback(
                                compressed, wav_path, workdir, job_id=job_id,
                            )
                        else:
                            raise
                    # If whisper fallback produced WAV needlessly, keep files until cleanup.
                    srt_path, srt_lang, srt_cues, srt_source = out_path, lang, cues, 'ASR'
                    asr_done = utcnow()
                    with SessionLocal.begin() as db:
                        job = db.get(Job, job_id)
                        job.progress = 90
                        job.asr_provider = provider
                        job.asr_completed_at = asr_done
                        job.asr_processing_seconds = float(asr_secs)
                        job.asr_fallback_used = bool(fallback_used)
                    logger.info(
                        'job transcribed job_id=%s provider=%s fallback=%s cues=%s asr_seconds=%.1f upload=%.1f recog=%.1f audio_size=%s',
                        job_id, provider, fallback_used, cues, asr_secs,
                        up_secs if up_secs is not None else -1,
                        rec_secs if rec_secs is not None else -1,
                        compressed.stat().st_size if compressed.exists() else -1,
                    )

            if srt_path is None:
                raise RuntimeError('subtitle generation produced no file')

            # ---- validating_srt (95) ----
            _set_stage(job_id, 'validating_srt', 95)
            logger.info('job validating srt job_id=%s stage=validating_srt bvid=%s cid=%s file=%s', job_id, bvid, cid, srt_path)
            try:
                validated_count = validate_srt(srt_path)
            except RuntimeError as exc:
                raise RuntimeError(f'INVALID_SRT_FORMAT: {exc}') from exc
            srt_cues = validated_count

            subtitle_key = storage.put_file(srt_path, f'jobs/{job_id}/source.zh.srt')
            with SessionLocal.begin() as db:
                job = db.get(Job, job_id)
                job.local_subtitle_path = str(srt_path)
                job.subtitle_storage_key = subtitle_key
                job.subtitle_language = srt_lang or 'zh'
                job.subtitle_cue_count = srt_cues
                job.subtitle_source = srt_source or 'ASR'
                job.progress = 95
            logger.info(
                'job subtitle done job_id=%s bvid=%s cid=%s source=%s lang=%s cues=%s',
                job_id, bvid, cid, srt_source, srt_lang, srt_cues,
            )

            # ---- cleanup: keep original.mp4 + source.zh.srt; remove intermediates ----
            try:
                for name in ('audio.wav', 'audio.m4a'):
                    p = workdir / name
                    if p.exists():
                        p.unlink()
                        logger.info('job cleanup audio removed job_id=%s file=%s', job_id, name)
                for tmp in workdir.glob('jianying.*.json'):
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            except OSError as exc:
                logger.warning('job cleanup audio failed job_id=%s error=%s', job_id, exc)

            _finish(job_id)
            elapsed = time.monotonic() - started_monotonic
            logger.info('job elapsed job_id=%s seconds=%.1f', job_id, elapsed)
    except Exception as exc:
        _fail(job_id, exc)
        raise
