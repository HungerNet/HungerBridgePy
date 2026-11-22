"""HMAC-SHA256 request signing (HungerBridge protocol v1).

Signature input (``string_to_sign``)::

    METHOD \\n normalized_path \\n timestamp \\n nonce \\n canonical_body

The key is the 32-byte per-token secret handed out once by the server's
``/pickup/<id>?passkey=...`` endpoint, encoded as 64 hex characters.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from .canonical import canonical_body, normalize_path

__all__ = [
    "HEADER_ID",
    "HEADER_TIMESTAMP",
    "HEADER_NONCE",
    "HEADER_SIGNATURE",
    "Credentials",
    "string_to_sign",
    "sign",
    "build_auth_headers",
]

HEADER_ID = "X-Auth-Id"
HEADER_TIMESTAMP = "X-Auth-Timestamp"
HEADER_NONCE = "X-Auth-Nonce"
HEADER_SIGNATURE = "X-Auth-Signature"


@dataclass(frozen=True)
class Credentials:
    """A HungerBridge API token (id + hex secret)."""

    token_id: str
    secret: str = field(repr=False)

    @classmethod
    def parse(cls, token_id: str | None, token_secret: str | None = None) -> "Credentials | None":
        """Build credentials from ``(id, secret)`` or a single ``"id:secret"`` string.

        Returns ``None`` when no usable credentials were supplied (unauthenticated
        client; every protected endpoint will answer 401).
        """
        if token_id and not token_secret and ":" in token_id:
            token_id, _, token_secret = token_id.rpartition(":")
        if not token_id or not token_secret:
            return None
        return cls(token_id=token_id.strip(), secret=token_secret.strip())

    @property
    def key(self) -> bytes:
        try:
            return bytes.fromhex(self.secret)
        except ValueError:
            # Historical fallback: non-hex secrets are used as raw UTF-8 bytes.
            return self.secret.encode("utf-8")


def string_to_sign(method: str, path: str, timestamp: str, nonce: str, body: str) -> str:
    return f"{method.strip().upper()}\n{normalize_path(path)}\n{timestamp}\n{nonce}\n{body}"


def sign(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def build_auth_headers(
    credentials: Credentials | None,
    method: str,
    path: str,
    payload: Any = None,
    *,
    base_headers: Mapping[str, str] | None = None,
    timestamp: int | str | None = None,
    nonce: str | None = None,
) -> dict:
    """Return request headers including a fresh signature.

    A new nonce and timestamp are generated on every call; never reuse the
    returned headers for another request (including retries) — the server
    rejects replayed nonces.
    """
    headers = dict(base_headers or {})
    if credentials is None:
        return headers
    ts = str(int(time.time()) if timestamp is None else timestamp)
    n = nonce or uuid.uuid4().hex
    message = string_to_sign(method, path, ts, n, canonical_body(payload))
    headers.update(
        {
            HEADER_ID: credentials.token_id,
            HEADER_TIMESTAMP: ts,
            HEADER_NONCE: n,
            HEADER_SIGNATURE: sign(credentials.key, message),
        }
    )
    return headers
