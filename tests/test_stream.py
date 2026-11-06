import threading
import time

from hungerbridge.stream import Stream, _split_lines, parse_sse

PROTO = {"X-HungerBridge-Protocol": "1"}


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- framing ------------------------------------------------------------------------


def test_split_lines_handles_chunk_boundaries_and_line_endings():
    chunks = [b"data:a\r", b"\ndata:b\n", b"\ndata:c\rdata:d\r\n\r\n", b"trailing-no-eol"]
    assert list(_split_lines(chunks)) == ["data:a", "data:b", "", "data:c", "data:d", ""]


def test_split_lines_does_not_split_on_unicode_separators():
    assert list(_split_lines(["data:a b\x0cc\n".encode("utf-8")])) == ["data:a b\x0cc"]


def test_split_lines_handles_split_utf8_sequences():
    raw = "data:é\U0001F600\n".encode("utf-8")
    chunks = [raw[i : i + 1] for i in range(len(raw))]
    assert list(_split_lines(chunks)) == ["data:é\U0001F600"]


def test_parse_sse_strips_exactly_one_space_and_preserves_indentation():
    # Regression: the old client lstrip()'d data, destroying stack-trace indentation.
    lines = ["data:\tat Foo.bar(Foo.java:1)", "", "data:  two spaces", "", ": keepalive", ":history-end", "data: x", ""]
    assert list(parse_sse(lines)) == [
        ("data", "\tat Foo.bar(Foo.java:1)"),
        ("data", " two spaces"),
        ("comment", "keepalive"),
        ("comment", "history-end"),
        ("data", "x"),
    ]


def test_parse_sse_joins_multi_line_data_and_ignores_other_fields():
    assert list(parse_sse(["event: log", "id: 1", "data:a", "data:b", "retry: 5", ""])) == [("data", "a\nb")]


def test_sanitize_and_timestamp():
    line = "\x1b[32m[12:34:56 INFO]: hi\\nthere  "
    clean = Stream.sanitize(line)
    assert clean == "[12:34:56 INFO]: hi\nthere"
    assert Stream.extractTimestamp(clean) == "12:34:56"
    assert Stream.extractTimestamp("no ts") is None


# -- live behaviour against a fake server ------------------------------------------------


def sse_route(server, script):
    """script(handler, request, connection_index)"""
    state = {"n": 0}

    def handler(h, rec):
        idx = state["n"]
        state["n"] += 1
        script(h, rec, idx)

    server.route("GET", "/server/stream", handler)
    return state


def test_history_marker_splits_history_from_live(fake_server):
    release = threading.Event()

    def script(h, rec, idx):
        h.start_chunked(PROTO)
        for i in range(3):
            h.chunk(f"data:old-{i}\n\n")
        h.chunk(":history-end\n\n")
        release.wait(5)
        h.chunk("data:new-0\n\n")
        h.end_chunked()

    sse_route(fake_server, script)
    histories, live = [], []
    s = Stream(fake_server.url, {}, history_handler=histories.append, new_log_handler=live.append)
    s.connect(history=3)
    assert wait_for(lambda: histories)
    assert histories == [["old-0", "old-1", "old-2"]]
    assert live == []
    release.set()
    assert wait_for(lambda: live == ["new-0"])
    s.disconnect()
    assert "history=3" in fake_server.requests[0].path


def test_without_history_every_line_is_live_immediately(fake_server):
    # Regression: the old client buffered the first `keepalive` (15) seconds as history.
    def script(h, rec, idx):
        h.start_chunked(PROTO)
        h.chunk(":history-end\n\n")
        h.chunk("data:first\n\n")
        time.sleep(0.5)
        h.end_chunked()

    sse_route(fake_server, script)
    live = []
    s = Stream(fake_server.url, {}, new_log_handler=live.append)
    s.connect()
    assert wait_for(lambda: live == ["first"], timeout=2.0)
    s.disconnect()


def test_legacy_server_without_marker_uses_time_window(fake_server):
    def script(h, rec, idx):
        h.start_chunked()  # no protocol header => legacy
        h.chunk("data:old\n\n")
        time.sleep(1.3)
        h.chunk("data:new\n\n")
        time.sleep(0.3)
        h.end_chunked()

    sse_route(fake_server, script)
    histories, live = [], []
    s = Stream(fake_server.url, {}, history_handler=histories.append, new_log_handler=live.append)
    s.connect(history=10)
    assert wait_for(lambda: live == ["new"], timeout=5.0)
    assert histories == [["old"]]
    s.disconnect()


def test_default_handlers_accumulate(fake_server):
    def script(h, rec, idx):
        h.start_chunked(PROTO)
        h.chunk("data:[01:02:03 INFO]: a\n\n:history-end\n\ndata:[01:02:04 INFO]: b\n\n")
        time.sleep(0.3)
        h.end_chunked()

    sse_route(fake_server, script)
    s = Stream(fake_server.url, {})
    s.connect(history=1)
    assert wait_for(lambda: len(s.getRaw()) == 2)
    assert s.getTimestamped() == {"01:02:03": "[01:02:03 INFO]: a", "01:02:04": "[01:02:04 INFO]: b"}
    s.disconnect()


def test_headers_provider_called_per_connection(fake_server):
    calls = []

    def provider():
        calls.append(1)
        return {"X-Auth-Id": f"tok-{len(calls)}"}

    def script(h, rec, idx):
        h.start_chunked(PROTO)
        h.chunk(":history-end\n\n")
        h.end_chunked()

    sse_route(fake_server, script)
    s = Stream(fake_server.url, provider)
    s.connect(reconnect=True, reconnect_delay=0.05)
    assert wait_for(lambda: len(fake_server.requests) >= 2)
    s.disconnect()
    ids = [r.headers.get("X-Auth-Id") for r in fake_server.requests[:2]]
    assert ids == ["tok-1", "tok-2"]


def test_reconnect_replays_only_missed_history(fake_server):
    def script(h, rec, idx):
        h.start_chunked(PROTO)
        if idx == 0:
            h.chunk("data:a\n\ndata:b\n\n:history-end\n\ndata:c\n\n")
            h.end_chunked()  # server goes away
        else:
            # history after reconnect overlaps with what was already seen
            h.chunk("data:b\n\ndata:c\n\ndata:d\n\ndata:e\n\n:history-end\n\n")
            time.sleep(0.5)
            h.end_chunked()

    sse_route(fake_server, script)
    histories, live = [], []
    s = Stream(fake_server.url, {}, history_handler=histories.append, new_log_handler=live.append)
    s.connect(history=10, reconnect=True, reconnect_delay=0.05)
    assert wait_for(lambda: live == ["c", "d", "e"], timeout=5.0), live
    assert histories == [["a", "b"]]
    s.disconnect()


def test_reconnect_honours_retry_after_on_429(fake_server):
    def script(h, rec, idx):
        if idx == 0:
            h.send_json(429, {"ok": False, "error": "rate_limited"}, {"Retry-After": "1"})
            return
        h.start_chunked(PROTO)
        h.chunk(":history-end\n\ndata:after-429\n\n")
        time.sleep(0.5)
        h.end_chunked()

    sse_route(fake_server, script)
    live = []
    s = Stream(fake_server.url, {}, new_log_handler=live.append)
    t0 = time.monotonic()
    s.connect(reconnect=True, reconnect_delay=0.05)
    assert wait_for(lambda: live == ["after-429"], timeout=5.0)
    assert time.monotonic() - t0 >= 0.9  # waited for Retry-After, not the 50 ms back-off
    s.disconnect()


def test_http_error_is_recorded_and_thread_ends(fake_server):
    fake_server.json_route("GET", "/server/stream", {"ok": False, "error": "unauthorized"}, status=401)
    s = Stream(fake_server.url, {})
    s.connect()
    assert wait_for(lambda: not s.isConnected())
    assert s.last_error is not None and "401" in str(s.last_error)


def test_disconnect_stops_blocking_stream(fake_server):
    hold = threading.Event()

    def script(h, rec, idx):
        h.start_chunked(PROTO)
        h.chunk(":history-end\n\n")
        hold.wait(10)

    sse_route(fake_server, script)
    s = Stream(fake_server.url, {})
    s.connect()
    assert s.wait_until_connected(5)
    assert s.isConnected()
    worker = s._thread
    t0 = time.monotonic()
    s.disconnect(timeout=2.0)
    # Regression: disconnect() used to block until the read timeout (45 s) because closing
    # the response waits for the reader's lock. It is now bounded by `timeout`.
    assert time.monotonic() - t0 < 3.0
    assert not s.isConnected()
    hold.set()
    # The socket is shut down; on Windows a blocked recv may only return once the server
    # sends/ends, after which the daemon worker exits because the stop flag is set.
    worker.join(5)
    assert not worker.is_alive()
