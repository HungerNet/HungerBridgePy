import time
import warnings

import pytest

from hungerbridge.auth import HEADER_NONCE, HEADER_SIGNATURE, Credentials, sign, string_to_sign
from hungerbridge.exceptions import (
    HungerBridgeAuthError,
    HungerBridgeConnectionError,
    HungerBridgeError,
    HungerBridgeForbidden,
    HungerBridgeNotFound,
    HungerBridgeRateLimit,
)
from hungerbridge.transport import Transport

SECRET = '11' * 32


def make(server, **kw):
    sleeps = []
    t = Transport(server.url, Credentials('tok', SECRET), sleep=sleeps.append, **kw)
    return t, sleeps


def test_get_is_signed_with_empty_body(fake_server):
    fake_server.json_route('GET', '/ping', {'ok': True})
    t, _ = make(fake_server)
    assert t.get('ping') == {'ok': True}
    rec = fake_server.requests[0]
    msg = string_to_sign('GET', '/ping', rec.headers['X-Auth-Timestamp'], rec.headers['X-Auth-Nonce'], '')
    assert rec.headers[HEADER_SIGNATURE] == sign(bytes.fromhex(SECRET), msg)
    assert rec.body == b''


def test_post_sends_canonical_body_that_was_signed(fake_server):
    fake_server.json_route('POST', '/server/log', {'ok': True})
    t, _ = make(fake_server)
    t.post('server/log', {'message': 'hé \U0001F600', 'level': 'info'})
    rec = fake_server.requests[0]
    body = rec.body.decode('ascii')
    assert body == '{"level":"info","message":"h\\u00e9 \\ud83d\\ude00"}'
    assert rec.json() == {'level': 'info', 'message': 'hé \U0001F600'}
    msg = string_to_sign('POST', '/server/log', rec.headers['X-Auth-Timestamp'], rec.headers['X-Auth-Nonce'], body)
    assert rec.headers[HEADER_SIGNATURE] == sign(bytes.fromhex(SECRET), msg)


def test_retry_on_429_uses_fresh_nonce_and_retry_after(fake_server):
    # Regression: retries used to resend the same nonce, which the server rejects as a replay.
    calls = {'n': 0}

    def handler(h, rec):
        calls['n'] += 1
        if calls['n'] < 3:
            h.send_json(429, {'ok': False, 'error': 'rate_limited'}, {'Retry-After': '2'})
        else:
            h.send_json(200, {'ok': True})

    fake_server.route('GET', '/ping', handler)
    t, sleeps = make(fake_server, max_retries=3, retry_backoff_sec=0.5)
    assert t.get('ping') == {'ok': True}
    nonces = [r.headers[HEADER_NONCE] for r in fake_server.requests]
    assert len(nonces) == 3 and len(set(nonces)) == 3
    assert sleeps == [2.0, 2.0]  # max(backoff*(attempt+1), Retry-After)


def test_rate_limit_raises_with_retry_after(fake_server):
    fake_server.json_route('GET', '/ping', {'ok': False, 'error': 'rate_limited'}, status=429, headers={'Retry-After': '7'})
    t, _ = make(fake_server)
    with pytest.raises(HungerBridgeRateLimit) as info:
        t.get('ping')
    assert info.value.retry_after == 7
    assert info.value.status == 429


def test_rate_limit_swallowed(fake_server):
    fake_server.json_route('GET', '/ping', {'ok': False}, status=429)
    t, _ = make(fake_server, swallow_rate_limit=True)
    assert t.get('ping') is None


@pytest.mark.parametrize(
    'status,exc',
    [(401, HungerBridgeAuthError), (403, HungerBridgeForbidden), (404, HungerBridgeNotFound), (500, HungerBridgeError)],
)
def test_error_mapping(fake_server, status, exc):
    fake_server.json_route('GET', '/x', {'ok': False, 'error': 'code_x', 'message': 'm', 'reason': 'nonce_replay'}, status=status)
    t, _ = make(fake_server)
    with pytest.raises(exc) as info:
        t.get('x')
    err = info.value
    assert isinstance(err, HungerBridgeError)
    assert err.status == status and err.code == 'code_x' and err.reason == 'nonce_replay'
    assert str(err).startswith(f'HungerBridge error {status}:')


def test_connection_refused_is_wrapped():
    t = Transport('http://127.0.0.1:9', None, timeout=(0.5, 0.5))
    with pytest.raises(HungerBridgeConnectionError):
        t.get('ping')


def test_read_timeout_is_wrapped(fake_server):
    fake_server.route('GET', '/slow', lambda h, r: (time.sleep(1.0), h.send_json(200, {})))
    t, _ = make(fake_server, timeout=(1.0, 0.2))
    with pytest.raises(HungerBridgeConnectionError):
        t.get('slow')


def test_protocol_version_mismatch_warns_once(fake_server):
    fake_server.json_route('GET', '/ping', {'ok': True}, headers={'X-HungerBridge-Protocol': '2'})
    t, _ = make(fake_server)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        t.get('ping')
        t.get('ping')
    assert t.server_protocol_version == 2
    assert len([w for w in caught if 'protocol v2' in str(w.message)]) == 1


def test_non_json_body_is_returned_as_text(fake_server):
    def handler(h, rec):
        data = b'plain'
        h.send_response(200)
        h.send_header('Content-Length', str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    fake_server.route('GET', '/text', handler)
    t, _ = make(fake_server)
    assert t.get('text') == 'plain'


def test_static_delay_throttles(fake_server):
    fake_server.json_route('GET', '/ping', {'ok': True})
    t, sleeps = make(fake_server, static_delay=10.0)
    t.get('ping')
    t.get('ping')
    assert len(sleeps) == 1 and 9.0 < sleeps[0] <= 10.0
