"""Client for ``GET /server/stream`` (Server-Sent Events, protocol v2).

Wire format (defined by HungerBridge)::

    retry: 2000

    id: <epoch>.<seq>
    event: log
    data: {"seq":..,"time":..,"level":..,"thread":..,"logger":..,"message":..,"line":..}

    event: ready
    data: {"epoch":..,"seq":..,"replayed":..,"gap":false}

    : keepalive

The first connection asks for ``?history=N`` (or ``all``); reconnections send
``Last-Event-ID`` so the server replays exactly the events that were missed. When the
server can no longer replay them (its buffer rolled over, or it restarted), ``ready``
reports ``gap: true``; the stream logs a warning and counts it in :attr:`Stream.gaps`.
"""

from __future__ import annotations

import _socket
import codecs
import json
import logging
import random
import re
import socket
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Union

import requests

from .exceptions import (
    HungerBridgeAuthError,
    HungerBridgeConnectionError,
    HungerBridgeError,
    HungerBridgeForbidden,
    HungerBridgeNotFound,
    HungerBridgeProtocolError,
)
from .transport import checkProtocol, errorFor

__all__ = ["LogEvent", "Stream", "SseEvent", "parseSse", "splitLines", "historyParam"]

log = logging.getLogger("hungerbridge")

#: Errors that reconnecting cannot fix; the worker stops instead of retrying them forever.
PERMANENT_ERRORS = (HungerBridgeAuthError, HungerBridgeForbidden, HungerBridgeNotFound, HungerBridgeProtocolError)

#: The server sends a keepalive every 15 s; a silent connection is considered dead after this.
READ_TIMEOUT = 45.0

_LINE_SPLIT = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True)
class LogEvent:
    """One console log event. ``str(event)`` is the canonical line
    ``[HH:mm:ss] [thread/LEVEL]: [logger] message`` (plain text, as rendered by the server)."""

    seq: int
    time: int
    level: str
    thread: str
    logger: str
    message: str
    line: str
    epoch: str
    throwable: Optional[str] = None

    @property
    def id(self) -> str:
        """The SSE event id, ``<epoch>.<seq>``."""
        return f"{self.epoch}.{self.seq}"

    def __str__(self) -> str:
        return self.line

    @classmethod
    def fromJson(cls, data: dict, epoch: str) -> "LogEvent":
        try:
            return cls(
                seq=int(data["seq"]),
                time=int(data["time"]),
                level=str(data["level"]),
                thread=str(data.get("thread") or ""),
                logger=str(data.get("logger") or ""),
                message=str(data.get("message") or ""),
                line=str(data["line"]),
                epoch=epoch,
                throwable=data.get("throwable"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HungerBridgeProtocolError(f"Malformed log event: {data!r}") from exc


@dataclass(frozen=True)
class SseEvent:
    event: str
    data: str
    id: Optional[str] = None


def splitLines(chunks: Iterable[bytes]) -> Iterator[str]:
    """Split a byte stream into SSE lines (CRLF, LF or CR terminated). Unlike
    ``str.splitlines`` this does not split on Unicode separators. A trailing line without
    terminator is discarded, as the SSE specification requires."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf = ""
    for chunk in chunks:
        if not chunk:
            continue
        buf += decoder.decode(chunk)
        while True:
            match = _LINE_SPLIT.search(buf)
            if match is None:
                break
            if match.group() == "\r" and match.end() == len(buf):
                break  # possibly the first half of a CRLF split across chunks
            yield buf[: match.start()]
            buf = buf[match.end():]
    buf += decoder.decode(b"", final=True)
    yield from _LINE_SPLIT.split(buf)[:-1]


def parseSse(lines: Iterable[str]) -> Iterator[SseEvent]:
    """Parse SSE lines into events (WHATWG rules: one leading space after the colon is
    removed, multiple ``data`` lines are joined with ``\\n``, comments are skipped, an event
    without ``data`` is not dispatched, ``event`` defaults to ``message``)."""
    event, data, event_id = None, None, None
    for line in lines:
        if line == "":
            if data is not None:
                yield SseEvent(event or "message", "\n".join(data), event_id)
            event, data, event_id = None, None, None
            continue
        if line.startswith(":"):
            continue
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "data":
            data = (data or []) + [value]
        elif name == "event":
            event = value
        elif name == "id":
            event_id = value


def _iterRawChunks(response: requests.Response) -> Iterator[bytes]:
    """Yield bytes as soon as they arrive (also behind proxies that drop chunked encoding)."""
    read1 = getattr(response.raw, "read1", None)
    if read1 is None:
        yield from response.iter_content(chunk_size=1)
        return
    while True:
        chunk = read1(8192, decode_content=True)
        if not chunk:
            return
        yield chunk


def historyParam(history: Union[int, bool, None]) -> Optional[str]:
    """``?history=`` value: ``True`` means every buffered event (``all``)."""
    if history is None or history is False:
        return None
    if history is True:
        return "all"
    if isinstance(history, int) and history >= 0:
        return str(history) if history else None
    raise ValueError(f"history must be a non-negative int, True or None, got {history!r}")


HeadersProvider = Callable[[], Dict[str, str]]


class Stream:
    """A background connection to ``/server/stream``.

    Callbacks run on the stream's worker thread; exceptions they raise are logged, not
    propagated:

    * ``on_event(event)`` — every live :class:`LogEvent`, including events replayed after a
      reconnect (they were missed while disconnected);
    * ``on_history(events)`` — the history requested by the first connection, once (without
      ``on_history`` these events go to ``on_event``);
    * ``on_close(error)`` — once when the worker stops: the error that ended it, or ``None``
      after :meth:`disconnect`.

    Use :meth:`hungerbridge.BridgeClient.openStream` to create one.
    """

    def __init__(
        self,
        base_url: str,
        headers_provider: HeadersProvider,
        *,
        on_event: Optional[Callable[[LogEvent], None]] = None,
        on_history: Optional[Callable[[List[LogEvent]], None]] = None,
        on_close: Optional[Callable[[Optional[BaseException]], None]] = None,
        connect_timeout: float = 5.0,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ):
        self.url = base_url.rstrip("/") + "/server/stream"
        self.headers_provider = headers_provider
        self.on_event = on_event
        self.on_history = on_history
        self.on_close = on_close
        self.connect_timeout = connect_timeout
        self._session_factory = session_factory

        #: Error that ended the last connection attempt (``None`` if it ended cleanly).
        self.last_error: Optional[BaseException] = None
        #: Id of the last event delivered (``<epoch>.<seq>``), used to resume.
        self.last_event_id: Optional[str] = None
        #: Number of times events were lost (the server could not replay them).
        self.gaps = 0
        #: Epoch of the server's event sequence (changes when the server restarts).
        self.epoch: Optional[str] = None

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[threading.Event] = None
        self._ready = threading.Event()
        self._response: Optional[requests.Response] = None

    # -- lifecycle ------------------------------------------------------------------

    def connect(
        self,
        history: Union[int, bool, None] = None,
        *,
        reconnect: bool = True,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
    ) -> "Stream":
        """Start streaming in a daemon thread (no-op while already running).

        ``history`` replays that many recent events first (``True``: everything the server
        still buffers). With ``reconnect`` the stream reconnects with exponential back-off
        (with jitter), resuming after the last delivered event.
        """
        param = historyParam(history)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop = threading.Event()
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(self._stop, param, reconnect, reconnect_delay, max_reconnect_delay),
                name="HungerBridgeStream",
                daemon=True,
            )
            self._thread.start()
        return self

    def disconnect(self, timeout: float = 5.0) -> None:
        with self._lock:
            stop, thread = self._stop, self._thread
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            self._abortSocket()
            thread.join(timeout)

    def waitUntilConnected(self, timeout: Optional[float] = None) -> bool:
        """Block until the server finished the replay of the current connection (``ready``)."""
        return self._ready.wait(timeout)

    def isConnected(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- worker ---------------------------------------------------------------------

    def _run(self, stop, history, reconnect, delay, max_delay) -> None:
        backoff = delay
        try:
            while not stop.is_set():
                received = False
                try:
                    received = self._streamOnce(stop, history)
                    self.last_error = None
                except HungerBridgeError as exc:
                    self.last_error = exc
                except Exception as exc:  # the connection broke while reading
                    error = HungerBridgeConnectionError(f"HungerBridge log stream interrupted: {exc}")
                    error.__cause__ = exc
                    self.last_error = error
                finally:
                    self._ready.clear()
                    self._closeResponse()
                if not reconnect or stop.is_set() or isinstance(self.last_error, PERMANENT_ERRORS):
                    break
                backoff = delay if received else min(backoff * 2, max_delay)
                wait = backoff * random.uniform(0.8, 1.2)
                retry_after = getattr(self.last_error, "retry_after", None)
                if isinstance(retry_after, int):
                    wait = max(wait, float(retry_after))
                log.info("HungerBridge log stream reconnecting in %.1fs (%s)", wait, self.last_error or "server closed the stream")
                stop.wait(wait)
        finally:
            with self._lock:
                if self._stop is stop:
                    self._thread = None
            if self.last_error is not None and not stop.is_set():
                log.warning("HungerBridge log stream stopped: %s", self.last_error)
            if self.on_close is not None:
                try:
                    self.on_close(None if stop.is_set() else self.last_error)
                except Exception:
                    log.exception("HungerBridge stream on_close handler failed")

    def _streamOnce(self, stop: threading.Event, history: Optional[str]) -> bool:
        """One connection. Returns True if any event was received."""
        headers = dict(self.headers_provider())
        headers["Accept"] = "text/event-stream"
        resuming = self.last_event_id is not None
        if resuming:
            headers["Last-Event-ID"] = self.last_event_id
        params = {"history": history} if history and not resuming else None
        session = self._session_factory()
        try:
            try:
                response = session.get(self.url, headers=headers, params=params, stream=True,
                                       timeout=(self.connect_timeout, READ_TIMEOUT))
            except requests.exceptions.RequestException as exc:
                raise HungerBridgeConnectionError(f"Cannot open the HungerBridge log stream at {self.url}: {exc}") from exc
            self._response = response
            checkProtocol(response)
            if not response.ok:
                raise errorFor(response)
            return self._consume(stop, response, first=not resuming)
        finally:
            session.close()

    def _consume(self, stop: threading.Event, response: requests.Response, first: bool) -> bool:
        replay: List[LogEvent] = []
        ready = False
        received = False
        for sse in parseSse(splitLines(_iterRawChunks(response))):
            if stop.is_set():
                break
            received = True
            try:
                data = json.loads(sse.data)
            except ValueError as exc:
                raise HungerBridgeProtocolError(f"Malformed stream data: {sse.data[:200]!r}") from exc
            if sse.event == "ready":
                ready = True
                self._onReady(data)
                if first and self.on_history is not None:
                    self._call(self.on_history, replay)
                else:
                    for event in replay:
                        self._call(self.on_event, event)
                replay = []
                self._ready.set()
            elif sse.event == "log":
                epoch = (sse.id or "").rpartition(".")[0] or self.epoch or ""
                event = LogEvent.fromJson(data, epoch)
                self.last_event_id = event.id
                if ready:
                    self._call(self.on_event, event)
                else:
                    replay.append(event)
        return received

    def _onReady(self, data: dict) -> None:
        epoch = data.get("epoch")
        if self.epoch is not None and epoch != self.epoch:
            log.info("HungerBridge restarted (event sequence %s -> %s)", self.epoch, epoch)
        self.epoch = epoch
        # Everything up to "seq" was replayed (or not requested): resume after it next time.
        if epoch is not None and data.get("seq") is not None:
            self.last_event_id = f"{epoch}.{int(data['seq'])}"
        if data.get("gap"):
            self.gaps += 1
            log.warning("HungerBridge log stream lost events while disconnected (the server could not replay them)")

    def _call(self, handler, value) -> None:
        if handler is None:
            return
        try:
            handler(value)
        except Exception:
            log.exception("HungerBridge stream handler failed")

    def _closeResponse(self) -> None:
        response, self._response = self._response, None
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def _abortSocket(self) -> None:
        """Unblock a worker stuck in recv(): shutdown() alone does not wake it on Windows,
        so the OS handle is closed directly."""
        response = self._response
        raw = getattr(response, "raw", None)
        candidates = [
            getattr(getattr(raw, "_connection", None), "sock", None),  # urllib3 2.x
            getattr(getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None), "_sock", None),  # http.client
        ]
        for sock in candidates:
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                _socket.socket.close(sock)
                return
            except OSError:
                continue
