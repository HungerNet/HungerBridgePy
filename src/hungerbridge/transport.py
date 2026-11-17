"""HTTP transport: signing, throttling, retries, timeouts and error mapping."""

from __future__ import annotations

import threading
import time
import warnings
from typing import Any, Callable, Optional, Tuple, Union

import requests

from .auth import Credentials, build_auth_headers
from .canonical import canonical_body
from .exceptions import (
    HungerBridgeAuthError,
    HungerBridgeConnectionError,
    HungerBridgeError,
    HungerBridgeForbidden,
    HungerBridgeNotFound,
    HungerBridgeRateLimit,
)
from .protocol import PROTOCOL_HEADER, PROTOCOL_VERSION

__all__ = ["Transport", "DEFAULT_TIMEOUT"]

Timeout = Union[float, Tuple[float, float], None]

#: (connect, read) seconds. ``/server/run`` waits up to 5 s for the game
#: thread server-side, so 30 s of read time is generous.
DEFAULT_TIMEOUT: Tuple[float, float] = (5.0, 30.0)

BASE_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

_STATUS_ERRORS = {
    401: HungerBridgeAuthError,
    403: HungerBridgeForbidden,
    404: HungerBridgeNotFound,
}


def parse_retry_after(value: Optional[str]) -> Union[int, str, None]:
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return value  # HTTP-date form; surfaced verbatim


class Transport:
    """Signs and sends requests to one HungerBridge server.

    Thread-safe: a single instance may be shared between threads.
    """

    def __init__(
        self,
        base_url: str,
        credentials: Optional[Credentials],
        *,
        max_retries: int = 0,
        retry_backoff_sec: float = 1.0,
        swallow_rate_limit: bool = False,
        static_delay: float = 0.0,
        timeout: Timeout = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials
        self.max_retries = int(max_retries or 0)
        self.retry_backoff_sec = float(retry_backoff_sec or 1.0)
        self.swallow_rate_limit = bool(swallow_rate_limit)
        self.static_delay = float(static_delay or 0.0)
        self.timeout = timeout
        self.session = session or requests.Session()
        self._sleep = sleep
        self._last_request_time = 0.0
        self._throttle_lock = threading.Lock()
        #: Protocol version advertised by the server (``None`` until the first response).
        self.server_protocol_version: Optional[int] = None
        self._warned_protocol = False

    # -- signing -----------------------------------------------------------------

    def auth_headers(self, method: str, path: str, payload: Any = None) -> dict:
        return build_auth_headers(self.credentials, method, path, payload, base_headers=BASE_HEADERS)

    # -- throttle ----------------------------------------------------------------

    def _throttle(self) -> None:
        if self.static_delay <= 0:
            return
        with self._throttle_lock:
            delta = time.time() - self._last_request_time
            if delta < self.static_delay:
                self._sleep(self.static_delay - delta)
            self._last_request_time = time.time()

    # -- requests ----------------------------------------------------------------

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: Any) -> Any:
        return self.request("POST", path, payload)

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        """Send a signed request and return the decoded JSON (or text) body.

        429 responses are retried up to ``max_retries`` times, waiting
        ``max(retry_backoff_sec * attempt, Retry-After)``. Each attempt is
        signed with a fresh nonce/timestamp. Returns ``None`` instead of
        raising on a final 429 when ``swallow_rate_limit`` is set.
        """
        self._throttle()
        full_path = "/" + path.lstrip("/")
        url = self.base_url + full_path
        body = canonical_body(payload) if method.upper() != "GET" else None

        for attempt in range(self.max_retries + 1):
            headers = self.auth_headers(method, full_path, payload if body is not None else None)
            try:
                response = self.session.request(method, url, headers=headers, data=body, timeout=self.timeout)
            except requests.exceptions.Timeout as exc:
                raise HungerBridgeConnectionError(f"HungerBridge request timed out: {method} {full_path}") from exc
            except requests.exceptions.ConnectionError as exc:
                raise HungerBridgeConnectionError(f"Cannot reach HungerBridge at {self.base_url}: {exc}") from exc

            self._observe_protocol(response)

            if response.status_code == 429:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if attempt < self.max_retries:
                    delay = self.retry_backoff_sec * (attempt + 1)
                    if isinstance(retry_after, (int, float)):
                        delay = max(delay, float(retry_after))
                    self._sleep(delay)
                    continue
                if self.swallow_rate_limit:
                    return None
                raise HungerBridgeRateLimit(retry_after=retry_after, body=_json_or_none(response))

            if not response.ok:
                raise self._error_for(response)

            return _json_or_text(response)
        return None  # pragma: no cover - loop always returns or raises

    # -- helpers -----------------------------------------------------------------

    def _observe_protocol(self, response: requests.Response) -> None:
        raw = response.headers.get(PROTOCOL_HEADER)
        if raw is None:
            return
        try:
            version = int(str(raw).split(".", 1)[0])
        except ValueError:
            return
        self.server_protocol_version = version
        if version != PROTOCOL_VERSION and not self._warned_protocol:
            self._warned_protocol = True
            warnings.warn(
                f"HungerBridge server speaks protocol v{version}, this client implements v{PROTOCOL_VERSION}",
                RuntimeWarning,
                stacklevel=4,
            )

    @staticmethod
    def _error_for(response: requests.Response) -> HungerBridgeError:
        data = _json_or_none(response)
        code = reason = None
        if isinstance(data, dict):
            code = data.get("error") if isinstance(data.get("error"), str) else None
            reason = data.get("reason") if isinstance(data.get("reason"), str) else None
        cls = _STATUS_ERRORS.get(response.status_code, HungerBridgeError)
        # Message format kept from the original client for log/grep compatibility.
        return cls(
            f"HungerBridge error {response.status_code}: {response.text}",
            status=response.status_code,
            code=code,
            reason=reason,
            body=data,
        )


def _json_or_none(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
