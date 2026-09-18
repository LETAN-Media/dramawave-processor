"""Render API client tests (transport mocked; retry/headers verified)."""

import json
import urllib.error


def _resp(payload):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(payload).encode()
    return FakeResp()


def test_client_sends_bearer_token(monkeypatch):
    import app.clients.dramawave_api as client
    from app.config import settings

    seen = {}
    monkeypatch.setattr(settings, 'dramawave_api_token', 'tok123')

    def fake_urlopen(req, timeout=None):
        seen['auth'] = req.get_header('Authorization')
        return _resp({'items': []})

    monkeypatch.setattr(client.urllib.request, 'urlopen', fake_urlopen)
    client.search('x')
    assert seen['auth'] == 'Bearer tok123'


def test_client_no_token_no_header(monkeypatch):
    import app.clients.dramawave_api as client
    from app.config import settings

    seen = {}
    monkeypatch.setattr(settings, 'dramawave_api_token', '')

    def fake_urlopen(req, timeout=None):
        seen['auth'] = req.get_header('Authorization')
        return _resp({'items': []})

    monkeypatch.setattr(client.urllib.request, 'urlopen', fake_urlopen)
    client.search('x')
    assert seen['auth'] is None


def test_cold_start_retry(monkeypatch):
    """Two timeouts (cold start) then success: bounded retries recover."""
    import app.clients.dramawave_api as client

    calls = {'n': 0}

    def fake_urlopen(req, timeout=None):
        calls['n'] += 1
        if calls['n'] < 3:
            raise TimeoutError('timed out')
        return _resp({'ok': True})

    monkeypatch.setattr(client.urllib.request, 'urlopen', fake_urlopen)
    monkeypatch.setattr('app.clients.dramawave_api.time.sleep', lambda s: None)
    out = client._request('GET', '/health')
    assert out == {'ok': True} and calls['n'] == 3


def test_unauthorized_mapping(monkeypatch):
    import app.clients.dramawave_api as client
    from app.clients.dramawave_api import DramaApiError

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, 'unauth', {}, None)

    monkeypatch.setattr(client.urllib.request, 'urlopen', fake_urlopen)
    try:
        client._request('GET', '/v1/search', params={'q': 'x'})
        raise AssertionError('should raise')
    except DramaApiError as exc:
        assert exc.code == 'DRAMA_API_UNAUTHORIZED'


def test_retry_after_respected(monkeypatch):
    import app.clients.dramawave_api as client
    from app.clients.dramawave_api import DramaApiError

    sleeps = []

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, 'rl', {'Retry-After': '1'}, None)

    monkeypatch.setattr(client.urllib.request, 'urlopen', fake_urlopen)
    monkeypatch.setattr('app.clients.dramawave_api.time.sleep', lambda s: sleeps.append(s))
    monkeypatch.setattr('app.clients.dramawave_api.random.uniform', lambda a, b: 0)
    try:
        client._request('GET', '/v1/search', params={'q': 'x'})
        raise AssertionError('should raise')
    except DramaApiError as exc:
        assert exc.code == 'DRAMA_API_UNAVAILABLE'
    assert sleeps and min(sleeps) >= 1
