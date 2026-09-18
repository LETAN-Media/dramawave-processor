"""YouTube integration tests. Google layers are faked; DB is real sqlite."""

import json

import pytest


@pytest.fixture()
def session_factory(tmp_path, monkeypatch):
    import app.models  # noqa: F401 - register tables
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base

    eng = create_engine(f'sqlite:///{tmp_path}/yt.db',
                        connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    for mod in ('app.youtube.credentials', 'app.youtube.service',
                'app.services.episodes', 'app.api.youtube', 'app.web.routes'):
        monkeypatch.setattr(f'{mod}.SessionLocal', Session)
    from cryptography.fernet import Fernet
    from app.config import settings
    monkeypatch.setattr(settings, 'app_encryption_key', Fernet.generate_key().decode())
    monkeypatch.setattr(settings, 'api_key', None)
    monkeypatch.setattr(settings, 'dashboard_username', '')
    monkeypatch.setattr(settings, 'dashboard_password', '')
    return Session


def _dest(Session, channel_id='UC123', title='Kênh Test', token='refresh-abc'):
    from app.youtube.credentials import save_destination

    return save_destination(channel_id, title, token, ' '.join([
        'https://www.googleapis.com/auth/youtube.upload',
        'https://www.googleapis.com/auth/youtube.readonly']))


_JOB_SEQ = [0]


def _job(Session, tmp_path, status='completed', stage='completed'):
    from app.models import Episode, EpisodeJob, Series

    _JOB_SEQ[0] += 1
    pid = f'P{_JOB_SEQ[0]}'
    with Session.begin() as db:
        series = Series(provider='dramawave', provider_series_id=pid, title='Phim Test',
                        episode_count=62)
        db.add(series)
        db.flush()
        ep = Episode(series_id=series.id, provider_episode_id='E1', episode_number=1,
                     title='Ep 1', duration=60.0, locked=False, status='ready')
        db.add(ep)
        db.flush()
        final = tmp_path / 'final.vi.mp4'
        final.write_bytes(b'\x00' * 1024)
        job = EpisodeJob(episode_id=ep.id, status=status, current_stage=stage,
                         progress=100, final_path=str(final), original_path=str(final))
        db.add(job)
        db.flush()
        jid = job.id
    return jid


# -- OAuth URL generation ----------------------------------------------------

def test_oauth_url_generation(monkeypatch):
    from app.config import settings
    from app.youtube import oauth as yt_oauth

    monkeypatch.setattr(settings, 'google_client_id', 'cid-123')
    monkeypatch.setattr(settings, 'google_client_secret', 'shh')
    monkeypatch.setattr(settings, 'youtube_redirect_uri', 'https://x.test/auth/youtube/callback')
    url = yt_oauth.build_authorize_url('state-1')
    assert url.startswith('https://accounts.google.com/o/oauth2/auth')
    assert 'access_type=offline' in url
    assert 'youtube.upload' in url
    assert 'youtube.readonly' in url
    assert 'state=state-1' in url
    assert 'shh' not in url and 'cid-123' in url  # secret never in URL


def test_oauth_requires_config(monkeypatch):
    from app.config import settings
    from app.youtube import oauth as yt_oauth

    monkeypatch.setattr(settings, 'google_client_id', '')
    with pytest.raises(Exception) as exc:
        yt_oauth.build_authorize_url('s')
    assert exc.value.code == 'YOUTUBE_NOT_CONFIGURED'


# -- OAuth callback + channel store -------------------------------------------

class _FakeChannels:
    def __init__(self, payload):
        self._payload = payload

    def list(self, **kwargs):
        assert kwargs.get('mine') is True

        class _Req:
            def execute(inner):
                return self._payload
        return _Req()


class _FakeService:
    def __init__(self, channel_payload=None):
        self._channel_payload = channel_payload

    def channels(self):
        return _FakeChannels(self._channel_payload)


def test_oauth_callback_stores_channel(session_factory, monkeypatch, tmp_path):
    import googleapiclient.discovery
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.youtube import oauth as yt_oauth

    monkeypatch.setattr(settings, 'google_client_id', 'cid')
    monkeypatch.setattr(settings, 'google_client_secret', 'shh')
    monkeypatch.setattr(settings, 'youtube_redirect_uri', 'https://x.test/auth/youtube/callback')
    monkeypatch.setattr(yt_oauth, 'exchange_code',
                        lambda code: {'access_token': 'at', 'refresh_token': 'rt-1',
                                      'expiry': None, 'scopes': 's'})
    monkeypatch.setattr(googleapiclient.discovery, 'build',
                        lambda *a, **k: _FakeService({'items': [
                            {'id': 'UC999', 'snippet': {'title': 'Kênh Mới'}}]}))
    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)
    import itsdangerous
    secret = (settings.dashboard_secret or settings.api_key or 'dramawave-studio').strip()
    signer = itsdangerous.URLSafeTimedSerializer(secret, salt='youtube-oauth')
    state = signer.dumps({'n': 'abc'})
    r = client.get('/auth/youtube/callback', params={'code': 'c', 'state': state},
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers['location'].startswith('/settings')
    from app.youtube.credentials import decrypt_refresh_token, list_destinations

    dests = list_destinations()
    assert len(dests) == 1 and dests[0].youtube_channel_id == 'UC999'
    assert decrypt_refresh_token(dests[0].refresh_token_encrypted) == 'rt-1'


def test_missing_refresh_token_keeps_old(session_factory):
    from app.youtube.credentials import decrypt_refresh_token, list_destinations, save_destination

    _dest(session_factory, token='rt-original')
    # Reconnect without a new refresh token: old one survives.
    dest = save_destination('UC123', 'Kênh Test', None, 's')
    assert decrypt_refresh_token(dest.refresh_token_encrypted) == 'rt-original'
    assert len(list_destinations()) == 1


def test_channel_info_retrieval():
    from app.youtube.client import get_own_channel

    info = get_own_channel(_FakeService({'items': [{'id': 'UC1', 'snippet': {'title': 'T'}}]}))
    assert info == {'youtube_channel_id': 'UC1', 'youtube_channel_title': 'T'}


# -- Encryption -----------------------------------------------------------------

def test_refresh_token_encryption(session_factory, monkeypatch):
    from app.config import settings
    from app.youtube.credentials import (
        YouTubeCredentialError,
        decrypt_refresh_token,
        encrypt_refresh_token,
    )

    blob = encrypt_refresh_token('rt-secret')
    assert 'rt-secret' not in blob
    assert decrypt_refresh_token(blob) == 'rt-secret'
    monkeypatch.setattr(settings, 'app_encryption_key', 'not-a-key')
    with pytest.raises(YouTubeCredentialError):
        decrypt_refresh_token(blob)


# -- Access token refresh ----------------------------------------------------------

def test_access_token_refresh_and_reauth(monkeypatch):
    import google.oauth2.credentials as gcreds
    from app.config import settings
    from app.youtube.client import YouTubeApiError, refresh_access_token

    monkeypatch.setattr(settings, 'google_client_id', 'cid')
    monkeypatch.setattr(settings, 'google_client_secret', 'shh')

    class GoodCreds:
        def __init__(self, **kwargs):
            self.token = None

        def refresh(self, request):
            self.token = 'access-1'

    monkeypatch.setattr(gcreds, 'Credentials', GoodCreds)
    assert refresh_access_token('rt') == 'access-1'

    class BadCreds:
        def __init__(self, **kwargs):
            self.token = None

        def refresh(self, request):
            raise Exception('invalid_grant: revoked')

    monkeypatch.setattr(gcreds, 'Credentials', BadCreds)
    with pytest.raises(YouTubeApiError) as exc:
        refresh_access_token('rt')
    assert exc.value.code == 'YOUTUBE_REAUTH_REQUIRED'


# -- Destinations --------------------------------------------------------------------

def test_multiple_channel_isolation(session_factory):
    from app.youtube.credentials import disconnect_destination, list_destinations

    _dest(session_factory, channel_id='UC_A', title='A')
    _dest(session_factory, channel_id='UC_B', title='B')
    assert {d.youtube_channel_id for d in list_destinations()} == {'UC_A', 'UC_B'}
    assert disconnect_destination('missing') is False
    assert disconnect_destination(next(d.id for d in list_destinations()
                                        if d.youtube_channel_id == 'UC_A')) is True
    remaining = list_destinations()
    assert [d.youtube_channel_id for d in remaining] == ['UC_B']


def test_channels_api_and_disconnect(session_factory):
    from fastapi.testclient import TestClient

    from app.main import app

    _dest(session_factory)
    client = TestClient(app, raise_server_exceptions=False)
    items = client.get('/api/youtube/channels').json()['items']
    assert len(items) == 1 and items[0]['reauth_required'] is False
    assert 'refresh_token' not in json.dumps(items)
    did = items[0]['id']
    r = client.delete(f'/api/youtube/channels/{did}')
    assert r.status_code == 200
    assert client.get('/api/youtube/channels').json()['items'] == []
    assert len(client.get('/api/youtube/channels?active_only=false').json()['items']) == 1
    assert client.delete('/api/youtube/channels/nope').status_code == 404


# -- Upload -----------------------------------------------------------------------------

class _FakeInsertRequest:
    def __init__(self, service, fail_first=0, quota=False, progress_cb=None):
        self.service = service
        self.fail_first = fail_first
        self.quota = quota
        self.resumable_progress = 0

    def next_chunk(self):
        from googleapiclient.errors import HttpError

        self.service.total_insert_calls += 1
        if self.quota:
            raise HttpError(_resp(403), b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}')
        if self.service.total_insert_calls <= self.fail_first:
            raise HttpError(_resp(500), b'{"error":{"errors":[{"reason":"backendError"}]}}')
        self.resumable_progress = 100

        class _Status:
            def progress(inner):
                return 1.0
        return _Status(), {'id': 'VID123'}


def _resp(status):
    from httplib2 import Response

    return Response({'status': str(status), 'reason': 'x'})


class _FakeVideos:
    def __init__(self, service):
        self.service = service
        self.last_body = None

    def insert(self, part=None, body=None, media_body=None):
        self.last_body = body
        return _FakeInsertRequest(self.service, fail_first=self.service.fail_first,
                                  quota=self.service.quota)

    def list(self, **kwargs):
        class _Req:
            def execute(inner):
                return {'items': [{'status': {'uploadStatus': 'processed'}}]}
        return _Req()


class _FakeUploadService:
    fail_first = 0
    quota = False

    def __init__(self, **kwargs):
        self.total_insert_calls = 0
        self._videos = _FakeVideos(self)

    def videos(self):
        return self._videos


def test_resumable_upload_success_and_privacy(session_factory, tmp_path, monkeypatch):
    import app.youtube.service as svc
    from app.youtube import uploader as up

    monkeypatch.setattr(up, 'wait_processing', lambda service, vid: 'processed')
    monkeypatch.setattr(svc, 'build_service', lambda blob: _FakeUploadService())
    monkeypatch.setattr(svc, 'validate_final_for_upload',
                        lambda final_path, original_path: __import__('pathlib').Path(final_path))
    monkeypatch.setattr(svc, 'build_publication_metadata',
                        lambda job_id, mode='auto': {'title': 'T', 'description': 'D',
                                                     'tags': ['a']})
    fake = _FakeUploadService()
    monkeypatch.setattr(svc, 'build_service', lambda blob: fake)
    dest = _dest(session_factory)
    jid = _job(session_factory, tmp_path)
    pub = svc.get_or_create_publication(jid, dest.id, 'unlisted')
    out = svc.run_upload(pub.id)
    assert out['youtube_video_id'] == 'VID123'
    assert out['youtube_url'] == 'https://youtu.be/VID123'
    assert fake.videos().last_body['status']['privacyStatus'] == 'unlisted'
    from app.models import EpisodeJob, YouTubePublication

    with session_factory() as db:
        assert db.get(EpisodeJob, jid).status == 'published'
        row = db.get(YouTubePublication, pub.id)
        assert row.upload_status == 'published' and row.upload_progress == 100


def test_upload_retry_then_success(session_factory, tmp_path, monkeypatch):
    import app.youtube.service as svc
    from app.youtube import uploader as up

    monkeypatch.setattr(up, '_sleep_backoff', lambda attempt: None)
    monkeypatch.setattr(up, 'wait_processing', lambda service, vid: 'processed')
    monkeypatch.setattr(svc, 'build_service', lambda blob: _FakeUploadService())
    monkeypatch.setattr(svc, 'validate_final_for_upload',
                        lambda final_path, original_path: __import__('pathlib').Path(final_path))
    _FakeUploadService.fail_first = 2
    try:
        monkeypatch.setattr(svc, 'build_publication_metadata',
                            lambda job_id, mode='auto': {'title': 'T', 'description': 'D',
                                                         'tags': []})
        dest = _dest(session_factory)
        jid = _job(session_factory, tmp_path)
        pub = svc.get_or_create_publication(jid, dest.id, 'public')
        out = svc.run_upload(pub.id)
        assert out['youtube_video_id'] == 'VID123'
    finally:
        _FakeUploadService.fail_first = 0


def test_quota_no_retry(session_factory, tmp_path, monkeypatch):
    import app.youtube.service as svc
    from app.youtube import uploader as up

    def _no_sleep(attempt):
        raise AssertionError('must not sleep')

    monkeypatch.setattr(up, '_sleep_backoff', _no_sleep)
    monkeypatch.setattr(svc, 'build_service', lambda blob: _FakeUploadService())
    monkeypatch.setattr(svc, 'validate_final_for_upload',
                        lambda final_path, original_path: __import__('pathlib').Path(final_path))
    _FakeUploadService.quota = True
    try:
        monkeypatch.setattr(svc, 'build_publication_metadata',
                            lambda job_id, mode='auto': {'title': 'T', 'description': 'D',
                                                         'tags': []})
        dest = _dest(session_factory)
        jid = _job(session_factory, tmp_path)
        pub = svc.get_or_create_publication(jid, dest.id, 'public')
        out = svc.run_upload(pub.id)
        assert out['error_code'] == 'YOUTUBE_QUOTA_EXCEEDED'
        from app.models import YouTubePublication

        with session_factory() as db:
            assert db.get(YouTubePublication, pub.id).upload_status == 'failed'
    finally:
        _FakeUploadService.quota = False


def test_final_gate_real_ffprobe(tmp_path):
    import shutil

    import app.youtube.service as svc

    if shutil.which('ffprobe') is None or shutil.which('ffmpeg') is None:
        pytest.skip('ffmpeg/ffprobe missing')
    import subprocess

    mp4 = tmp_path / 'real.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=duration=1:size=128x128:rate=10',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                    '-shortest', str(mp4)], check=True, timeout=120)
    out = svc.validate_final_for_upload(str(mp4), str(mp4))
    assert out == mp4
    bad = tmp_path / 'empty.mp4'
    bad.write_bytes(b'\x00' * 16)
    with pytest.raises(Exception):
        svc.validate_final_for_upload(str(bad), str(mp4))


# -- Metadata ------------------------------------------------------------------------------

def test_metadata_template_and_episode_guard():
    from app.youtube import metadata as md

    m = md.template_metadata('Cô Vợ Ngọt Ngào', 2, 62)
    assert m['title'] == 'Cô Vợ Ngọt Ngào - Tập 2'
    assert 'M3U8' not in m['description'] and 'http' not in m['description']
    # AI title with wrong episode number is replaced by the template title.
    out = md._sanitize({'title': 'Phim Hay - Tập 9', 'description': 'D', 'tags': ['x']},
                       2, md.template_metadata('Phim Hay', 2, 62))
    assert out['title'] == 'Phim Hay - Tập 2'
    assert len(out['title']) <= 100


def test_metadata_fallback_on_ai_fail(monkeypatch):
    import app.translation.providers_toolnet as pt
    from app.youtube import metadata as md

    def boom(messages, model, timeout, max_tokens):
        raise RuntimeError('TRANSLATION_HTTP: down')

    monkeypatch.setattr(pt, '_post_chat_stream', boom)
    m = md.ai_metadata('Phim Test', 1, 10)
    assert m['title'] == 'Phim Test - Tập 1'


def test_metadata_ai_success(monkeypatch):
    import app.translation.providers_toolnet as pt
    from app.youtube import metadata as md

    def ok(messages, model, timeout, max_tokens):
        return json.dumps({'title': 'Phim Test - Tập 3 cực hay',
                           'description': 'Mô tả hay', 'tags': ['phim', 'hay']})

    monkeypatch.setattr(pt, '_post_chat_stream', ok)
    m = md.ai_metadata('Phim Test', 3, 10)
    assert m['title'] == 'Phim Test - Tập 3 cực hay'
    assert m['tags'] == ['phim', 'hay']


# -- Publications -----------------------------------------------------------------------------

def test_duplicate_protection(session_factory, tmp_path):
    import app.youtube.service as svc

    dest = _dest(session_factory)
    jid = _job(session_factory, tmp_path)
    a = svc.get_or_create_publication(jid, dest.id, 'public')
    b = svc.get_or_create_publication(jid, dest.id, 'public')
    assert a.id == b.id
    from sqlalchemy.exc import IntegrityError
    from app.models import YouTubePublication

    with session_factory.begin() as db:
        db.add(YouTubePublication(job_id=jid, destination_id=dest.id))
        with pytest.raises(IntegrityError):
            db.flush()


def test_manual_retry_resets_only_youtube(session_factory, tmp_path):
    import app.youtube.service as svc
    from app.models import EpisodeJob

    dest = _dest(session_factory)
    jid = _job(session_factory, tmp_path)
    pub = svc.get_or_create_publication(jid, dest.id, 'public')
    with session_factory.begin() as db:
        row = db.get(type(pub), pub.id)
        row.upload_status = 'failed'
        row.error_code = 'YOUTUBE_UPLOAD_FAILED'
        row.upload_attempts = 2
    out = svc.retry_publication(jid)
    assert out.upload_status == 'queued' and out.error_code is None
    with session_factory() as db:
        job = db.get(EpisodeJob, jid)
        assert job.status == 'ready_to_upload'
        assert job.final_path is not None  # render artifacts untouched
        assert job.original_path is not None


def test_worker_restart_recovery_no_reupload(session_factory, tmp_path, monkeypatch):
    import app.youtube.service as svc
    from app.youtube import uploader as up

    calls = {'insert': 0}
    monkeypatch.setattr(svc, 'build_service', lambda blob: _FakeUploadService())
    monkeypatch.setattr(svc, 'validate_final_for_upload',
                        lambda final_path, original_path: __import__('pathlib').Path(final_path))
    monkeypatch.setattr(up, 'wait_processing', lambda service, vid: 'processed')

    orig_insert = _FakeVideos.insert

    def counting_insert(self, **kwargs):
        calls['insert'] += 1
        return orig_insert(self, **kwargs)

    monkeypatch.setattr(_FakeVideos, 'insert', counting_insert)
    dest = _dest(session_factory)
    jid = _job(session_factory, tmp_path)
    pub = svc.get_or_create_publication(jid, dest.id, 'public')
    # Simulate crash after insert commit but before publish: video id stored.
    from app.models import YouTubePublication

    with session_factory.begin() as db:
        row = db.get(YouTubePublication, pub.id)
        row.youtube_video_id = 'VID123'
        row.youtube_url = 'https://youtu.be/VID123'
        row.upload_status = 'uploading'
    out = svc.run_upload(pub.id)
    assert out['youtube_video_id'] == 'VID123'
    assert calls['insert'] == 0  # no duplicate upload
    with session_factory() as db:
        assert db.get(YouTubePublication, pub.id).upload_status == 'published'


def test_enqueue_youtube_validation(session_factory):
    import app.services.episodes as epmod
    from app.models import Series
    from app.sources.base import SourceError

    with session_factory.begin() as db:
        db.add(Series(id='s9', provider='dramawave', provider_series_id='P9', title='T'))
    with pytest.raises(SourceError) as exc:
        epmod.enqueue_episodes('s9', youtube_enabled=True, youtube_destination_id='nope')
    assert exc.value.code == 'YOUTUBE_DESTINATION_NOT_FOUND'
    dest = _dest(session_factory)
    out = epmod.enqueue_episodes('s9', youtube_enabled=True,
                                 youtube_destination_id=dest.id, youtube_privacy='unlisted')
    assert out['enqueued'] == []  # no episodes, but validation passed


def test_auto_disabled_no_publication(session_factory, tmp_path):
    import app.youtube.service as svc
    from app.models import YouTubePublication
    from sqlalchemy import select

    jid = _job(session_factory, tmp_path)  # youtube_enabled defaults False
    with session_factory() as db:
        pubs = list(db.execute(select(YouTubePublication).where(
            YouTubePublication.job_id == jid)).scalars().all())
    assert pubs == []
    assert svc.claim_upload() is None


def test_upload_concurrency_and_cancel(session_factory, tmp_path, monkeypatch):
    import app.youtube.service as svc
    from app.config import settings
    from app.models import YouTubePublication

    monkeypatch.setattr(settings, 'youtube_upload_concurrency', 1)
    dest = _dest(session_factory)
    j1 = _job(session_factory, tmp_path)
    j2 = _job(session_factory, tmp_path)
    p1 = svc.get_or_create_publication(j1, dest.id, 'public')
    p2 = svc.get_or_create_publication(j2, dest.id, 'public')
    with session_factory.begin() as db:
        db.get(YouTubePublication, p1.id).upload_status = 'uploading'
    assert svc.claim_upload() is None  # slot busy
    with session_factory.begin() as db:
        db.get(YouTubePublication, p1.id).upload_status = 'queued'
    assert svc.claim_upload() == p1.id
    assert svc.cancel_publication(j2) == 1
    with session_factory() as db:
        assert db.get(YouTubePublication, p2.id).upload_status == 'cancelled'


def test_web_youtube_endpoints(session_factory, tmp_path):
    import app.youtube.service as svc
    from fastapi.testclient import TestClient

    from app.main import app

    dest = _dest(session_factory)
    jid = _job(session_factory, tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    items = client.get('/web/api/youtube/channels').json()['items']
    assert len(items) == 1 and items[0]['youtube_channel_id'] == 'UC123'
    cfg = client.get('/web/api/youtube/config').json()
    assert 'callback_url' in cfg and 'default_privacy' in cfg
    r = client.post(f'/web/api/jobs/{jid}/youtube/upload',
                    json={'destination_id': dest.id, 'privacy': 'public'})
    assert r.status_code == 200
    st = client.get(f'/web/api/jobs/{jid}/youtube').json()
    assert st['publications'][0]['upload_status'] == 'queued'
    detail = client.get(f'/web/api/jobs/{jid}').json()
    assert detail['youtube'] and detail['youtube'][0]['upload_status'] == 'queued'
    assert client.get('/jobs/' + jid).status_code == 200
