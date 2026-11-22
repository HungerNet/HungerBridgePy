"""HTTP transport: signing, throttling, retries, timeouts, protocol check and error mapping."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional, Tuple, Union

import requests

from .auth import Credentials, buildAuthHeaders
from .canonical import canonicalBody
from .exceptions import (
    HungerBridgeAuthError,
    HungerBridgeConnectionError,
    HungerBridgeError,
    HungerBridgeForbidden,
    HungerBridgeNotFound,
    HungerBridgeProtocolError,
    HungerBridgeRateLimit,
)
from .protocol import PROTOCOL_HEADER, PROTOCOL_VERSION

__all__ = ["Transport", "DEFAULT_TIMEOUT", "Timeout", "errorFor", "parseRetryAfter", "checkProtocol"]

log = logging.getLogger("hungerbridge")

Timeout = Union[float, Tuple[float, float]]

#: (connect, read) seconds. ``/server/run`` waits up to 5 s for the game thread.
DEFAULT_TIMEOUT: Tuple[float, float] = (5.0, 30.0)

BASE_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

_STATUS_ERRORS = {
    401: HungerBridgeAuthError,
    403: HungerBridgeForbidden,
    404: HungerBridgeNotFound,
    429: HungerBridgeRateLimit,
}


def parseRetryAfter(value: Optional[str]) -> Union[int, str, None]:
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return value  # HTTP-date form; surfaced verbatim


def _jsonOrNone(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def errorFor(response: requests.Response) -> HungerBridgeError:
    """The typed error for a non-2xx HungerBridge response."""
    data = _jsonOrNone(response)
    data = data if isinstance(data, dict) else {}
    code = data.get("error") if isinstance(data.get("error"), str) else None
    reason = data.get("reason") if isinstance(data.get("reason"), str) else None
    text = data.get("message") if isinstance(data.get("message"), str) else response.text[:200]
    message = f"HungerBridge {response.status_code} {code or 'error'}" + (f" ({reason})" if reason else "") + (f": {text}" if text else "")
    cls = _STATUS_ERRORS.get(response.status_code, HungerBridgeError)
    kwargs = dict(status=response.status_code, code=code, reason=reason, body=data or None)
    if cls is HungerBridgeRateLimit:
        return HungerBridgeRateLimit(message, retry_after=parseRetryAfter(response.headers.get("Retry-After")), **kwargs)
    return cls(message, **kwargs)


def checkProtocol(response: requests.Response) -> None:
    """Raise :class:`HungerBridgeProtocolError` unless the server speaks :data:`PROTOCOL_VERSION`."""
    raw = response.headers.get(PROTOCOL_HEADER)
    try:
        version = int(str(raw).split(".", 1)[0]) if raw is not None else None
    except ValueError:
        version = None
    if version != PROTOCOL_VERSION:
        seen = f"protocol v{version}" if version is not None else "no protocol version (HungerBridge before 4.x)"
        raise HungerBridgeProtocolError(
            f"The HungerBridge server speaks {seen}; this client needs protocol v{PROTOCOL_VERSION} "
            f"(HungerBridge 5.x). Update the server jar or use a matching HungerBridgePy.",
            status=response.status_code,
            code="protocol_mismatch",
        )


class Transport:
    """Signs and sends requests to one HungerBridge server. Thread-safe."""

    def __init__(
        self,
        base_url: str,
        credentials: Credentials,
        *,
        max_retries: int = 2,
        retry_backoff: float = 1.0,
        min_interval: float = 0.0,
        timeout: Timeout = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if max_retries < 0:
            raise ValueError("max_retries must be 0 or more")
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials
        self.max_retries = int(max_retries)
        self.retry_backoff = float(retry_backoff)
        self.min_interval = float(min_interval)
        self.timeout = timeout
        self.session = session or requests.Session()
        self._sleep = sleep
        self._last_request = 0.0
        self._throttle_lock = threading.Lock()

    def authHeaders(self, method: str, path: str, payload: Any = None) -> dict:
        return buildAuthHeaders(self.credentials, method, path, payload, base_headers=BASE_HEADERS)

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with self._throttle_lock:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: Any) -> Any:
        return self.request("POST", path, payload)

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        """Send a signed request and return the decoded JSON body.

        HTTP 429 is retried up to ``max_retries`` times, waiting
        ``max(retry_backoff * attempt, Retry-After)``; every attempt is signed afresh (the
        server rate-limits before verifying, so a throttled request has no side effect).
        """
        full_path = "/" + path.lstrip("/")
        url = self.base_url + full_path
        body = canonicalBody(payload) if method.upper() != "GET" else None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            headers = self.authHeaders(method, full_path, payload if body is not None else None)
            try:
                response = self.session.request(method, url, headers=headers, data=body, timeout=self.timeout)
            except requests.exceptions.Timeout as exc:
                raise HungerBridgeConnectionError(f"HungerBridge did not answer in time: {method} {full_path}") from exc
            except requests.exceptions.RequestException as exc:
                raise HungerBridgeConnectionError(f"Cannot reach HungerBridge at {self.base_url}: {exc}") from exc
            checkProtocol(response)
            if response.status_code == 429 and attempt < self.max_retries:
                retry_after = parseRetryAfter(response.headers.get("Retry-After"))
                delay = self.retry_backoff * (attempt + 1)
                if isinstance(retry_after, int):
                    delay = max(delay, float(retry_after))
                log.debug("HungerBridge rate limit on %s %s; retrying in %.1fs", method, full_path, delay)
                self._sleep(delay)
                continue
            if not response.ok:
                raise errorFor(response)
            data = _jsonOrNone(response)
            if not isinstance(data, dict):
                raise HungerBridgeProtocolError(f"HungerBridge sent a non-JSON body for {method} {full_path}", status=response.status_code)
            return data
        raise AssertionError("unreachable")  # pragma: no cover
