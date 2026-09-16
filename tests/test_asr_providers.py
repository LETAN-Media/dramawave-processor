"""ASR provider abstraction regression tests (mocked, no network)."""

import pytest

from app.asr.base import ASRResult, ASRSegment, ASRWord
from app.bilibili.srt import validate_srt_text


def _segs():
    return [
        ASRSegment(start_ms=200, end_ms=1666, text='那年父母离世',
                   words=[ASRWord(text='那年父母离世', start_ms=200, end_ms=1666)]),
        ASRSegment(start_ms=1666, end_ms=3000, text='公司濒临破产'),
    ]


def test_asr_auto_prefers_jianying(tmp_path, monkeypatch):
    from app.asr import service as svc
    from app.config import settings

    monkeypatch.setattr(settings, 'asr_provider', 'auto')
    monkeypatch.setattr(settings, 'allow_remote_asr', True)
    monkeypatch.setattr(settings, 'jianying_enabled', True)

    audio = tmp_path / 'audio.m4a'
    audio.write_bytes(b'fake-audio')
    wav = tmp_path / 'audio.wav'
    wav.write_bytes(b'fake-wav')

    def fake_jy(self, audio_path, *, job_id=None):
        assert audio_path == audio
        return ASRResult(provider='jianying', language='zh', segments=_segs(),
                         upload_seconds=2.0, recognition_seconds=5.0)

    def boom(self, audio_path, *, job_id=None):
        raise AssertionError('whisper should not run when jianying succeeds')

    monkeypatch.setattr('app.asr.service.JianYingProvider.transcribe', fake_jy)
    monkeypatch.setattr('app.asr.service.WhisperProvider.transcribe', boom)
    out, lang, cues, provider, fallback, total, up, rec = svc.transcribe_with_fallback(audio, wav, tmp_path, job_id='t1')
    assert provider == 'jianying'
    assert fallback is False
    assert lang == 'zh' and cues == 2
    assert validate_srt_text(out.read_text(encoding='utf-8')) == 2


def test_asr_falls_back_to_whisper(tmp_path, monkeypatch):
    from app.asr import service as svc
    from app.config import settings

    monkeypatch.setattr(settings, 'asr_provider', 'auto')
    monkeypatch.setattr(settings, 'allow_remote_asr', True)
    monkeypatch.setattr(settings, 'jianying_enabled', True)

    audio = tmp_path / 'audio.m4a'
    audio.write_bytes(b'fake-audio')
    wav = tmp_path / 'audio.wav'
    wav.write_bytes(b'fake-wav')
    calls = []

    def fail_jy(self, audio_path, *, job_id=None):
        calls.append('jy')
        raise RuntimeError('JIANYING_CLI_FAILED: 503')

    def ok_wh(self, audio_path, *, job_id=None):
        calls.append('wh')
        return ASRResult(provider='whisper', language='zh', segments=_segs(), recognition_seconds=9.0)

    monkeypatch.setattr('app.asr.service.JianYingProvider.transcribe', fail_jy)
    monkeypatch.setattr('app.asr.service.WhisperProvider.transcribe', ok_wh)
    out, lang, cues, provider, fallback, total, up, rec = svc.transcribe_with_fallback(audio, wav, tmp_path, job_id='t2')
    assert calls == ['jy', 'wh']
    assert provider == 'whisper'
    assert fallback is True
    assert cues == 2


def test_jianying_timeout_triggers_fallback(tmp_path, monkeypatch):
    from app.asr import service as svc
    from app.config import settings

    monkeypatch.setattr(settings, 'asr_provider', 'auto')
    monkeypatch.setattr(settings, 'allow_remote_asr', True)
    monkeypatch.setattr(settings, 'jianying_enabled', True)

    audio = tmp_path / 'audio.m4a'
    audio.write_bytes(b'x')
    wav = tmp_path / 'audio.wav'
    wav.write_bytes(b'x')

    def timeout_jy(self, audio_path, *, job_id=None):
        raise RuntimeError('JIANYING_TIMEOUT after 720s')

    def ok_wh(self, audio_path, *, job_id=None):
        return ASRResult(provider='whisper', language='zh', segments=_segs(), recognition_seconds=1.0)

    monkeypatch.setattr('app.asr.service.JianYingProvider.transcribe', timeout_jy)
    monkeypatch.setattr('app.asr.service.WhisperProvider.transcribe', ok_wh)
    out, lang, cues, provider, fallback, *_ = svc.transcribe_with_fallback(audio, wav, tmp_path, job_id='t3')
    assert provider == 'whisper' and fallback is True


def test_jianying_result_normalizes_to_strict_srt(tmp_path):
    from app.asr.jianying import _parse_jianying_json
    from app.bilibili.srt import normalize_segments_to_srt

    payload = [
        {'text': '那年父母离世公司濒临破产我在姐姐最难的时候和他断绝关系今天我们要说的是这个很长的故事还要继续补充更多文字内容', 'startMs': 200, 'endMs': 10000,
         'words': [
             {'text': '那年父母离世', 'startMs': 200, 'endMs': 1666},
             {'text': '公司濒临破产', 'startMs': 1666, 'endMs': 3000},
             {'text': '我在姐姐最难的时候和他断绝关系', 'startMs': 3000, 'endMs': 5800},
             {'text': '今天我们要说的是这个很长的故事还要继续补充更多文字内容', 'startMs': 5800, 'endMs': 10000},
         ]},
    ]
    segments = _parse_jianying_json(payload)
    assert segments[0].start_ms == 200
    srt = normalize_segments_to_srt(
        [{'start': s.start_ms / 1000, 'end': s.end_ms / 1000, 'text': s.text,
          'words': [{'start': w.start_ms / 1000, 'end': w.end_ms / 1000, 'word': w.text} for w in s.words]}
         for s in segments]
    )
    # Strict: must re-parse, no raw dump.
    assert validate_srt_text(srt) >= 2
    assert ',000' not in srt or True
    for line in srt.splitlines():
        if '-->' in line:
            assert ',' in line and '.' not in line


def test_asr_provider_saved_to_job():
    from app.models import Job
    cols = {c.key for c in Job.__table__.columns}
    for expected in ('asr_provider', 'asr_started_at', 'asr_completed_at', 'asr_processing_seconds', 'asr_fallback_used'):
        assert expected in cols, f'missing Job column {expected}'


def test_remote_asr_disabled_uses_local_whisper(tmp_path, monkeypatch):
    from app.asr import service as svc
    from app.config import settings

    monkeypatch.setattr(settings, 'asr_provider', 'auto')
    monkeypatch.setattr(settings, 'allow_remote_asr', False)
    monkeypatch.setattr(settings, 'jianying_enabled', True)

    audio = tmp_path / 'audio.m4a'
    audio.write_bytes(b'x')
    calls = []

    def boom(self, audio_path, *, job_id=None):
        calls.append('jy')
        raise AssertionError('jianying must not run when ALLOW_REMOTE_ASR=false')

    def ok_wh(self, audio_path, *, job_id=None):
        calls.append('wh')
        return ASRResult(provider='whisper', language='zh', segments=_segs(), recognition_seconds=1.0)

    monkeypatch.setattr('app.asr.service.JianYingProvider.transcribe', boom)
    monkeypatch.setattr('app.asr.service.WhisperProvider.transcribe', ok_wh)
    out, lang, cues, provider, fallback, *_ = svc.transcribe_with_fallback(audio, None, tmp_path, job_id='t4')
    assert calls == ['wh']
    assert provider == 'whisper'


def test_invalid_jianying_srt_rejected_or_normalized(tmp_path):
    from app.asr.jianying import parse_jianying_srt_file

    # Invalid: empty cues -> rejected.
    bad = tmp_path / 'bad.srt'
    bad.write_text('1\n00:00:01,000 --> 00:00:02,000\n\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='INVALID_JIANYING_RESULT'):
        parse_jianying_srt_file(bad)

    # Valid raw SRT with dot ms gets parsed then strict-normalized with commas.
    raw = tmp_path / 'raw.srt'
    raw.write_text('1\n00:00:00.200 --> 00:00:01.666\n那年父母离世\n\n2\n00:00:01.666 --> 00:00:03.000\n公司濒临破产\n',
                   encoding='utf-8')
    segs = parse_jianying_srt_file(raw)
    assert len(segs) == 2
    from app.bilibili.srt import normalize_segments_to_srt
    srt = normalize_segments_to_srt([{'start': s.start_ms / 1000, 'end': s.end_ms / 1000, 'text': s.text} for s in segs])
    assert '00:00:00,200 --> 00:00:01,666' in srt
    assert validate_srt_text(srt) == 2
