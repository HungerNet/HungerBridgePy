"""Backwards-compatibility module.

The client used to live entirely in this file. It has been split into:

* :mod:`hungerbridge.canonical` – canonical JSON / path normalization
* :mod:`hungerbridge.auth`      – HMAC request signing
* :mod:`hungerbridge.transport` – HTTP, retries, timeouts, error mapping
* :mod:`hungerbridge.stream`    – SSE log stream
* :mod:`hungerbridge.client`    – ``BridgeClient`` endpoint wrappers
* :mod:`hungerbridge.exceptions`, :mod:`hungerbridge.units`

Existing imports such as ``from hungerbridge.bridge import BridgeClient``
keep working.
"""

from __future__ import annotations

from .canonical import canonical_body as _canonical_body
from .canonical import normalize_path as _normalize_path
from .client import BridgeClient
from .exceptions import HungerBridgeError, HungerBridgeRateLimit, InvalidModeError
from .stream import Stream
from .units import convert

__all__ = ["BridgeClient", "Stream", "HungerBridgeError", "HungerBridgeRateLimit", "InvalidModeError", "convert"]


def _canonicalize_json_body(value):
    """Deprecated private helper kept for compatibility (``None`` → ``''``)."""
    return _canonical_body(value)
