"""DramaWave series/episode orchestration + episode Phase-1 worker (download + ASR).

Reuses: provider (app/sources), compressed-audio extraction, ASR service
(JianYing primary + Whisper fallback), storage abstraction, lease pattern.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select

from app.config import settings
from app.db import SessionLocal
from app.models import Episode, EpisodeJob, Series
from app.sources.base import EpisodeInfo, SeriesInfo, SourceError
from app.sources.dramawave import DramaWaveSource
from app.storage.factory import get_storage

logger = logging.getLogger('dramawave')

EPISODE_ACTIVE_STATES = (
    'queued', 'resolving_playback', 'downloading', 'extracting_audio',
    'detecting_language', 'transcribing', 'validating_source_srt',
    'translating', 'validating_translation', 'generating_tts',
    'syncing_voice', 'rendering', 'validating_final',
)
# Back-compat aliases for jobs created before the rename.
EPISODE_STAGE_ALIASES = {
    'resolving': 'resolving_playback',
    'downloaded': 'extracting_audio',
    'validating_srt': 'validating_source_srt',
}
EPISODE_TERMINAL_OK = 'completed'


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _provider() -> DramaWaveSource:
    if not settings.dramawave_enabled:
        raise SourceError('DRAMAWAVE_RESOLVE_FAILED', 'provider disabled')
    return DramaWaveSource()


# -- Series / episode registry ----------------------------------------------

def get_or_create_series(info: SeriesInfo, source_url: str) -> Series:
    with SessionLocal.begin() as db:
        row = db.execute(select(Series).where(
            Series.provider == info.provider,
            Series.provider_series_id == info.provider_series_id)).scalars().first()
        meta = json.dumps({'title': info.title, 'description': info.description,
                           'cover_url': info.cover_url, 'episode_count': info.episode_count,
                           **(info.metadata or {})}, ensure_ascii=False)
        if row is None:
            row = Series(provider=info.provider, provider_series_id=info.provider_series_id,
                         title=info.title, cover_url=info.cover_url or None,
                         episode_count=info.episode_count,
                         source_url=source_url, series_metadata=meta)
            db.add(row)
            db.flush()
        else:
            row.title = info.title or row.title
            row.cover_url = info.cover_url or row.cover_url
            row.episode_count = info.episode_count if info.episode_count is not None else row.episode_count
            row.source_url = source_url or row.source_url
            row.series_metadata = meta
        db.flush()
        db.expunge(row)
        return row


def sync_episodes(series: Series, infos: list[EpisodeInfo]) -> list[Episode]:
    """Upsert episodes (no duplicates on re-resolve). Returns rows in order."""
    rows: list[Episode] = []
    with SessionLocal.begin() as db:
        for info in infos:
            row = db.execute(select(Episode).where(
                Episode.series_id == series.id,
                Episode.provider_episode_id == info.provider_episode_id)).scalars().first()
            if row is None:
                row = Episode(series_id=series.id, provider_episode_id=info.provider_episode_id,
                              episode_number=info.episode_number, title=info.title,
                              duration=info.duration, locked=info.locked,
                              status='locked' if info.locked else 'discovered')
                db.add(row)
                db.flush()
            else:
                row.episode_number = info.episode_number
                row.title = info.title or row.title
                row.duration = info.duration if info.duration is not None else row.duration
                row.locked = info.locked
                if info.locked and row.status not in ('ready',):
                    row.status = 'locked'
                elif not info.locked and row.status == 'locked':
                    row.status = 'discovered'
            db.flush()
            db.expunge(row)
            rows.append(row)
    return rows


def episode_workdir(series: Series, episode_number: int) -> Path:
    return settings.work_dir / 'series' / series.provider_series_id / f'{episode_number:03d}'


def storage_prefix(series: Series, episode_number: int) -> str:
    return f'series/{series.provider_series_id}/episodes/{episode_number:03d}'


def persist_series_metadata(series: Series, info: SeriesInfo, episodes: list[EpisodeInfo]) -> str:
    storage = get_storage()
    doc = {'provider': info.provider, 'series_id': info.provider_series_id, 'title': info.title,
           'description': info.description, 'cover_url': info.cover_url,
           'episode_count': info.episode_count, 'source_url': info.source_url,
           'episodes': [{'episode_number': e.episode_number, 'episode_id': e.provider_episode_id,
                         'title': e.title, 'duration': e.duration, 'locked': e.locked} for e in episodes]}
    tmp = settings.work_dir / 'series' / info.provider_series_id / 'metadata.json'
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding='utf-8')
    return storage.put_file(tmp, f'series/{info.provider_series_id}/metadata.json')


def enqueue_episodes(series_id: str, from_ep: int = 1, to_ep: int | None = None,
                     force: bool = False, quality: str | None = None,
                     target_language: str | None = None, voice: str | None = None) -> dict:
    """Enqueue unlocked episodes in [from_ep, to_ep]. Skips ready unless force."""
    with SessionLocal() as db:
        series = db.get(Series, series_id)
        if series is None:
            raise SourceError('DRAMAWAVE_SERIES_NOT_FOUND', series_id[:16])
        eps = list(db.execute(select(Episode).where(Episode.series_id == series_id)
                              .order_by(Episode.episode_number)).scalars().all())
    enqueued: list[str] = []
    skipped: list[int] = []
    for ep in eps:
        if ep.episode_number is None or ep.episode_number < from_ep:
            continue
        if to_ep is not None and ep.episode_number > to_ep:
            continue
        if ep.locked:
            skipped.append(ep.episode_number)
            continue
        with SessionLocal.begin() as db:
            job = db.execute(select(EpisodeJob).where(EpisodeJob.episode_id == ep.id)).scalars().first()
            if job is not None and job.status == 'ready' and not force:
                skipped.append(ep.episode_number)
                continue
            if job is None:
                job = EpisodeJob(episode_id=ep.id, status='queued', current_stage='queued',
                                 requested_quality=quality, target_language=target_language,
                                 voice=voice)
                db.add(job)
                db.flush()
            else:
                job.status = 'queued'
                job.current_stage = 'queued'
                job.progress = 0
                job.error_code = None
                job.error_message = None
                job.lease_owner = None
                job.lease_until = None
                if quality:
                    job.requested_quality = quality
                if target_language:
                    job.target_language = target_language
                if voice:
                    job.voice = voice
            ep_row = db.get(Episode, ep.id)
            if ep_row.status != 'ready' or force:
                ep_row.status = 'queued'
            enqueued.append(job.id)
    logger.info('dramawave enqueue series=%s range=%s-%s enqueued=%s skipped=%s',
                series_id[:8], from_ep, to_ep, len(enqueued), skipped)
    return {'enqueued': enqueued, 'skipped_locked_or_ready': skipped}


# -- Worker claim --------------------------------------------------------------

def claim_episode_job(worker_id: str) -> str | None:
    now = utcnow()
    max_active = max(1, settings.episode_concurrency)
    with SessionLocal.begin() as db:
        active = db.execute(select(func.count()).select_from(EpisodeJob).where(
            EpisodeJob.status.in_(EPISODE_ACTIVE_STATES),
            EpisodeJob.lease_owner.is_not(None))).scalar() or 0
        if active >= max_active:
            return None
        stmt = (select(EpisodeJob)
                .where(EpisodeJob.status.in_(EPISODE_ACTIVE_STATES + ('queued',)))
                .where(or_(EpisodeJob.lease_until.is_(None), EpisodeJob.lease_until < now,
                           EpisodeJob.lease_owner == worker_id))
                .order_by(EpisodeJob.created_at.asc()).limit(1))
        job = db.execute(stmt).scalars().first()
        if job is None:
            return None
        job.lease_owner = worker_id
        job.lease_until = now + timedelta(seconds=settings.job_lease_seconds)
        job.attempts += 1
        if job.started_at is None:
            job.started_at = now
        job.heartbeat_at = now
        return job.id


@contextmanager
def _heartbeat(job_id: str, worker_id: str):
    import threading

    stop = threading.Event()

    def run() -> None:
        while not stop.wait(max(5, settings.worker_heartbeat_seconds)):
            try:
                with SessionLocal.begin() as db:
                    job = db.get(EpisodeJob, job_id)
                    if job is not None and job.lease_owner == worker_id:
                        job.lease_until = utcnow() + timedelta(seconds=settings.job_lease_seconds)
                        job.heartbeat_at = utcnow()
            except Exception:
                pass

    th = threading.Thread(target=run, daemon=True)
    th.start()
    try:
        yield
    finally:
        stop.set()
        th.join(timeout=2)


def _set_stage(job_id: str, stage: str, progress: int, episode_status: str | None = None) -> None:
    with SessionLocal.begin() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            raise RuntimeError('Episode job disappeared')
        job.status = stage
        job.current_stage = stage
        job.progress = progress
        job.error_code = None
        job.error_message = None
        job.heartbeat_at = utcnow()
        if episode_status:
            ep = db.get(Episode, job.episode_id)
            if ep is not None:
                ep.status = episode_status


# Stage order for checkpoint resume: re-running a job skips phases whose
# artifacts are already valid instead of redoing download/ASR/translation.
_EP_STAGE_ORDER = [
    'queued', 'resolving', 'resolving_playback', 'downloading', 'extracting_audio',
    'detecting_language', 'transcribing', 'validating_source_srt',
    'translating', 'validating_translation', 'generating_tts',
    'syncing_voice', 'rendering', 'validating_final', 'completed',
]


def _stage_reached(current: str | None, target: str) -> bool:
    try:
        return _EP_STAGE_ORDER.index(current or '') >= _EP_STAGE_ORDER.index(target)
    except ValueError:
        return False


def _valid_file(path: str | None) -> Path | None:
    if not path:
        return None
    try:
        p = Path(path)
    except (TypeError, ValueError):
        return None
    return p if p.exists() and p.stat().st_size > 0 else None


def _fail_episode(job_id: str, code: str, message: str) -> None:
    with SessionLocal.begin() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            return
        job.status = 'failed'
        job.current_stage = 'failed'
        job.error_code = code[:64]
        job.error_message = message[:2000]
        job.lease_owner = None
        job.lease_until = None
        ep = db.get(Episode, job.episode_id)
        if ep is not None and ep.status != 'ready':
            ep.status = 'failed'
    logger.error('dramawave episode failed job=%s code=%s msg=%s', job_id[:8], code, message[:300])


# -- Episode Phase 1 --------------------------------------------------------------

def process_episode_job(job_id: str, worker_id: str) -> None:
    storage = get_storage()
    t_all = time.monotonic()
    with SessionLocal() as db:
        job = db.get(EpisodeJob, job_id)
        if job is None:
            return
        ep = db.get(Episode, job.episode_id)
        series = db.get(Series, ep.series_id) if ep else None
        stage = job.current_stage
    if ep is None or series is None:
        raise RuntimeError('Episode/series missing')
    workdir = episode_workdir(series, ep.episode_number or 0)
    workdir.mkdir(parents=True, exist_ok=True)
    prefix = storage_prefix(series, ep.episode_number or 0)
    provider = _provider()

    try:
        with _heartbeat(job_id, worker_id):
            logger.info('dramawave episode start job=%s series=%s ep=%s stage=%s',
                        job_id[:8], series.provider_series_id, ep.episode_number, stage)
            # resolving_playback: refresh episode info + playback for unlocked episodes.
            t0 = time.monotonic()
            if stage in ('queued', 'resolving', 'resolving_playback'):
                _set_stage(job_id, 'resolving_playback', 5, 'resolving_playback')
                episodes = provider.list_episodes(_series_info(series))
                match = next((e for e in episodes if e.provider_episode_id == _provider_episode_id(ep)), None)
                if match is None:
                    raise SourceError('DRAMAWAVE_EPISODES_NOT_FOUND', f'ep{ep.episode_number}')
                if match.locked:
                    with SessionLocal.begin() as db:
                        db.get(Episode, ep.id).locked = True
                        db.get(Episode, ep.id).status = 'locked'
                    raise SourceError('DRAMAWAVE_EPISODE_LOCKED', f'ep{ep.episode_number}')
                with SessionLocal.begin() as db:
                    row = db.get(Episode, ep.id)
                    row.title = match.title or row.title
                    row.duration = match.duration if match.duration is not None else row.duration
                    j = db.get(EpisodeJob, job_id)
                    j.resolve_seconds = time.monotonic() - t0
            # downloading.
            with SessionLocal() as db:
                j = db.get(EpisodeJob, job_id)
                local_video = Path(j.original_path) if j.original_path else None
            video_path = workdir / 'original.mp4'
            has_remote = storage.exists(f'{prefix}/original.mp4')
            if not has_remote and (not local_video or not local_video.exists()) and not video_path.exists():
                _set_stage(job_id, 'downloading', 20, 'downloading')
                episodes = provider.list_episodes(_series_info(series))
                match = next((e for e in episodes if e.provider_episode_id == _provider_episode_id(ep)), None)
                if match is None or match.locked:
                    raise SourceError('DRAMAWAVE_EPISODE_LOCKED' if match else 'DRAMAWAVE_EPISODES_NOT_FOUND',
                                      f'ep{ep.episode_number}')
                playback = provider.resolve_episode(match, quality=(j.requested_quality or None))
                t0 = time.monotonic()
                try:
                    result = provider.download_episode(playback, video_path)
                except SourceError as dl_exc:
                    # Signed URL may have expired between resolve and download:
                    # re-resolve once, then retry once.
                    if dl_exc.code == 'DRAMAWAVE_DOWNLOAD_FAILED' and _looks_expired(str(dl_exc)):
                        logger.info('dramawave playback possibly expired job=%s, re-resolving', job_id[:8])
                        playback = provider.refresh_playback(match)
                        result = provider.download_episode(playback, video_path)
                    else:
                        raise
                with SessionLocal.begin() as db:
                    j = db.get(EpisodeJob, job_id)
                    j.original_path = str(result.path)
                    j.playback_type = result.playback_type
                    j.quality = result.quality
                    j.download_seconds = time.monotonic() - t0
                    j.progress = 45
                logger.info('dramawave downloaded job=%s size=%s quality=%s seconds=%.1f',
                            job_id[:8], result.size_bytes, result.quality, result.download_seconds)
                storage.put_file(video_path, f'{prefix}/original.mp4')
            else:
                logger.info('dramawave download skipped job=%s (exists)', job_id[:8])
                if not video_path.exists() and local_video and local_video.exists():
                    video_path = local_video
            # extracting_audio + ASR (resume: skip when a valid source SRT exists
            # and the job already passed this phase — no re-download/re-ASR).
            with SessionLocal() as db:
                _j = db.get(EpisodeJob, job_id)
                _resume_src = _valid_file(_j.source_srt_path) if _j else None
                _resume_cues = (_j.subtitle_cue_count or 0) if _j else 0
                _resume_lang = (_j.source_language or 'zh') if _j else 'zh'
            if _stage_reached(stage, 'translating') and _resume_src and _resume_cues:
                logger.info('dramawave asr skipped job=%s (resume: %s cues=%s)',
                            job_id[:8], _resume_src.name, _resume_cues)
                cue_count = _resume_cues
                lang = _resume_lang
            else:
                _set_stage(job_id, 'extracting_audio', 25, 'extracting_audio')
                from app.media.audio import extract_compressed_audio

                t0 = time.monotonic()
                audio_path = extract_compressed_audio(video_path, workdir, job_id=f'dw-{ep.episode_number}')
                audio_secs = time.monotonic() - t0
                # detecting_language + transcribing (language-routed ASR, reused service).
                _set_stage(job_id, 'detecting_language', 27, 'detecting_language')
                _set_stage(job_id, 'transcribing', 30, 'transcribing')
                from app.asr.service import transcribe_with_fallback

                t0 = time.monotonic()
                match = next((e for e in provider.list_episodes(_series_info(series))
                              if e.provider_episode_id == _provider_episode_id(ep)), None)
                if match is None:
                    raise SourceError('DRAMAWAVE_EPISODES_NOT_FOUND', f'ep{ep.episode_number}')
                source_lang = _detect_language(match)
                with SessionLocal.begin() as db:
                    db.get(EpisodeJob, job_id).source_language = source_lang or None
                out_path, lang, cues, asr_provider, fallback_used, asr_secs, _up, _rec = transcribe_with_fallback(
                    audio_path, None, workdir, job_id=f'dw-{ep.episode_number}', language=source_lang)
                logger.info('dramawave asr job=%s lang=%s provider=%s fallback=%s cues=%s seconds=%.1f',
                            job_id[:8], lang, asr_provider, fallback_used, cues, asr_secs)
                # validating_source_srt.
                _set_stage(job_id, 'validating_source_srt', 48, 'validating_source_srt')
                from app.media.srt import validate_srt

                cue_count = validate_srt(out_path)
                srt_key = storage.put_file(out_path, f'{prefix}/source.original.srt')
                try:
                    (workdir / 'audio.m4a').unlink()
                except OSError:
                    pass
                with SessionLocal.begin() as db:
                    j = db.get(EpisodeJob, job_id)
                    j.original_path = str(video_path)
                    j.source_srt_path = str(out_path)
                    j.source_language = lang
                    j.subtitle_cue_count = cue_count
                    j.audio_extract_seconds = audio_secs
                    j.asr_seconds = asr_secs
                    j.asr_provider = asr_provider
                    j.asr_fallback_used = bool(fallback_used)
                    j.progress = 48
            # ---- Phase 2: translation -> TTS -> voice (reused pipeline) ----
            # Resume: skip full translation when a valid VI SRT already exists.
            with SessionLocal() as db:
                _j = db.get(EpisodeJob, job_id)
                _resume_vi = _valid_file(_j.vi_srt_path) if _j else None
            if _stage_reached(stage, 'generating_tts') and _resume_vi:
                logger.info('dramawave translation skipped job=%s (resume: %s)',
                            job_id[:8], _resume_vi.name)
            else:
                _translate_episode(job_id, series, ep, workdir, prefix, storage, lang)
            _tts_episode_voice(job_id, series, ep, workdir, prefix, storage)
            # ---- Phase 3: render ----
            _render_episode(job_id, series, ep, workdir, prefix, storage, video_path)
            with SessionLocal.begin() as db:
                j = db.get(EpisodeJob, job_id)
                j.status = 'completed'
                j.current_stage = 'completed'
                j.progress = 100
                j.completed_at = utcnow()
                j.lease_owner = None
                j.lease_until = None
                j.heartbeat_at = utcnow()
                db.get(Episode, ep.id).status = 'ready'
                _snapshot_job(db, j, workdir)
            storage.put_file(workdir / 'job.json', f'{prefix}/job.json')
            logger.info('dramawave episode completed job=%s ep=%s cues=%s total=%.1fs',
                        job_id[:8], ep.episode_number, cue_count, time.monotonic() - t_all)
    except SourceError as exc:
        _fail_episode(job_id, exc.code, str(exc))
        raise
    except Exception as exc:
        _fail_episode(job_id, 'DRAMAWAVE_DOWNLOAD_FAILED', f'{type(exc).__name__}: {exc}')
        raise


def _series_info(series: Series) -> SeriesInfo:
    import json as _json

    meta = {}
    try:
        meta = _json.loads(series.series_metadata or '{}')
    except (ValueError, TypeError):
        pass
    return SeriesInfo(provider=series.provider, provider_series_id=series.provider_series_id,
                      title=series.title or '', source_url=series.source_url or '', metadata=meta)


def _provider_episode_id(ep: Episode) -> str:
    return ep.provider_episode_id


def _looks_expired(message: str) -> bool:
    lowered = (message or '').lower()
    return any(marker in lowered for marker in
               ('401', '403', 'forbidden', 'unauthorized', 'expired', 'expire', 'token', 'denied'))


def _detect_language(match: EpisodeInfo) -> str:
    """Source language for ASR routing: API tag -> short code, '' = auto-detect.

    DramaWave is Chinese-first, but never assume: unknown tags fall through to
    Whisper auto-detect (JianYing is zh-only and must not receive other audio).
    """
    raw = str((match.metadata or {}).get('original_audio_language') or '').strip().lower()
    if not raw:
        return (settings.source_language or '').strip().lower()
    if raw.startswith('zh') or 'chinese' in raw or 'cmn' in raw or 'mandarin' in raw:
        return 'zh'
    for code in ('en', 'ko', 'ja', 'es', 'fr', 'de', 'it', 'pt', 'ru', 'ar',
                 'hi', 'th', 'vi', 'id', 'ms', 'tr', 'nl', 'pl', 'uk'):
        if raw == code or raw.startswith(code + '-') or raw.startswith(code + '_'):
            return code
    return ''


# -- Series translation memory -------------------------------------------------

def _series_glossary(series: Series) -> dict:
    def _load(raw):
        try:
            data = json.loads(raw or '{}')
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {'characters': _load(series.character_glossary),
            'relationships': _load(series.relationship_glossary),
            'pronouns': {}}


def _write_series_glossary_file(series: Series, workdir: Path) -> None:
    (workdir / 'glossary.json').write_text(
        json.dumps(_series_glossary(series), ensure_ascii=False, indent=1), encoding='utf-8')


def _toolnet_json(system: str, user_obj: dict, max_tokens: int = 800) -> str:
    """Single ad-hoc ToolNet call (style detect, glossary learn). Same chain."""
    import urllib.request

    from app.translation.providers_toolnet import _post_chat_stream
    from app.translation.providers_toolnet import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider()
    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps(user_obj, ensure_ascii=False)}]
    last_err: Exception | None = None
    for model in provider.models:
        try:
            return _post_chat_stream(messages, model, settings.translation_timeout, max_tokens)
        except Exception as exc:  # noqa: BLE001 - try next model
            last_err = exc
    raise RuntimeError(f'TRANSLATION_FAILED: {last_err}')


def _ensure_style(series_id: str, title: str, desc: str, sample: str) -> str:
    with SessionLocal() as db:
        row = db.get(Series, series_id)
        if row is not None and (row.translation_style or '').strip().upper() in {
                'MODERN_DRAMA', 'XIANXIA', 'NINETIES'}:
            return row.translation_style.strip().upper()
    style = 'MODERN_DRAMA'
    try:
        raw = _toolnet_json(
            'Classify the drama style. Reply JSON only: {"style": "MODERN_DRAMA"|"XIANXIA"|"NINETIES"}.',
            {'title': title, 'description': (desc or '')[:500], 'sample': sample[:800]})
        text = raw.strip()
        if text.startswith('```'):
            text = text.strip('`').strip()
            if text.lower().startswith('json'):
                text = text[4:].strip()
        guess = (json.loads(text).get('style') or '').strip().upper()
        if guess in {'MODERN_DRAMA', 'XIANXIA', 'NINETIES'}:
            style = guess
    except Exception as exc:  # noqa: BLE001 - default style
        logger.warning('style detect failed series=%s: %s', series_id[:8], str(exc)[:150])
    with SessionLocal.begin() as db:
        row = db.get(Series, series_id)
        if row is not None:
            row.translation_style = style
    logger.info('series style series=%s style=%s', series_id[:8], style)
    return style


def _learn_glossary(series_id: str, sample_vi: list[str]) -> None:
    """Extract character naming (zh->vi) from translated sample; merge into series."""
    if not sample_vi:
        return
    try:
        raw = _toolnet_json(
            'Extract character names used in these Vietnamese subtitles. Reply JSON only: '
            '{"characters": {"<zh or vi name>": "<vi name>"}, "relationships": {}}.',
            {'subtitles': sample_vi[:30]})
        text = raw.strip()
        if text.startswith('```'):
            text = text.strip('`').strip()
            if text.lower().startswith('json'):
                text = text[4:].strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            return
        with SessionLocal.begin() as db:
            row = db.get(Series, series_id)
            if row is None:
                return
            chars = json.loads(row.character_glossary or '{}') if row.character_glossary else {}
            rels = json.loads(row.relationship_glossary or '{}') if row.relationship_glossary else {}
            for k, v in (data.get('characters') or {}).items():
                chars.setdefault(str(k), str(v))
            for k, v in (data.get('relationships') or {}).items():
                rels.setdefault(str(k), str(v))
            row.character_glossary = json.dumps(chars, ensure_ascii=False)
            row.relationship_glossary = json.dumps(rels, ensure_ascii=False)
        logger.info('glossary learned series=%s', series_id[:8])
    except Exception as exc:  # noqa: BLE001 - best effort only
        logger.warning('glossary learn failed series=%s: %s', series_id[:8], str(exc)[:150])


# -- Episode translation --------------------------------------------------------

def _translate_episode(job_id: str, series: Series, ep: Episode, workdir: Path,
                       prefix: str, storage, lang: str) -> None:
    from app.translation.service import (
        build_vi_srt,
        compute_cps,
        parse_srt_cues,
        translate_cues,
        validate_vi_against_zh,
    )

    _set_stage(job_id, 'translating', 50, 'translating')
    with SessionLocal() as db:
        j = db.get(EpisodeJob, job_id)
        zh_path = Path(j.source_srt_path) if j.source_srt_path else workdir / 'source.original.srt'
    cues = parse_srt_cues(zh_path)
    if not cues:
        raise SourceError('SRT_VALIDATION_FAILED', 'empty source SRT')
    _write_series_glossary_file(series, workdir)
    style = _ensure_style(series.id, series.title or '', '', ' '.join(c['text'] for c in cues[:10]))
    t0 = time.monotonic()

    def _on_chunk(index: int, total: int) -> None:
        with SessionLocal.begin() as db:
            j = db.get(EpisodeJob, job_id)
            if j is not None:
                j.progress = 50 + int(15 * (index + 1) / max(1, total))

    mapping, info = translate_cues(cues, workdir, job_id=job_id, style=style,
                                   source_language=lang or 'zh', on_chunk=_on_chunk)
    # CPS auto-compression (timestamps untouched).
    from app.translation.providers_toolnet import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider()
    by_id = {c['id']: c for c in cues}
    for _ in range(2):
        over = [cid for cid, text in mapping.items()
                if compute_cps(text, by_id[cid]['end_ms'] - by_id[cid]['start_ms']) > settings.cps_target]
        if not over:
            break
        for cid in over:
            dur = by_id[cid]['end_ms'] - by_id[cid]['start_ms']
            try:
                shorter = provider.compress_text(cid, mapping[cid], dur)
                if compute_cps(shorter, dur) < compute_cps(mapping[cid], dur):
                    mapping[cid] = shorter
            except Exception:  # noqa: BLE001 - keep original
                pass
    vi_text = build_vi_srt(cues, mapping)
    validate_vi_against_zh(zh_path, vi_text)
    vi_path = workdir / 'source.vi.srt'
    vi_path.write_text(vi_text if vi_text.endswith('\n') else vi_text + '\n', encoding='utf-8', newline='\n')
    key = storage.put_file(vi_path, f'{prefix}/source.vi.srt')
    with SessionLocal.begin() as db:
        j = db.get(EpisodeJob, job_id)
        j.vi_srt_path = str(vi_path)
        j.translation_seconds = time.monotonic() - t0
        j.progress = 68
    logger.info('episode translated job=%s cues=%s batches=%s seconds=%.1f srt=%s',
                job_id[:8], len(cues), info.get('batches'), time.monotonic() - t0, key)
    _set_stage(job_id, 'validating_translation', 68, 'validating_translation')
    _learn_glossary(series.id, [mapping[c['id']] for c in cues[:30] if c['id'] in mapping])


# -- Episode speech-block TTS + voice --------------------------------------------

def _tts_episode_voice(job_id: str, series: Series, ep: Episode, workdir: Path,
                       prefix: str, storage) -> None:
    from app.translation.service import parse_srt_cues
    from app.tts.blocks import build_blocks
    from app.tts.providers_edge import EdgeTTSProvider
    from app.tts.service import clip_mp3, clip_wav, decode_to_wav, fit_tempo
    from app.tts.voice import assemble_voice

    _set_stage(job_id, 'generating_tts', 70, 'generating_tts')
    with SessionLocal() as db:
        j = db.get(EpisodeJob, job_id)
        vi_path = Path(j.vi_srt_path) if j.vi_srt_path else workdir / 'source.vi.srt'
    cues = parse_srt_cues(vi_path)
    specs = build_blocks([{'id': c['id'], 'start_ms': c['start_ms'], 'end_ms': c['end_ms'], 'text': c['text']}
                          for c in cues],
                         max_gap_ms=settings.voice_block_max_gap_ms,
                         target_ms=settings.voice_block_target_ms,
                         max_ms=settings.voice_block_max_ms)
    logger.info('episode blocks job=%s blocks=%s', job_id[:8], len(specs))
    tts_dir = workdir / 'tts_blocks'
    tts_dir.mkdir(parents=True, exist_ok=True)
    state_path = tts_dir / 'state.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    provider = EdgeTTSProvider()
    with SessionLocal() as db:
        _j = db.get(EpisodeJob, job_id)
        voice = (_j.voice or '').strip() if _j and _j.voice else provider.resolve_voice()
    t0 = time.monotonic()

    # Initial synthesis (resume: skip valid wavs).
    with SessionLocal.begin() as db:
        j = db.get(EpisodeJob, job_id)
        if j is not None:
            j.tts_blocks_total = len(specs)
            j.tts_blocks_completed = sum(1 for s in specs if _block_ok(tts_dir, state, s))
            j.voice_qa_round = 0
            j.overflow_blocks_remaining = 0
            j.heartbeat_at = utcnow()
    pending = [s for s in specs if not _block_ok(tts_dir, state, s)]
    _run_parallel(pending, lambda s: _synth_block(s, provider, tts_dir, state, job_id, voice), 'tts')
    with SessionLocal.begin() as db:
        j = db.get(EpisodeJob, job_id)
        if j is not None:
            j.tts_blocks_completed = sum(1 for s in specs if _block_ok(tts_dir, state, s))
            j.progress = 70 + int(5 * (j.tts_blocks_completed or 0) / max(1, len(specs)))
            j.heartbeat_at = utcnow()
    # QA rounds: batch-compress overflow tts_text (SRT untouched), regen changed blocks.
    # Fast AI path (short timeout, no primary retries): one sick model call costs
    # seconds, and the circuit breaker routes straight to fallback when open.
    from app.translation.providers_toolnet import OpenAICompatibleProvider

    tprovider = OpenAICompatibleProvider()
    max_rounds = max(1, int(settings.voice_qa_max_rounds))
    for rnd in range(max_rounds):
        _touch_heartbeat(job_id)
        over = _overflow_blocks(tts_dir, state, specs)
        with SessionLocal.begin() as db:
            j = db.get(EpisodeJob, job_id)
            if j is not None:
                j.voice_qa_round = rnd + 1
                j.overflow_blocks_remaining = len(over)
                j.progress = 75 + int(10 * (rnd + 1) / max_rounds)
                j.heartbeat_at = utcnow()
        if not over:
            break
        logger.info('episode voice QA round %s/%s job=%s overflow=%s',
                    rnd + 1, max_rounds, job_id[:8], len(over))
        changed = _compress_qa_batch(tts_dir, state, specs, over, tprovider, job_id)
        _touch_heartbeat(job_id)
        if changed:
            _run_parallel([s for s in specs if s.block_id in changed],
                          lambda s: _synth_block(s, provider, tts_dir, state, job_id, voice), 'tts-regen')
            _save_block_state(tts_dir, state)
            with SessionLocal.begin() as db:
                j = db.get(EpisodeJob, job_id)
                if j is not None:
                    j.tts_blocks_completed = sum(1 for s in specs if _block_ok(tts_dir, state, s))
                    j.overflow_blocks_remaining = len(_overflow_blocks(tts_dir, state, specs))
                    j.heartbeat_at = utcnow()
    _save_block_state(tts_dir, state)
    # Anything still overflowing after max rounds keeps capped atempo (<=1.25x)
    # with timing_warning and the pipeline moves on — no infinite loop.
    leftover = _overflow_blocks(tts_dir, state, specs)
    with SessionLocal.begin() as db:
        j = db.get(EpisodeJob, job_id)
        if j is not None:
            j.overflow_blocks_remaining = len(leftover)
            j.heartbeat_at = utcnow()
    if leftover:
        logger.warning('episode voice QA leftover overflow job=%s blocks=%s (atempo-capped, continuing)',
                       job_id[:8], leftover)
    with SessionLocal.begin() as db:
        j = db.get(EpisodeJob, job_id)
        j.tts_seconds = time.monotonic() - t0
        j.tts_timing_warnings = sum(1 for v in state.values() if v.get('timing_warning'))
        j.progress = 85
    # Syncing voice timeline.
    _set_stage(job_id, 'syncing_voice', 87, 'syncing_voice')
    from app.media.audio import ffprobe_duration

    with SessionLocal() as db:
        j = db.get(EpisodeJob, job_id)
        video_dur = ffprobe_duration(Path(j.original_path)) if j.original_path else None
    if not video_dur:
        raise SourceError('FFPROBE_FAILED', 'video duration unknown')
    pseudo = [{'id': s.block_id, 'start_ms': s.start_ms, 'end_ms': s.end_ms} for s in specs]

    def _get_wav(bid: int) -> Path:
        p = clip_path(tts_dir, bid, 'wav')
        if not p.exists():
            raise SourceError('TTS_FAILED', f'block wav missing {bid}')
        return p

    voice_path = workdir / 'voice.vi.wav'
    voice_path, voice_dur = assemble_voice(pseudo, _get_wav, float(video_dur), voice_path,
                                           job_id=f'dw-{ep.episode_number}')
    storage.put_file(voice_path, f'{prefix}/voice.vi.wav')
    with SessionLocal.begin() as db:
        j = db.get(EpisodeJob, job_id)
        j.voice_path = str(voice_path)
        j.voice_duration = voice_dur
        j.progress = 88


def clip_path(tts_dir: Path, block_id: int, ext: str) -> Path:
    return tts_dir / f'{block_id:06d}.{ext}'


def _block_ok(tts_dir: Path, state: dict, spec) -> bool:
    st = state.get(str(spec.block_id)) or {}
    wav = clip_path(tts_dir, spec.block_id, 'wav')
    return bool(st.get('tts_duration_ms') and wav.exists())


def _run_parallel(items: list, fn, label: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    errors: list[str] = []
    import threading

    lock = threading.Lock()

    def _wrap(item):
        try:
            fn(item)
        except Exception as exc:  # noqa: BLE001 - collect, fail at end
            with lock:
                errors.append(f'{label} {getattr(item, "block_id", "?")}: {exc}')

    with ThreadPoolExecutor(max_workers=max(1, settings.tts_concurrency)) as pool:
        list(pool.map(_wrap, items))
    if errors:
        raise SourceError('TTS_FAILED', '; '.join(errors[:3]))


def _synth_block(spec, provider, tts_dir: Path, state: dict, job_id: str, voice: str | None = None) -> None:
    from app.tts.service import decode_to_wav, fit_tempo

    mp3, wav = clip_path(tts_dir, spec.block_id, 'mp3'), clip_path(tts_dir, spec.block_id, 'wav')
    st = state.get(str(spec.block_id)) or {}
    text = st.get('tts_text') or spec.tts_text
    last_err: Exception | None = None
    for _ in range(max(1, settings.tts_max_retries) + 1):
        try:
            if not mp3.exists() or mp3.stat().st_size <= 0:
                clip = provider.synthesize(spec.block_id, text, mp3, voice)
                spoken = clip.duration_ms
            else:
                from app.tts.providers_edge import ffprobe_ms

                spoken = ffprobe_ms(mp3)
            tempo, _warn = fit_tempo(spoken, spec.available_ms)
            decode_to_wav(mp3, wav, tempo=tempo, sample_rate=settings.tts_sample_rate)
            cls, final_ms, overflow = _classify_spoken(spoken, tempo, spec.available_ms)
            state[str(spec.block_id)] = {'tts_text': text, 'tts_duration_ms': spoken,
                                         'tempo': tempo, 'qa_class': cls,
                                         'timing_warning': cls in ('OVERFLOW', 'SEVERE_OVERFLOW'),
                                         'overflow_ms': overflow}
            return
        except Exception as exc:  # noqa: BLE001 - retry single block only
            last_err = exc
            time.sleep(2)
    raise RuntimeError(f'block {spec.block_id}: {last_err}')


def _classify_spoken(spoken_ms: int | None, tempo: float | None, available_ms: int):
    if spoken_ms is None or spoken_ms <= 0 or available_ms <= 0:
        return 'OVERFLOW', spoken_ms, max(0, (spoken_ms or 0))
    if spoken_ms <= available_ms:
        return 'FIT', spoken_ms, 0
    tempo = min(tempo if tempo and tempo > 0 else 1.0, settings.tts_max_tempo)
    final_ms = int(round(spoken_ms / tempo))
    if final_ms <= available_ms:
        return 'ADJUSTED_FIT', final_ms, 0
    overflow = final_ms - available_ms
    if overflow > settings.qa_severe_overflow_ms or overflow / available_ms > settings.qa_severe_overflow_ratio:
        return 'SEVERE_OVERFLOW', final_ms, overflow
    return 'OVERFLOW', final_ms, overflow


def _overflow_blocks(tts_dir: Path, state: dict, specs: list) -> list[int]:
    out = []
    for spec in specs:
        st = state.get(str(spec.block_id)) or {}
        if not st.get('tts_duration_ms'):
            out.append(spec.block_id)
            continue
        cls, _, _ = _classify_spoken(st['tts_duration_ms'], st.get('tempo') or 1.0, spec.available_ms)
        st['qa_class'] = cls
        if cls in ('OVERFLOW', 'SEVERE_OVERFLOW'):
            out.append(spec.block_id)
    _save_block_state(tts_dir, state)
    return out


def _save_block_state(tts_dir: Path, state: dict) -> None:
    (tts_dir / 'state.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')


def _touch_heartbeat(job_id: str) -> None:
    """Refresh heartbeat while a long AI/TTS step is in flight (lease stays alive)."""
    try:
        with SessionLocal.begin() as db:
            j = db.get(EpisodeJob, job_id)
            if j is not None:
                j.heartbeat_at = utcnow()
    except Exception:  # noqa: BLE001 - heartbeat must never fail the job
        pass


def _compress_qa_batch(tts_dir: Path, state: dict, specs: list, over: list[int],
                       tprovider, job_id: str) -> list[int]:
    """Compress overflow blocks with ONE batched AI call per chunk (SRT untouched).

    Returns changed block ids (their mp3/wav are deleted for regen).
    Batch failure falls back to per-block compression.
    """
    by_id = {s.block_id: s for s in specs}
    qa_timeout = max(10, int(settings.voice_qa_ai_timeout))
    qa_primary = max(0, int(settings.voice_qa_primary_retries))
    qa_fallback = max(0, int(settings.voice_qa_fallback_retries))
    items: list[tuple[int, str, int]] = []
    for bid in over:
        st = state.get(str(bid), {})
        current = st.get('tts_text', '')
        if not current:
            continue
        avail = next((s.available_ms for s in specs if s.block_id == bid), 0)
        items.append((bid, current, avail))
    changed: list[int] = []
    for i in range(0, len(items), 10):
        chunk = items[i:i + 10]
        try:
            res, used = tprovider.compress_batch(
                chunk, timeout=qa_timeout,
                primary_retries=qa_primary, fallback_retries=qa_fallback)
            logger.info('episode voice QA compress batch job=%s blocks=%s model=%s',
                        job_id[:8], len(chunk), used)
        except Exception as exc:  # noqa: BLE001 - per-block fallback
            logger.warning('episode voice QA batch failed job=%s error=%s, per-block fallback',
                           job_id[:8], str(exc)[:200])
            res = {}
            for bid, text, dur in chunk:
                try:
                    res[bid] = tprovider.compress_text(
                        bid, text, dur, timeout=qa_timeout,
                        primary_retries=qa_primary, fallback_retries=qa_fallback)
                except Exception as exc2:  # noqa: BLE001
                    logger.warning('episode voice QA compress failed job=%s block=%s error=%s',
                                   job_id[:8], bid, str(exc2)[:200])
        for bid, text, _dur in chunk:
            shorter = res.get(bid)
            if shorter and shorter != text:
                st = state.get(str(bid), {})
                st['tts_text'] = shorter
                state[str(bid)] = st
                for p in (clip_path(tts_dir, bid, 'mp3'), clip_path(tts_dir, bid, 'wav')):
                    try:
                        if p.exists():
                            p.unlink()
                    except OSError:
                        pass
                changed.append(bid)
        _touch_heartbeat(job_id)
    if changed:
        _save_block_state(tts_dir, state)
    logger.info('episode voice QA compressed job=%s changed=%s/%s',
                job_id[:8], len(changed), len(over))
    return changed


# -- Episode render ---------------------------------------------------------------

def _render_episode(job_id: str, series: Series, ep: Episode, workdir: Path,
                    prefix: str, storage, video_path: Path) -> None:
    from app.services.render import render_final

    _set_stage(job_id, 'rendering', 90, 'rendering')
    with SessionLocal() as db:
        j = db.get(EpisodeJob, job_id)
        vi_srt = Path(j.vi_srt_path) if j.vi_srt_path else workdir / 'source.vi.srt'
        voice_wav = Path(j.voice_path) if j.voice_path else workdir / 'voice.vi.wav'
    out_mp4 = workdir / 'final.vi.mp4'
    needs_render = True
    if out_mp4.exists() and out_mp4.stat().st_size > 0:
        try:
            from app.services.render import validate_final, video_info

            validate_final(out_mp4, video_info(video_path))
            needs_render = False
            logger.info('episode render skipped job=%s (valid final exists)', job_id[:8])
        except RuntimeError:
            needs_render = True
    t0 = time.monotonic()
    result = render_final(video_path, vi_srt, voice_wav, out_mp4, job_id=job_id) if needs_render else None
    secs = (result or {}).get('seconds', 0.0)
    if result is None:
        from app.services.render import video_info as _vi

        probe = _vi(out_mp4)
        result = {'duration': probe.get('duration'), 'width': probe.get('width'),
                  'height': probe.get('height'), 'fps': probe.get('fps'),
                  'size': out_mp4.stat().st_size, 'seconds': 0.0}
    key = storage.put_file(out_mp4, f'{prefix}/final.vi.mp4')
    _set_stage(job_id, 'validating_final', 98, 'validating_final')
    with SessionLocal.begin() as db:
        j = db.get(EpisodeJob, job_id)
        j.final_path = str(out_mp4)
        j.render_seconds = secs
        j.progress = 98
    logger.info('episode rendered job=%s final=%s dur=%.1f size=%s key=%s',
                job_id[:8], out_mp4.name, result.get('duration'), result.get('size'), key)


def _snapshot_job(db, job: EpisodeJob, workdir: Path) -> None:
    doc = {c.key: getattr(job, c.key) for c in job.__table__.columns}
    for k, v in list(doc.items()):
        if hasattr(v, 'isoformat'):
            doc[k] = v.isoformat()
    (workdir / 'job.json').write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str),
                                      encoding='utf-8')
