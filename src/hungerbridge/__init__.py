"""Python client for HungerBridge, the HTTP bridge inside Paper, Purpur, Folia and Fabric servers."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _distVersion

try:
    __version__ = _distVersion("hungerbridge")
except PackageNotFoundError:  # running from a checkout
    __version__ = "0.0.0"

from .auth import Credentials  # noqa: E402
from .client import BridgeClient, redeemPickup  # noqa: E402
from .exceptions import (  # noqa: E402
    HungerBridgeAuthError,
    HungerBridgeConnectionError,
    HungerBridgeError,
    HungerBridgeForbidden,
    HungerBridgeNotFound,
    HungerBridgeProtocolError,
    HungerBridgeRateLimit,
)
from .protocol import BRIDGE_MAJOR, PROTOCOL_VERSION  # noqa: E402
from .releases import BridgeRelease, findRelease  # noqa: E402
from .stream import LogEvent, Stream  # noqa: E402

__all__ = [
    "BridgeClient",
    "Credentials",
    "redeemPickup",
    "Stream",
    "LogEvent",
    "findRelease",
    "BridgeRelease",
    "PROTOCOL_VERSION",
    "BRIDGE_MAJOR",
    "HungerBridgeError",
    "HungerBridgeRateLimit",
    "HungerBridgeAuthError",
    "HungerBridgeForbidden",
    "HungerBridgeNotFound",
    "HungerBridgeConnectionError",
    "HungerBridgeProtocolError",
    "__version__",
]
