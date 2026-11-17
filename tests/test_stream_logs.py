"""BridgeClient.streamLogs(): the blocking iterator over GET /server/stream."""

import threading
import time

import pytest

from hungerbridge import BridgeClient, HungerBridgeAuthError, MAX_HISTORY

SECRET = "33" * 32
PROTO = {"X-HungerBridge-Protocol": "1"}


@pytest.fixture
def client(fake_server):
    return BridgeClient(fake_server.url, "tok", SECRET)


def test_history_then_live_lines(client, fake_server):
    def handler(h, rec):
        h.start_chunked(PROTO)
        h.chunk("data:old-1\n\ndata:old-2\n\n:history-end\n\n")
        h.chunk("data:\x1b[32m[12:00:00 INFO]: new\x1b[0m\n\n")
        time.sleep(0.2)
        h.end_chunked()

    fake_server.route("GET", "/server/stream", handler)
    lines = list(client.streamLogs(history=2, timeout=5))
    assert lines == ["old-1", "old-2", "\x1b[32m[12:00:00 INFO]: new\x1b[0m"]
    assert "history=2" in fake_server.requests[0].path
    assert fake_server.requests[0].headers["X-Auth-Id"] == "tok"


def test_sanitize_and_history_true(client, fake_server):
    def handler(h, rec):
        h.start_chunked(PROTO)
        h.chunk(":history-end\n\ndata:\x1b[31mline\\nnext\x1b[0m  \n\n")
        h.end_chunked()

    fake_server.route("GET", "/server/stream", handler)
    assert list(client.streamLogs(history=True, timeout=5, sanitize=True)) == ["line\nnext"]
    assert f"history={MAX_HISTORY}" in fake_server.requests[0].path


def test_timeout_ends_iteration_and_disconnects(client, fake_server):
    hold = threading.Event()

    def handler(h, rec):
        h.start_chunked(PROTO)
        h.chunk(":history-end\n\ndata:only\n\n")
        hold.wait(10)

    fake_server.route("GET", "/server/stream", handler)
    t0 = time.monotonic()
    assert list(client.streamLogs(timeout=0.5)) == ["only"]
    assert time.monotonic() - t0 < 4
    hold.set()


def test_break_closes_the_stream(client, fake_server):
    hold = threading.Event()

    def handler(h, rec):
        h.start_chunked(PROTO)
        h.chunk(":history-end\n\ndata:a\n\ndata:b\n\n")
        hold.wait(10)

    fake_server.route("GET", "/server/stream", handler)
    for line in client.streamLogs():
        assert line == "a"
        break
    hold.set()


def test_errors_are_raised_typed(client, fake_server):
    fake_server.json_route("GET", "/server/stream", {"ok": False, "error": "unauthorized", "reason": "token_expired"},
                           status=401)
    with pytest.raises(HungerBridgeAuthError) as error:
        list(client.streamLogs(timeout=5, reconnect=True))
    assert error.value.reason == "token_expired"
    assert len(fake_server.requests) == 1  # permanent errors are not retried


def test_reconnect_continues_after_server_restart(client, fake_server):
    state = {"n": 0}

    def handler(h, rec):
        state["n"] += 1
        h.start_chunked(PROTO)
        h.chunk(f":history-end\n\ndata:connection-{state['n']}\n\n")
        h.end_chunked()

    fake_server.route("GET", "/server/stream", handler)
    seen = []
    for line in client.streamLogs(timeout=5, reconnect=True):
        seen.append(line)
        if len(seen) == 2:
            break
    assert seen == ["connection-1", "connection-2"]


def test_independent_streams(client, fake_server):
    first, second = client.openStream(), client.openStream()
    assert first is not second and first is not client.stream
