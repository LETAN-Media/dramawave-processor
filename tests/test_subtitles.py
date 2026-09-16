from app.bilibili.subtitles import cues_to_srt, vtt_to_srt


def test_cues_to_srt():
    text = cues_to_srt([
        {'from': 1.25, 'to': 2.5, 'content': '你好'},
        {'from': 3.0, 'to': 4.1, 'content': '世界'},
    ])
    assert '00:00:01,250 --> 00:00:02,500' in text
    assert '你好' in text
    assert text.count(' --> ') == 2


def test_vtt_to_srt():
    vtt = 'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n你好\n'
    text = vtt_to_srt(vtt)
    assert '00:00:01,000 --> 00:00:02,000' in text
    assert '你好' in text
