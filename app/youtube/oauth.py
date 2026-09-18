"""Google OAuth 2.0 flow for YouTube channel connect.

Minimal scopes: upload + readonly (to read the connected channel's id/title).
access_type=offline so Google issues a refresh token. If Google omits the
refresh token (account authorized before), the caller must keep the stored one.
"""

from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger('youtube-oauth')

SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.readonly',
]

AUTH_URI = 'https://accounts.google.com/o/oauth2/auth'
TOKEN_URI = 'https://oauth2.googleapis.com/token'


class YouTubeOAuthError(Exception):
    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(f'{code}: {message}' if message else code)
        self.code = code
        self.message = message


def _client_config() -> dict:
    client_id = (settings.google_client_id or '').strip()
    client_secret = settings.google_client_secret or ''
    if not client_id or not client_secret:
        raise YouTubeOAuthError('YOUTUBE_NOT_CONFIGURED',
                                'GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET missing')
    return {
        'web': {
            'client_id': client_id,
            'client_secret': client_secret,
            'auth_uri': AUTH_URI,
            'token_uri': TOKEN_URI,
        }
    }


def redirect_uri() -> str:
    uri = (settings.youtube_redirect_uri or '').strip()
    if not uri:
        raise YouTubeOAuthError('YOUTUBE_NOT_CONFIGURED', 'YOUTUBE_REDIRECT_URI missing')
    return uri


def _flow():
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=redirect_uri())


def build_authorize_url(state: str) -> str:
    """Authorization URL. Secrets never appear in the URL."""
    flow = _flow()
    url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='false',
        state=state,
        prompt='select_account',
    )
    return url


def exchange_code(code: str) -> dict:
    """Exchange authorization code for tokens. Returns token dict (no logging)."""
    flow = _flow()
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        raise YouTubeOAuthError('YOUTUBE_OAUTH_FAILED', type(exc).__name__) from exc
    creds = flow.credentials
    granted = ' '.join(sorted(set(creds.scopes or SCOPES)))
    return {
        'access_token': creds.token,
        'refresh_token': creds.refresh_token,  # may be None if authorized before
        'expiry': creds.expiry.isoformat() if creds.expiry else None,
        'scopes': granted,
    }
