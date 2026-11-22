import time

import pytest

from hungerbridge.auth import HEADER_NONCE, HEADER_SIGNATURE, Credentials, sign, stringToSign
from hungerbridge.exceptions import (
    HungerBridgeAuthError,
    HungerBridgeConnectionError,
    HungerBridgeError,
    HungerBridgeForbidden,
    HungerBridgeNotFound,
    HungerBridgeProtocolError,
    HungerBridgeRateLimit,
)
from hungerbridge.transport import Transport

SECRET = "11" * 32


def make(server, **kw):
    sleeps = []
    t = Transport(server.url, Credentials(token_id="tok", token_secret=SECRET), sleep=sleeps.append, **kw)
    return t, sleeps


def test_get_is_signed_with_empty_body(fake_server):
    fake_server.json_route("GET", "/ping", {"ok": True})
    t, _ = make(fake_server)
    assert t.get("ping") == {"ok": True}
    rec = fake_server.requests[0]
    msg = stringToSign("GET", "/ping", rec.headers["X-Auth-Timestamp"], rec.headers["X-Auth-Nonce"], "")
    assert rec.headers[HEADER_SIGNATURE] == sign(bytes.fromhex(SECRET), msg)
    assert rec.body == b""


def test_post_sends_the_canonical_body_that_was_signed(fake_server):
    fake_server.json_route("POST", "/server/log", {"ok": True})
    t, _ = make(fake_server)
    t.post("server/log", {"message": "hé \U0001F600", "level": "info"})
    rec = fake_server.requests[0]
    body = rec.body.decode("ascii")
    assert body == '{"level":"info","message":"h\\u00e9 \\ud83d\\ude00"}'
    msg = stringToSign("POST", "/server/log", rec.headers["X-Auth-Timestamp"], rec.headers["X-Auth-Nonce"], body)
    assert rec.headers[HEADER_SIGNATURE] == sign(bytes.fromhex(SECRET), msg)


def test_retry_on_429_uses_fresh_nonce_and_retry_after(fake_server):
    calls = {"n": 0}

    def handler(h, rec):
        calls["n"] += 1
        if calls["n"] < 3:
            h.send_json(429, {"ok": False, "error": "rate_limited"}, {"Retry-After": "2"})
        else:
            h.send_json(200, {"ok": True})

    fake_server.route("GET", "/ping", handler)
    t, sleeps = make(fake_server, max_retries=3, retry_backoff=0.5)
    assert t.get("ping") == {"ok": True}
    nonces = [r.headers[HEADER_NONCE] for r in fake_server.requests]
    assert len(set(nonces)) == 3
    assert sleeps == [2.0, 2.0]


def test_rate_limit_raises_after_retries(fake_server):
    fake_server.json_route("GET", "/ping", {"ok": False, "error": "rate_limited"}, status=429, headers={"Retry-After": "7"})
    t, sleeps = make(fake_server, max_retries=1)
    with pytest.raises(HungerBridgeRateLimit) as info:
        t.get("ping")
    assert info.value.retry_after == 7 and info.value.status == 429 and sleeps == [7.0]


@pytest.mark.parametrize(
    "status,exc",
    [(401, HungerBridgeAuthError), (403, HungerBridgeForbidden), (404, HungerBridgeNotFound), (500, HungerBridgeError)],
)
def test_error_mapping(fake_server, status, exc):
    fake_server.json_route("GET", "/x", {"ok": False, "error": "code_x", "message": "Something", "reason": "nonce_replay"}, status=status)
    t, _ = make(fake_server)
    with pytest.raises(exc) as info:
        t.get("x")
    err = info.value
    assert err.status == status and err.code == "code_x" and err.reason == "nonce_replay"
    assert str(err) == f"HungerBridge {status} code_x (nonce_replay): Something"


def test_protocol_mismatch_raises(fake_server):
    fake_server.json_route("GET", "/v1", {"ok": True}, protocol="1")
    fake_server.json_route("GET", "/none", {"ok": True}, protocol=None)
    t, _ = make(fake_server)
    with pytest.raises(HungerBridgeProtocolError, match="protocol v1"):
        t.get("v1")
    with pytest.raises(HungerBridgeProtocolError, match="no protocol version"):
        t.get("none")


def test_connection_errors_are_wrapped(fake_server):
    with pytest.raises(HungerBridgeConnectionError):
        Transport("http://127.0.0.1:9", Credentials(token_id="t", token_secret=SECRET), timeout=(0.5, 0.5)).get("ping")
    fake_server.route("GET", "/slow", lambda h, r: (time.sleep(1.0), h.send_json(200, {})))
    t, _ = make(fake_server, timeout=(1.0, 0.2))
    with pytest.raises(HungerBridgeConnectionError):
        t.get("slow")


def test_non_json_body_is_a_protocol_error(fake_server):
    def handler(h, rec):
        data = b"plain"
        h.send_response(200)
        h.send_header("X-HungerBridge-Protocol", "2")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    fake_server.route("GET", "/text", handler)
    t, _ = make(fake_server)
    with pytest.raises(HungerBridgeProtocolError):
        t.get("text")


def test_min_interval_throttles(fake_server):
    fake_server.json_route("GET", "/ping", {"ok": True})
    t, sleeps = make(fake_server, min_interval=10.0)
    t.get("ping")
    t.get("ping")
    assert len(sleeps) == 1 and 9.0 < sleeps[0] <= 10.0


def test_negative_retries_rejected(fake_server):
    with pytest.raises(ValueError):
        make(fake_server, max_retries=-1)
