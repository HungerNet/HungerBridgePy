from __future__ import annotations

__all__ = ['PROTOCOL_VERSION', 'PROTOCOL_HEADER', 'SSE_HISTORY_END', 'SSE_KEEPALIVE']

#: Protocol major version implemented by this client. The server advertises
#: its version in the ``X-HungerBridge-Protocol`` response header and in
#: ``/server/meta`` (``protocol_version``). Servers that predate versioning
#: send neither and are treated as version 1.
PROTOCOL_VERSION = 1
PROTOCOL_HEADER = 'X-HungerBridge-Protocol'

#: SSE comment sent by the server right after the requested history has been
#: written (also sent when no history was requested). Older clients ignore it.
SSE_HISTORY_END = 'history-end'
#: SSE comment sent every 15 seconds by the server to keep the stream alive.
SSE_KEEPALIVE = 'keepalive'
