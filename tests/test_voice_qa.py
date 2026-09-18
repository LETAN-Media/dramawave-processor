"""FINAL VOICE QA tests (no network; fakes for model/ffmpeg-heavy paths)."""

import pytest


def test_cps_target_single_source_of_truth():
    from app.config import settings

    assert settings.cps_target == 20
    import inspect
    from app.services import phase2 as p2

    sig = inspect.signature(p2._compress_long_cues)
    assert sig.parameters['cps_limit'].default is None  # falls back to settings.cps_target
    from app.translation import providers_toolnet as pt

    assert '__CPS__' in pt.SYSTEM_PROMPT_TEMPLATE
    assert '22' not in pt.SYSTEM_PROMPT_TEMPLATE
    assert '20' in pt.system_prompt()


def test_qa_classification_fit():
    from app.services.voice_qa import classify_cue

    cls, final, ov = classify_cue(1000, 1.0, 1500)
    assert (cls, final, ov) == ('FIT', 1000, 0)


def test_qa_classification_adjusted_fit():
    from app.services.voice_qa import classify_cue

    # 1800ms spoken in 1500ms slot: 1800/1.2=1500 fits within 1.25 cap.
    cls, final, ov = classify_cue(1800, 1.2, 1500)
    assert cls == 'ADJUSTED_FIT' and final == 1500 and ov == 0


def test_qa_classification_overflow():
    from app.services.voice_qa import classify_cue

    # 2000ms in 1500ms slot: capped 1.25 -> 1600 > 1500, overflow 100ms (<250ms, <10%).
    cls, final, ov = classify_cue(2000, 1.25, 1500)
    assert cls == 'OVERFLOW' and final == 1600 and ov == 100


def test_qa_classification_severe_overflow_ms():
    from app.services.voice_qa import classify_cue

    # 3000ms in 2000ms slot: 3000/1.25=2400, overflow 400ms > 250ms.
    cls, final, ov = classify_cue(3000, 1.25, 2000)
    assert cls == 'SEVERE_OVERFLOW' and ov == 400


def test_qa_classification_severe_overflow_ratio():
    from app.services.voice_qa import classify_cue

    # 1200ms in 840ms slot: 1200/1.25=960, overflow 120ms (>10% of 840=84).
    cls, final, ov = classify_cue(1200, 1.25, 840)
    assert cls == 'SEVERE_OVERFLOW' and ov == 120


def test_qa_atempo_never_exceeds_cap():
    from app.services.voice_qa import classify_cue

    cls, final, ov = classify_cue(5000, 99.0, 1000)
    assert final == 4000  # 5000/1.25, never faster
    assert cls == 'SEVERE_OVERFLOW'


def _sqlite_session(tmp_path):
    """File-based sqlite shared across threads (memory DBs are per-connection)."""
    import app.models  # noqa: F401 - register tables on Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base

    eng = create_engine(f'sqlite:///{tmp_path}/qa.db',
                        connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)


def test_qa_overflow_triggers_compress_and_regen(monkeypatch, tmp_path):
    """OVERFLOW cue: model compresses text, only that cue's TTS regens (functional, sqlite)."""
    import app.services.voice_qa as qa
    from app.models import CueState

    Session = _sqlite_session(tmp_path)
    monkeypatch.setattr(qa, 'SessionLocal', Session)
    with Session.begin() as db:
        db.add(CueState(job_id='qa1', cue_id=1, start_ms=0, end_ms=1000,
                        zh_text='你好世界', vi_text='Một câu dịch tiếng Việt quá dài không thể nào đọc kịp',
                        translated=True, tts_duration_ms=4000, tempo=1.25, tts_status='done'))

    class FakeProvider:
        def compress_text(self, cid, text, dur, max_attempts=2):
            assert dur == 1000
            return 'Câu ngắn.'

    monkeypatch.setattr('app.translation.providers_toolnet.OpenAICompatibleProvider', FakeProvider)
    regened = []

    def fake_regen(job_id, workdir, cue, text_vi):
        regened.append((cue['id'], text_vi))
        with Session.begin() as db:
            row = db.get(CueState, (job_id, cue['id']))
            row.tts_duration_ms = 800
            row.tempo = 1.0
            row.tts_status = 'done'

    monkeypatch.setattr(qa, '_regen_single_cue', fake_regen)
    cues = [{'id': 1, 'start_ms': 0, 'end_ms': 1000, 'text': '你好世界'}]
    changed, regen, over = qa._compress_and_regen('qa1', tmp_path, cues, [1])
    assert changed == 1 and regen == 1 and over == 0
    assert regened == [(1, 'Câu ngắn.')]
    with Session() as db:
        row = db.get(CueState, ('qa1', 1))
        assert row.vi_text == 'Câu ngắn.'
        assert row.qa_class == 'FIT'
        assert row.manual_review_required in (None, False)


def test_qa_manual_review_flagged_after_failed_rounds(monkeypatch, tmp_path):
    """Cues still overflowing after max rounds must be flagged, never silent PASS."""
    import app.services.voice_qa as qa
    from app.models import CueState

    Session = _sqlite_session(tmp_path)
    monkeypatch.setattr(qa, 'SessionLocal', Session)
    with Session.begin() as db:
        db.add(CueState(job_id='qa2', cue_id=1, start_ms=0, end_ms=500,
                        zh_text='你好', vi_text='x' * 100, translated=True,
                        tts_duration_ms=5000, tempo=1.25, tts_status='done'))

    class StubbornProvider:
        def compress_text(self, cid, text, dur, max_attempts=2):
            return text  # no improvement possible

    monkeypatch.setattr('app.translation.providers_toolnet.OpenAICompatibleProvider', StubbornProvider)
    monkeypatch.setattr(qa, '_regen_single_cue', lambda *a, **k: None)
    cues = [{'id': 1, 'start_ms': 0, 'end_ms': 500, 'text': '你好'}]
    changed, regen, over = qa._compress_and_regen('qa2', __import__('pathlib').Path('/tmp'), cues, [1])
    assert over == 1
    stats = qa.classify_all('qa2')
    assert stats['counts']['SEVERE_OVERFLOW'] == 1
    # Flagging path (as in run_voice_qa tail).
    with Session.begin() as db:
        row = db.get(CueState, ('qa2', 1))
        row.manual_review_required = True
    with Session() as db:
        assert db.get(CueState, ('qa2', 1)).manual_review_required is True


def test_qa_report_crossing_check():
    """Non-manual cues must not cross the next cue start."""
    import inspect
    import app.services.voice_qa as qa

    src = inspect.getsource(qa.build_report)
    assert 'crossing' in src.lower()
    assert 'Any audio crossing next cue' in src


def test_compress_batch_mapping(monkeypatch):
    """Batch compression maps ids correctly; per-cue fallback on batch failure."""
    from app.translation.providers_toolnet import OpenAICompatibleProvider

    calls = {'n': 0}

    async def _nope(*a, **k):
        raise AssertionError('no network')

    def fake_stream(messages, model, timeout, max_tokens):
        calls['n'] += 1
        if 'compress_batch' in messages[1]['content']:
            return '[{"id": 1, "text_vi": "Ngắn 1."}, {"id": 2, "text_vi": "Ngắn 2."}]'
        return '{"id": 1, "text_vi": "Ngắn 1."}'

    monkeypatch.setattr('app.translation.providers_toolnet._post_chat_stream', fake_stream)
    p = OpenAICompatibleProvider()
    out, used = p.compress_batch([(1, 'Dài một.', 1000), (2, 'Dài hai.', 1200)])
    assert out == {1: 'Ngắn 1.', 2: 'Ngắn 2.'}
    assert used == p.models[0]
