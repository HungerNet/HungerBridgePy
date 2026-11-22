"""BridgeClient.streamLogs() and openStream()."""

import threading
import time

import pytest

from hungerbridge import BridgeClient, HungerBridgeAuthError, LogEvent

from conftest import SECRET, sse_event, sse_ready


@pytest.fixture
def client(fake_server):
    return BridgeClient(fake_server.url, token_id="tok", token_secret=SECRET)


def test_history_then_live_events(client, fake_server):
    def handler(h, rec):
        h.start_chunked()
        h.chunk(sse_event(1, "old-1") + sse_event(2, "old-2") + sse_ready(2, 2))
        h.chunk(sse_event(3, "new", logger="CustomLogger", thread="CustomThread", level="CUSTOMLEVEL"))
        time.sleep(0.2)
        h.end_chunked()

    fake_server.route("GET", "/server/stream", handler)
    events = list(client.streamLogs(history=2, timeout=5, reconnect=False))
    assert all(isinstance(e, LogEvent) for e in events)
    assert [e.message for e in events] == ["old-1", "old-2", "new"]
    assert str(events[2]).endswith("[CustomThread/CUSTOMLEVEL]: [CustomLogger] new")
    assert "history=2" in fake_server.requests[0].path
    assert fake_server.requests[0].headers["X-Auth-Id"] == "tok"


def test_timeout_ends_iteration_and_disconnects(client, fake_server):
    hold = threading.Event()

    def handler(h, rec):
        h.start_chunked()
        h.chunk(sse_ready(0) + sse_event(1, "only"))
        hold.wait(10)

    fake_server.route("GET", "/server/stream", handler)
    t0 = time.monotonic()
    assert [e.message for e in client.streamLogs(timeout=0.5)] == ["only"]
    assert time.monotonic() - t0 < 4
    hold.set()


def test_break_closes_the_stream(client, fake_server):
    hold = threading.Event()

    def handler(h, rec):
        h.start_chunked()
        h.chunk(sse_ready(0) + sse_event(1, "a") + sse_event(2, "b"))
        hold.wait(10)

    fake_server.route("GET", "/server/stream", handler)
    for event in client.streamLogs():
        assert event.message == "a"
        break
    hold.set()


def test_errors_are_raised_typed_and_not_retried(client, fake_server):
    fake_server.json_route("GET", "/server/stream", {"ok": False, "error": "unauthorized", "reason": "token_expired"}, status=401)
    with pytest.raises(HungerBridgeAuthError) as error:
        list(client.streamLogs(timeout=5))
    assert error.value.reason == "token_expired"
    assert len(fake_server.requests) == 1


def test_reconnect_continues_after_server_restart(client, fake_server):
    state = {"n": 0}

    def handler(h, rec):
        state["n"] += 1
        epoch = f"e{state['n']}"
        h.start_chunked()
        h.chunk(sse_event(1, f"connection-{state['n']}", epoch=epoch) + sse_ready(1, 1, epoch=epoch))
        h.end_chunked()

    fake_server.route("GET", "/server/stream", handler)
    seen = []
    for event in client.streamLogs(history=1, timeout=5):
        seen.append(event.message)
        if len(seen) == 2:
            break
    assert seen == ["connection-1", "connection-2"]


def test_independent_streams(client):
    assert client.openStream() is not client.openStream()
    assert not hasattr(client, "stream")
