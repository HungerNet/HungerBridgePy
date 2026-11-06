'''Server-Sent-Events client for ``GET /server/stream``.

Wire format (protocol v1)::

    : history-end          <- comment, sent once the requested history is written
    data:<log line>        <- one event per log line; '\\n'/'\\r' inside the line
                              are escaped as the two characters backslash+n/r
    : keepalive            <- comment every 15 s

Older servers do not send ``history-end``; for them the client falls back to
the previous time-based heuristic (lines received within one second of
connecting are treated as history).
'''

from __future__ import annotations

import codecs
import re
import socket
import threading
import time
from typing import Callable, Iterable, Iterator, List, Optional, Tuple, Union

import requests

from .protocol import PROTOCOL_HEADER, SSE_HISTORY_END
from .transport import parse_retry_after

__all__ = ['Stream', 'parse_sse']

HeadersProvider = Union[Callable[[], dict], dict]

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
# Matches '[12:34:56]' and the server's '[12:34:56 INFO]' line prefix.
_TIMESTAMP_RE = re.compile(r'\[([0-9]{2}:[0-9]{2}:[0-9]{2})(?:\]|\s)')
_LINE_SPLIT_RE = re.compile(r'\r\n|\r|\n')

#: Legacy servers (no ``history-end`` marker): history window after connect.
LEGACY_HISTORY_WINDOW_SEC = 1.0


def _split_lines(chunks: Iterable[bytes]) -> Iterator[str]:
    '''Split a byte stream into SSE lines (CRLF, LF or CR terminated).

    Unlike ``str.splitlines`` this does not split on Unicode line separators
    (U+2028, form feed, ...) that may legitimately appear inside a log line.
    A trailing line without terminator is discarded, as the SSE spec requires.
    '''
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    buf = ''
    for chunk in chunks:
        if not chunk:
            continue
        buf += decoder.decode(chunk)
        while True:
            match = _LINE_SPLIT_RE.search(buf)
            if match is None:
                break
            if match.group() == '\r' and match.end() == len(buf):
                break  # possibly the first half of a CRLF split across chunks
            yield buf[: match.start()]
            buf = buf[match.end() :]
    buf += decoder.decode(b'', final=True)
    yield from _LINE_SPLIT_RE.split(buf)[:-1]


def _iter_raw_chunks(response: requests.Response) -> Iterator[bytes]:
    '''Yield bytes as soon as they arrive.

    ``iter_content(chunk_size=None)`` only streams chunked responses; behind a
    proxy that turns the stream into a close-delimited body it would block
    until EOF. urllib3 2's ``read1`` returns whatever is available instead.
    '''
    read1 = getattr(response.raw, 'read1', None)
    if read1 is None:
        yield from response.iter_content(chunk_size=1)
        return
    while True:
        chunk = read1(8192, decode_content=True)
        if not chunk:
            return
        yield chunk


def parse_sse(lines: Iterable[str]) -> Iterator[Tuple[str, str]]:
    '''Parse SSE lines into ``('data', text)`` and ``('comment', text)`` items.

    Follows the WHATWG SSE rules: a single space after the colon is removed
    (and nothing else, so indentation inside log lines is preserved);
    multiple ``data`` fields in one event are joined with ``\\n``.
    '''
    data: Optional[List[str]] = None
    for line in lines:
        if line == '':
            if data is not None:
                yield ('data', '\n'.join(data))
            data = None
            continue
        name, sep, value = line.partition(':')
        if sep and value.startswith(' '):
            value = value[1:]
        if name == '':
            yield ('comment', value)
        elif name == 'data':
            if data is None:
                data = []
            data.append(value)
        # 'event', 'id', 'retry' and unknown fields are ignored.
    if data is not None:
        yield ('data', '\n'.join(data))


class Stream:
    '''SSE streaming wrapper for ``/server/stream``.

    ``history_handler(lines)`` receives the requested history once per
    connection; ``new_log_handler(line)`` receives each live line. Without
    handlers, lines are accumulated in :meth:`getRaw`, :meth:`getSanitized`
    and :meth:`getTimestamped`.
    '''

    def __init__(
        self,
        base_url: str,
        headers_provider: HeadersProvider,
        history_handler: Optional[Callable[[List[str]], None]] = None,
        new_log_handler: Optional[Callable[[str], None]] = None,
        *,
        connect_timeout: float = 5.0,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ):
        self.url = base_url.rstrip('/') + '/server/stream'
        self.headers_provider = headers_provider
        self.history_handler = history_handler
        self.new_log_handler = new_log_handler
        self.connect_timeout = connect_timeout
        self._session_factory = session_factory

        self.raw_stream: List[str] = []
        self.sanitized_stream: List[str] = []
        self.timestamped_stream: dict = {}
        #: Last exception that ended a connection attempt (``None`` if clean).
        self.last_error: Optional[BaseException] = None

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._connected = threading.Event()
        self._session: Optional[requests.Session] = None
        self._response: Optional[requests.Response] = None
        self._last_line: Optional[str] = None
        self._connections = 0

    # -- default handlers ----------------------------------------------------------

    def _record(self, line: str) -> None:
        clean = self.sanitize(line)
        ts = self.extractTimestamp(clean)
        with self._lock:
            self.raw_stream.append(line)
            self.sanitized_stream.append(clean)
            if ts is not None:
                self.timestamped_stream[ts] = clean

    def _default_history_handler(self, historic_lines: List[str]) -> None:
        for line in historic_lines:
            self._record(line)

    def _default_new_log_handler(self, line: str) -> None:
        self._record(line)

    def _emit_history(self, lines: List[str]) -> None:
        try:
            (self.history_handler or self._default_history_handler)(list(lines))
        except Exception:  # handler errors must not kill the stream
            pass

    def _emit_live(self, line: str) -> None:
        try:
            (self.new_log_handler or self._default_new_log_handler)(line)
        except Exception:
            pass

    # -- lifecycle -----------------------------------------------------------------

    def connect(
        self,
        keepalive: int = 15,
        history: Optional[int] = None,
        *,
        reconnect: bool = False,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
    ) -> None:
        '''Start streaming in a background daemon thread (no-op if already running).

        ``keepalive`` is the server's keepalive interval; the read timeout is
        derived from it so dead connections are detected. With
        ``reconnect=True`` the stream reconnects with exponential back-off and
        de-duplicates the replayed history against the last line seen.
        '''
        if self._thread and self._thread.is_alive():
            return
        stop = threading.Event()
        self._stop_event = stop
        self._connected.clear()
        read_timeout = max(3.0 * float(keepalive or 15), 30.0)
        self._thread = threading.Thread(
            target=self._run,
            args=(stop, history, read_timeout, reconnect, reconnect_delay, max_reconnect_delay),
            name='HungerBridgeStream',
            daemon=True,
        )
        self._thread.start()

    def disconnect(self, timeout: float = 5.0) -> None:
        stop, thread = self._stop_event, self._thread
        if stop:
            stop.set()
        if thread and thread is not threading.current_thread():
            # Closing the response here would block: the buffered reader's lock is held by
            # the worker's blocking read. Shutting the socket down makes that read return
            # immediately; the worker then closes the response/session itself.
            self._abort_socket()
            thread.join(timeout)
        else:
            self._close_transport()
        self._thread = None
        self._stop_event = None
        self._connected.clear()

    def wait_until_connected(self, timeout: Optional[float] = None) -> bool:
        '''Block until the server accepted the stream (HTTP 200 received).'''
        return self._connected.wait(timeout)

    def isConnected(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def getRaw(self) -> list:
        with self._lock:
            return list(self.raw_stream)

    def getSanitized(self) -> list:
        with self._lock:
            return list(self.sanitized_stream)

    def getTimestamped(self) -> dict:
        with self._lock:
            return dict(self.timestamped_stream)

    # -- worker --------------------------------------------------------------------

    def _abort_socket(self) -> None:
        response = self._response
        if response is None:
            return
        raw = getattr(response, 'raw', None)
        candidates = [
            getattr(getattr(raw, '_connection', None), 'sock', None),  # urllib3 2.x
            getattr(getattr(getattr(getattr(raw, '_fp', None), 'fp', None), 'raw', None), '_sock', None),  # http.client
        ]
        for sock in candidates:
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
                return
            except OSError:
                continue

    def _close_transport(self) -> None:
        response, session = self._response, self._session
        for closable in (response, session):
            if closable is not None:
                try:
                    closable.close()
                except Exception:
                    pass

    def _request_url(self, history: Optional[int]) -> str:
        if not history:
            return self.url
        sep = '&' if '?' in self.url else '?'
        return f'{self.url}{sep}history={int(history)}'

    def _run(self, stop, history, read_timeout, reconnect, delay, max_delay) -> None:
        backoff = delay
        try:
            while not stop.is_set():
                received = False
                try:
                    received = self._stream_once(stop, history, read_timeout)
                    self.last_error = None
                except Exception as exc:  # network errors, HTTP errors
                    self.last_error = exc
                finally:
                    self._connected.clear()
                    self._close_transport()
                    self._response = None
                    self._session = None
                if not reconnect or stop.is_set():
                    break
                backoff = delay if received else min(backoff * 2, max_delay)
                retry_after = getattr(self.last_error, 'retry_after', None)
                wait = max(backoff, float(retry_after)) if isinstance(retry_after, (int, float)) else backoff
                stop.wait(wait)
        finally:
            if self._stop_event is stop:
                self._thread = None
                self._stop_event = None

    def _stream_once(self, stop: threading.Event, history: Optional[int], read_timeout: float) -> bool:
        '''Run a single connection. Returns True if any event was received.'''
        headers = self.headers_provider() if callable(self.headers_provider) else dict(self.headers_provider)
        headers.setdefault('Accept', 'text/event-stream')
        self._session = self._session_factory()
        response = self._session.get(
            self._request_url(history),
            headers=headers,
            stream=True,
            timeout=(self.connect_timeout, read_timeout),
        )
        self._response = response
        if not response.ok:
            error = requests.HTTPError(f'{response.status_code}: {response.text}', response=response)
            if response.status_code == 429:
                error.retry_after = parse_retry_after(response.headers.get('Retry-After'))  # type: ignore[attr-defined]
            raise error
        self._connections += 1
        self._connected.set()

        reconnecting = self._connections > 1
        in_history = bool(history)
        history_lines: List[str] = []
        # Servers that advertise a protocol version always send the history-end
        # marker; only legacy servers need the time-window heuristic.
        legacy = response.headers.get(PROTOCOL_HEADER) is None
        legacy_deadline = time.monotonic() + LEGACY_HISTORY_WINDOW_SEC
        received = False

        def finish_history() -> None:
            nonlocal in_history
            in_history = False
            lines = history_lines
            if reconnecting:
                # Replay only what was missed while disconnected, as live lines.
                if self._last_line is not None and self._last_line in lines:
                    idx = len(lines) - 1 - lines[::-1].index(self._last_line)
                    lines = lines[idx + 1 :]
                for line in lines:
                    self._last_line = line
                    self._emit_live(line)
            else:
                if lines:
                    self._last_line = lines[-1]
                self._emit_history(lines)

        for kind, value in parse_sse(_split_lines(_iter_raw_chunks(response))):
            if stop.is_set():
                break
            received = True
            if kind == 'comment':
                if in_history and (value.strip() == SSE_HISTORY_END or (legacy and time.monotonic() >= legacy_deadline)):
                    finish_history()
                continue
            if in_history:
                if not legacy or time.monotonic() < legacy_deadline:
                    history_lines.append(value)
                    continue
                finish_history()  # legacy server: window elapsed, this line is live
            self._last_line = value
            self._emit_live(value)
        if in_history and history_lines and not stop.is_set():
            finish_history()
        return received

    # -- helpers -------------------------------------------------------------------

    @staticmethod
    def sanitize(line: str) -> str:
        '''Strip ANSI colors, unescape ``\\n``/``\\r`` and trailing whitespace.'''
        clean = _ANSI_RE.sub('', line)
        clean = clean.replace('\\n', '\n').replace('\\r', '\r')
        return clean.rstrip()

    @staticmethod
    def extractTimestamp(line: str) -> Optional[str]:
        m = _TIMESTAMP_RE.match(line)
        return m.group(1) if m else None
