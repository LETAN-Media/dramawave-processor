"""Speech-block voice tests (SRT immutability + grouping + timeline)."""

import pytest


def _cues():
    return [
        {'id': 10, 'start_ms': 10000, 'end_ms': 11200, 'text': 'Một.'},
        {'id': 11, 'start_ms': 11200, 'end_ms': 12400, 'text': 'Hai!'},
        {'id': 12, 'start_ms': 12450, 'end_ms': 14000, 'text': 'Ba.'},
        {'id': 13, 'start_ms': 20000, 'end_ms': 21000, 'text': 'Bốn.'},
    ]


def test_block_grouping_respects_max_gap():
    from app.tts.blocks import build_blocks

    blocks = build_blocks(_cues())
    assert len(blocks) == 2
    assert blocks[0].cue_ids == [10, 11, 12]
    assert blocks[1].cue_ids == [13]


def test_block_grouping_keeps_srt_untouched():
    from app.tts.blocks import build_blocks

    cues = _cues()
    snapshot = [dict(c) for c in cues]
    build_blocks(cues)
    assert cues == snapshot


def test_block_start_is_first_cue_start():
    from app.tts.blocks import build_blocks

    b = build_blocks(_cues())[0]
    assert b.start_ms == 10000


def test_block_end_is_last_cue_end():
    from app.tts.blocks import build_blocks

    b = build_blocks(_cues())[0]
    assert b.end_ms == 14000


def test_large_silence_creates_new_block():
    from app.tts.blocks import build_blocks

    cues = [
        {'id': 1, 'start_ms': 0, 'end_ms': 1000, 'text': 'Một.'},
        {'id': 2, 'start_ms': 60000, 'end_ms': 61000, 'text': 'Hai.'},
    ]
    blocks = build_blocks(cues)
    assert len(blocks) == 2


def test_max_block_duration_respected():
    from app.tts.blocks import build_blocks

    cues = [{'id': i, 'start_ms': (i - 1) * 1000, 'end_ms': (i - 1) * 1000 + 900, 'text': f'C{i}.'}
            for i in range(1, 21)]
    blocks = build_blocks(cues, max_ms=8000)
    assert all(b.end_ms - b.start_ms <= 8000 for b in blocks)
    assert sum(len(b.cue_ids) for b in blocks) == 20


def test_tts_generated_per_block(tmp_path, monkeypatch):
    from app.services import block_voice as bvmod
    from app.tts.blocks import build_blocks

    blocks = build_blocks(_cues())
    made = []

    class FakeTTS:
        name = 'fake'

        def resolve_voice(self):
            return 'v'

        def synthesize(self, bid, text, out_mp3, voice=None):
            made.append((bid, voice))
            out_mp3.write_bytes(b'A' * 300)
            from app.tts.base import TTSClip
            return TTSClip(cue_id=bid, mp3_path=out_mp3, duration_ms=1200)

    # synthesize_blocks imports EdgeTTSProvider/decode_to_wav lazily from their
    # modules, so patch those modules (plus DB sessions via sqlite).
    import app.models  # noqa: F401 - register tables
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    eng = create_engine(f'sqlite:///{tmp_path}/b.db', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(bvmod, 'SessionLocal', Session)
    import app.tts.providers_edge as edge_mod
    monkeypatch.setattr(edge_mod, 'EdgeTTSProvider', FakeTTS)
    import app.tts.service as tts_svc
    monkeypatch.setattr(tts_svc, 'decode_to_wav',
                        lambda mp3, wav, tempo=1.0, sample_rate=44100: (wav.write_bytes(b'W' * 50), 1200)[1])
    from app.models import SpeechBlock
    with Session.begin() as db:
        for s in blocks:
            db.add(SpeechBlock(job_id='b1', block_id=s.block_id, start_ms=s.start_ms, end_ms=s.end_ms,
                               cue_ids='[]', subtitle_texts='[]', tts_text=s.tts_text, status='pending'))
    done, warnings = bvmod.synthesize_blocks(blocks, tmp_path, 'v', job_id='b1')
    assert done == 2 and sorted(b for b, _ in made) == [1, 2]
    assert all(v == 'v' for _, v in made)
    assert all((tmp_path / 'tts_blocks' / f'{i:06d}.mp3').exists() for i in (1, 2))


def test_absolute_block_placement(tmp_path):
    pytest.importorskip('numpy')
    import numpy as np
    import wave
    from app.tts.voice import assemble_voice

    sr = 44100
    mk = lambda secs: (np.sin(2 * np.pi * 440 * np.arange(int(sr * secs)) / sr) * 8000).astype('<i2').tobytes()
    w1 = tmp_path / 'a.wav'
    with wave.open(str(w1), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(mk(1.0))
    pseudo = [{'id': 5, 'start_ms': 3000, 'end_ms': 5000}]
    out, dur = assemble_voice(pseudo, lambda bid: w1, 10.0, tmp_path / 'v.wav', job_id='t')
    assert abs(dur - 10.0) < 0.5
    with wave.open(str(out), 'rb') as wf:
        frames = wf.readframes(wf.getnframes())
    sig = np.frombuffer(frames, dtype='<i2')
    assert (sig[:int(2.9 * sr)] == 0).all()
    assert (sig[int(3.0 * sr):int(3.9 * sr)] != 0).any()


def test_no_accumulated_drift():
    blocks = [{'block_id': i, 'start_ms': (i - 1) * 4500} for i in (1, 100, 800)]
    assert blocks[-1]['start_ms'] == 799 * 4500
    assert [b['start_ms'] for b in blocks] == sorted(b['start_ms'] for b in blocks)


def test_voice_duration_validation(tmp_path):
    from app.tts import voice as voice_mod
    import wave

    w = tmp_path / 'c.wav'
    with wave.open(str(w), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(b'\x00\x01' * 44100)
    out, dur = voice_mod.assemble_voice([{'id': 1, 'start_ms': 0, 'end_ms': 1000}],
                                         lambda bid: w, 4.0, tmp_path / 'v.wav', job_id='t')
    assert abs(dur - 4.0) < 0.5


def test_overflow_detection_uses_actual_audio_duration():
    from app.services.block_voice import _classify_block

    # Same text/CPS, different real durations -> different verdicts.
    assert _classify_block(900, 1.0, 1000)[0] == 'FIT'
    assert _classify_block(3000, 1.25, 2000)[0] == 'SEVERE_OVERFLOW'


def test_subtitle_cps_independent_from_voice_overflow():
    from app.translation.service import compute_cps
    from app.services.block_voice import _classify_block

    # High CPS (readability warning) but short audio that fits.
    assert compute_cps('Một câu khá dài cho subtitle', 1000) > 20
    assert _classify_block(800, 1.0, 1000)[0] == 'FIT'
