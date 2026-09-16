import http.cookiejar
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from app.bilibili.cookies import resolve_cookies_file
from app.config import settings


logger = logging.getLogger('bilibili-resolver')


BVID_RE = re.compile(r'(BV[0-9A-Za-z]{10})', re.IGNORECASE)
ALLOWED_HOST_SUFFIXES = ('bilibili.com', 'b23.tv')


@dataclass
class ResolvedVideo:
    source_url: str
    resolved_url: str
    bvid: str
    cid: str | None
    title: str | None
    author: str | None
    cover_url: str | None
    duration_seconds: float | None


def _validate_host(url: str) -> None:
    host = (urlparse(url).hostname or '').lower()
    if not any(host == suffix or host.endswith('.' + suffix) for suffix in ALLOWED_HOST_SUFFIXES):
        raise ValueError('Only bilibili.com and b23.tv URLs are supported')


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({'User-Agent': settings.bilibili_user_agent, 'Referer': 'https://www.bilibili.com/'})
    cookie_path = resolve_cookies_file()
    if cookie_path is not None:
        jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(jar)
        except Exception:
            pass
    return session


def resolve_redirect(url: str) -> str:
    _validate_host(url)
    if 'b23.tv' not in (urlparse(url).hostname or '').lower():
        return url
    response = _session().get(url, allow_redirects=True, timeout=settings.http_timeout_seconds, stream=True)
    response.raise_for_status()
    resolved = response.url
    _validate_host(resolved)
    return resolved


def _view_api(bvid: str) -> dict:
    response = _session().get(
        'https://api.bilibili.com/x/web-interface/view',
        params={'bvid': bvid},
        timeout=settings.http_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != 0 or not payload.get('data'):
        raise RuntimeError(f"Bilibili view API failed: code={payload.get('code')} message={payload.get('message')}")
    return payload['data']


def _yt_dlp_metadata(url: str) -> dict:
    from yt_dlp import YoutubeDL

    from app.bilibili.cookies import yt_dlp_cookie_args

    options = {
        'quiet': True,
        'skip_download': True,
        'noplaylist': True,
        'user_agent': settings.bilibili_ytdlp_user_agent,
        'referer': 'https://www.bilibili.com/',
    }
    options.update(yt_dlp_cookie_args())
    with YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


def resolve_bilibili_url(url: str) -> ResolvedVideo:
    resolved_url = resolve_redirect(url.strip())
    match = BVID_RE.search(resolved_url)
    bvid = match.group(1) if match else None

    data = None
    if bvid:
        try:
            data = _view_api(bvid)
        except Exception:
            data = None

    if data:
        pages = data.get('pages') or []
        cid = str(pages[0]['cid']) if pages and pages[0].get('cid') is not None else None
        owner = data.get('owner') or {}
        return ResolvedVideo(
            source_url=url,
            resolved_url=resolved_url,
            bvid=data.get('bvid') or bvid,
            cid=cid,
            title=data.get('title'),
            author=owner.get('name'),
            cover_url=data.get('pic'),
            duration_seconds=float(data['duration']) if data.get('duration') is not None else None,
        )

    info = _yt_dlp_metadata(resolved_url)
    info_url = info.get('webpage_url') or resolved_url
    info_bvid_match = BVID_RE.search(info_url) or BVID_RE.search(str(info.get('id', '')))
    if not info_bvid_match:
        raise RuntimeError('Unable to determine Bilibili BV id')
    bvid = info_bvid_match.group(1)
    cid = info.get('cid') or info.get('page_id')
    return ResolvedVideo(
        source_url=url,
        resolved_url=info_url,
        bvid=bvid,
        cid=str(cid) if cid is not None else None,
        title=info.get('title'),
        author=info.get('uploader') or info.get('channel'),
        cover_url=info.get('thumbnail'),
        duration_seconds=float(info['duration']) if info.get('duration') is not None else None,
    )
