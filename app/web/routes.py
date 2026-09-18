"""DramaWave Studio web UI: pages + cookie-authed JSON API + media streaming.

Same FastAPI service, same backend/pipeline. API routes under /v1/* are
untouched; the browser talks to /web/api/* (dashboard session cookie) which
calls the same services (search/resolve/enqueue/progress/artifacts).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select

from app import web_auth
from app.clients import drama_source_api as api
from app.config import settings
from app.db import SessionLocal
from app.models import Episode, EpisodeJob, Series
from app.services import episodes as episode_service
from app.sources.base import SourceError

logger = logging.getLogger('studio-web')

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))


def _static_version() -> str:
    try:
        return str(int(Path(__file__).parent.joinpath('static', 'app.js').stat().st_mtime))
    except OSError:
        return '1'

QUALITIES = ['best', '1080p', '720p', '540p', '480p']
VOICES = [
    {'id': 'vi-VN-HoaiMyNeural', 'label': 'Nữ - Hoài My'},
    {'id': 'vi-VN-NamMinhNeural', 'label': 'Nam - Nam Minh'},
]
STYLES = ['AUTO', 'MODERN_DRAMA', 'XIANXIA', 'NINETIES']

STAGE_ORDER = [
    ('resolving_playback', 'Resolve playback'),
    ('downloading', 'Download'),
    ('extracting_audio', 'Extract audio'),
    ('detecting_language', 'Detect language'),
    ('transcribing', 'ASR'),
    ('validating_source_srt', 'Validate source SRT'),
    ('translating', 'Translation'),
    ('validating_translation', 'Validate translation'),
    ('generating_tts', 'TTS'),
    ('syncing_voice', 'Sync voice'),
    ('rendering', 'Render'),
    ('validating_final', 'Validate final'),
    ('ready_to_upload', 'Ready to upload'),
    ('uploading_youtube', 'YouTube upload'),
    ('youtube_processing', 'YouTube processing'),
    ('published', 'Published'),
]


# -- helpers ---------------------------------------------------------------

def _template_ctx(request: Request, **extra: Any) -> dict:
    return {
        'request': request,
        'app_name': 'DramaWave Studio',
        'user': web_auth.current_web_user(request),
        'auth_enabled': web_auth.dashboard_auth_enabled(),
        'static_version': _static_version(),
        **extra,
    }


def _series_catalog() -> list[dict]:
    with SessionLocal() as db:
        rows = db.execute(select(Series).order_by(desc(Series.updated_at)).limit(60)).scalars().all()
        out = []
        for s in rows:
            total = db.execute(
                select(Episode).where(Episode.series_id == s.id)).scalars().all()
            unlocked = sum(1 for e in total if not e.locked)
            out.append({
                'id': s.id, 'provider_series_id': s.provider_series_id,
                'title': s.title or s.provider_series_id, 'cover_url': s.cover_url,
                'episode_count': s.episode_count or len(total),
                'unlocked': unlocked, 'total': len(total),
            })
        return out


def _get_local_series_by_provider(pid: str) -> Series | None:
    with SessionLocal() as db:
        return db.execute(select(Series).where(
            Series.provider == 'dramawave',
            Series.provider_series_id == pid)).scalars().first()


def _import_series(pid: str) -> tuple[Series, list[Episode]]:
    """Fetch live resolver data and sync into local DB. Raises HTTPException."""
    from app.sources.base import SeriesInfo

    try:
        data = api.get_series(pid)
    except api.DramaApiError as exc:
        raise HTTPException(status_code=502, detail=_friendly_api_error(exc)) from exc
    info = SeriesInfo(
        provider='multi', provider_series_id=str(data.get('canonical_series_id') or pid),
        title=str(data.get('canonical_title') or pid),
        description=str(data.get('description') or ''),
        cover_url=str(data.get('cover_url') or ''),
        episode_count=data.get('episode_count'),
        source_url=f'cw:{pid}' if not pid.startswith('cw:') else pid,
        metadata=dict(data.get('metadata') or {}))
    series = episode_service.get_or_create_series(info, info.source_url)
    try:
        raw = api.list_episodes(pid)
    except api.DramaApiError as exc:
        raise HTTPException(status_code=502, detail=_friendly_api_error(exc)) from exc
    from app.sources.base import EpisodeInfo

    infos = []
    for e in raw.get('episodes') or []:
        sources = e.get('sources') or []
        free = any(s.get('status') == 'free' for s in sources)
        infos.append(EpisodeInfo(
            provider_episode_id=str(e.get('episode_number') or 0),
            episode_number=int(e.get('episode_number') or 0),
            title=f"Episode {e.get('episode_number') or 0}",
            duration=None,
            locked=not free,
            source_url=info.source_url,
            metadata={'sources': sources}))
    rows = episode_service.sync_episodes(series, infos)
    return series, rows


def _series_episode_state(series_id: str) -> list[dict]:
    import json
    with SessionLocal() as db:
        eps = list(db.execute(select(Episode).where(Episode.series_id == series_id)
                              .order_by(Episode.episode_number)).scalars().all())
        jobs = {j.episode_id: j for j in db.execute(
            select(EpisodeJob).where(EpisodeJob.episode_id.in_([e.id for e in eps]))
        ).scalars().all()} if eps else {}
        from app.models import YouTubePublication

        yt = {}
        if jobs:
            for pub in db.execute(select(YouTubePublication).where(
                    YouTubePublication.job_id.in_([j.id for j in jobs.values()])
            )).scalars().all():
                yt.setdefault(pub.job_id, []).append(pub.upload_status)
        out = []
        for ep in eps:
            job = jobs.get(ep.id)
            states = yt.get(job.id, []) if job else []
            if any(s == 'published' for s in states):
                yt_state: str | None = 'published'
            elif any(s in ('uploading', 'processing', 'queued') for s in states):
                yt_state = 'uploading'
            elif any(s == 'failed' for s in states):
                yt_state = 'failed'
            else:
                yt_state = None
                
            try:
                meta = json.loads(ep.episode_metadata) if ep.episode_metadata else {}
            except Exception:
                meta = {}
            
            out.append({
                'id': ep.id,
                'number': ep.episode_number,
                'title': ep.title,
                'locked': ep.locked,
                'sources': meta.get('sources') or [],
                'status': ep.status,
                'progress': job.progress if job else 0,
                'job_id': job.id if job else None,
                'yt_status': yt_state,
            })
        return out


def _friendly_api_error(exc: api.DramaApiError) -> str:
    msg = (exc.message or '').strip()
    if exc.code in ('DRAMA_API_TIMEOUT',):
        return 'DramaWave API waking up (cold start)... please retry in ~30s.'
    if 'SERIES_NOT_FOUND' in exc.code:
        return 'Series not found.'
    if 'UNAUTHORIZED' in exc.code:
        return 'Processor offline (resolver auth failed).'
    return f'{exc.code}: {msg[:160]}' if msg else exc.code


def _job_detail(job_id: str) -> dict:
    with SessionLocal() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        ep = db.get(Episode, job.episode_id)
        series = db.get(Series, ep.series_id) if ep else None
        steps = _pipeline_steps(job)
        from app.youtube.service import publication_info

        youtube = publication_info(job_id, db)
        artifacts = {
            'final': bool(job.final_path and Path(job.final_path).exists()),
            'vi_srt': bool(job.vi_srt_path and Path(job.vi_srt_path).exists()),
            'source_srt': bool(job.source_srt_path and Path(job.source_srt_path).exists()),
            'voice': bool(job.voice_path and Path(job.voice_path).exists()),
        }
        return {
            'job_id': job.id, 'status': job.status, 'current_stage': job.current_stage,
            'progress': job.progress, 'attempts': job.attempts,
            'episode_number': ep.episode_number if ep else None,
            'episode_title': ep.title if ep else None,
            'series_title': series.title if series else None,
            'series_id': series.id if series else None,
            'provider_series_id': series.provider_series_id if series else None,
            'source_language': job.source_language,
            'target_language': job.target_language or 'vi',
            'quality': job.quality, 'requested_quality': job.requested_quality,
            'voice': job.voice, 'asr_provider': job.asr_provider,
            'asr_fallback_used': job.asr_fallback_used,
            'subtitle_cue_count': job.subtitle_cue_count,
            'error_code': job.error_code, 'error_message': job.error_message,
            'timings': {
                'resolve': job.resolve_seconds, 'download': job.download_seconds,
                'audio_extract': job.audio_extract_seconds, 'asr': job.asr_seconds,
                'translation': job.translation_seconds, 'tts': job.tts_seconds,
                'render': job.render_seconds,
            },
            'steps': steps, 'artifacts': artifacts,
            'youtube_enabled': bool(job.youtube_enabled),
            'youtube': youtube,
            'created_at': job.created_at.isoformat() if job.created_at else None,
            'updated_at': job.updated_at.isoformat() if job.updated_at else None,
        }


def _pipeline_steps(job: EpisodeJob) -> list[dict]:
    order = [s for s, _ in STAGE_ORDER]
    timings = {
        'resolving_playback': job.resolve_seconds,
        'downloading': job.download_seconds,
        'extracting_audio': job.audio_extract_seconds,
        'transcribing': job.asr_seconds,
        'translating': job.translation_seconds,
        'generating_tts': job.tts_seconds,
        'rendering': job.render_seconds,
    }
    steps = []
    if job.status in ('completed', 'published'):
        for stage, label in STAGE_ORDER:
            steps.append({'stage': stage, 'label': label, 'state': 'done',
                          'seconds': timings.get(stage)})
        return steps
    try:
        cur = order.index(job.current_stage) if job.current_stage in order else -1
    except ValueError:
        cur = -1
    failed = job.status in ('failed', 'youtube_upload_failed')
    for i, (stage, label) in enumerate(STAGE_ORDER):
        if failed:
            state = 'done' if timings.get(stage) is not None else ('active' if i == max(cur, 0) else 'waiting')
            if i > max(cur, 0):
                state = 'waiting'
        else:
            state = 'done' if i < cur else ('active' if i == cur else 'waiting')
        steps.append({'stage': stage, 'label': label, 'state': state,
                      'seconds': timings.get(stage)})
    return steps


def _valid_next(value: str | None) -> str:
    if value and value.startswith('/') and not value.startswith('//'):
        return value
    return '/'


# -- auth pages ------------------------------------------------------------

@router.api_route('/login', response_class=HTMLResponse, methods=["GET", "HEAD"])
def login_page(request: Request, next: str = '/'):
    if web_auth.current_web_user(request) is not None:
        return RedirectResponse(url=_valid_next(next), status_code=303)
    return templates.TemplateResponse(request=request, name='login.html',
                                        context=_template_ctx(request, next=_valid_next(next), error=None))


@router.post('/login')
def login_submit(request: Request, username: str = Form(''), password: str = Form(''),
                 next: str = Form('/')):
    if not web_auth.dashboard_auth_enabled():
        return RedirectResponse(url=_valid_next(next), status_code=303)
    if web_auth.check_credentials(username.strip(), password):
        resp = RedirectResponse(url=_valid_next(next), status_code=303)
        resp.set_cookie(web_auth.SESSION_COOKIE, web_auth.create_session_token(username.strip()),
                        max_age=max(1, int(settings.dashboard_session_hours)) * 3600,
                        httponly=True, samesite='lax',
                        secure=(request.url.scheme == 'https'), path='/')
        return resp
    return templates.TemplateResponse(
        request=request, name='login.html',
        context=_template_ctx(request, next=_valid_next(next),
                              error='Sai tài khoản hoặc mật khẩu.'),
        status_code=401)


@router.post('/logout')
def logout(request: Request):
    resp = RedirectResponse(url='/login', status_code=303)
    resp.delete_cookie(web_auth.SESSION_COOKIE, path='/')
    return resp


# -- pages -----------------------------------------------------------------

@router.api_route('/', response_class=HTMLResponse, methods=["GET", "HEAD"])
def home(request: Request):
    user = web_auth.current_web_user(request)
    if user is None:
        return web_auth.login_redirect(request)
    return templates.TemplateResponse(
        request=request, name='home.html',
        context=_template_ctx(request, catalog=_series_catalog(),
                              qualities=QUALITIES, voices=VOICES))


@router.api_route('/series/{provider_series_id}', response_class=HTMLResponse, methods=["GET", "HEAD"])
def series_page(request: Request, provider_series_id: str):
    user = web_auth.current_web_user(request)
    if user is None:
        return web_auth.login_redirect(request)
    if '..' in provider_series_id or '/' in provider_series_id:
        raise HTTPException(status_code=404, detail='Series not found')
    return templates.TemplateResponse(
        request=request, name='series.html',
        context=_template_ctx(request, provider_series_id=provider_series_id,
                              qualities=QUALITIES, voices=VOICES, styles=STYLES,
                              default_quality=settings.video_quality,
                              default_voice=settings.tts_voice))


@router.api_route('/jobs', response_class=HTMLResponse, methods=["GET", "HEAD"])
def jobs_page(request: Request):
    user = web_auth.current_web_user(request)
    if user is None:
        return web_auth.login_redirect(request)
    return templates.TemplateResponse(request=request, name='jobs.html',
                                        context=_template_ctx(request))


@router.api_route('/jobs/{job_id}', response_class=HTMLResponse, methods=["GET", "HEAD"])
def job_page(request: Request, job_id: str):
    user = web_auth.current_web_user(request)
    if user is None:
        return web_auth.login_redirect(request)
    with SessionLocal() as db:
        if db.get(EpisodeJob, job_id) is None:
            raise HTTPException(status_code=404, detail='Job not found')
    return templates.TemplateResponse(request=request, name='job_detail.html',
                                        context=_template_ctx(request, job_id=job_id))


@router.api_route('/settings', response_class=HTMLResponse, methods=["GET", "HEAD"])
def settings_page(request: Request):
    user = web_auth.current_web_user(request)
    if user is None:
        return web_auth.login_redirect(request)
    return templates.TemplateResponse(
        request=request, name='settings.html',
        context=_template_ctx(request, qualities=QUALITIES, voices=VOICES,
                              styles=STYLES,
                              default_quality=settings.video_quality,
                              default_voice=settings.tts_voice))


# -- web JSON API (dashboard session, not X-API-Key) ------------------------

@router.get('/web/api/search')
def web_search(request: Request, q: str = Query(min_length=1, max_length=200),
               limit: int = Query(default=20, ge=1, le=50)):
    web_auth.require_web_user_api(request)
    try:
        items = api.search(q.strip(), limit)
    except api.DramaApiError as exc:
        raise HTTPException(status_code=502, detail=_friendly_api_error(exc)) from exc
    return {'items': items}


@router.get('/web/api/series')
def web_catalog(request: Request):
    web_auth.require_web_user_api(request)
    return {'items': _series_catalog()}


@router.get('/web/api/series/{provider_series_id}')
def web_series_detail(request: Request, provider_series_id: str):
    web_auth.require_web_user_api(request)
    if '..' in provider_series_id or '/' in provider_series_id:
        raise HTTPException(status_code=404, detail='Series not found')
    series, _rows = _import_series(provider_series_id)
    with SessionLocal() as db:
        fresh = db.get(Series, series.id)
        meta = {'title': fresh.title, 'cover_url': fresh.cover_url,
                'episode_count': fresh.episode_count,
                'translation_style': fresh.translation_style}
    episodes = _series_episode_state(series.id)
    unlocked = sum(1 for e in episodes if not e['locked'])
    return {'series_id': series.id, 'provider_series_id': series.provider_series_id,
            'meta': meta, 'episodes': episodes,
            'unlocked': unlocked, 'locked': len(episodes) - unlocked,
            'total': len(episodes)}


@router.post('/web/api/series/{provider_series_id}/process')
async def web_process_range(request: Request, provider_series_id: str):
    web_auth.require_web_user_api(request)
    if '..' in provider_series_id or '/' in provider_series_id:
        raise HTTPException(status_code=404, detail='Series not found')
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        from_ep = int(body.get('from_episode', 1))
        to_ep = body.get('to_episode')
        to_ep = int(to_ep) if to_ep is not None else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail='from_episode/to_episode must be integers')
    if from_ep < 1 or (to_ep is not None and to_ep < from_ep):
        raise HTTPException(status_code=422, detail='Invalid episode range')
    quality = str(body.get('quality') or settings.video_quality or '1080p').strip().lower()
    if quality not in QUALITIES:
        raise HTTPException(status_code=422, detail=f'quality must be one of {QUALITIES}')
    voice = str(body.get('voice') or settings.tts_voice).strip()
    if voice not in [v['id'] for v in VOICES]:
        raise HTTPException(status_code=422, detail='Unknown voice')
    style = str(body.get('translation_style') or 'AUTO').strip().upper()
    if style not in STYLES:
        raise HTTPException(status_code=422, detail=f'translation_style must be one of {STYLES}')
    force = bool(body.get('force', False))
    yt = body.get('youtube') or {}
    youtube_enabled = bool(yt.get('enabled', False))
    youtube_destination_id = yt.get('destination_id')
    youtube_privacy = str(yt.get('privacy') or settings.youtube_default_privacy or 'public').strip().lower()
    if youtube_privacy not in ('public', 'unlisted', 'private'):
        raise HTTPException(status_code=422, detail='youtube.privacy must be public|unlisted|private')
    youtube_metadata_mode = str(yt.get('metadata_mode') or 'auto').strip().lower() or 'auto'
    # Locked episodes can never be submitted: validate range against live data.
    series, _rows = _import_series(provider_series_id)
    if style != 'AUTO':
        with SessionLocal.begin() as db:
            row = db.get(Series, series.id)
            if row is not None:
                row.translation_style = style
    episodes = _series_episode_state(series.id)
    in_range = [e for e in episodes
                if e['number'] is not None and e['number'] >= from_ep
                and (to_ep is None or e['number'] <= to_ep)]
    if not in_range:
        raise HTTPException(status_code=404, detail='No episodes in range')
    if all(e['locked'] for e in in_range):
        raise HTTPException(status_code=409, detail='Episodes locked')
    try:
        result = episode_service.enqueue_episodes(
            series.id, from_ep, to_ep, force=force, quality=quality,
            target_language='vi', voice=voice,
            youtube_enabled=youtube_enabled,
            youtube_destination_id=youtube_destination_id,
            youtube_privacy=youtube_privacy,
            youtube_metadata_mode=youtube_metadata_mode)
    except SourceError as exc:
        code = exc.code or ''
        if code.startswith('YOUTUBE_'):
            raise HTTPException(status_code=422, detail=code) from exc
        raise HTTPException(status_code=404, detail=code) from exc
    if not result['enqueued']:
        raise HTTPException(status_code=409, detail={
            'message': 'Episodes locked or already completed',
            'skipped': result['skipped_locked_or_ready']})
    return {'series_id': series.id, 'jobs': result['enqueued'],
            'skipped': result['skipped_locked_or_ready'], 'status': 'queued'}


@router.get('/web/api/jobs')
def web_jobs(request: Request, status: str = Query(default='all'),
             limit: int = Query(default=100, ge=1, le=200)):
    web_auth.require_web_user_api(request)
    filt = (status or 'all').strip().lower()
    if filt not in ('all', 'processing', 'completed', 'failed'):
        raise HTTPException(status_code=422, detail='status must be all|processing|completed|failed')
    with SessionLocal() as db:
        rows = list(db.execute(select(EpisodeJob).order_by(desc(EpisodeJob.created_at))
                               .limit(limit)).scalars().all())
        from app.models import YouTubePublication

        pubs = {}
        if rows:
            for pub in db.execute(select(YouTubePublication).where(
                    YouTubePublication.job_id.in_([j.id for j in rows]))).scalars().all():
                pubs.setdefault(pub.job_id, []).append(pub.upload_status)
        out = []
        for job in rows:
            ep = db.get(Episode, job.episode_id)
            series = db.get(Series, ep.series_id) if ep else None
            bucket = ('completed' if job.status in ('completed', 'published')
                      else 'failed' if job.status in ('failed', 'youtube_upload_failed')
                      else 'processing')
            if filt != 'all' and bucket != filt:
                continue
            states = pubs.get(job.id, [])
            youtube_badge = None
            if any(s == 'published' for s in states):
                youtube_badge = 'published'
            elif any(s in ('uploading', 'processing', 'queued') for s in states):
                youtube_badge = 'uploading'
            elif any(s == 'failed' for s in states):
                youtube_badge = 'failed'
            out.append({
                'job_id': job.id, 'status': job.status, 'bucket': bucket,
                'current_stage': job.current_stage, 'progress': job.progress,
                'episode_number': ep.episode_number if ep else None,
                'series_title': series.title if series else None,
                'series_id': series.id if series else None,
                'provider_series_id': series.provider_series_id if series else None,
                'error_code': job.error_code,
                'has_final': bool(job.final_path and Path(job.final_path).exists()),
                'youtube_badge': youtube_badge,
            })
        return {'items': out}


@router.get('/web/api/jobs/{job_id}')
def web_job_detail(request: Request, job_id: str):
    web_auth.require_web_user_api(request)
    return _job_detail(job_id)


@router.get('/web/api/provider-status')
def web_provider_status(request: Request):
    web_auth.require_web_user_api(request)
    import shutil

    drama = api.health()
    return {
        'drama_source_api': {
            'online': bool(drama.get('online')),
            'latency_ms': drama.get('latency_ms'),
            'base_url': settings.drama_source_api_base_url,
            'error': drama.get('error'),
        },
        'processor': {'online': True, 'service': settings.app_name},
        'asr': {
            'jianying_available': bool(settings.jianying_enabled and shutil.which(settings.jianying_cli)),
            'whisper_available': _module_available('faster_whisper'),
        },
        'tts': {'provider': settings.tts_provider, 'voice': settings.tts_voice,
                'available': _module_available('edge_tts')},
        'translation': {'provider': settings.translation_provider,
                        'primary_model': settings.translation_model,
                        'fallback_model': settings.translation_fallback_model,
                        'configured': bool(settings.translation_api_key)},
        'defaults': {'quality': settings.video_quality, 'voice': settings.tts_voice,
                     'target_language': settings.target_language,
                     'episode_concurrency': settings.episode_concurrency},
    }


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


# -- media (job_id lookup only; never raw filesystem paths) -----------------

_MEDIA_KINDS = {
    'final.mp4': ('final_path', 'video/mp4', 'final.vi.mp4'),
    'source.vi.srt': ('vi_srt_path', 'application/x-subrip; charset=utf-8', None),
    'source.original.srt': ('source_srt_path', 'application/x-subrip; charset=utf-8', None),
}


@router.get('/web/media/{job_id}/{kind}')
def web_media(request: Request, job_id: str, kind: str, download: int = 0):
    web_auth.require_web_user_api(request)
    if kind not in _MEDIA_KINDS:
        raise HTTPException(status_code=404, detail='Unknown artifact')
    attr, media_type, default_name = _MEDIA_KINDS[kind]
    with SessionLocal() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        raw = getattr(job, attr, None)
    if not raw:
        raise HTTPException(status_code=404, detail=f'{kind} not ready')
    path = Path(raw)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f'{kind} not ready')
    filename = path.name if not download else (default_name or path.name)
    headers = {'Content-Disposition': f'attachment; filename="{filename}"'} if download else None
    return FileResponse(path, media_type=media_type, filename=filename, headers=headers)


@router.get('/web/api/youtube/channels')
def web_youtube_channels(request: Request):
    web_auth.require_web_user_api(request)
    from app.youtube.credentials import list_destinations, public_info

    return {'items': [public_info(d) for d in list_destinations(active_only=False)]}


@router.get('/web/api/youtube/config')
def web_youtube_config(request: Request):
    web_auth.require_web_user_api(request)
    from app.youtube import oauth as yt_oauth

    try:
        callback_url = yt_oauth.redirect_uri()
        oauth_configured = True
    except yt_oauth.YouTubeOAuthError:
        callback_url = None
        oauth_configured = False
    return {
        'callback_url': callback_url,
        'oauth_configured': oauth_configured,
        'default_privacy': settings.youtube_default_privacy,
        'auto_upload': bool(settings.youtube_auto_upload),
        'upload_concurrency': settings.youtube_upload_concurrency,
    }


@router.delete('/web/api/youtube/channels/{destination_id}')
def web_youtube_disconnect(request: Request, destination_id: str):
    web_auth.require_web_user_api(request)
    from app.youtube.credentials import disconnect_destination

    if not disconnect_destination(destination_id):
        raise HTTPException(status_code=404, detail='YOUTUBE_DESTINATION_NOT_FOUND')
    return {'id': destination_id, 'is_active': False}


@router.get('/web/api/jobs/{job_id}/youtube')
def web_job_youtube(request: Request, job_id: str):
    web_auth.require_web_user_api(request)
    with SessionLocal() as db:
        if db.get(EpisodeJob, job_id) is None:
            raise HTTPException(status_code=404, detail='Job not found')
    from app.youtube.service import publication_info

    return {'job_id': job_id, 'publications': publication_info(job_id)}


@router.post('/web/api/jobs/{job_id}/youtube/upload')
async def web_job_youtube_upload(request: Request, job_id: str):
    web_auth.require_web_user_api(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    destination_id = (body.get('destination_id') or '').strip()
    if not destination_id:
        raise HTTPException(status_code=422, detail='destination_id required')
    privacy = str(body.get('privacy') or settings.youtube_default_privacy or 'public').strip().lower()
    if privacy not in ('public', 'unlisted', 'private'):
        raise HTTPException(status_code=422, detail='privacy must be public|unlisted|private')
    with SessionLocal() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        if not job.final_path:
            raise HTTPException(status_code=409, detail='final video not ready')
    from app.youtube.service import YouTubeServiceError, get_or_create_publication

    try:
        pub = get_or_create_publication(job_id, destination_id, privacy)
    except YouTubeServiceError as exc:
        status = 404 if exc.code == 'YOUTUBE_DESTINATION_NOT_FOUND' else 409
        raise HTTPException(status_code=status, detail=exc.code) from exc
    with SessionLocal.begin() as db:
        job = db.get(EpisodeJob, job_id)
        if job is not None and job.status not in ('uploading_youtube',):
            job.status = 'ready_to_upload'
            job.current_stage = 'ready_to_upload'
            job.error_code = None
            job.error_message = None
            job.lease_owner = None
            job.lease_until = None
    return {'job_id': job_id, 'publication_id': pub.id, 'status': 'ready_to_upload'}


@router.post('/web/api/jobs/{job_id}/youtube/retry')
async def web_job_youtube_retry(request: Request, job_id: str):
    web_auth.require_web_user_api(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    from app.youtube.service import YouTubeServiceError, retry_publication

    try:
        pub = retry_publication(job_id, (body.get('destination_id') or None))
    except YouTubeServiceError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return {'job_id': job_id, 'publication_id': pub.id, 'status': 'ready_to_upload'}


@router.delete('/web/api/jobs/{job_id}/youtube')
def web_job_youtube_cancel(request: Request, job_id: str):
    web_auth.require_web_user_api(request)
    with SessionLocal() as db:
        if db.get(EpisodeJob, job_id) is None:
            raise HTTPException(status_code=404, detail='Job not found')
    from app.youtube.service import cancel_publication

    return {'job_id': job_id, 'cancelled': cancel_publication(job_id)}


@router.get('/web/api/jobs/{job_id}/artifacts')
def web_artifacts(request: Request, job_id: str):
    web_auth.require_web_user_api(request)
    detail = _job_detail(job_id)
    base = f'/web/media/{job_id}'
    return {'job_id': job_id, 'status': detail['status'],
            'artifacts': {
                'final': f'{base}/final.mp4' if detail['artifacts']['final'] else None,
                'vi_srt': f'{base}/source.vi.srt' if detail['artifacts']['vi_srt'] else None,
                'source_srt': (f'{base}/source.original.srt'
                               if detail['artifacts']['source_srt'] else None),
            }}


@router.get('/healthz', include_in_schema=False)
def web_healthz():
    return {'ok': True, 'ui': 'dramawave-studio'}

