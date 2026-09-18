"""Generic/DramaWave API routes: health, drama proxy, series/episode reads, jobs, artifacts."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Episode, EpisodeJob, Series, WorkerHeartbeat
from app.schemas import (
    DramaWaveProcessIn,
    EpisodeJobOut,
    EpisodeOut,
    EpisodeProcessIn,
    HealthOut,
    JobAccepted,
    SeriesOut,
    SeriesProcessOut,
)
from app.security import require_api_key
from app.services import episodes as episode_service
from app.sources.base import SourceError

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
    try:
        import shutil

        jianying_available = bool(settings.jianying_enabled and shutil.which(settings.jianying_cli))
    except Exception:
        jianying_available = False
    try:
        import faster_whisper  # noqa: F401

        whisper_available = True
    except ImportError:
        whisper_available = False
    try:
        import edge_tts  # noqa: F401

        tts_available = True
    except ImportError:
        tts_available = False
    return HealthOut(
        ok=database_ok,
        service=settings.app_name,
        source='dramawave',
        database=database_ok,
        worker=worker_ok,
        worker_last_seen_at=last_seen,
        dramawave={'available': bool(settings.dramawave_enabled)},
        dramawave_api={
            'base_url': settings.dramawave_api_base_url,
            'configured': bool((settings.dramawave_api_base_url or '').strip()),
        },
        asr={'jianying_available': jianying_available, 'whisper_available': whisper_available},
        translation={
            'provider': settings.translation_provider,
            'primary_model': settings.translation_model,
            'fallback_model': settings.translation_fallback_model,
            'configured': bool(settings.translation_api_key),
        },
        tts={'provider': settings.tts_provider, 'voice': settings.tts_voice, 'available': tts_available},
    )


@router.get('/v1/jobs/{job_id}', response_model=EpisodeJobOut, dependencies=[Depends(require_api_key)])
def get_job(job_id: str) -> EpisodeJobOut:
    with SessionLocal() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        return EpisodeJobOut.model_validate(job)


@router.get('/v1/series/{series_id}', response_model=SeriesOut, dependencies=[Depends(require_api_key)])
def get_series(series_id: str) -> SeriesOut:
    with SessionLocal() as db:
        series = db.get(Series, series_id)
        if series is None:
            raise HTTPException(status_code=404, detail='Series not found')
        return SeriesOut.model_validate(series)


@router.get('/v1/series/{series_id}/episodes', response_model=list[EpisodeOut],
            dependencies=[Depends(require_api_key)])
def list_series_episodes(series_id: str) -> list[EpisodeOut]:
    with SessionLocal() as db:
        if db.get(Series, series_id) is None:
            raise HTTPException(status_code=404, detail='Series not found')
        rows = list(db.execute(select(Episode).where(Episode.series_id == series_id)
                               .order_by(Episode.episode_number)).scalars().all())
        return [EpisodeOut.model_validate(r) for r in rows]


# -- Drama flows (via hosted resolver API) ------------------------------------

@router.get('/v1/drama/search', dependencies=[Depends(require_api_key)])
def drama_search(q: str = Query(min_length=1, max_length=200)) -> dict:
    from app.clients import dramawave_api as api

    try:
        return {'items': api.search(q.strip())}
    except api.DramaApiError as exc:
        raise HTTPException(status_code=502, detail=f'{exc.code}: {exc.message}') from exc


@router.get('/v1/drama/series/{series_id}', dependencies=[Depends(require_api_key)])
def drama_series(series_id: str) -> dict:
    from app.clients import dramawave_api as api

    try:
        return api.get_series(series_id)
    except api.DramaApiError as exc:
        raise HTTPException(status_code=502 if exc.code not in ('DRAMA_API_UNAUTHORIZED',) else 401,
                            detail=f'{exc.code}: {exc.message}') from exc


@router.get('/v1/drama/series/{series_id}/episodes', dependencies=[Depends(require_api_key)])
def drama_episodes(series_id: str) -> dict:
    from app.clients import dramawave_api as api

    try:
        data = api.list_episodes(series_id)
        eps = data.get('episodes') or []
        return {'series_id': series_id, 'total': data.get('total', len(eps)), 'episodes': eps}
    except api.DramaApiError as exc:
        raise HTTPException(status_code=502, detail=f'{exc.code}: {exc.message}') from exc


TRANSLATION_STYLES = {'AUTO', 'MODERN_DRAMA', 'XIANXIA', 'NINETIES'}


def _apply_translation_style(series_id: str, style: str | None) -> None:
    if not style:
        return
    normalized = style.strip().upper()
    if normalized not in TRANSLATION_STYLES:
        raise HTTPException(status_code=422,
                            detail=f'translation_style must be one of {sorted(TRANSLATION_STYLES)}')
    if normalized == 'AUTO':
        return
    with SessionLocal.begin() as db:
        series = db.get(Series, series_id)
        if series is not None:
            series.translation_style = normalized


def _youtube_kwargs(payload: DramaWaveProcessIn | EpisodeProcessIn) -> dict:
    yt = payload.youtube
    if yt is None:
        return {}
    privacy = (yt.privacy or settings.youtube_default_privacy or 'public').strip().lower()
    if privacy not in ('public', 'unlisted', 'private'):
        raise HTTPException(status_code=422, detail='youtube.privacy must be public|unlisted|private')
    return {'youtube_enabled': bool(yt.enabled),
            'youtube_destination_id': yt.destination_id,
            'youtube_privacy': privacy,
            'youtube_metadata_mode': (yt.metadata_mode or 'auto').strip().lower() or 'auto'}


def _process_series_range(series_id: str, payload: DramaWaveProcessIn) -> SeriesProcessOut:
    try:
        result = episode_service.enqueue_episodes(
            series_id, payload.from_episode, payload.to_episode, payload.force,
            quality=payload.quality, target_language=payload.target_language,
            voice=payload.voice, **_youtube_kwargs(payload))
    except SourceError as exc:
        code = exc.code or ''
        if code.startswith('YOUTUBE_'):
            raise HTTPException(status_code=422, detail=code) from exc
        raise HTTPException(status_code=404, detail=code) from exc
    _apply_translation_style(series_id, payload.translation_style)
    return SeriesProcessOut(series_id=series_id, jobs=result['enqueued'],
                            skipped=result['skipped_locked_or_ready'], status='queued')


@router.post('/v1/series/{series_id}/process', response_model=SeriesProcessOut,
             dependencies=[Depends(require_api_key)])
def process_series_range(series_id: str, payload: DramaWaveProcessIn) -> SeriesProcessOut:
    return _process_series_range(series_id, payload)


@router.post('/v1/drama/series/{series_id}/process', response_model=SeriesProcessOut,
             dependencies=[Depends(require_api_key)])
def process_drama_series_range(series_id: str, payload: DramaWaveProcessIn) -> SeriesProcessOut:
    """Alias used by DramaWave Studio web UI (same behavior)."""
    return _process_series_range(series_id, payload)


@router.post('/v1/episodes/{episode_id}/process', response_model=JobAccepted,
             dependencies=[Depends(require_api_key)])
def process_episode(episode_id: str, payload: EpisodeProcessIn | None = None) -> JobAccepted:
    with SessionLocal() as db:
        ep = db.get(Episode, episode_id)
        if ep is None:
            raise HTTPException(status_code=404, detail='Episode not found')
        series_id, number = ep.series_id, ep.episode_number or 0
    force = True if payload is None else bool(payload.force)
    try:
        result = episode_service.enqueue_episodes(
            series_id, number, number, force=force,
            quality=(payload.quality if payload else None),
            target_language=(payload.target_language if payload else None),
            voice=(payload.voice if payload else None),
            **(_youtube_kwargs(payload) if payload else {}))
    except SourceError as exc:
        code = exc.code or ''
        if code.startswith('YOUTUBE_'):
            raise HTTPException(status_code=422, detail=code) from exc
        raise HTTPException(status_code=404, detail=code) from exc
    if payload is not None:
        _apply_translation_style(series_id, payload.translation_style)
    if not result['enqueued']:
        # Already completed and force=false.
        with SessionLocal() as db:
            job = db.execute(select(EpisodeJob).where(EpisodeJob.episode_id == episode_id)).scalars().first()
            if job is not None:
                return JobAccepted(job_id=job.id, status=job.status)
        raise HTTPException(status_code=409, detail='Episode locked')
    with SessionLocal() as db:
        job = db.get(EpisodeJob, result['enqueued'][0])
        return JobAccepted(job_id=job.id, status=job.status)


@router.get('/v1/drama/series/{series_id}/jobs', dependencies=[Depends(require_api_key)])
def series_jobs_progress(series_id: str) -> dict:
    with SessionLocal() as db:
        if db.get(Series, series_id) is None:
            raise HTTPException(status_code=404, detail='Series not found')
        eps = list(db.execute(select(Episode).where(Episode.series_id == series_id)
                              .order_by(Episode.episode_number)).scalars().all())
        jobs = {j.episode_id: j for j in db.execute(
            select(EpisodeJob).where(EpisodeJob.episode_id.in_([e.id for e in eps]))
        ).scalars().all()} if eps else {}
    counts = {'queued': 0, 'processing': 0, 'completed': 0, 'failed': 0}
    items = []
    for ep in eps:
        job = jobs.get(ep.id)
        status = job.status if job else ep.status
        if status in ('completed', 'published'):
            counts['completed'] += 1
        elif status in ('failed', 'youtube_upload_failed'):
            counts['failed'] += 1
        elif status in ('discovered', 'locked', 'queued'):
            counts['queued'] += 1
        else:
            counts['processing'] += 1
        items.append({'number': ep.episode_number, 'episode_id': ep.id,
                      'status': status if status != 'ready' else 'completed',
                      'progress': job.progress if job else 0,
                      'current_step': job.current_stage if job else ep.status,
                      'final_path': job.final_path if job else None,
                      'job_id': job.id if job else None})
    return {'series_id': series_id, 'total': len(eps), **counts, 'episodes': items}


def _artifact(job_id: str, kind: str):
    with SessionLocal() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        mapping = {'source': (job.source_srt_path, 'application/x-subrip; charset=utf-8'),
                   'vi': (job.vi_srt_path, 'application/x-subrip; charset=utf-8'),
                   'voice': (job.voice_path, 'audio/wav'),
                   'final': (job.final_path, 'video/mp4')}
        if kind not in mapping:
            raise HTTPException(status_code=404, detail='Unknown artifact')
        path, media_type = mapping[kind]
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail=f'{kind} not ready')
        return FileResponse(path, media_type=media_type, filename=Path(path).name)


@router.get('/v1/jobs/{job_id}/subtitle/source', dependencies=[Depends(require_api_key)])
def get_source_subtitle(job_id: str):
    return _artifact(job_id, 'source')


@router.get('/v1/jobs/{job_id}/subtitle/vi', dependencies=[Depends(require_api_key)])
def get_vi_subtitle(job_id: str):
    return _artifact(job_id, 'vi')


@router.get('/v1/jobs/{job_id}/voice', dependencies=[Depends(require_api_key)])
def get_voice(job_id: str):
    return _artifact(job_id, 'voice')


@router.get('/v1/jobs/{job_id}/final', dependencies=[Depends(require_api_key)])
def get_final(job_id: str):
    return _artifact(job_id, 'final')


@router.get('/v1/drama/provider-status', dependencies=[Depends(require_api_key)])
def drama_provider_status() -> dict:
    from app.clients import dramawave_api as api

    return api.health()
