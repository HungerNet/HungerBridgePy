"""Python client for the HungerBridge Minecraft HTTP bridge."""

from __future__ import annotations

from .auth import Credentials
from .client import BridgeClient, redeem_pickup
from .exceptions import (
    HungerBridgeAuthError,
    HungerBridgeConnectionError,
    HungerBridgeError,
    HungerBridgeForbidden,
    HungerBridgeNotFound,
    HungerBridgeRateLimit,
    InvalidModeError,
)
from .protocol import PROTOCOL_VERSION
from .stream import Stream

try:  # installed distribution
    from importlib.metadata import PackageNotFoundError, version as _dist_version

    try:
        __version__ = _dist_version("hungerbridge")
    except PackageNotFoundError:
        __version__ = "0.0.0"
except ImportError:  # pragma: no cover
    __version__ = "0.0.0"

#: Alias kept for the name the original ``__init__`` tried to export.
HungerBridge = BridgeClient

__all__ = [
    "BridgeClient",
    "HungerBridge",
    "Stream",
    "Credentials",
    "redeem_pickup",
    "PROTOCOL_VERSION",
    "HungerBridgeError",
    "HungerBridgeRateLimit",
    "HungerBridgeAuthError",
    "HungerBridgeForbidden",
    "HungerBridgeNotFound",
    "HungerBridgeConnectionError",
    "InvalidModeError",
    "__version__",
]
