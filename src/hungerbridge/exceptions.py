"""Exception hierarchy. Every error raised by this package is a ``HungerBridgeError``."""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "HungerBridgeError",
    "HungerBridgeRateLimit",
    "HungerBridgeAuthError",
    "HungerBridgeForbidden",
    "HungerBridgeNotFound",
    "HungerBridgeConnectionError",
    "HungerBridgeProtocolError",
]


class HungerBridgeError(Exception):
    """Base error. ``status``, ``code`` and ``reason`` mirror the server's error body.

    Server error bodies look like ``{"ok": false, "error": "<code>", "message": "<text>",
    "reason": "<optional detail>"}``. Notable codes: ``command_timeout`` (HTTP 504, the server
    thread did not run a command in time), ``too_many_clients`` (503, stream limit).
    """

    def __init__(
        self,
        message: str = "",
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
        reason: Optional[str] = None,
        body: Any = None,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.reason = reason
        self.body = body


class HungerBridgeRateLimit(HungerBridgeError):
    """HTTP 429. ``retry_after`` is the server's ``Retry-After`` hint in seconds, if any."""

    def __init__(self, message: Optional[str] = None, *, retry_after: Any = None, **kwargs: Any):
        if message is None:
            message = "HungerBridge 429 rate_limited"
            if retry_after is not None:
                message += f": retry after {retry_after}s"
        kwargs.setdefault("status", 429)
        kwargs.setdefault("code", "rate_limited")
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class HungerBridgeAuthError(HungerBridgeError):
    """HTTP 401. ``reason`` is one of ``missing_credentials``, ``invalid_credentials``,
    ``timestamp_out_of_range`` (clock skew), ``nonce_replay``, ``token_revoked``,
    ``token_expired``."""


class HungerBridgeForbidden(HungerBridgeError):
    """HTTP 403: the token lacks the endpoint's permission node, or the client IP is not
    allowed by ``security.yaml`` (``reason == "ip_not_allowed"``)."""


class HungerBridgeNotFound(HungerBridgeError):
    """HTTP 404: unknown endpoint or pickup; also a release that does not exist."""


class HungerBridgeConnectionError(HungerBridgeError):
    """The server (or Modrinth) could not be reached or did not answer in time."""


class HungerBridgeProtocolError(HungerBridgeError):
    """The server speaks a different protocol version than this client (see
    :data:`hungerbridge.PROTOCOL_VERSION`), or sent data that violates the protocol."""
