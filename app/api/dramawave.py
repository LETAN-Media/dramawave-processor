"""DramaWave series/episode API (resolve, process, inspect). Enqueues; never blocks on media."""

from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.db import SessionLocal
from app.models import Episode, EpisodeJob, Series
from app.schemas import (
    DramaWaveEpisodeOut,
    DramaWaveProcessIn,
    DramaWaveResolveIn,
    DramaWaveSearchIn,
    DramaWaveSeriesOut,
    EpisodeJobOut,
    JobAccepted,
)
from app.security import require_api_key
from app.services import episodes as episode_service
from app.sources.base import SourceError
from app.sources.dramawave import DramaWaveSource

router = APIRouter(prefix='/v1/dramawave', dependencies=[Depends(require_api_key)])


def _provider() -> DramaWaveSource:
    if not settings.dramawave_enabled:
        raise HTTPException(status_code=503, detail='DramaWave provider disabled')
    return DramaWaveSource()


@router.post('/search')
def search_series(payload: DramaWaveSearchIn) -> dict:
    """Series search (official endpoint). No media fetched."""
    provider = _provider()
    try:
        return {'results': provider.search(payload.keyword, payload.limit)}
    except SourceError as exc:
        raise HTTPException(status_code=502, detail=f'{exc.code}: {exc}') from exc


@router.post('/resolve', response_model=DramaWaveSeriesOut)
def resolve_series(payload: DramaWaveResolveIn) -> DramaWaveSeriesOut:
    provider = _provider()
    try:
        info = provider.resolve_series(payload.url)
        series = episode_service.get_or_create_series(info, payload.url)
        episode_infos = provider.list_episodes(info)
        rows = episode_service.sync_episodes(series, episode_infos)
        episode_service.persist_series_metadata(series, info, episode_infos)
    except SourceError as exc:
        raise HTTPException(status_code=422, detail=f'{exc.code}: {exc}') from exc
    return DramaWaveSeriesOut(
        series_id=series.id, title=series.title,
        episode_count=info.episode_count,
        episodes=[DramaWaveEpisodeOut(episode_number=r.episode_number or 0,
                                      episode_id=r.provider_episode_id, title=r.title,
                                      duration=r.duration, locked=bool(r.locked),
                                      status=r.status) for r in rows])


@router.post('/series/{series_id}/process')
def process_range(series_id: str, payload: DramaWaveProcessIn) -> dict:
    try:
        result = episode_service.enqueue_episodes(
            series_id, payload.from_episode, payload.to_episode, payload.force,
            quality=payload.quality, target_language=payload.target_language,
            voice=payload.voice)
    except SourceError as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    return {'series_id': series_id, **result}


@router.post('/series/{series_id}/episodes/{episode_number}/process', response_model=JobAccepted)
def process_one(series_id: str, episode_number: int) -> JobAccepted:
    try:
        result = episode_service.enqueue_episodes(series_id, episode_number, episode_number, force=True)
    except SourceError as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    if not result['enqueued']:
        raise HTTPException(status_code=409, detail='Episode locked or not found')
    with SessionLocal() as db:
        job = db.get(EpisodeJob, result['enqueued'][0])
        return JobAccepted(job_id=job.id, status=job.status)


@router.post('/episodes/{episode_id}/process', response_model=JobAccepted)
def process_episode_by_id(episode_id: str) -> JobAccepted:
    """Enqueue a single episode by its row id (force re-run if already ready)."""
    from sqlalchemy import select

    with SessionLocal() as db:
        ep = db.get(Episode, episode_id)
        if ep is None:
            raise HTTPException(status_code=404, detail='Episode not found')
        series_id, number = ep.series_id, ep.episode_number or 0
    try:
        result = episode_service.enqueue_episodes(series_id, number, number, force=True)
    except SourceError as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    if not result['enqueued']:
        raise HTTPException(status_code=409, detail='Episode locked')
    with SessionLocal() as db:
        job = db.get(EpisodeJob, result['enqueued'][0])
        return JobAccepted(job_id=job.id, status=job.status)


@router.get('/series/{series_id}/jobs', response_model=list[EpisodeJobOut])
def series_jobs(series_id: str) -> list[EpisodeJobOut]:
    with SessionLocal() as db:
        from sqlalchemy import select

        ep_ids = [r.id for r in db.execute(
            select(Episode).where(Episode.series_id == series_id)).scalars().all()]
        if not ep_ids:
            raise HTTPException(status_code=404, detail='DRAMAWAVE_SERIES_NOT_FOUND')
        jobs = list(db.execute(
            select(EpisodeJob).where(EpisodeJob.episode_id.in_(ep_ids))
            .order_by(EpisodeJob.created_at)).scalars().all())
        return [EpisodeJobOut.model_validate(j) for j in jobs]
