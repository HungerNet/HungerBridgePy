"""Stream (protocol v2): SSE framing, history/ready, resume by id, gaps, errors, lifecycle."""

import threading
import time

import pytest

from hungerbridge import HungerBridgeAuthError, HungerBridgeConnectionError, HungerBridgeForbidden, HungerBridgeProtocolError
from hungerbridge.stream import LogEvent, Stream, historyParam, parseSse, splitLines

from conftest import sse_event, sse_ready


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def messages(events):
    return [e.message for e in events]


# -- framing ------------------------------------------------------------------------


def test_split_lines_handles_chunk_boundaries_and_line_endings():
    chunks = [b"data:a\r", b"\ndata:b\n", b"\ndata:c\rdata:d\r\n\r\n", b"trailing-no-eol"]
    assert list(splitLines(chunks)) == ["data:a", "data:b", "", "data:c", "data:d", ""]


def test_split_lines_keeps_unicode_separators_and_split_utf8():
    assert list(splitLines(["data:a b\x0cc\n".encode("utf-8")])) == ["data:a b\x0cc"]
    raw = "data:é\U0001F600\n".encode("utf-8")
    assert list(splitLines([raw[i:i + 1] for i in range(len(raw))])) == ["data:é\U0001F600"]


def test_parse_sse_events():
    lines = ["retry: 2000", "", "id: e.1", "event: log", "data: {\"a\":1}", "", ": keepalive", "",
             "event: ready", "data:x", "data: y", "", "event: nodata", ""]
    events = list(parseSse(lines))
    assert [(e.event, e.id, e.data) for e in events] == [("log", "e.1", '{"a":1}'), ("ready", None, "x\ny")]


def test_log_event_from_json_and_str():
    event = LogEvent.fromJson({"seq": 3, "time": 5, "level": "INFO", "thread": "t", "logger": "L",
                               "message": "C:\\new", "line": "[00:00:00] [t/INFO]: [L] C:\\new"}, "ep")
    assert str(event) == "[00:00:00] [t/INFO]: [L] C:\\new"
    assert event.message == "C:\\new"  # protocol v1 turned a literal backslash-n into a newline
    assert event.id == "ep.3"
    with pytest.raises(HungerBridgeProtocolError):
        LogEvent.fromJson({"seq": 1}, "ep")


def test_history_param():
    assert historyParam(None) is None and historyParam(0) is None and historyParam(False) is None
    assert historyParam(True) == "all" and historyParam(5) == "5"
    with pytest.raises(ValueError):
        historyParam(-1)


# -- behaviour against a fake server -------------------------------------------------


def sse_route(server, script):
    """script(handler, request, connection_index)"""
    state = {"n": 0}

    def handler(h, rec):
        idx = state["n"]
        state["n"] += 1
        script(h, rec, idx)

    server.route("GET", "/server/stream", handler)
    return state


def test_history_then_ready_then_live(fake_server):
    release = threading.Event()

    def script(h, rec, idx):
        h.start_chunked()
        h.chunk("retry: 2000\n\n" + sse_event(1, "old-1") + sse_event(2, "old-2") + sse_ready(2, 2))
        release.wait(5)
        h.chunk(sse_event(3, "new"))
        h.end_chunked()

    sse_route(fake_server, script)
    histories, live = [], []
    s = Stream(fake_server.url, dict, on_history=histories.append, on_event=live.append)
    s.connect(history=2, reconnect=False)
    assert s.waitUntilConnected(5)
    assert [messages(h) for h in histories] == [["old-1", "old-2"]] and live == []
    release.set()
    assert wait_for(lambda: messages(live) == ["new"])
    s.disconnect()
    assert "history=2" in fake_server.requests[0].path
    assert s.epoch == "e1" and s.last_event_id == "e1.3"


def test_without_on_history_replayed_events_go_to_on_event(fake_server):
    def script(h, rec, idx):
        h.start_chunked()
        h.chunk(sse_event(1, "old") + sse_ready(1, 1) + sse_event(2, "new"))
        time.sleep(0.3)
        h.end_chunked()

    sse_route(fake_server, script)
    live = []
    s = Stream(fake_server.url, dict, on_event=live.append)
    s.connect(history=True, reconnect=False)
    assert wait_for(lambda: messages(live) == ["old", "new"])
    assert "history=all" in fake_server.requests[0].path
    s.disconnect()


def test_reconnect_resumes_after_the_last_event_exactly(fake_server):
    # Regression: v1 de-duplicated by line text, dropping repeated identical lines.
    def script(h, rec, idx):
        h.start_chunked()
        if idx == 0:
            h.chunk(sse_ready(0) + sse_event(1, "same") + sse_event(2, "same"))
            h.end_chunked()   # server goes away
        else:
            assert rec.headers.get("Last-Event-ID") == "e1.2"
            assert "history" not in rec.path
            h.chunk(sse_event(3, "same") + sse_ready(3, 1) + sse_event(4, "after"))
            time.sleep(0.5)
            h.end_chunked()

    sse_route(fake_server, script)
    live = []
    s = Stream(fake_server.url, dict, on_event=live.append)
    s.connect(history=10, reconnect=True, reconnect_delay=0.05)
    assert wait_for(lambda: [e.seq for e in live] == [1, 2, 3, 4], timeout=5), [e.seq for e in live]
    assert messages(live) == ["same", "same", "same", "after"]
    s.disconnect()


def test_resume_point_is_known_before_any_event(fake_server):
    def script(h, rec, idx):
        h.start_chunked()
        if idx == 0:
            h.chunk(sse_ready(41))
            h.end_chunked()
        else:
            assert rec.headers.get("Last-Event-ID") == "e1.41"
            h.chunk(sse_ready(41))
            time.sleep(0.3)
            h.end_chunked()

    state = sse_route(fake_server, script)
    s = Stream(fake_server.url, dict)
    s.connect(reconnect=True, reconnect_delay=0.05)
    assert wait_for(lambda: state["n"] >= 2)
    s.disconnect()


def test_gaps_and_restarts_are_reported(fake_server, caplog):
    def script(h, rec, idx):
        h.start_chunked()
        if idx == 0:
            h.chunk(sse_ready(5, epoch="e1"))
            h.end_chunked()
        else:
            h.chunk(sse_event(1, "after restart", epoch="e2") + sse_ready(1, 1, epoch="e2", gap=True))
            time.sleep(0.5)
            h.end_chunked()

    sse_route(fake_server, script)
    live = []
    s = Stream(fake_server.url, dict, on_event=live.append)
    with caplog.at_level("INFO", logger="hungerbridge"):
        s.connect(reconnect=True, reconnect_delay=0.05)
        assert wait_for(lambda: s.gaps == 1)
    assert messages(live) == ["after restart"] and s.epoch == "e2"
    assert any("restarted" in r.message for r in caplog.records)
    assert any("lost events" in r.message for r in caplog.records)
    s.disconnect()


def test_headers_provider_called_per_connection(fake_server):
    calls = []

    def provider():
        calls.append(1)
        return {"X-Auth-Id": f"tok-{len(calls)}"}

    def script(h, rec, idx):
        h.start_chunked()
        h.chunk(sse_ready(0))
        h.end_chunked()

    sse_route(fake_server, script)
    s = Stream(fake_server.url, provider)
    s.connect(reconnect=True, reconnect_delay=0.05)
    assert wait_for(lambda: len(fake_server.requests) >= 2)
    s.disconnect()
    assert [r.headers.get("X-Auth-Id") for r in fake_server.requests[:2]] == ["tok-1", "tok-2"]


def test_reconnect_honours_retry_after_on_429(fake_server):
    def script(h, rec, idx):
        if idx == 0:
            h.send_json(429, {"ok": False, "error": "rate_limited"}, {"Retry-After": "1"})
            return
        h.start_chunked()
        h.chunk(sse_ready(0) + sse_event(1, "after-429"))
        time.sleep(0.5)
        h.end_chunked()

    sse_route(fake_server, script)
    live = []
    s = Stream(fake_server.url, dict, on_event=live.append)
    t0 = time.monotonic()
    s.connect(reconnect=True, reconnect_delay=0.05)
    assert wait_for(lambda: messages(live) == ["after-429"])
    assert time.monotonic() - t0 >= 0.9
    s.disconnect()


@pytest.mark.parametrize("status, exc", [(401, HungerBridgeAuthError), (403, HungerBridgeForbidden)])
def test_permanent_errors_end_the_stream(fake_server, status, exc):
    fake_server.json_route("GET", "/server/stream", {"ok": False, "error": "x", "reason": "token_revoked"}, status=status)
    closed = []
    s = Stream(fake_server.url, dict, on_close=closed.append)
    s.connect(reconnect=True, reconnect_delay=0.05)
    assert wait_for(lambda: closed)
    time.sleep(0.2)
    assert len(fake_server.requests) == 1
    assert isinstance(closed[0], exc) and closed[0] is s.last_error


def test_old_server_is_a_protocol_error(fake_server):
    def script(h, rec, idx):
        h.start_chunked(protocol="1")
        h.chunk("data:legacy line\n\n")
        h.end_chunked()

    sse_route(fake_server, script)
    closed = []
    s = Stream(fake_server.url, dict, on_close=closed.append)
    s.connect(reconnect=True, reconnect_delay=0.05)
    assert wait_for(lambda: closed)
    assert isinstance(closed[0], HungerBridgeProtocolError)


def test_unreachable_server_is_a_connection_error():
    closed = []
    s = Stream("http://127.0.0.1:9", dict, on_close=closed.append, connect_timeout=1)
    s.connect(reconnect=False)
    assert wait_for(lambda: closed)
    assert isinstance(closed[0], HungerBridgeConnectionError)


def test_handler_errors_do_not_kill_the_stream(fake_server):
    def script(h, rec, idx):
        h.start_chunked()
        h.chunk(sse_ready(0) + sse_event(1, "boom") + sse_event(2, "ok"))
        time.sleep(0.3)
        h.end_chunked()

    sse_route(fake_server, script)
    seen = []

    def handler(event):
        seen.append(event.message)
        if event.message == "boom":
            raise RuntimeError("handler bug")

    s = Stream(fake_server.url, dict, on_event=handler)
    s.connect(reconnect=False)
    assert wait_for(lambda: seen == ["boom", "ok"])
    s.disconnect()


def test_disconnect_is_prompt_and_reports_a_clean_close(fake_server):
    hold = threading.Event()

    def script(h, rec, idx):
        h.start_chunked()
        h.chunk(sse_ready(0))
        hold.wait(10)

    sse_route(fake_server, script)
    closed = []
    s = Stream(fake_server.url, dict, on_close=closed.append)
    s.connect()
    assert s.waitUntilConnected(5) and s.isConnected()
    t0 = time.monotonic()
    s.disconnect(timeout=2.0)
    assert time.monotonic() - t0 < 3.0
    hold.set()
    assert wait_for(lambda: closed == [None])
    assert not s.isConnected()


def test_connect_twice_starts_one_worker(fake_server):
    hold = threading.Event()

    def script(h, rec, idx):
        h.start_chunked()
        h.chunk(sse_ready(0))
        hold.wait(5)

    sse_route(fake_server, script)
    s = Stream(fake_server.url, dict)
    threads = [threading.Thread(target=s.connect) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert s.waitUntilConnected(5)
    time.sleep(0.2)
    assert len(fake_server.requests) == 1
    hold.set()
    s.disconnect()
