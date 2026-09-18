"""Client for the hosted DramaWave resolver API (Render)."""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request

from app.config import settings

logger = logging.getLogger('drama-source-api-client')

class DramaApiError(Exception):
    def __init__(self, code: str, message: str = '', status: int | None = None) -> None:
        super().__init__(f'{code}: {message}' if message else code)
        self.code = code
        self.message = message
        self.status = status

def _request(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    base = (settings.drama_source_api_base_url or '').rstrip('/')
    if not base:
        raise DramaApiError('DRAMA_API_UNAVAILABLE', 'DRAMAWAVE_API_BASE_URL not configured')
    timeout = max(30, settings.drama_source_api_timeout)
    max_retries = max(0, settings.drama_source_api_max_retries)
    url = base + path
    if params:
        url += '?' + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body or {}).encode() if method == 'POST' else None
    headers = {'Content-Type': 'application/json', 'User-Agent': 'dramawave-processor/2.0'}
    token = (settings.drama_source_api_token or '').strip()
    if token:
        headers['Authorization'] = 'Bearer ' + token
    last_err: DramaApiError | None = None
    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode('utf-8', 'replace'))
            logger.info('drama api %s %s ok latency=%.1fs', method, path, time.monotonic() - t0)
            return payload
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode('utf-8', 'replace') or '{}')
                err = detail.get('error') or {}
                code = err.get('code') or 'DRAMA_API_UNAVAILABLE'
                message = err.get('message') or f'HTTP {exc.code}'
            except (ValueError, OSError):
                code, message = 'DRAMA_API_UNAVAILABLE', f'HTTP {exc.code}'
            if exc.code == 401:
                raise DramaApiError('DRAMA_API_UNAUTHORIZED', message)
            if exc.code == 429:
                wait = 2 * (attempt + 1) + random.uniform(0, 1)
                time.sleep(wait)
                last_err = DramaApiError('DRAMA_API_UNAVAILABLE', 'rate limited')
                continue
            if exc.code == 404:
                raise DramaApiError(code if code.startswith(('SERIES', 'EPISODE', 'PLAYBACK')) else 'DRAMA_API_UNAVAILABLE', message)
            if 500 <= exc.code < 600 and attempt < max_retries:
                wait = 2 * (attempt + 1) + random.uniform(0, 1)
                time.sleep(wait)
                last_err = DramaApiError('DRAMA_API_UNAVAILABLE', message)
                continue
            raise DramaApiError(code, message)
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = DramaApiError('DRAMA_API_TIMEOUT', f'{path} {type(exc).__name__}')
            if attempt < max_retries:
                wait = 2 * (attempt + 1) + random.uniform(0, 1)
                time.sleep(wait)
                continue
            break
    raise last_err or DramaApiError('DRAMA_API_UNAVAILABLE', path)

def health() -> dict:
    req = urllib.request.Request(
        (settings.drama_source_api_base_url or '').rstrip('/') + '/health',
        headers={'User-Agent': 'dramawave-processor/2.0'})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode('utf-8', 'replace'))
        return {'online': True, 'latency_ms': int((time.monotonic() - t0) * 1000),
                'cold_start_possible': (time.monotonic() - t0) > 15, 'payload': payload}
    except Exception as exc:
        return {'online': False, 'latency_ms': None, 'cold_start_possible': True,
                'error': f'{type(exc).__name__}'}

def search(keyword: str, limit: int = 20) -> list[dict]:
    payload = _request('GET', '/v1/search-all', params={'q': keyword})
    return (payload.get('items') or [])[:max(1, limit)]

def get_series(series_id: str) -> dict:
    if not series_id.startswith('cw:'):
        series_id = f"cw:{series_id}"
    return _request('GET', f'/v1/series/{urllib.parse.quote(series_id)}')

def list_episodes(series_id: str) -> dict:
    if not series_id.startswith('cw:'):
        series_id = f"cw:{series_id}"
    return _request('GET', f'/v1/series/{urllib.parse.quote(series_id)}/episodes')

def resolve_playback(series_id: str, episode_number: int, quality: str = 'best') -> dict:
    if not series_id.startswith('cw:'):
        series_id = f"cw:{series_id}"
    t0 = time.monotonic()
    out = _request('GET', f'/v1/series/{urllib.parse.quote(series_id)}/episodes/{episode_number}/playback',
                   params={'provider': 'auto', 'quality': quality or 'best'})
    out['_resolve_seconds'] = time.monotonic() - t0
    return out
