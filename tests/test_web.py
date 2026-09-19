"""DramaWave Studio web UI tests. No network (resolver client mocked)."""

import pytest
from fastapi.testclient import TestClient


def _test_session(tmp_path, name='web.db'):
    import app.models  # noqa: F401 - register tables
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base

    eng = create_engine(f'sqlite:///{tmp_path}/{name}',
                        connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    Session = _test_session(tmp_path, 'shared.db')
    monkeypatch.setattr('app.web.routes.SessionLocal', Session)
    monkeypatch.setattr('app.services.episodes.SessionLocal', Session)
    monkeypatch.setattr('app.api.routes.SessionLocal', Session)
    from app.config import settings
    monkeypatch.setattr(settings, 'dashboard_username', '')
    monkeypatch.setattr(settings, 'dashboard_password', '')
    monkeypatch.setattr(settings, 'api_key', None)
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _seed_series(monkeypatch, Session, pid='PID1', episodes=5, locked_from=4):
    from app.sources.base import EpisodeInfo, SeriesInfo
    import app.services.episodes as epmod

    series = epmod.get_or_create_series(
        SeriesInfo(provider='dramawave', provider_series_id=pid, title='Test Drama',
                   cover_url='http://x/poster.jpg', episode_count=episodes), 'http://x')
    infos = [EpisodeInfo(provider_episode_id=f'E{i}', episode_number=i, title=f'Ep {i}',
                         duration=60.0, locked=(i >= locked_from)) for i in range(1, episodes + 1)]
    epmod.sync_episodes(series, infos)
    return series


def test_home_page(client):
    r = client.get('/')
    assert r.status_code == 200
    assert 'DramaWave Studio' in r.text
    assert 'name="viewport"' in r.text
    assert 'class="bottomnav"' in r.text
    assert 'Tìm phim' in r.text


def test_series_page_has_process_form(client):
    r = client.get('/series/PID1')
    assert r.status_code == 200
    assert 'id="process-form"' in r.text
    assert 'Bắt đầu xử lý' in r.text
    for q in ('1080p', '720p', '540p', '480p', 'best'):
        assert q in r.text
    assert 'HoaiMy' in r.text and 'NamMinh' in r.text
    assert 'XIANXIA' in r.text


def test_inline_init_runs_after_app_js(client):
    """Inline StudioSeries.init must execute AFTER app.js defines it.

    Regression: <script src defer> made inline init throw ReferenceError,
    leaving every page stuck on loading forever with no error shown.
    """
    r = client.get('/series/PID1')
    assert r.status_code == 200
    html = r.text
    print('HTML CONTENT:', html[:500])
    js_tag = html.find('<script src="/static/app.js')
    assert js_tag != -1
    js_close = html.find('>', js_tag)
    assert 'defer' not in html[js_tag:js_close], 'app.js must not be deferred'
    init_call = html.find('StudioSeries.init(')
    assert init_call != -1
    assert js_tag < init_call, 'app.js must load before inline init call'
    r2 = client.get('/static/app.js')
    assert r2.status_code == 200
    assert 'Không tải được thông tin phim' in r2.text


def test_v1_jobs_list_and_filter(client, tmp_path, monkeypatch):
    import app.web.routes as webmod
    from app.models import Episode, EpisodeJob

    with webmod.SessionLocal.begin() as db:
        db.add(Episode(id='e9', series_id='s9', provider_episode_id='E9',
                       episode_number=9, status='ready'))
        db.add(Episode(id='e8', series_id='s9', provider_episode_id='E8',
                       episode_number=8, status='failed'))
        db.add(EpisodeJob(id='j9', episode_id='e9', status='completed',
                          current_stage='completed', progress=100))
        db.add(EpisodeJob(id='j8', episode_id='e8', status='failed',
                          current_stage='failed', progress=10))
    r = client.get('/v1/jobs?status=all')
    assert r.status_code == 200
    body = r.json()
    assert body['total'] == len(body['items']) == 2
    r = client.get('/v1/jobs?status=completed')
    assert [j['id'] for j in r.json()['items']] == ['j9']
    r = client.get('/v1/jobs?status=failed')
    assert [j['id'] for j in r.json()['items']] == ['j8']
    r = client.get('/v1/jobs?status=bogus')
    assert r.status_code == 422


def test_static_cache_busting_and_mobile_nav(client):
    r = client.get('/')
    assert r.status_code == 200
    assert '/static/app.js?v=' in r.text
    assert '/static/style.css?v=' in r.text
    css = client.get('/static/style.css')
    assert css.status_code == 200
    assert '@media (max-width: 759px)' in css.text
    assert '.topnav { display: none; }' in css.text


def test_api_fetch_wrapper_robust(client):
    r = client.get('/static/app.js')
    assert r.status_code == 200
    js = r.text
    assert "credentials: 'same-origin'" in js
    assert 'AbortController' in js and 'API_TIMEOUT_MS = 90000' in js
    assert 'unhandledrejection' in js
    assert 'apiErrorBox' in js
    assert 'Thử lại' in js


def test_jobs_and_settings_pages(client):
    assert client.get('/jobs').status_code == 200
    r = client.get('/settings')
    assert r.status_code == 200
    assert 'id="provider-status"' in r.text


def test_job_page_404_unknown(client):
    assert client.get('/jobs/does-not-exist').status_code == 404


def test_web_search(client, monkeypatch):
    monkeypatch.setattr('app.web.routes.api.search',
                        lambda q, limit=20: [{'series_id': 'S1', 'title': 'Alpha Test',
                                              'cover_url': None, 'episode_count': 40}])
    r = client.get('/web/api/search?q=Alpha')
    assert r.status_code == 200
    assert r.json()['items'][0]['series_id'] == 'S1'


def test_web_search_empty_and_error(client, monkeypatch):
    monkeypatch.setattr('app.web.routes.api.search', lambda q, limit=20: [])
    assert client.get('/web/api/search?q=zzz').json() == {'items': []}

    from app.clients.drama_source_api import DramaApiError

    def boom(q, limit=20):
        raise DramaApiError('DRAMA_API_TIMEOUT', 'slow')
    monkeypatch.setattr('app.web.routes.api.search', boom)
    r = client.get('/web/api/search?q=zzz')
    assert r.status_code == 502
    assert 'waking up' in r.json()['detail']


def test_series_detail_locked_flags(client, monkeypatch):
    monkeypatch.setattr('app.web.routes.api.get_series',
                        lambda pid: {'canonical_series_id': pid, 'title': 'T', 'description': '',
                                     'cover_url': None, 'episode_count': 5, 'sources': [{'provider': 'dramawave', 'provider_series_id': pid}]})
    monkeypatch.setattr('app.web.routes.api.list_episodes',
                        lambda pid: {'canonical_series_id': pid, 'total': 5, 'episodes': [
                            {'episode_number': i, 'title': f'Ep {i}',
                             'duration': 60.0, 'status': 'locked' if i >= 4 else 'free', 'sources': [{'provider': 'dramawave', 'provider_episode_id': f'E{i}', 'locked': i >= 4, 'status': 'locked' if i >= 4 else 'free'}]} for i in range(1, 6)]})
    r = client.get('/web/api/series/PID1')
    assert r.status_code == 200
    data = r.json()
    assert data['total'] == 5 and data['unlocked'] == 3 and data['locked'] == 2
    by_num = {e['number']: e for e in data['episodes']}
    assert by_num[1]['locked'] is False and by_num[4]['locked'] is True


def test_process_rejects_locked_only(client, monkeypatch):
    monkeypatch.setattr('app.web.routes.api.get_series',
                        lambda pid: {'canonical_series_id': pid, 'title': 'T', 'description': '',
                                     'cover_url': None, 'episode_count': 5, 'sources': [{'provider': 'dramawave', 'provider_series_id': pid}]})
    monkeypatch.setattr('app.web.routes.api.list_episodes',
                        lambda pid: {'canonical_series_id': pid, 'total': 5, 'episodes': [
                            {'episode_number': i, 'title': f'Ep {i}',
                             'duration': 60.0, 'status': 'locked' if i >= 4 else 'free', 'sources': [{'provider': 'dramawave', 'provider_episode_id': f'E{i}', 'locked': i >= 4, 'status': 'locked' if i >= 4 else 'free'}]} for i in range(1, 6)]})
    r = client.post('/web/api/series/PID1/process', json={
        'from_episode': 4, 'to_episode': 5, 'quality': '1080p',
        'voice': 'vi-VN-HoaiMyNeural', 'translation_style': 'AUTO'})
    assert r.status_code == 409


def test_process_validation(client, monkeypatch):
    monkeypatch.setattr('app.web.routes.api.get_series',
                        lambda pid: {'canonical_series_id': pid, 'title': 'T', 'description': '',
                                     'cover_url': None, 'episode_count': 5, 'sources': [{'provider': 'dramawave', 'provider_series_id': pid}]})
    monkeypatch.setattr('app.web.routes.api.list_episodes',
                        lambda pid: {'canonical_series_id': pid, 'total': 5, 'episodes': [
                            {'episode_number': i, 'title': f'Ep {i}',
                             'duration': 60.0, 'status': 'free', 'sources': [{'provider': 'dramawave', 'provider_episode_id': f'E{i}', 'locked': False, 'status': 'free'}]} for i in range(1, 6)]})
    base = {'from_episode': 1, 'to_episode': 2, 'quality': '1080p',
            'voice': 'vi-VN-HoaiMyNeural', 'translation_style': 'AUTO'}
    bad_q = dict(base, quality='4k')
    assert client.post('/web/api/series/PID1/process', json=bad_q).status_code == 422
    bad_v = dict(base, voice='robot')
    assert client.post('/web/api/series/PID1/process', json=bad_v).status_code == 422
    bad_r = dict(base, from_episode=3, to_episode=2)
    assert client.post('/web/api/series/PID1/process', json=bad_r).status_code == 422


def test_process_enqueues_and_style_saved(client, monkeypatch):
    monkeypatch.setattr('app.web.routes.api.get_series',
                        lambda pid: {'canonical_series_id': pid, 'title': 'T', 'description': '',
                                     'cover_url': None, 'episode_count': 5, 'sources': [{'provider': 'dramawave', 'provider_series_id': pid}]})
    monkeypatch.setattr('app.web.routes.api.list_episodes',
                        lambda pid: {'canonical_series_id': pid, 'total': 5, 'episodes': [
                            {'episode_number': i, 'title': f'Ep {i}',
                             'duration': 60.0, 'status': 'free', 'sources': [{'provider': 'dramawave', 'provider_episode_id': f'E{i}', 'locked': False, 'status': 'free'}]} for i in range(1, 6)]})
    r = client.post('/web/api/series/PID1/process', json={
        'from_episode': 1, 'to_episode': 2, 'quality': '720p',
        'voice': 'vi-VN-NamMinhNeural', 'translation_style': 'XIANXIA'})
    assert r.status_code == 200
    assert len(r.json()['jobs']) == 2
    from app.models import Series
    from sqlalchemy import select
    import app.web.routes as webmod

    with webmod.SessionLocal() as db:
        row = db.execute(select(Series).where(Series.provider_series_id == 'PID1')).scalars().first()
        assert row.translation_style == 'XIANXIA'


def test_jobs_polling_and_detail(client, tmp_path, monkeypatch):
    import app.web.routes as webmod

    series = _seed_series(monkeypatch, webmod.SessionLocal)
    from app.models import Episode, EpisodeJob
    from sqlalchemy import select

    with webmod.SessionLocal.begin() as db:
        ep = db.execute(select(Episode).where(
            Episode.series_id == series.id, Episode.episode_number == 1)).scalars().first()
        job = EpisodeJob(episode_id=ep.id, status='translating', current_stage='translating',
                         progress=62, asr_provider='jianying', translation_seconds=20.0)
        db.add(job)
        db.flush()
        jid = job.id
    r = client.get('/web/api/jobs?status=all')
    assert r.status_code == 200
    assert any(j['job_id'] == jid for j in r.json()['items'])
    r = client.get('/web/api/jobs?status=completed')
    assert all(j['bucket'] == 'completed' for j in r.json()['items'])
    d = client.get(f'/web/api/jobs/{jid}').json()
    assert d['progress'] == 62 and d['asr_provider'] == 'jianying'
    labels = [s['label'] for s in d['steps']]
    assert 'Download' in labels and 'Translation' in labels and 'Render' in labels
    assert client.get('/jobs/' + jid).status_code == 200


def test_failed_job_error_shown(client, monkeypatch):
    import app.web.routes as webmod

    series = _seed_series(monkeypatch, webmod.SessionLocal)
    from app.models import Episode, EpisodeJob
    from sqlalchemy import select

    with webmod.SessionLocal.begin() as db:
        ep = db.execute(select(Episode).where(
            Episode.series_id == series.id, Episode.episode_number == 1)).scalars().first()
        db.add(EpisodeJob(episode_id=ep.id, status='failed', current_stage='failed',
                          progress=30, error_code='TTS_FAILED', error_message='net blip'))
        db.flush()
        jid = db.execute(select(EpisodeJob)).scalars().first().id
    d = client.get(f'/web/api/jobs/{jid}').json()
    assert d['status'] == 'failed' and d['error_code'] == 'TTS_FAILED'


def test_completed_media_and_traversal_blocked(client, tmp_path, monkeypatch):
    import app.web.routes as webmod

    series = _seed_series(monkeypatch, webmod.SessionLocal)
    from app.models import Episode, EpisodeJob
    from sqlalchemy import select

    final = tmp_path / 'final.vi.mp4'
    final.write_bytes(b'FAKEVIDEO' * 100)
    with webmod.SessionLocal.begin() as db:
        ep = db.execute(select(Episode).where(
            Episode.series_id == series.id, Episode.episode_number == 1)).scalars().first()
        db.add(EpisodeJob(episode_id=ep.id, status='completed', current_stage='completed',
                          progress=100, final_path=str(final)))
        db.flush()
        jid = db.execute(select(EpisodeJob)).scalars().first().id
    r = client.get(f'/web/media/{jid}/final.mp4')
    assert r.status_code == 200 and r.content == b'FAKEVIDEO' * 100
    assert client.get(f'/web/media/{jid}/nope.mp4').status_code == 404
    assert client.get('/web/media/../../etc/passwd/final.mp4').status_code in (404, 422)
    assert client.get('/web/media/unknown-id/final.mp4').status_code == 404
    # Artifact not ready -> 404, not 500.
    with webmod.SessionLocal.begin() as db:
        job = db.get(EpisodeJob, jid)
        job.final_path = None
    assert client.get(f'/web/media/{jid}/final.mp4').status_code == 404


def test_dashboard_auth_flow(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr('app.web.routes.SessionLocal', _test_session(tmp_path, 'auth.db'))
    monkeypatch.setattr('app.services.episodes.SessionLocal',
                        _test_session(tmp_path, 'auth2.db'))
    monkeypatch.setattr(settings, 'dashboard_username', 'boss')
    monkeypatch.setattr(settings, 'dashboard_password', 's3cret')
    monkeypatch.setattr(settings, 'api_key', None)
    from app.main import app
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get('/', follow_redirects=False).status_code == 307
    assert c.get('/web/api/jobs').status_code == 401
    bad = c.post('/login', data={'username': 'boss', 'password': 'nope', 'next': '/'})
    assert bad.status_code == 401
    ok = c.post('/login', data={'username': 'boss', 'password': 's3cret', 'next': '/'},
                follow_redirects=False)
    assert ok.status_code in (303, 302, 307)
    assert c.get('/').status_code == 200
    assert c.post('/logout', follow_redirects=False).status_code in (303, 302, 307)
    assert c.get('/', follow_redirects=False).status_code == 307


def test_alias_process_route_with_style(tmp_path, monkeypatch):
    import app.web.routes as webmod  # noqa: F401 - ensure app imports
    from app.config import settings

    Session = _test_session(tmp_path, 'alias.db')
    monkeypatch.setattr('app.services.episodes.SessionLocal', Session)
    monkeypatch.setattr('app.api.routes.SessionLocal', Session)
    monkeypatch.setattr('app.api.routes.SessionLocal', Session)
    monkeypatch.setattr(settings, 'api_key', None)
    series = _seed_series(monkeypatch, Session)
    from app.main import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post(f'/v1/drama/series/{series.id}/process', json={
        'from_episode': 1, 'to_episode': 2, 'translation_style': 'NINETIES'})
    assert r.status_code == 200
    assert len(r.json()['jobs']) == 2
    from app.models import Series as SeriesModel
    with Session() as db:
        assert db.get(SeriesModel, series.id).translation_style == 'NINETIES'
    # Old route still works.
    r = c.post(f'/v1/series/{series.id}/process', json={'from_episode': 1, 'to_episode': 1})
    assert r.status_code == 200
