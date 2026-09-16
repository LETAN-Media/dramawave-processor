"""Regression tests for BILIBILI_COOKIES_FILE handling.

Unset / "" / "   " must all mean NO COOKIE (None), never Path(".").
A directory must never be treated as a cookie file.
A real file must be used.
"""

from pathlib import Path

from app.bilibili.cookies import resolve_cookies_file, yt_dlp_cookie_args
from app.config import Settings, settings


def _patch_settings_cookies(monkeypatch, value):
    """Patch the global settings singleton's cookie value."""
    monkeypatch.setattr(settings, 'bilibili_cookies_file', value, raising=False)


def test_empty_cookie_env_does_not_become_current_directory():
    s = Settings(_env_parse_none=True, bilibili_cookies_file='')
    assert s.bilibili_cookies_file is None

    s2 = Settings(_env_parse_none=True, bilibili_cookies_file='   ')
    assert s2.bilibili_cookies_file is None

    # Direct validator behavior: empty string must not become Path(".")
    assert s.bilibili_cookies_file != Path('.')


def test_missing_cookie_env_is_none():
    s = Settings(_env_parse_none=True, bilibili_cookies_file=None)
    assert s.bilibili_cookies_file is None


def test_valid_cookie_file_is_used(tmp_path, monkeypatch):
    cookie = tmp_path / 'cookies.txt'
    cookie.write_text('# Netscape HTTP Cookie File\n', encoding='utf-8')
    _patch_settings_cookies(monkeypatch, cookie)
    resolved = resolve_cookies_file()
    assert resolved == cookie
    args = yt_dlp_cookie_args()
    assert args == {'cookiefile': str(cookie)}


def test_cookie_directory_is_rejected(tmp_path, monkeypatch):
    _patch_settings_cookies(monkeypatch, tmp_path)
    assert resolve_cookies_file() is None
    assert yt_dlp_cookie_args() == {}


def test_empty_string_resolves_to_none(monkeypatch):
    # Defensive: even if settings somehow holds Path("."), resolve -> None.
    _patch_settings_cookies(monkeypatch, Path('.'))
    assert resolve_cookies_file() is None
    assert yt_dlp_cookie_args() == {}


def test_missing_file_resolves_to_none(tmp_path, monkeypatch):
    missing = tmp_path / 'does-not-exist.txt'
    _patch_settings_cookies(monkeypatch, missing)
    assert resolve_cookies_file() is None
    assert yt_dlp_cookie_args() == {}
