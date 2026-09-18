"""Thin YouTube Data API v3 client: credentials refresh + service build.

All Google errors are mapped to stable YOUTUBE_* codes. Tokens are never logged.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.youtube.credentials import YouTubeCredentialError, decrypt_refresh_token

logger = logging.getLogger('youtube-client')


class YouTubeApiError(Exception):
    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(f'{code}: {message}' if message else code)
        self.code = code
        self.message = message


def _google_error_code(exc: Exception) -> str | None:
    """Extract YouTube API reason (quotaExceeded, etc.) without logging bodies."""
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            import json as _json

            try:
                payload = _json.loads(exc.content.decode('utf-8', 'replace') or '{}')
            except (ValueError, UnicodeDecodeError):
                return None
            errors = (payload.get('error') or {}).get('errors') or []
            if errors and isinstance(errors[0], dict):
                return errors[0].get('reason')
    except ImportError:
        pass
    return None


def _http_status(exc: Exception) -> int | None:
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            return exc.resp.status
    except ImportError:
        pass
    return getattr(exc, 'status', None)


def is_quota_error(exc: Exception) -> bool:
    return _google_error_code(exc) in ('quotaExceeded', 'dailyLimitExceeded',
                                       'rateLimitExceeded', 'userRateLimitExceeded')


def is_retryable_http(exc: Exception) -> bool:
    return _http_status(exc) in (500, 502, 503, 504)


def refresh_access_token(refresh_token: str) -> str:
    """Exchange refresh token for a fresh access token."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=(settings.google_client_id or '').strip(),
        client_secret=settings.google_client_secret or '',
    )
    try:
        creds.refresh(Request())
    except Exception as exc:
        msg = f'{type(exc).__name__}'
        if 'invalid_grant' in msg or 'invalid_grant' in str(exc):
            raise YouTubeApiError('YOUTUBE_REAUTH_REQUIRED', 'refresh token rejected') from exc
        raise YouTubeApiError('YOUTUBE_TOKEN_REFRESH_FAILED', type(exc).__name__) from exc
    if not creds.token:
        raise YouTubeApiError('YOUTUBE_TOKEN_REFRESH_FAILED', 'empty access token')
    return creds.token


def build_service(refresh_token_encrypted: str):
    """Build an authorized YouTube API service from the stored credential."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    try:
        refresh_token = decrypt_refresh_token(refresh_token_encrypted)
    except YouTubeCredentialError as exc:
        raise YouTubeApiError(exc.code, exc.message) from exc
    access_token = refresh_access_token(refresh_token)
    creds = Credentials(token=access_token)
    return build('youtube', 'v3', credentials=creds, cache_discovery=False)


def get_own_channel(service) -> dict:
    """channels.list(mine=true): id + title of the connected channel."""
    try:
        resp = service.channels().list(part='id,snippet', mine=True, maxResults=1).execute()
    except Exception as exc:
        raise YouTubeApiError('YOUTUBE_OAUTH_FAILED', type(exc).__name__) from exc
    items = (resp or {}).get('items') or []
    if not items:
        raise YouTubeApiError('YOUTUBE_OAUTH_FAILED', 'no channel found')
    item = items[0]
    return {
        'youtube_channel_id': str(item.get('id') or ''),
        'youtube_channel_title': ((item.get('snippet') or {}).get('title') or '').strip() or None,
    }
