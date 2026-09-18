"""Dashboard auth for DramaWave Studio web UI.

Simple single-user login (DASHBOARD_USERNAME / DASHBOARD_PASSWORD).
Session is an HMAC-signed, timestamped cookie (itsdangerous), HttpOnly,
SameSite=Lax, Secure when served over HTTPS (set in route).

When no credentials are configured the UI is open (single-user VPS default).
API routes under /v1/* keep their existing X-API-Key behavior untouched.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from app.config import settings

logger = logging.getLogger('studio-auth')

SESSION_COOKIE = 'dw_session'


def dashboard_auth_enabled() -> bool:
    return bool((settings.dashboard_username or '').strip()
                and (settings.dashboard_password or '').strip())


def _signing_secret() -> str:
    secret = (settings.dashboard_secret or '').strip() or (settings.api_key or '').strip()
    if not secret:
        logger.warning('dashboard auth enabled without DASHBOARD_SECRET/API_KEY; using built-in fallback')
        secret = 'dramawave-studio-dev-secret'
    return secret


def _serializer():
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(_signing_secret(), salt='dw-studio-session')


def create_session_token(username: str) -> str:
    return _serializer().dumps({'u': username})


def verify_session_username(token: str | None) -> str | None:
    if not token:
        return None
    try:
        max_age = max(1, int(settings.dashboard_session_hours)) * 3600
        data = _serializer().loads(token, max_age=max_age)
    except Exception:
        return None
    username = (data or {}).get('u')
    if not username or not isinstance(username, str):
        return None
    if dashboard_auth_enabled() and username != (settings.dashboard_username or '').strip():
        return None
    return username


def check_credentials(username: str, password: str) -> bool:
    expected_user = (settings.dashboard_username or '').strip()
    expected_pass = settings.dashboard_password or ''
    if not expected_user or not expected_pass:
        return False
    return (secrets.compare_digest(username or '', expected_user)
            and secrets.compare_digest(password or '', expected_pass))


def current_web_user(request: Request) -> str | None:
    """Return logged-in username, 'anonymous' when auth disabled, else None."""
    if not dashboard_auth_enabled():
        return 'anonymous'
    return verify_session_username(request.cookies.get(SESSION_COOKIE))


def require_web_user_api(request: Request) -> str:
    user = current_web_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail='Dashboard login required')
    return user


def login_redirect(request: Request) -> RedirectResponse:
    nxt = str(request.url.path)
    if request.url.query:
        nxt += '?' + request.url.query
    return RedirectResponse(url=f'/login?next={nxt}', status_code=307)
