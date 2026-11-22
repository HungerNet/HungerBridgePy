"""Shared fixtures: a tiny programmable HTTP server (real sockets, HTTP/1.1, chunked).

Every response carries ``X-HungerBridge-Protocol: 2`` like the real server, unless a test
passes ``protocol=None`` (or another version).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pytest

DATA_DIR = Path(__file__).parent / "data"
PROTOCOL = "2"
SECRET = "ab" * 32


@pytest.fixture(scope="session")
def vectors() -> dict:
    return json.loads((DATA_DIR / "protocol-vectors.json").read_text(encoding="utf-8"))


class Recorded:
    def __init__(self, method: str, path: str, headers: dict, body: bytes):
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8")) if self.body else None


Handler = Callable[["FakeRequestHandler", Recorded], None]


class FakeRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "FakeServer"

    def log_message(self, *args):  # silence
        pass

    def _dispatch(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0]
        rec = Recorded(self.command, self.path, {k: v for k, v in self.headers.items()}, body)
        self.server.requests.append(rec)
        handler = self.server.routes.get((self.command, path)) or self.server.routes.get(("*", path))
        if handler is None:
            self.send_json(404, {"ok": False, "error": "not_found", "message": "No route"})
            return
        handler(self, rec)

    do_GET = _dispatch
    do_POST = _dispatch

    def send_json(self, status: int, obj, headers: Optional[Dict[str, str]] = None, protocol: Optional[str] = PROTOCOL):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if protocol is not None:
            self.send_header("X-HungerBridge-Protocol", protocol)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def start_chunked(self, headers: Optional[Dict[str, str]] = None, protocol: Optional[str] = PROTOCOL):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=UTF-8")
        if protocol is not None:
            self.send_header("X-HungerBridge-Protocol", protocol)
        self.send_header("Transfer-Encoding", "chunked")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def chunk(self, text: str):
        data = text.encode("utf-8")
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def end_chunked(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeRequestHandler)
        self.routes: Dict[Tuple[str, str], Handler] = {}
        self.requests: List[Recorded] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def route(self, method: str, path: str, handler: Handler):
        self.routes[(method, path)] = handler

    def json_route(self, method: str, path: str, obj, status: int = 200, headers=None, protocol=PROTOCOL):
        self.route(method, path, lambda h, r: h.send_json(status, obj, headers, protocol))


@pytest.fixture
def fake_server():
    server = FakeServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def sse_event(seq: int, message: str, epoch: str = "e1", level: str = "INFO", logger: str = "Test", thread: str = "main") -> str:
    """One protocol-v2 ``log`` event as the server writes it."""
    data = {"seq": seq, "time": 1_700_000_000_000 + seq, "level": level, "thread": thread, "logger": logger,
            "message": message, "line": f"[00:00:{seq % 60:02d}] [{thread}/{level}]: [{logger}] {message}"}
    return f"id: {epoch}.{seq}\nevent: log\ndata: {json.dumps(data)}\n\n"


def sse_ready(seq: int, replayed: int = 0, epoch: str = "e1", gap: bool = False) -> str:
    """The ``ready`` event that ends the replay."""
    data = {"epoch": epoch, "seq": seq, "replayed": replayed, "gap": gap}
    return f"event: ready\ndata: {json.dumps(data)}\n\n"
