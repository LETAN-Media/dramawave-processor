from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import CueState, Job, WorkerHeartbeat
from app.schemas import HealthOut, JobAccepted, JobCreate, JobOut, ViPatch
from app.security import require_api_key
from app.services.jobs import create_job
from app.storage.factory import get_storage


router = APIRouter()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.get('/health', response_model=HealthOut)
def health() -> HealthOut:
    database_ok = True
    worker_ok = False
    last_seen = None
    try:
        with SessionLocal() as db:
            heartbeat = db.execute(
                select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()).limit(1)
            ).scalar_one_or_none()
            if heartbeat:
                last_seen = heartbeat.last_seen_at
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                worker_ok = last_seen >= utcnow() - timedelta(seconds=max(60, settings.worker_heartbeat_seconds * 3))
    except Exception:
        database_ok = False
    return HealthOut(
        ok=database_ok,
        service=settings.app_name,
        database=database_ok,
        worker=worker_ok,
        worker_last_seen_at=last_seen,
        asr=_asr_health(),
    )


def _asr_health() -> dict:
    try:
        import shutil

        primary = (settings.asr_provider or 'auto').strip().lower()
        jy = bool(settings.jianying_enabled and shutil.which(settings.jianying_cli))
        out: dict = {'primary': primary, 'fallback': 'whisper', 'jianying_available': jy}
    except Exception:
        out = {'primary': 'auto', 'fallback': 'whisper', 'jianying_available': False}
    try:
        out['translation'] = {
            'provider': settings.translation_provider,
            'primary_model': settings.translation_model,
            'fallback_model': settings.translation_fallback_model,
            'configured': bool(settings.translation_api_key),
        }
    except Exception:
        out['translation'] = {'provider': 'toolnet', 'configured': False}
    try:
        from app.tts.providers_edge import resolve_voice_name

        try:
            import edge_tts  # noqa: F401

            tts_available = True
        except ImportError:
            tts_available = False
        out['tts'] = {
            'provider': settings.tts_provider,
            'voice': resolve_voice_name(),
            'available': tts_available,
        }
    except Exception:
        out['tts'] = {'provider': 'edge', 'available': False}
    return out


@router.post('/v1/jobs', response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_api_key)])
def submit_job(payload: JobCreate) -> JobAccepted:
    with SessionLocal() as db:
        job = create_job(db, payload.url)
        return JobAccepted(job_id=job.id, status=job.status)


@router.get('/v1/jobs', response_model=list[JobOut], dependencies=[Depends(require_api_key)])
def list_jobs(limit: int = 50) -> list[JobOut]:
    limit = max(1, min(limit, 200))
    with SessionLocal() as db:
        jobs = db.execute(select(Job).order_by(Job.created_at.desc()).limit(limit)).scalars().all()
        return [JobOut.model_validate(job) for job in jobs]


@router.get('/v1/jobs/{job_id}', response_model=JobOut, dependencies=[Depends(require_api_key)])
def get_job(job_id: str) -> JobOut:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        return JobOut.model_validate(job)


@router.get('/v1/jobs/{job_id}/subtitle', dependencies=[Depends(require_api_key)])
def get_subtitle(job_id: str) -> Response:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        key = job.subtitle_storage_key
        local_path = job.local_subtitle_path

    if key:
        try:
            data = get_storage().get_bytes(key)
            return Response(content=data, media_type='application/x-subrip; charset=utf-8')
        except Exception:
            pass  # fall back to local workdir copy (shared volume)

    if local_path:
        from pathlib import Path
        path = Path(local_path)
        if path.exists():
            return Response(content=path.read_bytes(), media_type='application/x-subrip; charset=utf-8')

    raise HTTPException(status_code=404, detail='Subtitle not ready')


@router.post('/v1/jobs/{job_id}/phase2', response_model=JobAccepted, dependencies=[Depends(require_api_key)])
def start_phase2(job_id: str) -> JobAccepted:
    """Queue Phase 2 (VI translation + synchronized TTS). Returns immediately; worker does the work."""
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        if job.status not in {'ready', 'ready_for_render', 'failed'}:
            raise HTTPException(status_code=409, detail=f'Phase 2 requires a ready job (status={job.status})')
        has_zh = bool(job.subtitle_storage_key) or bool(
            job.local_subtitle_path and Path(job.local_subtitle_path).exists())
        if not has_zh:
            raise HTTPException(status_code=409, detail='Phase 1 Chinese subtitles not ready')
        job.status = 'translating'
        job.current_stage = 'translating'
        job.progress = 0
        job.error = None
        job.lease_owner = None
        job.lease_until = None
        return JobAccepted(job_id=job.id, status=job.status)


def _subtitle_bytes(job: Job, kind: str) -> bytes:
    if kind == 'zh':
        key, local = job.subtitle_storage_key, job.local_subtitle_path
    else:
        key, local = job.vi_storage_key, job.vi_local_path
    if key:
        try:
            return get_storage().get_bytes(key)
        except Exception:
            pass
    if local and Path(local).exists():
        return Path(local).read_bytes()
    raise HTTPException(status_code=404, detail=f'Subtitle {kind} not ready')


@router.get('/v1/jobs/{job_id}/subtitles/zh', dependencies=[Depends(require_api_key)])
def get_subtitle_zh(job_id: str) -> Response:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        data = _subtitle_bytes(job, 'zh')
    return Response(content=data, media_type='application/x-subrip; charset=utf-8')


@router.get('/v1/jobs/{job_id}/subtitles/vi', dependencies=[Depends(require_api_key)])
def get_subtitle_vi(job_id: str) -> Response:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        data = _subtitle_bytes(job, 'vi')
    return Response(content=data, media_type='application/x-subrip; charset=utf-8')


@router.patch('/v1/jobs/{job_id}/subtitles/vi', dependencies=[Depends(require_api_key)])
def patch_subtitle_vi(job_id: str, payload: ViPatch) -> dict:
    """Edit VI cues; only affected TTS clips regenerate (job requeues to syncing_voice)."""
    with SessionLocal() as db:
        if db.get(Job, job_id) is None:
            raise HTTPException(status_code=404, detail='Job not found')
    try:
        from app.services.phase2 import apply_vi_edits

        return apply_vi_edits(job_id, [{'id': c.id, 'text': c.text} for c in payload.cues])
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post('/v1/jobs/{job_id}/voice-qa', response_model=JobAccepted, dependencies=[Depends(require_api_key)])
def start_voice_qa(job_id: str) -> JobAccepted:
    """Queue FINAL VOICE QA (classify fit, compress OVERFLOW, regen TTS, reassemble). Fast return."""
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        if job.status not in {'ready_for_render', 'ready', 'failed'}:
            raise HTTPException(status_code=409, detail=f'Voice QA requires a rendered job (status={job.status})')
        has_vi = bool(job.vi_storage_key) or bool(
            job.vi_local_path and Path(job.vi_local_path).exists())
        if not has_vi:
            raise HTTPException(status_code=409, detail='Vietnamese subtitles not ready')
        job.status = 'voice_qa'
        job.current_stage = 'voice_qa'
        job.progress = 0
        job.error = None
        job.lease_owner = None
        job.lease_until = None
        return JobAccepted(job_id=job.id, status=job.status)


@router.post('/v1/jobs/{job_id}/retry', response_model=JobAccepted, dependencies=[Depends(require_api_key)])
def retry_job(job_id: str) -> JobAccepted:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        if job.status not in {'failed'}:
            raise HTTPException(status_code=409, detail='Only failed jobs can be retried')
        # No duplicate row: reuse same job id, clear error/lease/ownership.
        has_voice = bool(job.voice_storage_key) or bool(
            job.voice_local_path and Path(job.voice_local_path).exists())
        has_vi = bool(job.vi_storage_key) or bool(
            job.vi_local_path and Path(job.vi_local_path).exists())
        has_zh = bool(job.subtitle_storage_key) or bool(
            job.local_subtitle_path and Path(job.local_subtitle_path).exists())
        phase2_started = bool(
            job.translation_started_at or job.vi_storage_key or job.vi_local_path
            or job.voice_storage_key or job.voice_local_path)
        if has_voice:
            job.status = 'ready_for_render'
            job.current_stage = 'ready_for_render'
            job.progress = 100
        elif has_vi:
            job.status = 'generating_tts'
            job.current_stage = 'generating_tts'
            job.progress = 40
        elif has_zh and phase2_started:
            # Phase 2 was interrupted before VI subtitles existed: resume it.
            job.status = 'translating'
            job.current_stage = 'translating'
            job.progress = 0
        elif job.subtitle_source in {'ASR', 'BILIBILI_API', 'BILIBILI', 'NONE'}:
            # If subtitle file actually exists keep ready, else re-queue subtitle stage.
            has_srt = bool(job.subtitle_storage_key) or bool(
                job.local_subtitle_path and Path(job.local_subtitle_path).exists()
            )
            if has_srt or job.subtitle_source == 'NONE':
                job.status = 'ready'
                job.current_stage = 'ready'
                job.progress = 100
            else:
                job.status = 'extracting_audio'
                job.current_stage = 'extracting_audio'
                job.progress = 50
        elif job.resolved_url:
            has_video = bool(job.video_storage_key) or bool(
                job.local_video_path and Path(job.local_video_path).exists()
            )
            if has_video:
                job.status = 'extracting_audio'
                job.current_stage = job.status
                job.progress = 50
            else:
                job.status = 'downloading'
                job.current_stage = job.status
                job.progress = 20
        else:
            job.status = 'queued'
            job.current_stage = 'queued'
            job.progress = 0
        job.error = None
        job.lease_owner = None
        job.lease_until = None
        return JobAccepted(job_id=job.id, status=job.status)
