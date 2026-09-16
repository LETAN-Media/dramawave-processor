import json
import logging
import subprocess
from pathlib import Path

from app.bilibili.cookies import resolve_cookies_file
from app.config import settings

logger = logging.getLogger('bilibili-downloader')

AUTH_MARKERS = (
    'login required',
    'not logged in',
    'need login',
    'require login',
    'authentication',
    'auth required',
    'restricted',
    'forbidden',
    '403',
    'only available to members',
)

# Network errors that justify trying the fallback (lower-res AVC) format.
RETRYABLE_NETWORK_MARKERS = (
    'bytes read',
    'more expected',
    'giving up after',
    'connection',
    'timed out',
    'timeout',
    'temporary failure',
    'http error',
    'got error',
)


class YDLLogger:
    def debug(self, msg: str) -> None:
        return

    def warning(self, msg: str) -> None:
        return

    def error(self, msg: str) -> None:
        return


def _is_auth_error(message: str) -> bool:
    lowered = (message or '').lower()
    return any(marker in lowered for marker in AUTH_MARKERS)


def _is_retryable_network_error(message: str) -> bool:
    lowered = (message or '').lower()
    return any(marker in lowered for marker in RETRYABLE_NETWORK_MARKERS)


def _yt_dlp_version() -> str:
    try:
        from yt_dlp.version import __version__ as v

        return str(v)
    except Exception:
        try:
            import yt_dlp

            mod = getattr(yt_dlp, 'version', None)
            if isinstance(mod, str):
                return mod
            return str(getattr(mod, '__version__', '?'))
        except Exception:
            return '?'


def canonical_bilibili_url(url: str, bvid: str | None = None) -> str:
    """Prefer canonical www URL for yt-dlp.

    The m.bilibili share URL with tracking params forces the generic
    extractor (especially with mobile UA). Canonical form is stable:
    https://www.bilibili.com/video/<BVID>
    """
    if bvid:
        return f'https://www.bilibili.com/video/{bvid}'
    return url


def _build_options(workdir: Path, cookies_file: Path | None, download_format: str | None = None) -> dict:
    outtmpl = str(workdir / 'source.%(ext)s')
    options = {
        'format': download_format or settings.download_format,
        'outtmpl': outtmpl,
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'concurrent_fragment_downloads': settings.download_concurrent_fragments,
        # Desktop UA: iPhone UA forces m.bilibili generic extractor.
        'user_agent': settings.bilibili_ytdlp_user_agent,
        'referer': 'https://www.bilibili.com/',
        'logger': YDLLogger(),
        'retries': 10,
        'fragment_retries': 10,
        'continuedl': True,
        'overwrites': False,
        'nocheckcertificate': False,
        'socket_timeout': settings.http_timeout_seconds,
    }
    # Only add cookies when a real file exists. Never "--cookies .".
    if cookies_file is not None:
        options['cookiefile'] = str(cookies_file)
    return options


def _run_download(url: str, workdir: Path, cookies_file: Path | None, download_format: str | None = None) -> Path:
    from yt_dlp import YoutubeDL

    options = _build_options(workdir, cookies_file, download_format)
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        requested = info.get('requested_downloads') or []
        candidates = []
        if info.get('_filename'):
            candidates.append(Path(info['_filename']))
        for item in requested:
            if item.get('filepath'):
                candidates.append(Path(item['filepath']))

    candidates.extend(sorted(workdir.glob('source.*'), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True))
    for candidate in candidates:
        if candidate.exists() and candidate.suffix not in {'.part', '.ytdl'}:
            if candidate.suffix == '.mp4':
                return candidate

    mp4 = workdir / 'source.mp4'
    if mp4.exists():
        return mp4
    raise RuntimeError('yt-dlp completed but source.mp4 was not found')


def verify_video_file(path: Path) -> dict:
    """Verify with ffprobe: file size > 0, video+audio streams, sane duration."""
    if not path.exists():
        raise RuntimeError(f'downloaded file missing: {path}')
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError(f'downloaded file is empty: {path}')
    cmd = [
        'ffprobe',
        '-v', 'error',
        '-print_format', 'json',
        '-show_streams',
        '-show_format',
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise RuntimeError('ffprobe not available for verification') from exc
    if proc.returncode != 0:
        raise RuntimeError(f'ffprobe failed: {(proc.stderr or "").strip()[:2000]}')
    try:
        payload = json.loads(proc.stdout or '{}')
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'ffprobe output invalid: {exc}') from exc
    streams = payload.get('streams') or []
    has_video = any((s.get('codec_type') == 'video') for s in streams)
    has_audio = any((s.get('codec_type') == 'audio') for s in streams)
    if not has_video:
        raise RuntimeError('ffprobe: no video stream found')
    if not has_audio:
        raise RuntimeError('ffprobe: no audio stream found')
    duration = None
    try:
        fmt = payload.get('format') or {}
        if fmt.get('duration') is not None:
            duration = float(fmt['duration'])
    except (TypeError, ValueError):
        duration = None
    if duration is not None and duration <= 0:
        raise RuntimeError(f'ffprobe: invalid duration {duration}')
    return {'size': size, 'has_video': has_video, 'has_audio': has_audio, 'duration': duration}


def _finalize_downloaded_file(workdir: Path, path: Path) -> Path:
    """Normalize output to original.mp4 (keep source.* compat)."""
    # Cleanup stale fragments/partials from earlier attempts.
    for stale in list(workdir.glob('*.part')) + list(workdir.glob('*.ytdl')):
        try:
            stale.unlink()
        except OSError:
            pass
    target = workdir / 'original.mp4'
    if path.resolve() == target.resolve():
        return target
    if path.suffix == '.mp4':
        # Keep original download; also ensure original.mp4 exists via rename/copy.
        if not target.exists():
            try:
                path.rename(target)
                return target
            except OSError:
                import shutil

                shutil.copy2(path, target)
                return target
        return path
    return path


def download_video(
    url: str,
    workdir: Path,
    *,
    job_id: str | None = None,
    bvid: str | None = None,
    cid: str | None = None,
) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    cookies_file = resolve_cookies_file()
    cookie_mode = 'cookies' if cookies_file else 'anonymous'
    dl_url = canonical_bilibili_url(url, bvid)
    ctx = f'job_id={job_id} stage=downloading bvid={bvid} cid={cid} mode={cookie_mode} yt-dlp={_yt_dlp_version()}'
    logger.info('download start %s url=%s', ctx, dl_url)

    # Anonymous-first: always try without cookies first.
    primary_format = settings.download_format
    fallback_format = settings.download_format_fallback
    last_exc: Exception | None = None

    for attempt, (fmt, label, use_cookies) in enumerate([
        (primary_format, 'anonymous', None),
        (fallback_format, 'anonymous-fallback', None),
    ]):
        # Skip fallback if identical to primary.
        if attempt == 1 and (not fallback_format or fallback_format.strip() == primary_format.strip()):
            continue
        try:
            logger.info('download attempt job_id=%s format=%s label=%s', job_id, fmt, label)
            path = _run_download(dl_url, workdir, None, fmt)
            logger.info('download %s success job_id=%s file=%s', label, job_id, path)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            logger.warning('download %s failed job_id=%s error=%s', label, job_id, msg[:2000])
            if _is_auth_error(msg):
                break  # go to cookie retry below
            if attempt == 0 and _is_retryable_network_error(msg):
                continue  # try fallback format
            # Non-retryable or fallback already failed.
            if attempt == 1:
                break
            continue

    if last_exc is not None and _is_auth_error(str(last_exc)):
        if cookies_file is not None:
            retry_ctx = f'job_id={job_id} stage=downloading bvid={bvid} cid={cid} mode=cookies-retry'
            logger.info('auth error detected, retrying with cookies %s', retry_ctx)
            path = _run_download(dl_url, workdir, cookies_file, primary_format)
            logger.info('download cookies-retry success %s file=%s', retry_ctx, path)
            last_exc = None
        else:
            raise RuntimeError(f'AUTH_REQUIRED: anonymous download blocked and no cookie file configured: {last_exc}') from last_exc

    if last_exc is not None:
        raise last_exc

    path = _finalize_downloaded_file(workdir, path)
    info = verify_video_file(path)
    logger.info(
        'download verified job_id=%s stage=downloading bvid=%s cid=%s file=%s size=%s duration=%s video=%s audio=%s',
        job_id, bvid, cid, path, info['size'], info.get('duration'), info['has_video'], info['has_audio'],
    )
    return path
