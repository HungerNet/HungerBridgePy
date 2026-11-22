"""High-level HungerBridge API client."""

from __future__ import annotations

import json
import queue
import time
from typing import Any, Callable, Iterator, List, Optional, Union
from urllib.parse import parse_qs, urlparse

import requests

from .auth import Credentials
from .exceptions import HungerBridgeProtocolError
from .stream import LogEvent, Stream
from .transport import DEFAULT_TIMEOUT, Timeout, Transport, checkProtocol, errorFor
from .units import convertBytes

__all__ = ["BridgeClient", "redeemPickup"]

_TPS_FIELDS = {"current": "tps", "1m": "tps_1m", "5m": "tps_5m", "15m": "tps_15m"}
_MEMORY_FIELDS = ("heap_used", "heap_committed", "heap_max", "nonheap_used", "nonheap_committed", "nonheap_max",
                  "jvm_used", "jvm_committed", "jvm_max", "process_used", "process_virtual")


def redeemPickup(pickup_url: str, *, timeout: Timeout = DEFAULT_TIMEOUT) -> Credentials:
    """Redeem the single-use pickup printed by ``/hb token create`` and return the token.

    ``pickup_url`` is the full URL, ``http://host:1913/pickup/<id>?passkey=<hex>``.
    """
    parsed = urlparse(pickup_url)
    pickup_id = parsed.path.rstrip("/").rpartition("/pickup/")[2]
    passkey = (parse_qs(parsed.query).get("passkey") or [None])[0]
    if not parsed.scheme or not pickup_id or not passkey:
        raise ValueError(f"Expected http(s)://host:port/pickup/<id>?passkey=<hex>, got {pickup_url!r}")
    try:
        response = requests.get(f"{parsed.scheme}://{parsed.netloc}/pickup/{pickup_id}", params={"passkey": passkey}, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        from .exceptions import HungerBridgeConnectionError

        raise HungerBridgeConnectionError(f"Cannot reach HungerBridge at {parsed.netloc}: {exc}") from exc
    checkProtocol(response)
    if not response.ok:
        raise errorFor(response)
    data = response.json()
    try:
        return Credentials(token_id=data["token_id"], token_secret=data["token_secret"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HungerBridgeProtocolError("The pickup response did not contain a valid token") from exc


class BridgeClient:
    """Client for one HungerBridge server.

    Args:
        url: Base URL, e.g. ``"http://127.0.0.1:1913"``.
        token_id: Token id (``/hb token create <id> <policy>``).
        token_secret: The token's 64 hex character secret, from the pickup.
        max_retries: Retries after HTTP 429 (each signed afresh).
        retry_backoff: Seconds; the n-th retry waits ``max(n * retry_backoff, Retry-After)``.
        min_interval: Seconds between requests (client-side throttle), 0 = off.
        timeout: Seconds, or ``(connect, read)``.
        session: A ``requests.Session`` to use.

    Example::

        client = BridgeClient("http://127.0.0.1:1913", token_id="bot", token_secret="9f2c...")
        print(client.runCommand("list"))
    """

    def __init__(
        self,
        url: str,
        *,
        token_id: str,
        token_secret: str,
        max_retries: int = 2,
        retry_backoff: float = 1.0,
        min_interval: float = 0.0,
        timeout: Timeout = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
    ):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError(f"url must start with http:// or https://, got {url!r}")
        self.base = url.rstrip("/")
        self.credentials = Credentials(token_id=token_id, token_secret=token_secret)
        self.transport = Transport(self.base, self.credentials, max_retries=max_retries, retry_backoff=retry_backoff,
                                   min_interval=min_interval, timeout=timeout, session=session)

    def __repr__(self) -> str:
        return f"<BridgeClient {self.base} token={self.credentials.token_id!r}>"

    @classmethod
    def fromPickup(cls, pickup_url: str, **options: Any) -> "BridgeClient":
        """Redeem ``pickup_url`` and return a client for the new token (``options``: see the constructor)."""
        credentials = redeemPickup(pickup_url)
        parsed = urlparse(pickup_url)
        return cls(f"{parsed.scheme}://{parsed.netloc}", token_id=credentials.token_id,
                   token_secret=credentials.token_secret, **options)

    def close(self) -> None:
        """Close the HTTP session (streams have their own connections)."""
        self.transport.session.close()

    def _get(self, path: str) -> dict:
        return self.transport.get(path)

    def _post(self, path: str, payload: dict) -> dict:
        return self.transport.post(path, payload)

    # -- connectivity ---------------------------------------------------------------

    def isOk(self) -> bool:
        """True if the server answers an authenticated ``/ping``."""
        return self._get("ping").get("ok") is True

    def getServerTime(self) -> int:
        """Server wall-clock time in epoch milliseconds."""
        return self._get("ping")["server_time"]

    def getPing(self) -> int:
        """Round-trip time of an authenticated ``/ping`` in milliseconds."""
        start = time.perf_counter()
        self._get("ping")
        return int((time.perf_counter() - start) * 1000)

    def getTokenInfo(self) -> dict:
        """``token_id``, ``policy_id``, effective ``permissions`` and ``expiry`` (unix seconds, 0 = never)."""
        resp = self._get("auth/check")
        return {key: resp.get(key) for key in ("token_id", "policy_id", "permissions", "expiry")}

    # -- server metadata ------------------------------------------------------------

    def getServerMeta(self) -> dict:
        """``platform`` (paper/folia/fabric), ``minecraft_version``, ``bridge_version``, ``port``, ``protocol_version``."""
        resp = self._get("server/meta")
        return {key: resp.get(key) for key in ("platform", "minecraft_version", "bridge_version", "port", "protocol_version")}

    def getPlatform(self) -> str:
        return self.getServerMeta()["platform"]

    def getMinecraftVersion(self) -> str:
        return self.getServerMeta()["minecraft_version"]

    def getBridgeVersion(self) -> str:
        return self.getServerMeta()["bridge_version"]

    # -- players --------------------------------------------------------------------

    def getPlayers(self) -> List[str]:
        """Names of the online players (capped by the server's ``players.max-list``)."""
        return list(self._get("players/list").get("players") or [])

    def getPlayerCount(self) -> int:
        return int(self._get("players/list")["count"])

    def getMaxPlayers(self) -> int:
        return int(self._get("players/list")["max"])

    def getPlayerStats(self) -> dict:
        """One request: ``count`` (online), ``max`` (slots), ``players`` (names, capped)."""
        resp = self._get("players/list")
        return {"count": resp.get("count"), "max": resp.get("max"), "players": list(resp.get("players") or [])}

    # -- world ----------------------------------------------------------------------

    def getTPS(self, window: str = "current") -> float:
        """Ticks per second over ``window``: ``current``, ``1m``, ``5m`` or ``15m`` (-1 if unavailable)."""
        field = _TPS_FIELDS.get(window)
        if field is None:
            raise ValueError(f"window must be one of {', '.join(_TPS_FIELDS)}, got {window!r}")
        return self._get("world/tps")[field]

    def getTPSStats(self) -> dict:
        """Every TPS window in one request: ``{"current", "1m", "5m", "15m"}``."""
        resp = self._get("world/tps")
        return {window: resp.get(field) for window, field in _TPS_FIELDS.items()}

    def getMSPT(self) -> float:
        """Average milliseconds per tick (-1 if unavailable)."""
        return self._get("world/mspt")["mspt"]

    def getWorldTime(self) -> int:
        """Overworld time of day in ticks (0-23999)."""
        return self._get("world/time")["time"]

    def getWorldTimeInfo(self) -> dict:
        """``time`` (tick of day), ``full_time``, ``day``, ``server_time`` (epoch seconds)."""
        resp = self._get("world/time")
        return {key: resp.get(key) for key in ("time", "full_time", "day", "server_time")}

    def getWorldWeather(self) -> str:
        """``clear``, ``rain`` or ``thunder``."""
        return self._get("world/weather")["weather"]

    def getLoadedChunks(self) -> dict:
        """``total`` and per dimension (``world``, ``world_nether``, ``world_the_end``, others by name)."""
        return {k: v for k, v in self._get("world/chunks").items() if k != "ok"}

    def getLoadedEntities(self) -> dict:
        """Like :meth:`getLoadedChunks`, for entities."""
        return {k: v for k, v in self._get("world/entities").items() if k != "ok"}

    # -- system ---------------------------------------------------------------------

    def getUptime(self) -> int:
        """JVM uptime in milliseconds."""
        return self._get("system/uptime")["uptime_ms"]

    def getCPUStats(self) -> dict:
        """``load`` (process CPU load, 0-1) and ``processors``."""
        resp = self._get("system/cpu")
        return {"load": resp.get("cpu_load"), "processors": resp.get("processors")}

    def getMemoryStats(self, unit: str = "mib") -> dict:
        """Heap, non-heap, JVM and process memory plus ``heap_free``, in ``unit``
        (``b``, ``kb``, ``mb``, ``gb``, ``kib``, ``mib``, ``gib``, ...)."""
        resp = self._get("system/memory")
        raw = {name: resp.get(name + "_bytes") for name in _MEMORY_FIELDS}
        out = {name: convertBytes(value, unit) for name, value in raw.items()}
        used, committed = raw["heap_used"], raw["heap_committed"]
        out["heap_free"] = convertBytes(max(0, committed - used), unit) if used is not None and committed is not None else None
        return out

    def getGCStats(self) -> dict:
        """``gc_type``, ``gc_count``, ``gc_time_ms``, ``last_gc_pause_ms``, ``avg_gc_pause_ms``."""
        resp = self._get("system/gc")
        return {k: resp.get(k) for k in ("gc_type", "gc_count", "gc_time_ms", "last_gc_pause_ms", "avg_gc_pause_ms")}

    def getThreadStats(self) -> dict:
        """JVM threads: ``current``, ``peak``, ``daemon``."""
        resp = self._get("system/threads")
        return {k: resp.get(k) for k in ("current", "peak", "daemon")}

    def getNetworkStats(self) -> dict:
        """Host network (Linux): ``bytes_in_per_sec``, ``bytes_out_per_sec``, ``total_bytes_in``, ``total_bytes_out``."""
        resp = self._get("system/network")
        return {k: resp.get(k) for k in ("bytes_in_per_sec", "bytes_out_per_sec", "total_bytes_in", "total_bytes_out")}

    # -- actions --------------------------------------------------------------------

    def runCommand(self, command: str, show_console: bool = False) -> str:
        """Run a console command and return its output (feedback lines joined with ``\\n``).

        The command runs with console permissions on the server thread. Its feedback is
        captured directly and is **not** printed to the server console unless
        ``show_console`` is true.

        Raises:
            HungerBridgeError: ``code == "command_timeout"`` (HTTP 504) when the server thread
                did not run the command within 5 seconds.
        """
        resp = self._post("server/run", {"command": command, "show_console": bool(show_console)})
        return "\n".join(str(line) for line in resp.get("output") or [])

    def broadcast(self, message: str, prefix: Optional[str] = None) -> str:
        """Show ``prefix + message`` to every player (``tellraw @a``)."""
        component = json.dumps({"text": (prefix or "") + message}, ensure_ascii=False)
        return self.runCommand(f"tellraw @a {component}")

    def log(
        self,
        message: str,
        level: str = "INFO",
        thread: Optional[str] = None,
        logger: Optional[str] = None,
        normalize: bool = False,
    ) -> str:
        """Write ``message`` to the server log and return the text that was logged.

        Rendered as ``[10:53:54] [thread/LEVEL]: [logger] message`` (``thread`` and ``logger``
        default to ``HungerBridge``). ``level`` is a built-in (``INFO``, ``WARN``, ...) or a
        custom name (letters, digits, ``_``). ``normalize`` makes the server reduce the
        message to plain text first: JSON text components are flattened, formatting codes
        and chat decorations such as ``System chat:`` are removed.
        """
        body = {"message": message, "level": level, "normalize": bool(normalize)}
        if thread is not None:
            body["thread"] = thread
        if logger is not None:
            body["logger"] = logger
        return self._post("server/log", body).get("message", message)

    def stopServer(self) -> None:
        """Stop the Minecraft server (like ``/stop``)."""
        self._post("server/stop", {})

    # -- log stream -----------------------------------------------------------------

    def openStream(
        self,
        *,
        on_event: Optional[Callable[[LogEvent], None]] = None,
        on_history: Optional[Callable[[List[LogEvent]], None]] = None,
        on_close: Optional[Callable[[Optional[BaseException]], None]] = None,
    ) -> Stream:
        """A new, independent :class:`Stream`; call ``.connect()`` on it."""
        return Stream(
            self.base,
            lambda: self.transport.authHeaders("GET", "/server/stream"),
            on_event=on_event,
            on_history=on_history,
            on_close=on_close,
        )

    def streamLogs(
        self,
        history: Union[int, bool, None] = None,
        *,
        timeout: Optional[float] = None,
        reconnect: bool = True,
    ) -> Iterator[LogEvent]:
        """Yield console :class:`LogEvent` objects as they are logged (blocking iterator).

        ``history`` replays that many recent events first (``True``: all the server keeps).
        Iteration ends when nothing arrives for ``timeout`` seconds, when the server closes
        the stream (unless ``reconnect``), or by raising the error that ended the stream.
        Leaving the loop disconnects. ``str(event)`` is the console line.

        Example::

            for event in client.streamLogs(history=20):
                if "joined the game" in event.message:
                    print(event.line)
        """
        items: "queue.Queue[Any]" = queue.Queue()
        finished = object()

        def onHistory(events):
            for event in events:
                items.put(event)

        stream = self.openStream(on_event=items.put, on_history=onHistory, on_close=lambda error: items.put((finished, error)))
        stream.connect(history, reconnect=reconnect)
        try:
            while True:
                try:
                    item = items.get(timeout=timeout)
                except queue.Empty:
                    return
                if isinstance(item, tuple) and item and item[0] is finished:
                    if item[1] is not None:
                        raise item[1]
                    return
                yield item
        finally:
            stream.disconnect()

    def __enter__(self) -> "BridgeClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
