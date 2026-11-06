'''Exception hierarchy. Every error raised by this package is a ``HungerBridgeError``.'''

from __future__ import annotations

from typing import Any

import requests

__all__ = [
    'HungerBridgeError',
    'HungerBridgeRateLimit',
    'HungerBridgeAuthError',
    'HungerBridgeForbidden',
    'HungerBridgeNotFound',
    'HungerBridgeConnectionError',
    'InvalidModeError',
]


class HungerBridgeError(Exception):
    '''Base error. ``status``/``code``/``reason`` mirror the server's error body.

    Server error bodies look like ``{'ok': false, 'error': '<code>',
    'message': '<text>', 'reason': '<optional detail>'}``.
    '''

    def __init__(
        self,
        message: str = '',
        *,
        status: int | None = None,
        code: str | None = None,
        reason: str | None = None,
        body: Any = None,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.reason = reason
        self.body = body


class HungerBridgeRateLimit(HungerBridgeError):
    '''HTTP 429. ``retry_after`` is the server's ``Retry-After`` hint (seconds) if any.'''

    def __init__(self, message: str | None = None, *, retry_after: Any = None, **kwargs: Any):
        if message is None:
            message = 'HungerBridge rate limit exceeded'
            if retry_after is not None:
                message += f' (retry after {retry_after}s)'
        kwargs.setdefault('status', 429)
        kwargs.setdefault('code', 'rate_limited')
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class HungerBridgeAuthError(HungerBridgeError):
    '''HTTP 401: missing/invalid signature, unknown/revoked/expired token,
    clock skew (``reason == 'timestamp_out_of_range'``) or nonce replay.'''


class HungerBridgeForbidden(HungerBridgeError):
    '''HTTP 403: authenticated, but the token lacks the permission node
    (or the client IP is not allowed by ``security.yaml``).'''


class HungerBridgeNotFound(HungerBridgeError):
    '''HTTP 404: unknown endpoint or pickup.'''


class HungerBridgeConnectionError(HungerBridgeError, requests.exceptions.ConnectionError):
    '''The server could not be reached or did not answer in time.

    Also a ``requests.ConnectionError`` so code written against the previous
    client (which leaked raw ``requests`` exceptions) keeps working.
    '''


class InvalidModeError(HungerBridgeError, ValueError):
    '''An unsupported ``mode`` argument was passed to a client getter.'''
