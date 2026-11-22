"""Wire-protocol constants shared with the HungerBridge server (the source of truth)."""

from __future__ import annotations

__all__ = ["PROTOCOL_VERSION", "PROTOCOL_HEADER", "BRIDGE_MAJOR"]

#: Protocol version implemented by this client. The server advertises its version in the
#: ``X-HungerBridge-Protocol`` header of every response; any other version is rejected with
#: :class:`~hungerbridge.HungerBridgeProtocolError`.
PROTOCOL_VERSION = 2
PROTOCOL_HEADER = "X-HungerBridge-Protocol"

#: HungerBridge's major version changes exactly when the protocol changes: releases of
#: HungerBridge 5.x speak protocol 2. Release resolution only offers such releases.
BRIDGE_MAJOR = 5
