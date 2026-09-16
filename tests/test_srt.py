"""Strict SRT format tests (video -> Chinese SRT)."""

import re

import pytest

from app.bilibili.srt import (
    TIMECODE_RE,
    format_timestamp,
    normalize_segments_to_srt,
    validate_srt_text,
)


def _sample_segments():
    return [
        {'start': 0.2, 'end': 1.666, 'text': '那年父母离世'},
        {'start': 1.666, 'end': 3.0, 'text': '公司濒临破产'},
        {'start': 3.0, 'end': 5.8, 'text': '我在姐姐最难的时候和他断绝关系'},
    ]


def test_srt_numbering_sequential():
    srt = normalize_segments_to_srt(_sample_segments())
    indexes = [int(m) for m in re.findall(r'(?m)^(\d+)$', srt)]
    assert indexes == [1, 2, 3]
    # Re-parse must keep sequential numbering.
    assert validate_srt_text(srt) == 3


def test_srt_uses_comma_milliseconds():
    srt = normalize_segments_to_srt(_sample_segments())
    assert ',200 -->' in srt or ',666 -->' in srt
    # No dot milliseconds in timecodes.
    for line in srt.splitlines():
        if '-->' in line:
            assert '.' not in line, f'dot found in timecode: {line!r}'
            assert ',' in line


def test_srt_has_three_digit_milliseconds():
    srt = normalize_segments_to_srt(_sample_segments())
    for line in srt.splitlines():
        if '-->' in line:
            assert TIMECODE_RE.match(line), f'bad timecode: {line!r}'
            m = re.findall(r',(\d+)', line)
            assert len(m) == 2
            assert all(len(x) == 3 for x in m)


def test_srt_blank_line_between_cues():
    srt = normalize_segments_to_srt(_sample_segments())
    assert '\n\n\n' not in srt.replace('\r\n', '\n')
    blocks = [b for b in srt.strip().split('\n\n') if b.strip()]
    assert len(blocks) == 3
    # Each block: index, timecode, text.
    for b in blocks:
        assert len(b.split('\n')) >= 3


def test_srt_no_empty_text():
    srt = normalize_segments_to_srt([
        {'start': 0.0, 'end': 1.0, 'text': '你好'},
        {'start': 1.0, 'end': 2.0, 'text': '   '},
        {'start': 2.0, 'end': 3.0, 'text': '世界'},
    ])
    # Empty segment must be removed and renumbered.
    assert validate_srt_text(srt) == 2
    assert '你好' in srt and '世界' in srt


def test_srt_valid_start_end():
    srt = normalize_segments_to_srt(_sample_segments())
    assert validate_srt_text(srt) == 3
    # Broken overlap must be rejected on re-parse.
    bad = '1\n00:00:01,000 --> 00:00:05,000\n你好\n\n2\n00:00:02,000 --> 00:00:04,000\n世界\n'
    with pytest.raises(RuntimeError, match='INVALID_SRT_FORMAT'):
        validate_srt_text(bad)
    # start >= end must be rejected.
    bad2 = '1\n00:00:02,000 --> 00:00:01,000\n你好\n'
    with pytest.raises(RuntimeError, match='INVALID_SRT_FORMAT'):
        validate_srt_text(bad2)


def test_long_chinese_segment_is_split():
    long_text = '我在姐姐最难的时候和他断绝关系，公司濒临破产的时候我没有帮忙，那年父母离世我非常难过，今天我们要说的是这个很长的故事。'
    assert len(long_text) > 48
    srt = normalize_segments_to_srt([{'start': 0.0, 'end': 10.0, 'text': long_text}])
    count = validate_srt_text(srt)
    assert count >= 2, f'long segment should split, got {count}'
    for block in srt.strip().split('\n\n'):
        body = '\n'.join(block.split('\n')[2:])
        for line in body.split('\n'):
            assert len(line) <= 24, f'line too long ({len(line)}): {line!r}'


def test_word_timestamps_generate_tighter_cues():
    # Same speech: without words -> fewer cues; with words -> tighter split.
    seg_no_words = [{'start': 0.2, 'end': 3.0, 'text': '那年父母离世，公司濒临破产'}]
    srt_plain = normalize_segments_to_srt(seg_no_words)
    seg_words = [{
        'start': 0.2, 'end': 3.0, 'text': '那年父母离世，公司濒临破产',
        'words': [
            {'start': 0.2, 'end': 0.5, 'word': '那年'},
            {'start': 0.5, 'end': 1.0, 'word': '父母'},
            {'start': 1.0, 'end': 1.666, 'word': '离世，'},
            {'start': 1.666, 'end': 2.2, 'word': '公司'},
            {'start': 2.2, 'end': 2.6, 'word': '濒临'},
            {'start': 2.6, 'end': 3.0, 'word': '破产'},
        ],
    }]
    srt_words = normalize_segments_to_srt(seg_words)
    assert validate_srt_text(srt_plain) >= 1
    assert validate_srt_text(srt_words) == 2
    # Tighter timing from words: first cue ends ~1.666.
    assert '00:00:00,200 --> 00:00:01,666' in srt_words
    assert '00:00:01,666 --> 00:00:03,000' in srt_words


def test_generated_srt_can_be_parsed_again(tmp_path):
    from app.bilibili.srt import write_srt

    srt = normalize_segments_to_srt(_sample_segments())
    # Must look exactly like the required sample shape.
    assert srt.startswith('1\n00:00:00,200 --> 00:00:01,666\n那年父母离世\n\n2\n')
    p = tmp_path / 'source.zh.srt'
    count = write_srt(p, srt)
    assert count == 3
    # File: UTF-8, LF, no BOM.
    data = p.read_bytes()
    assert not data.startswith(b'\xef\xbb\xbf')
    assert b'\r' not in data
    assert data.decode('utf-8')
    # Parse again.
    assert validate_srt_text(p.read_text(encoding='utf-8')) == 3


def test_format_timestamp_comma_and_padding():
    assert format_timestamp(0.2) == '00:00:00,200'
    assert format_timestamp(1.666) == '00:00:01,666'
    assert format_timestamp(63.05) == '00:01:03,050'
