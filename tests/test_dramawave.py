"""DramaWave provider (Render-API backed) + episode flow tests. Network mocked."""

import pytest


def test_series_resolve_schema(monkeypatch):
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    monkeypatch.setattr('app.sources.dramawave.api_get_series', lambda sid: {
        'canonical_series_id': sid, 'canonical_title': 'Test Drama', 'description': 'd', 'cover_url': 'c',
        'episode_count': 75, 'metadata': {}, 'sources': [{'provider': 'dramawave', 'provider_series_id': 'S1'}]})
    info = dw.resolve_series('https://m.mydramawave.com/series/ABC123')
    assert info.provider == 'multi'
    assert 'ABC123' in info.provider_series_id


def test_locked_episode_handling():
    from app.sources.base import EpisodeInfo, SourceError
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    locked = EpisodeInfo(provider_episode_id='E9', episode_number=9, locked=True,
                         metadata={'series_id': 'S1'})
    with pytest.raises(SourceError) as exc:
        dw.resolve_episode(locked)
    assert exc.value.code == 'LOCKED'


def test_playback_mp4(monkeypatch):
    from app.sources.base import EpisodeInfo
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    monkeypatch.setattr('app.sources.dramawave.api_resolve_playback', lambda series_id, episode_number, quality='best': {
        'episode_id': episode_number, 'duration': 60.0, 'type': 'mp4', 'codec': 'h264',
        'quality': 'source', 'url': 'https://cdn.example.com/v/1.mp4', 'headers': {}})
    ep = EpisodeInfo(provider_episode_id='E1', episode_number=1, locked=False,
                     metadata={'series_id': 'S1'})
    pb = dw.resolve_episode(ep)
    assert pb.playback_type == 'mp4'
    assert pb.playback_url.endswith('.mp4')


def test_playback_hls(monkeypatch):
    from app.sources.base import EpisodeInfo
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    monkeypatch.setattr('app.sources.dramawave.api_resolve_playback', lambda series_id, episode_number, quality='best': {
        'episode_id': episode_number, 'duration': 60.0, 'type': 'hls', 'codec': 'h264',
        'quality': '1080p', 'url': 'https://cdn.example.com/v/1.m3u8', 'headers': {}})
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

    def fake_playback(series_id, episode_number, quality='best'):
        seen['q'] = quality
        return {'episode_id': episode_number, 'duration': 60.0, 'type': 'hls', 'codec': 'h264',
                'quality': '480p' if quality == '480p' else '1080p',
                'url': 'https://cdn.example.com/v.m3u8', 'headers': {}}

    monkeypatch.setattr('app.sources.dramawave.api_resolve_playback', fake_playback)
    ep = EpisodeInfo(provider_episode_id='E1', episode_number=1, locked=False,
                     metadata={'series_id': 'S1'})
    pb = dw.resolve_episode(ep)
    assert pb.quality == '1080p'  # default VIDEO_QUALITY=1080p
    assert seen['q'] == 'best'


def test_download_retry(tmp_path):
    from app.sources.base import EpisodePlayback, SourceError
    from app.sources.dramawave import DramaWaveSource

    dw = DramaWaveSource()
    pb = EpisodePlayback(episode_id='E1', playback_type='hls', playback_url='https://cdn.example.com/x.m3u8')
    with pytest.raises(SourceError) as exc:
        dw.download_episode(pb, tmp_path / 'original.mp4')
    assert exc.value.code == 'DRAMAWAVE_DOWNLOAD_FAILED' or exc.value.code == 'DOWNLOAD_FAILED'

