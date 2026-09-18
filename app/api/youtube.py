"""YouTube OAuth + channels + publication API.

Browser OAuth entries (/auth/youtube*) use the dashboard session cookie;
machine endpoints use X-API-Key like the rest of /v1/* and /api/*.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import web_auth
from app.config import settings
from app.db import SessionLocal
from app.models import EpisodeJob
from app.schemas import YouTubeDestinationOut, YouTubePublicationOut, YouTubeUploadIn
from app.security import require_api_key
from app.youtube import oauth as yt_oauth
from app.youtube.client import YouTubeApiError, get_own_channel
from app.youtube.credentials import (
    YouTubeCredentialError,
    disconnect_destination,
    get_destination,
    list_destinations,
    public_info,
    save_destination,
)
from app.youtube.service import (
    YouTubeServiceError,
    cancel_publication,
    get_or_create_publication,
    normalize_privacy,
    publication_info,
    retry_publication,
)

logger = logging.getLogger('youtube-api')

router = APIRouter()


def _oauth_state() -> str:
    import itsdangerous

    secret = (settings.dashboard_secret or settings.api_key or 'dramawave-studio').strip()
    signer = itsdangerous.URLSafeTimedSerializer(secret, salt='youtube-oauth')
    return signer.dumps({'n': secrets.token_hex(8)})


def _check_oauth_state(state: str | None) -> None:
    import itsdangerous

    if not state:
        raise HTTPException(status_code=400, detail='missing state')
    secret = (settings.dashboard_secret or settings.api_key or 'dramawave-studio').strip()
    signer = itsdangerous.URLSafeTimedSerializer(secret, salt='youtube-oauth')
    try:
        signer.loads(state, max_age=600)
    except Exception as exc:
        raise HTTPException(status_code=400, detail='invalid state') from exc


@router.get('/auth/youtube')
def youtube_auth_start(request: Request):
    if web_auth.current_web_user(request) is None:
        return RedirectResponse(url='/login?next=/auth/youtube', status_code=307)
    try:
        url = yt_oauth.build_authorize_url(_oauth_state())
    except yt_oauth.YouTubeOAuthError as exc:
        raise HTTPException(status_code=503, detail=exc.code) from exc
    return RedirectResponse(url=url, status_code=307)


@router.get('/auth/youtube/callback')
def youtube_auth_callback(request: Request, code: str | None = None,
                          state: str | None = None, error: str | None = None):
    if error:
        logger.warning('youtube oauth denied: %s', error[:80])
        return RedirectResponse(url='/settings?youtube=denied', status_code=303)
    _check_oauth_state(state)
    if not code:
        raise HTTPException(status_code=400, detail='missing code')
    try:
        tokens = yt_oauth.exchange_code(code)
        # Temporary service from the fresh tokens (no DB row yet) to learn
        # which channel the user picked.
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(token=tokens['access_token'])
        temp_service = build('youtube', 'v3', credentials=creds, cache_discovery=False)
        channel = get_own_channel(temp_service)
        dest = save_destination(channel['youtube_channel_id'], channel['youtube_channel_title'],
                                tokens.get('refresh_token'), tokens.get('scopes'))
    except (yt_oauth.YouTubeOAuthError, YouTubeApiError, YouTubeCredentialError) as exc:
        raise HTTPException(status_code=502, detail=exc.code) from exc
    logger.info('youtube connected channel=%s', dest.youtube_channel_id[:8] + '…')
    return RedirectResponse(url='/settings?youtube=connected', status_code=303)


@router.get('/api/youtube/channels', dependencies=[Depends(require_api_key)])
def youtube_channels(active_only: bool = True) -> dict:
    return {'items': [public_info(d) for d in list_destinations(active_only=active_only)]}


@router.delete('/api/youtube/channels/{destination_id}', dependencies=[Depends(require_api_key)])
def youtube_disconnect(destination_id: str) -> dict:
    if not disconnect_destination(destination_id):
        raise HTTPException(status_code=404, detail='YOUTUBE_DESTINATION_NOT_FOUND')
    return {'id': destination_id, 'is_active': False}


@router.get('/api/youtube/callback-url', dependencies=[Depends(require_api_key)])
def youtube_callback_url() -> dict:
    try:
        url = yt_oauth.redirect_uri()
        return {'callback_url': url, 'oauth_configured': True}
    except yt_oauth.YouTubeOAuthError:
        return {'callback_url': None, 'oauth_configured': False}


def _pub_out(pub) -> YouTubePublicationOut:
    return YouTubePublicationOut.model_validate(pub)


@router.post('/v1/jobs/{job_id}/youtube/upload', response_model=YouTubePublicationOut,
             dependencies=[Depends(require_api_key)])
def youtube_manual_upload(job_id: str, payload: YouTubeUploadIn):
    with SessionLocal() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        if not job.final_path:
            raise HTTPException(status_code=409, detail='final video not ready')
    try:
        pub = get_or_create_publication(job_id, payload.destination_id,
                                        normalize_privacy(payload.privacy))
    except YouTubeServiceError as exc:
        status = 404 if exc.code == 'YOUTUBE_DESTINATION_NOT_FOUND' else 409
        raise HTTPException(status_code=status, detail=exc.code) from exc
    # Park the job for the upload pump without touching render artifacts.
    with SessionLocal.begin() as db:
        job = db.get(EpisodeJob, job_id)
        if job is not None and job.status not in ('uploading_youtube',):
            job.status = 'ready_to_upload'
            job.current_stage = 'ready_to_upload'
            job.error_code = None
            job.error_message = None
            job.lease_owner = None
            job.lease_until = None
    from app.models import YouTubePublication

    with SessionLocal() as db:
        return _pub_out(db.get(YouTubePublication, pub.id))


@router.post('/v1/jobs/{job_id}/youtube/retry', response_model=YouTubePublicationOut,
             dependencies=[Depends(require_api_key)])
def youtube_retry(job_id: str, payload: dict | None = None):
    dest_id = (payload or {}).get('destination_id')
    try:
        pub = retry_publication(job_id, dest_id)
    except YouTubeServiceError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    from app.models import YouTubePublication

    with SessionLocal() as db:
        return _pub_out(db.get(YouTubePublication, pub.id))


@router.delete('/v1/jobs/{job_id}/youtube', dependencies=[Depends(require_api_key)])
def youtube_cancel(job_id: str):
    with SessionLocal() as db:
        if db.get(EpisodeJob, job_id) is None:
            raise HTTPException(status_code=404, detail='Job not found')
    cancelled = cancel_publication(job_id)
    return {'job_id': job_id, 'cancelled': cancelled}


@router.get('/v1/jobs/{job_id}/youtube', dependencies=[Depends(require_api_key)])
def youtube_status(job_id: str) -> dict:
    with SessionLocal() as db:
        if db.get(EpisodeJob, job_id) is None:
            raise HTTPException(status_code=404, detail='Job not found')
    return {'job_id': job_id, 'publications': publication_info(job_id)}
