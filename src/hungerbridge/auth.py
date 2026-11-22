"""Credentials and HMAC-SHA256 request signing.

Signature input::

    METHOD \\n normalized_path \\n timestamp \\n nonce \\n canonical_body

The key is the 32-byte per-token secret handed out once by the server's
``/pickup/<id>?passkey=...`` endpoint, as 64 hex characters (``token_secret``).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

from .canonical import canonicalBody, normalizePath

__all__ = [
    "HEADER_ID",
    "HEADER_TIMESTAMP",
    "HEADER_NONCE",
    "HEADER_SIGNATURE",
    "Credentials",
    "stringToSign",
    "sign",
    "buildAuthHeaders",
]

HEADER_ID = "X-Auth-Id"
HEADER_TIMESTAMP = "X-Auth-Timestamp"
HEADER_NONCE = "X-Auth-Nonce"
HEADER_SIGNATURE = "X-Auth-Signature"

_SECRET = re.compile(r"[0-9a-fA-F]{64}")


@dataclass(frozen=True)
class Credentials:
    """A HungerBridge API token: ``token_id`` and its 64-hex-character ``token_secret``.

    Raises:
        ValueError: an empty id, or a secret that is not 64 hex characters.
    """

    token_id: str
    token_secret: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.token_id, str) or not self.token_id.strip():
            raise ValueError("token_id must be a non-empty string")
        if not isinstance(self.token_secret, str) or not _SECRET.fullmatch(self.token_secret.strip()):
            raise ValueError("token_secret must be the 64 hex characters from the token pickup")
        object.__setattr__(self, "token_id", self.token_id.strip())
        object.__setattr__(self, "token_secret", self.token_secret.strip().lower())

    @property
    def key(self) -> bytes:
        return bytes.fromhex(self.token_secret)


def stringToSign(method: str, path: str, timestamp: str, nonce: str, body: str) -> str:
    return f"{method.strip().upper()}\n{normalizePath(path)}\n{timestamp}\n{nonce}\n{body}"


def sign(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def buildAuthHeaders(
    credentials: Credentials,
    method: str,
    path: str,
    payload: Any = None,
    *,
    base_headers: Optional[Mapping[str, str]] = None,
    timestamp: Optional[Union[int, str]] = None,
    nonce: Optional[str] = None,
) -> dict:
    """Return request headers including a fresh signature.

    A new nonce and timestamp are generated on every call; never reuse the returned headers
    for another request (including retries) — the server rejects replayed nonces.
    """
    headers = dict(base_headers or {})
    ts = str(int(time.time()) if timestamp is None else timestamp)
    n = nonce or uuid.uuid4().hex
    message = stringToSign(method, path, ts, n, canonicalBody(payload))
    headers.update(
        {
            HEADER_ID: credentials.token_id,
            HEADER_TIMESTAMP: ts,
            HEADER_NONCE: n,
            HEADER_SIGNATURE: sign(credentials.key, message),
        }
    )
    return headers
