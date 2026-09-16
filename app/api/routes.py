from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Job, WorkerHeartbeat
from app.schemas import HealthOut, JobAccepted, JobCreate, JobOut
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
        return {'primary': primary, 'fallback': 'whisper', 'jianying_available': jy}
    except Exception:
        return {'primary': 'auto', 'fallback': 'whisper', 'jianying_available': False}


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


@router.post('/v1/jobs/{job_id}/retry', response_model=JobAccepted, dependencies=[Depends(require_api_key)])
def retry_job(job_id: str) -> JobAccepted:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        if job.status not in {'failed'}:
            raise HTTPException(status_code=409, detail='Only failed jobs can be retried')
        # No duplicate row: reuse same job id, clear error/lease/ownership.
        if job.subtitle_storage_key or job.subtitle_source in {'ASR', 'BILIBILI_API', 'BILIBILI', 'NONE'}:
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
