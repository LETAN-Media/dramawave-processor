import pytest

from app.bilibili.resolver import BVID_RE, _validate_host


def test_bvid_regex():
    url = 'https://www.bilibili.com/video/BV1xx411c7mD/'
    assert BVID_RE.search(url).group(1) == 'BV1xx411c7mD'


def test_host_guard():
    _validate_host('https://www.bilibili.com/video/BV1xx411c7mD')
    _validate_host('https://b23.tv/abc')
    with pytest.raises(ValueError):
        _validate_host('https://example.com/video/BV1xx411c7mD')
