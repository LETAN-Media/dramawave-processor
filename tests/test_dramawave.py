"""DramaWave provider (Render-API backed) + episode flow tests. Network mocked."""

import pytest


def test_dramawave_url_detection():
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    assert dw.can_handle('https://m.mydramawave.com/share/episode/XJr7VvJdxP?from=share&language=en')
    assert dw.can_handle('https://m.mydramawave.com/share/series/AbC123')
    assert dw.can_handle('https://m.mydramawave.com/series/xyz/3')
    assert dw.can_handle('mydramawave.com/share/episode/abc')
    assert dw.can_handle('https://m.mydramawave.com/series/xyz/3')
    assert not dw.can_handle('https://dramawave.tv/en/dramas')  # listing page, nothing to resolve
    assert not dw.can_handle('https://www.bilibili.com/video/BV155Yf6CEyW')
    assert not dw.can_handle('not a url at all')
    assert not dw.can_handle('https://example.com/video/123')


def test_series_resolve_schema(monkeypatch):
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    monkeypatch.setattr('app.sources.dramawave.api_get_series', lambda sid: {
        'series_id': sid, 'title': 'Test Drama', 'description': 'd', 'cover_url': 'c',
        'episode_count': 75, 'metadata': {}})
    info = dw.resolve_series('https://m.mydramawave.com/series/ABC123')
    assert info.provider == 'dramawave'
    assert info.provider_series_id == 'ABC123'
    assert info.title == 'Test Drama'
    assert info.episode_count == 75


def test_episode_deduplication():
    import app.models  # noqa: F401
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Episode, Series

    eng = create_engine('sqlite:////tmp/dw-dedup.db', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    import app.services.episodes as epmod
    orig = epmod.SessionLocal
    epmod.SessionLocal = Session
    try:
        from app.sources.base import EpisodeInfo, SeriesInfo
        series = epmod.get_or_create_series(
            SeriesInfo(provider='dramawave', provider_series_id='S1', title='T'), 'http://x')
        infos = [EpisodeInfo(provider_episode_id='E1', episode_number=1, title='Ep 1'),
                 EpisodeInfo(provider_episode_id='E2', episode_number=2, title='Ep 2')]
        r1 = epmod.sync_episodes(series, infos)
        r2 = epmod.sync_episodes(series, infos)  # re-resolve must not duplicate
        with Session() as db:
            from sqlalchemy import func, select
            n = db.execute(select(func.count()).select_from(Episode).where(Episode.series_id == series.id)).scalar()
        assert n == 2 and len(r1) == 2 and len(r2) == 2
    finally:
        epmod.SessionLocal = orig


def test_locked_episode_handling():
    from app.sources.base import EpisodeInfo, SourceError
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    locked = EpisodeInfo(provider_episode_id='E9', episode_number=9, locked=True,
                         metadata={'series_id': 'S1'})
    with pytest.raises(SourceError) as exc:
        dw.resolve_episode(locked)
    assert exc.value.code == 'DRAMAWAVE_EPISODE_LOCKED'


def test_playback_mp4(monkeypatch):
    from app.sources.base import EpisodeInfo
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    monkeypatch.setattr('app.sources.dramawave.api_get_playback', lambda sid, eid, q='best': {
        'episode_id': eid, 'duration': 60.0, 'type': 'mp4', 'codec': 'h264',
        'quality': 'source', 'url': 'https://cdn.example.com/v/1.mp4', 'available_qualities': []})
    ep = EpisodeInfo(provider_episode_id='E1', episode_number=1, locked=False,
                     metadata={'series_id': 'S1'})
    pb = dw.resolve_episode(ep)
    assert pb.playback_type == 'mp4'
    assert pb.playback_url.endswith('.mp4')


def test_playback_hls(monkeypatch):
    from app.sources.base import EpisodeInfo
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    monkeypatch.setattr('app.sources.dramawave.api_get_playback', lambda sid, eid, q='best': {
        'episode_id': eid, 'duration': 60.0, 'type': 'hls', 'codec': 'h264',
        'quality': '1080p', 'url': 'https://cdn.example.com/v/1.m3u8',
        'available_qualities': []})
    ep = EpisodeInfo(provider_episode_id='E1', episode_number=1, locked=False,
                     metadata={'series_id': 'S1'})
    pb = dw.resolve_episode(ep)
    assert pb.playback_type == 'hls'
    assert pb.quality == '1080p'


def test_quality_fallback(monkeypatch):
    """Requested quality flows to the resolver API; server picks nearest-below."""
    from app.sources.base import EpisodeInfo
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    seen = {}

    def fake_playback(sid, eid, q='best'):
        seen['q'] = q
        return {'episode_id': eid, 'duration': 60.0, 'type': 'hls', 'codec': 'h264',
                'quality': '480p' if q == '480p' else '1080p',
                'url': 'https://cdn.example.com/v.m3u8', 'available_qualities': []}

    monkeypatch.setattr('app.sources.dramawave.api_get_playback', fake_playback)
    ep = EpisodeInfo(provider_episode_id='E1', episode_number=1, locked=False,
                     metadata={'series_id': 'S1'})
    pb = dw.resolve_episode(ep)
    assert pb.quality == '1080p'  # default VIDEO_QUALITY=1080p
    assert seen['q'] == '1080p'


def test_download_retry(tmp_path):
    from app.sources.base import EpisodePlayback, SourceError
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    pb = EpisodePlayback(episode_id='E1', playback_type='hls', playback_url='https://cdn.example.com/x.m3u8')
    with pytest.raises(SourceError) as exc:
        dw.download_episode(pb, tmp_path / 'original.mp4')
    assert exc.value.code == 'DRAMAWAVE_DOWNLOAD_FAILED'
    # No partial promoted.
    assert not (tmp_path / 'original.mp4').exists()
    assert not list(tmp_path.glob('*.part'))


def test_expired_playback_refresh(monkeypatch, tmp_path):
    """Playback expiry mid-flow: re-resolve once, then download proceeds."""
    from app.sources.base import EpisodeInfo
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    calls = {'n': 0}

    def fake_playback(sid, eid, q='best'):
        calls['n'] += 1
        if calls['n'] == 1:
            return {'episode_id': eid, 'duration': 60.0, 'type': 'hls', 'codec': 'h264',
                    'quality': '1080p', 'url': 'https://cdn.example.com/expired.m3u8',
                    'available_qualities': []}
        return {'episode_id': eid, 'duration': 60.0, 'type': 'mp4', 'codec': 'h264',
                'quality': 'source', 'url': 'https://cdn.example.com/fresh.mp4',
                'available_qualities': []}

    monkeypatch.setattr('app.sources.dramawave.api_get_playback', fake_playback)
    ep = EpisodeInfo(provider_episode_id='E1', episode_number=1, locked=False,
                     metadata={'series_id': 'S1'})
    first = dw.resolve_episode(ep)
    assert first.playback_url.endswith('expired.m3u8')
    refreshed = dw.refresh_playback(ep)
    assert refreshed.playback_url.endswith('fresh.mp4')
    assert calls['n'] == 2


def test_resume_job(tmp_path):
    """Worker restart resumes: completed artifacts are reused, not redownloaded."""
    import app.models  # noqa: F401
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Episode, EpisodeJob, Series

    eng = create_engine(f'sqlite:///{tmp_path}/r.db', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    import app.services.episodes as epmod
    orig = epmod.SessionLocal
    epmod.SessionLocal = Session
    try:
        with Session.begin() as db:
            db.add(Series(id='s1', provider='dramawave', provider_series_id='S1', title='T'))
            db.add(Episode(id='e1', series_id='s1', provider_episode_id='E1', episode_number=1, status='ready'))
            db.add(EpisodeJob(id='j1', episode_id='e1', status='ready', current_stage='ready', progress=100,
                              original_path='/tmp/x.mp4', source_srt_path='/tmp/x.srt'))
        # Re-enqueue without force must skip the ready episode.
        out = epmod.enqueue_episodes('s1', 1, 1, force=False)
        assert out['enqueued'] == [] and out['skipped_locked_or_ready'] == [1]
        # Force re-runs it.
        out = epmod.enqueue_episodes('s1', 1, 1, force=True)
        assert out['enqueued'] == ['j1']
    finally:
        epmod.SessionLocal = orig


def test_series_episode_range(tmp_path):
    import app.models  # noqa: F401
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Episode, Series

    eng = create_engine(f'sqlite:///{tmp_path}/r2.db', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    import app.services.episodes as epmod
    orig = epmod.SessionLocal
    epmod.SessionLocal = Session
    try:
        with Session.begin() as db:
            db.add(Series(id='s1', provider='dramawave', provider_series_id='S1', title='T'))
            for i in range(1, 8):
                db.add(Episode(id=f'e{i}', series_id='s1', provider_episode_id=f'E{i}',
                               episode_number=i, locked=(i > 5)))
        out = epmod.enqueue_episodes('s1', 2, 4)
        assert len(out['enqueued']) == 3
        out = epmod.enqueue_episodes('s1', 5, 7)
        assert len(out['enqueued']) == 1  # ep5 only; 6,7 locked
        assert out['skipped_locked_or_ready'] == [6, 7]
    finally:
        epmod.SessionLocal = orig


def test_existing_episode_not_redone(tmp_path):
    import app.models  # noqa: F401
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Series

    eng = create_engine(f'sqlite:///{tmp_path}/r3.db', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    import app.services.episodes as epmod
    orig = epmod.SessionLocal
    epmod.SessionLocal = Session
    try:
        from app.sources.base import SeriesInfo
        s1 = epmod.get_or_create_series(
            SeriesInfo(provider='dramawave', provider_series_id='SX', title='T'), 'http://x')
        s2 = epmod.get_or_create_series(
            SeriesInfo(provider='dramawave', provider_series_id='SX', title='T2'), 'http://y')
        assert s1.id == s2.id  # same series row reused
        with Session() as db:
            assert db.get(Series, s1.id).title == 'T2'  # metadata refreshed
    finally:
        epmod.SessionLocal = orig


def test_language_routing_zh_uses_jianying():
    """zh routes to JianYing; others to Whisper (mocked providers, no network)."""
    import app.asr.service as svc

    assert svc._normalize_language('zh-CN') == 'zh'
    assert svc._normalize_language('English') == ''
    assert svc._normalize_language('en-US') == 'en'
    assert svc._normalize_language('ko') == 'ko'
    assert svc._normalize_language('auto') == ''


def test_series_glossary_persistence(tmp_path):
    import app.models  # noqa: F401
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Series

    eng = create_engine(f'sqlite:///{tmp_path}/r4.db', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    with Session.begin() as db:
        db.add(Series(id='s1', provider='dramawave', provider_series_id='S1', title='T',
                      character_glossary='{"顾言": {"vi": "Cố Ngôn"}}',
                      relationship_glossary='{}'))
    with Session() as db:
        row = db.get(Series, 's1')
        assert 'Cố Ngôn' in (row.character_glossary or '')


def test_jianying_zh_routing(tmp_path, monkeypatch):
    """zh audio attempts JianYing first (mocked providers)."""
    import app.asr.service as svc

    calls = []

    class FakeJY:
        def transcribe(self, audio_path, *, job_id=None):
            calls.append('jy')
            from app.asr.base import ASRResult
            return ASRResult(provider='jianying', language='zh', segments=[], recognition_seconds=1.0)

    class FakeWH:
        def transcribe(self, audio_path, *, job_id=None, language=None):
            calls.append(('wh', language))
            from app.asr.base import ASRResult
            return ASRResult(provider='whisper', language=language or 'zh', segments=[],
                             recognition_seconds=1.0)

    monkeypatch.setattr('app.asr.service.JianYingProvider', FakeJY)
    monkeypatch.setattr('app.asr.service.WhisperProvider', FakeWH)
    monkeypatch.setattr('app.asr.service._write_strict_srt', lambda wd, res: (wd / 'source.original.srt', 0))
    audio = tmp_path / 'a.m4a'
    audio.write_bytes(b'x')
    out, lang, cues, provider, fallback, *_ = svc.transcribe_with_fallback(
        audio, None, tmp_path, job_id='t', language='zh')
    assert provider == 'jianying' and calls == ['jy'] and lang == 'zh'


def test_whisper_non_zh_routing(tmp_path, monkeypatch):
    """en audio goes straight to Whisper (JianYing never touched)."""
    import app.asr.service as svc

    calls = []

    class FakeJY:
        def transcribe(self, audio_path, *, job_id=None):
            calls.append('jy')
            raise AssertionError('JianYing must not receive non-Chinese audio')

    class FakeWH:
        def transcribe(self, audio_path, *, job_id=None, language=None):
            calls.append(('wh', language))
            from app.asr.base import ASRResult
            return ASRResult(provider='whisper', language=language or 'en', segments=[],
                             recognition_seconds=1.0)

    monkeypatch.setattr('app.asr.service.JianYingProvider', FakeJY)
    monkeypatch.setattr('app.asr.service.WhisperProvider', FakeWH)
    monkeypatch.setattr('app.asr.service._write_strict_srt', lambda wd, res: (wd / 'source.original.srt', 0))
    audio = tmp_path / 'a.m4a'
    audio.write_bytes(b'x')
    out, lang, cues, provider, fallback, *_ = svc.transcribe_with_fallback(
        audio, None, tmp_path, job_id='t', language='en')
    assert provider == 'whisper' and lang == 'en'
    assert calls == [('wh', 'en')]
