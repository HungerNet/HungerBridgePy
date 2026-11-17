# Changelog

## 1.0-dev.2 (2026-11)

### Added
* `BridgeClient.streamLogs(history, *, timeout, reconnect, sanitize)`: a blocking iterator
  over `/server/stream` (used by CalagoAPI's `MinecraftServer.streamConsole()`).
* `BridgeClient.openStream(...)`: an independent `Stream` per consumer.
* `Stream(close_handler=...)`: called with the ending error (or `None`) when the worker stops.
* `hungerbridge.releases`: `find_release()`, `list_release_assets()`, `parse_asset_name()`,
  `loader_for()`, `ReleaseAsset`. Understands the new `HungerBridge-<loader>-<mc>-<version>.jar`
  asset names and the older `<mc>-<loader>-<version>.jar` ones.
* `MAX_HISTORY` (200), the server's history cap.
* `getTPSStats()` (every TPS window in one request) and `getPlayerStats()` (count, slots and
  names in one request).

### Changed
* Stream errors are typed (`HungerBridgeAuthError` with `reason`, `HungerBridgeForbidden`,
  `HungerBridgeNotFound`, `HungerBridgeRateLimit`, `HungerBridgeConnectionError`) instead of
  `requests.HTTPError`. With `reconnect=True`, 401/403/404 now end the stream instead of being
  retried forever.
* `getTokenInfo()` also returns `expiry`.

### Fixed
* `Stream.disconnect()` blocked for its whole timeout on Windows: `shutdown()` does not wake a
  blocked `recv()` there. The socket handle is now closed directly.

## 1.0-dev.1 (2026-11)

### Fixed
* The package could not be imported (`hungerbridge/__init__.py` imported a missing
  `HungerBridge` symbol, `bridge.py` imported a missing `hungerbridge.utils` package).
* Signatures failed (and payloads were corrupted) for characters outside the BMP such as
  emoji: they were escaped as `ὠ0` instead of the UTF-16 surrogate pair the server uses.
* Retries after HTTP 429 re-sent the same nonce; every attempt is now signed afresh and
  `Retry-After` is honoured.
* No timeouts anywhere (a dead server hung the caller forever); default `(5, 30)` s.
* SSE: leading whitespace of log lines (stack-trace indentation) was stripped; without a
  `history` request the first 15 s of lines were held back as "history"; lines were split on
  Unicode separators; `disconnect()` could block for the read timeout.
* `broadcast()` produced invalid `tellraw` JSON for backslashes/newlines.
* `getTimestamped()` never matched the server's `[HH:mm:ss LEVEL]` lines.
* `requires-python >= 3.9` was not true (PEP 604 annotations at runtime); now tested on 3.9.

### Changed
* `bridge.py` split into `client`, `transport`, `auth`, `canonical`, `stream`, `exceptions`,
  `units`, `protocol`; `hungerbridge.bridge` re-exports the old names.
* Distribution renamed from `hungerlib` (an unrelated existing PyPI project) to
  `hungerbridge`. **Maintainers:** configure PyPI trusted publishing for `hungerbridge`.
* Errors are typed: `HungerBridgeAuthError` (401, `.reason`), `HungerBridgeForbidden`,
  `HungerBridgeNotFound`, `HungerBridgeRateLimit` (`.retry_after`),
  `HungerBridgeConnectionError` — all subclasses of `HungerBridgeError`.

### Added
* `BridgeClient(url, "id:secret")`, `BridgeClient.fromPickup(url)`, `redeem_pickup()`.
* `getProtocolVersion()`, `getWorldTimeInfo()`; protocol-version mismatch warning.
* `Stream.connect(..., reconnect=True)` with back-off and history de-duplication;
  `Stream.wait_until_connected()`.
* Test suite (unit, protocol golden vectors shared with the server, live suite).
