"""YouTube publication orchestration: gating, claim, upload, retry, recovery.

Idempotency: one (job_id, destination_id) row (unique constraint). The YouTube
video id is committed immediately after insert returns, so a worker restart
between upload and processing-poll resumes at the poll instead of re-uploading.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import desc, func, or_, select

from app.config import settings
from app.db import SessionLocal
from app.models import Episode, EpisodeJob, Series, YouTubeDestination, YouTubePublication
from app.youtube.client import YouTubeApiError, build_service
from app.youtube.credentials import get_destination, mark_upload
from app.youtube.metadata import build_metadata
from app.youtube.uploader import YouTubeUploadError, upload_video

logger = logging.getLogger('youtube-service')

PRIVACIES = ('public', 'unlisted', 'private')
STALE_MINUTES = 30


class YouTubeServiceError(Exception):
    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(f'{code}: {message}' if message else code)
        self.code = code
        self.message = message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_privacy(value: str | None) -> str:
    s = (value or settings.youtube_default_privacy or 'public').strip().lower()
    return s if s in PRIVACIES else 'public'


def validate_final_for_upload(final_path: str | None, original_path: str | None) -> Path:
    """Gate: final must exist with video+audio streams, valid duration, size>0."""
    from app.services.render import validate_final, video_info

    if not final_path or not Path(final_path).is_file():
        raise YouTubeServiceError('YOUTUBE_UPLOAD_FAILED', 'final video missing')
    final = Path(final_path)
    if final.stat().st_size <= 0:
        raise YouTubeServiceError('YOUTUBE_UPLOAD_FAILED', 'final video empty')
    try:
        reference = video_info(Path(original_path)) if original_path else {}
        validate_final(final, reference)
    except RuntimeError as exc:
        raise YouTubeServiceError('YOUTUBE_UPLOAD_FAILED', str(exc)[:300]) from exc
    return final


def get_or_create_publication(job_id: str, destination_id: str, privacy: str,
                              metadata_mode: str = 'auto') -> YouTubePublication:
    with SessionLocal.begin() as db:
        dest = db.get(YouTubeDestination, destination_id)
        if dest is None or not dest.is_active:
            raise YouTubeServiceError('YOUTUBE_DESTINATION_NOT_FOUND', destination_id[:16])
        if not dest.refresh_token_encrypted:
            raise YouTubeServiceError('YOUTUBE_REAUTH_REQUIRED', destination_id[:16])
        job = db.get(EpisodeJob, job_id)
        if job is None:
            raise YouTubeServiceError('YOUTUBE_UPLOAD_FAILED', 'job not found')
        pub = db.execute(select(YouTubePublication).where(
            YouTubePublication.job_id == job_id,
            YouTubePublication.destination_id == destination_id)).scalars().first()
        if pub is None:
            pub = YouTubePublication(job_id=job_id, destination_id=destination_id,
                                     privacy=normalize_privacy(privacy),
                                     upload_status='queued')
            db.add(pub)
            try:
                db.flush()
            except Exception:
                # Lost a race with another worker: reuse the winner's row.
                db.rollback()
                pub = db.execute(select(YouTubePublication).where(
                    YouTubePublication.job_id == job_id,
                    YouTubePublication.destination_id == destination_id)).scalars().first()
                if pub is None:
                    raise
        else:
            if pub.upload_status in ('failed', 'cancelled'):
                pub.upload_status = 'queued'
                pub.error_code = None
                pub.error_message = None
        db.flush()
        db.expunge(pub)
        return pub


def build_publication_metadata(job_id: str, mode: str = 'auto') -> dict:
    with SessionLocal() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            raise YouTubeServiceError('YOUTUBE_UPLOAD_FAILED', 'job not found')
        ep = db.get(Episode, job.episode_id)
        series = db.get(Series, ep.series_id) if ep else None
        number = ep.episode_number if ep and ep.episode_number else 0
        count = series.episode_count if series else None
        title = series.title if series else None
        desc = None
        try:
            meta = json.loads(series.series_metadata or '{}') if series else {}
            desc = meta.get('description')
        except (ValueError, TypeError):
            desc = None
        style = series.translation_style if series else None
    return build_metadata(title, number, count, style=style, series_description=desc,
                          mode=mode or 'auto')


def _set_job(job_id: str, status: str, stage: str, progress: int | None = None,
             error_code: str | None = None, error_message: str | None = None) -> None:
    with SessionLocal.begin() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            return
        job.status = status
        job.current_stage = stage
        if progress is not None:
            job.progress = progress
        job.error_code = error_code
        job.error_message = error_message[:2000] if error_message else None
        job.heartbeat_at = utcnow()
        if status not in ('failed', 'youtube_upload_failed'):
            job.lease_owner = None
            job.lease_until = None


def run_upload(publication_id: str) -> dict:
    """Execute one publication: upload (+resume at processing poll). Idempotent."""
    with SessionLocal() as db:
        pub = db.get(YouTubePublication, publication_id)
        if pub is None:
            raise YouTubeServiceError('YOUTUBE_UPLOAD_FAILED', 'publication not found')
        job = db.get(EpisodeJob, pub.job_id)
        dest = db.get(YouTubeDestination, pub.destination_id)
        if job is None or dest is None or not dest.is_active:
            raise YouTubeServiceError('YOUTUBE_DESTINATION_NOT_FOUND',
                                      (pub.destination_id or '')[:16])
        if not dest.refresh_token_encrypted:
            raise YouTubeServiceError('YOUTUBE_REAUTH_REQUIRED',
                                      (pub.destination_id or '')[:16])
        # Copy scalars out of the session (instances detach on close).
        job_id, dest_id = pub.job_id, pub.destination_id
        token_blob = dest.refresh_token_encrypted
        final_path, original_path = job.final_path, job.original_path
        metadata_mode = job.youtube_metadata_mode or 'auto'
        resume_video_id = pub.youtube_video_id
        privacy = pub.privacy
    final = validate_final_for_upload(final_path, original_path)
    if pub.upload_status not in ('queued', 'uploading', 'processing'):
        raise YouTubeServiceError('YOUTUBE_UPLOAD_FAILED',
                                  f'publication is {pub.upload_status}')
    with SessionLocal.begin() as db:
        pub = db.get(YouTubePublication, publication_id)
        pub.upload_status = 'uploading'
        pub.upload_attempts = (pub.upload_attempts or 0) + 1
    _set_job(job_id, 'uploading_youtube', 'uploading_youtube', progress=99)

    def _progress(pct: int) -> None:
        try:
            with SessionLocal.begin() as db:
                pub = db.get(YouTubePublication, publication_id)
                if pub is not None:
                    pub.upload_progress = pct
        except Exception:
            pass

    try:
        service = build_service(token_blob)
    except YouTubeApiError as exc:
        return _fail_publication(publication_id, job_id, dest_id, exc.code, exc.message)

    try:
        if resume_video_id:
            # Crash recovery: upload already succeeded, resume at processing poll.
            from app.youtube.uploader import wait_processing

            logger.info('youtube resume at processing poll video already uploaded job=%s',
                        job_id[:8])
            processing = wait_processing(service, resume_video_id)
            video_id = resume_video_id
        else:
            meta = build_publication_metadata(job_id, metadata_mode)
            with SessionLocal.begin() as db:
                pub = db.get(YouTubePublication, publication_id)
                pub.title = meta['title']
                pub.description = meta['description']
                pub.tags = json.dumps(meta['tags'], ensure_ascii=False)
            video_id, processing = upload_video(
                service, final, meta['title'], meta['description'], meta['tags'],
                normalize_privacy(privacy), on_progress=_progress)
            with SessionLocal.begin() as db:
                # Commit the video id FIRST: restart from here must not re-upload.
                pub = db.get(YouTubePublication, publication_id)
                pub.youtube_video_id = video_id
                pub.youtube_url = f'https://youtu.be/{video_id}'
                pub.upload_status = 'processing'
                pub.youtube_processing_status = processing
            _set_job(job_id, 'uploading_youtube', 'youtube_processing', progress=99)
    except (YouTubeUploadError, YouTubeApiError) as exc:
        return _fail_publication(publication_id, job_id, dest_id, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - never crash the worker loop
        return _fail_publication(publication_id, job_id, dest_id,
                                 'YOUTUBE_UPLOAD_FAILED', f'{type(exc).__name__}')

    with SessionLocal.begin() as db:
        pub = db.get(YouTubePublication, publication_id)
        pub.youtube_video_id = video_id
        pub.youtube_url = f'https://youtu.be/{video_id}'
        pub.youtube_processing_status = processing
        pub.upload_progress = 100
        if processing == 'processed':
            pub.upload_status = 'published'
            pub.published_at = utcnow()
            pub.error_code = None
            pub.error_message = None
        else:
            # Uploaded but YouTube still processing: keep polling on next pump.
            pub.upload_status = 'processing'
    mark_upload(dest_id)
    if processing == 'processed':
        _set_job(job_id, 'published', 'published', progress=100)
        logger.info('youtube published job=%s video=%s', job_id[:8], video_id[:8] + '…')
    else:
        _set_job(job_id, 'uploading_youtube', 'youtube_processing', progress=99)
    return {'publication_id': publication_id, 'youtube_video_id': video_id,
            'youtube_url': f'https://youtu.be/{video_id}', 'processing': processing}


def _fail_publication(publication_id: str, job_id: str, dest_id: str,
                      code: str, message: str) -> dict:
    with SessionLocal.begin() as db:
        pub = db.get(YouTubePublication, publication_id)
        if pub is not None:
            pub.upload_status = 'failed'
            pub.error_code = code[:64]
            pub.error_message = message[:2000]
    mark_upload(dest_id, error=f'{code}: {message[:200]}')
    _set_job(job_id, 'youtube_upload_failed', 'uploading_youtube',
             error_code=code, error_message=message)
    logger.warning('youtube upload failed job=%s code=%s', job_id[:8], code)
    if code == 'YOUTUBE_QUOTA_EXCEEDED':
        logger.warning('youtube quota exceeded: NOT retrying automatically')
    return {'publication_id': publication_id, 'error_code': code}


def claim_upload() -> str | None:
    """Claim one queued (or stale) publication honoring concurrency + interval."""
    concurrency = max(1, settings.youtube_upload_concurrency)
    interval = max(0, settings.youtube_upload_interval_seconds)
    now = utcnow()
    with SessionLocal.begin() as db:
        active = db.execute(select(func.count()).select_from(YouTubePublication).where(
            YouTubePublication.upload_status == 'uploading')).scalar() or 0
        if active >= concurrency:
            return None
        if interval:
            last = db.execute(select(func.max(YouTubePublication.published_at)).select_from(
                YouTubePublication).where(
                    YouTubePublication.upload_status == 'published')).scalar()
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (now - last).total_seconds() < interval:
                    return None
        stale_cutoff = now - timedelta(seconds=max(60, settings.job_lease_seconds * 3))
        pub = db.execute(select(YouTubePublication)
                         .where(or_(
                             YouTubePublication.upload_status == 'queued',
                             YouTubePublication.upload_status == 'processing',
                             ((YouTubePublication.upload_status == 'uploading')
                              & (YouTubePublication.updated_at < stale_cutoff)),
                         ))
                         .order_by(YouTubePublication.created_at.asc()).limit(1)).scalars().first()
        if pub is None:
            return None
        if pub.upload_status == 'uploading':
            logger.info('youtube reclaiming stale upload pub=%s', pub.id[:8])
            pub.upload_status = 'queued'
        else:
            pub.upload_status = 'uploading'
        return pub.id


def retry_publication(job_id: str, destination_id: str | None = None) -> YouTubePublication:
    """Re-queue a failed/cancelled publication. Render artifacts are untouched."""
    with SessionLocal() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            raise YouTubeServiceError('YOUTUBE_UPLOAD_FAILED', 'job not found')
        stmt = select(YouTubePublication).where(YouTubePublication.job_id == job_id)
        if destination_id:
            stmt = stmt.where(YouTubePublication.destination_id == destination_id)
        pubs = list(db.execute(stmt).scalars().all())
    if not pubs:
        raise YouTubeServiceError('YOUTUBE_UPLOAD_FAILED', 'no publication to retry')
    for pub in pubs:
        if pub.upload_status not in ('failed', 'cancelled'):
            continue
        with SessionLocal.begin() as db:
            row = db.get(YouTubePublication, pub.id)
            row.upload_status = 'queued'
            row.error_code = None
            row.error_message = None
    _set_job(job_id, 'ready_to_upload', 'ready_to_upload')
    with SessionLocal() as db:
        out = db.get(YouTubePublication, pubs[0].id)
        db.expunge(out)
        return out


def cancel_publication(job_id: str, destination_id: str | None = None) -> int:
    """Cancel pending uploads; the rendered video is kept, job returns to completed."""
    n = 0
    with SessionLocal.begin() as db:
        stmt = select(YouTubePublication).where(
            YouTubePublication.job_id == job_id,
            YouTubePublication.upload_status.in_(['queued', 'processing']))
        if destination_id:
            stmt = stmt.where(YouTubePublication.destination_id == destination_id)
        for pub in db.execute(stmt).scalars().all():
            pub.upload_status = 'cancelled'
            n += 1
        job = db.get(EpisodeJob, job_id)
        if job is not None and job.status in ('ready_to_upload', 'uploading_youtube'):
            job.status = 'completed'
            job.current_stage = 'validating_final'
            job.error_code = None
            job.error_message = None
    return n


def publication_info(job_id: str, db=None) -> list[dict]:
    if db is None:
        with SessionLocal() as _db:
            return publication_info(job_id, _db)
    pubs = list(db.execute(select(YouTubePublication).where(
        YouTubePublication.job_id == job_id)
        .order_by(desc(YouTubePublication.created_at))).scalars().all())
    out = []
    for pub in pubs:
        dest = db.get(YouTubeDestination, pub.destination_id)
        out.append({
            'id': pub.id, 'job_id': pub.job_id,
            'destination_id': pub.destination_id,
            'channel_title': dest.youtube_channel_title if dest else None,
            'channel_id': dest.youtube_channel_id if dest else None,
            'youtube_video_id': pub.youtube_video_id, 'youtube_url': pub.youtube_url,
            'title': pub.title, 'privacy': pub.privacy,
            'upload_status': pub.upload_status, 'upload_progress': pub.upload_progress,
            'upload_attempts': pub.upload_attempts,
            'youtube_processing_status': pub.youtube_processing_status,
            'published_at': pub.published_at.isoformat() if pub.published_at else None,
            'error_code': pub.error_code, 'error_message': pub.error_message,
        })
    return out
