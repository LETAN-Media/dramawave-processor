"""Voice QA performance fix: circuit breaker, fast AI path, batch fallback, resume."""

import pytest


@pytest.fixture(autouse=True)
def _clean_circuit():
    from app.translation import providers_toolnet as pt

    pt.reset_ai_circuit()
    yield
    pt.reset_ai_circuit()


def _provider(monkeypatch, settings_kwargs=None):
    from app.config import settings

    for k, v in (settings_kwargs or {}).items():
        monkeypatch.setattr(settings, k, v)
    from app.translation.providers_toolnet import OpenAICompatibleProvider

    return OpenAICompatibleProvider()


def test_breaker_opens_after_threshold(monkeypatch):
    from app.translation import providers_toolnet as pt

    p = _provider(monkeypatch, {'translation_primary_failure_threshold': 1,
                                'translation_primary_cooldown_seconds': 600,
                                'translation_max_retries': 0})
    calls = []

    def fake_stream(messages, model, timeout, max_tokens):
        calls.append(model)
        if model == p.models[0]:
            raise RuntimeError('TRANSLATION_HTTP: curl exit 28: timeout')
        return '[{"id": 1, "text_vi": "Ngắn."}]'

    monkeypatch.setattr(pt, '_post_chat_stream', fake_stream)
    from app.translation.base import TransContext, TransCue

    ctx = TransContext(previous=[], glossary={})
    mapping, used = p.translate_batch_with_model([TransCue(cue_id=1, start_ms=0, end_ms=1000, text='x')], ctx)
    assert mapping == {1: 'Ngắn.'} and used == p.models[1]
    snap = pt.breaker_snapshot()
    assert snap['model'] == p.models[0] and snap['failures'] >= 1
    # Second call skips the sick primary entirely (no primary call).
    calls.clear()
    p.translate_batch_with_model([TransCue(cue_id=1, start_ms=0, end_ms=1000, text='x')], ctx)
    assert calls == [p.models[1]]
    stats = pt.ai_stats_snapshot()
    assert stats['primary_timeouts'] >= 1 and stats['fallback_calls'] >= 2


def test_breaker_ignores_schema_errors(monkeypatch):
    from app.translation import providers_toolnet as pt

    p = _provider(monkeypatch, {'translation_primary_failure_threshold': 1,
                                'translation_max_retries': 0})
    seen = {'n': 0}

    def fake_stream(messages, model, timeout, max_tokens):
        seen['n'] += 1
        if model == p.models[0]:
            return 'not json'
        return '[{"id": 1, "text_vi": "Ngắn."}]'

    monkeypatch.setattr(pt, '_post_chat_stream', fake_stream)
    from app.translation.base import TransContext, TransCue

    ctx = TransContext(previous=[], glossary={})
    p.translate_batch_with_model([TransCue(cue_id=1, start_ms=0, end_ms=1000, text='x')], ctx)
    assert pt.breaker_snapshot()['model'] is None
    assert pt.ai_stats_snapshot()['primary_timeouts'] == 0


def test_voice_qa_fast_path_no_primary_retry(monkeypatch):
    from app.translation import providers_toolnet as pt

    p = _provider(monkeypatch, {})
    calls = []

    def fake_stream(messages, model, timeout, max_tokens):
        calls.append((model, timeout))
        if model == p.models[0]:
            raise RuntimeError('TRANSLATION_HTTP: curl exit 28: timeout')
        return '[{"id": 7, "text_vi": "Ngắn."}]'

    monkeypatch.setattr(pt, '_post_chat_stream', fake_stream)
    out = p.compress_text(7, 'Một câu rất dài cần rút gọn', 1000,
                          timeout=20, primary_retries=0, fallback_retries=2)
    assert out == 'Ngắn.'
    assert [c[0] for c in calls] == [p.models[0], p.models[1]]
    assert calls[0][1] == 20  # short QA timeout, not 120s


def test_compress_batch_failover_to_fallback(monkeypatch):
    from app.translation import providers_toolnet as pt

    p = _provider(monkeypatch, {})
    calls = []

    def fake_stream(messages, model, timeout, max_tokens):
        calls.append(model)
        if model == p.models[0]:
            return '[{"id": 1, "text_vi": "Gọn 1."}]'  # missing id 2 -> batch invalid
        return '[{"id": 1, "text_vi": "Gọn 1."}, {"id": 2, "text_vi": "Gọn 2."}]'

    monkeypatch.setattr(pt, '_post_chat_stream', fake_stream)
    out, used = p.compress_batch([(1, 'Dài một.', 1000), (2, 'Dài hai.', 1000)],
                                 timeout=20, primary_retries=0, fallback_retries=0)
    assert out == {1: 'Gọn 1.', 2: 'Gọn 2.'}
    assert used == p.models[1]
    assert calls == [p.models[0], p.models[1]]


def test_qa_batch_helper_falls_back_per_block(tmp_path, monkeypatch):
    import app.services.episodes as epmod

    class FakeProvider:
        def compress_batch(self, items, **kwargs):
            raise RuntimeError('COMPRESS_BATCH_FAILED: boom')

        def compress_text(self, bid, text, dur, **kwargs):
            return f'Gọn {bid}.'

    changed = epmod._compress_qa_batch(
        tmp_path, {'1': {'tts_text': 'Dài một.'}, '2': {'tts_text': 'Dài hai.'}},
        [type('S', (), {'block_id': 1, 'available_ms': 1000})(),
         type('S', (), {'block_id': 2, 'available_ms': 1000})()],
        [1, 2], FakeProvider(), 'job1')
    assert sorted(changed) == [1, 2]


def test_resume_gating_helpers():
    import app.services.episodes as epmod

    assert epmod._stage_reached('generating_tts', 'translating')
    assert epmod._stage_reached('completed', 'translating')
    assert not epmod._stage_reached('queued', 'translating')
    assert not epmod._stage_reached('failed', 'translating')  # unknown -> full run (safe)
    assert not epmod._stage_reached(None, 'translating')
    assert epmod._valid_file(None) is None
    assert epmod._valid_file('/nonexistent-xyz/file.srt') is None


def test_resume_skips_asr_and_translation(tmp_path, monkeypatch):
    """Stage=generating_tts + valid SRTs -> TTS runs, ASR/translation untouched."""
    import app.models  # noqa: F401
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Episode, EpisodeJob, Series

    eng = create_engine(f'sqlite:///{tmp_path}/resume.db',
                        connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    import app.services.episodes as epmod

    orig_session, orig_workdir = epmod.SessionLocal, epmod.episode_workdir
    epmod.SessionLocal = Session
    epmod.episode_workdir = lambda series, num: tmp_path / 'work'
    try:
        src = tmp_path / 'source.original.srt'
        src.write_text('1\n00:00:00,000 --> 00:00:01,000\n你好\n', encoding='utf-8')
        vi = tmp_path / 'source.vi.srt'
        vi.write_text('1\n00:00:00,000 --> 00:00:01,000\nXin chào\n', encoding='utf-8')
        vid = tmp_path / 'original.mp4'
        vid.write_bytes(b'\x00' * 100)
        with Session.begin() as db:
            db.add(Series(id='s1', provider='dramawave', provider_series_id='P1', title='T'))
            db.add(Episode(id='e1', series_id='s1', provider_episode_id='E1',
                           episode_number=1, status='generating_tts'))
            db.add(EpisodeJob(id='j1', episode_id='e1', status='generating_tts',
                              current_stage='generating_tts', progress=70,
                              original_path=str(vid), source_srt_path=str(src),
                              source_language='zh', subtitle_cue_count=1,
                              vi_srt_path=str(vi)))
        calls = []
        monkeypatch.setattr(epmod, '_tts_episode_voice',
                            lambda *a, **k: calls.append('tts'))
        monkeypatch.setattr(epmod, '_render_episode',
                            lambda *a, **k: calls.append('render'))
        monkeypatch.setattr(epmod, '_translate_episode',
                            lambda *a, **k: calls.append('translate-NO'))
        import app.asr.service as asr_mod
        monkeypatch.setattr(asr_mod, 'transcribe_with_fallback',
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError('ASR must be skipped')))
        epmod.process_episode_job('j1', 'test-worker')
        assert 'translate-NO' not in calls
        assert calls.count('tts') == 1 and calls.count('render') == 1
        with Session() as db:
            assert db.get(EpisodeJob, 'j1').status == 'completed'
    finally:
        epmod.SessionLocal = orig_session
        epmod.episode_workdir = orig_workdir
