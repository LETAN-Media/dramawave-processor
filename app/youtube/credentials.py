"""Refresh-token storage (Fernet-encrypted) + destination registry.

The refresh token is NEVER stored in plain text and NEVER logged.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import YouTubeDestination, utcnow

logger = logging.getLogger('youtube-creds')


class YouTubeCredentialError(Exception):
    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(f'{code}: {message}' if message else code)
        self.code = code
        self.message = message


def _fernet():
    from cryptography.fernet import Fernet, InvalidToken

    raw = (settings.app_encryption_key or '').strip()
    if not raw:
        raise YouTubeCredentialError('YOUTUBE_NOT_CONFIGURED', 'APP_ENCRYPTION_KEY missing')
    try:
        return Fernet(raw.encode()), InvalidToken
    except Exception as exc:
        raise YouTubeCredentialError('YOUTUBE_NOT_CONFIGURED', 'APP_ENCRYPTION_KEY invalid') from exc


def encrypt_refresh_token(refresh_token: str) -> str:
    f, _ = _fernet()
    return 'f1:' + f.encrypt(refresh_token.encode()).decode()


def decrypt_refresh_token(blob: str | None) -> str:
    f, InvalidToken = _fernet()
    if not blob:
        raise YouTubeCredentialError('YOUTUBE_REAUTH_REQUIRED', 'no stored credential')
    token = blob[3:] if blob.startswith('f1:') else blob
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise YouTubeCredentialError('YOUTUBE_REAUTH_REQUIRED', 'credential unreadable') from exc


def generate_encryption_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def save_destination(channel_id: str, channel_title: str | None, refresh_token: str | None,
                     scopes: str | None) -> YouTubeDestination:
    """Upsert destination. A missing refresh_token keeps the stored one."""
    with SessionLocal.begin() as db:
        dest = db.execute(select(YouTubeDestination).where(
            YouTubeDestination.youtube_channel_id == channel_id)).scalars().first()
        if dest is None:
            if not refresh_token:
                raise YouTubeCredentialError('YOUTUBE_OAUTH_FAILED', 'no refresh token issued')
            dest = YouTubeDestination(
                youtube_channel_id=channel_id, youtube_channel_title=channel_title,
                refresh_token_encrypted=encrypt_refresh_token(refresh_token),
                scope=scopes, is_active=True)
            db.add(dest)
            db.flush()
        else:
            if channel_title:
                dest.youtube_channel_title = channel_title
            if refresh_token:
                dest.refresh_token_encrypted = encrypt_refresh_token(refresh_token)
            elif not dest.refresh_token_encrypted:
                raise YouTubeCredentialError('YOUTUBE_OAUTH_FAILED', 'no refresh token issued')
            if scopes:
                dest.scope = scopes
            dest.is_active = True
            dest.last_error = None
        db.flush()
        db.expunge(dest)
        logger.info('youtube destination saved channel_id=%s', channel_id[:8] + '…')
        return dest


def list_destinations(active_only: bool = True) -> list[YouTubeDestination]:
    with SessionLocal() as db:
        stmt = select(YouTubeDestination).order_by(YouTubeDestination.created_at)
        if active_only:
            stmt = stmt.where(YouTubeDestination.is_active.is_(True))
        rows = list(db.execute(stmt).scalars().all())
        for r in rows:
            db.expunge(r)
        return rows


def get_destination(destination_id: str) -> YouTubeDestination | None:
    with SessionLocal() as db:
        row = db.get(YouTubeDestination, destination_id)
        if row is not None:
            db.expunge(row)
        return row


def disconnect_destination(destination_id: str) -> bool:
    """Disconnect: deactivate + drop the stored token (reconnect required)."""
    with SessionLocal.begin() as db:
        dest = db.get(YouTubeDestination, destination_id)
        if dest is None:
            return False
        dest.is_active = False
        dest.refresh_token_encrypted = None
        return True


def mark_upload(dest_id: str, error: str | None = None) -> None:
    with SessionLocal.begin() as db:
        dest = db.get(YouTubeDestination, dest_id)
        if dest is None:
            return
        if error:
            dest.last_error = error[:500]
        else:
            dest.last_upload_at = utcnow()
            dest.last_error = None


def public_info(dest: YouTubeDestination) -> dict:
    """Safe for API/UI responses: no tokens, no secrets."""
    return {
        'id': dest.id,
        'youtube_channel_id': dest.youtube_channel_id,
        'youtube_channel_title': dest.youtube_channel_title,
        'is_active': bool(dest.is_active),
        'last_upload_at': dest.last_upload_at.isoformat() if dest.last_upload_at else None,
        'last_error': dest.last_error,
        'reauth_required': not bool(dest.refresh_token_encrypted),
    }
