# Migrating from HungerBridgePy 1.x to 2.0

HungerBridgePy 2 speaks protocol v2 and needs **HungerBridge 5.x** on the server (a server
with another protocol raises `HungerBridgeProtocolError`). Backward compatibility was
deliberately not kept; every change is listed here.

## Authentication

```python
# 1.x
BridgeClient(url, "bot:9f2c...")
BridgeClient(url, "bot", "9f2c...")

# 2.0: keyword arguments only, both required, the secret must be 64 hex characters
BridgeClient(url, token_id="bot", token_secret="9f2c...")
```

`Credentials.parse()` and the `"id:secret"` format are gone; `Credentials(token_id=...,
token_secret=...)` validates its values. `redeem_pickup()` is `redeemPickup(url)` and
returns `Credentials`.

## Constructor options

| 1.x | 2.0 |
|---|---|
| `retry_backoff_sec` | `retry_backoff` |
| `static_delay` | `min_interval` |
| `max_retries=0` | `max_retries=2` (default) |
| `swallow_rate_limit` | removed (429 always raises after the retries) |
| `history_handler`, `new_log_handler`, `client.stream` | removed; use `client.openStream(...)` |

## Commands

```python
# 1.x
client.runCommand("list", showConsole=False, silent=False, normalize=True)
# 2.0
client.runCommand("list", show_console=False)        # always returns the output text
```

`show_console=False` now really keeps the output off the server console. `silent` and the
raw-dict mode (`normalize=False`) are removed. A timeout raises `HungerBridgeError` with
`code == "command_timeout"`.

## Logging

`log(message, level="INFO", thread=None, logger=None, normalize=False)`; `level=None` (the
backspace trick) is removed. It returns the logged text instead of the response dict.

## Getters

| 1.x | 2.0 |
|---|---|
| `getPlayers("count")` / `getPlayers("list")` | `getPlayerCount()` / `getPlayers()` |
| `getTPS(mode)` | `getTPS(window)` |
| `getBridgePort()`, `getProtocolVersion()` | `getServerMeta()["port"]`, `["protocol_version"]` |
| `getMemoryStats()["used"/"total"/"free"/"max"]` | `["heap_used"/"heap_committed"/"heap_free"/"heap_max"]` |
| `getTokenInfo()` | unchanged keys (`token_id`, `policy_id`, ...) |
| `InvalidModeError` | `ValueError` |
| `stopServer()` returned a dict | returns `None` |

## Streaming

```python
# 1.x: strings
for line in client.streamLogs(history=50, reconnect=True, sanitize=True):
    print(line)

# 2.0: LogEvent objects; str(event) is the line, already plain text
for event in client.streamLogs(history=50):          # reconnect=True by default
    print(event.line, event.logger)
```

`Stream(close_handler=...)` → `openStream(on_event=, on_history=, on_close=)`;
`wait_until_connected` → `waitUntilConnected`. `Stream.sanitize()`, `extractTimestamp()`,
`getRaw()`, `getSanitized()`, `getTimestamped()` and `MAX_HISTORY` are removed: lines are
plain, events carry their time, and the server owns the history size (`history=True` = all).

## Releases

```python
# 1.x: GitHub releases, file-name parsing
find_release("1.21.11", "PURPUR")
# 2.0: Modrinth metadata, loader as published on Modrinth
findRelease(minecraft_version="1.21.11", loader="purpur")
```

`ReleaseAsset` → `BridgeRelease` (`version_number`, `filename`, `url`, `sha512`,
`directory`, ...). `"latest"` as a Minecraft version, `list_release_assets`,
`parse_asset_name`, `loader_for`, `GITHUB_API` and `DEFAULT_REPOSITORY` are removed.

## Removed modules and names

`hungerbridge.bridge`, `HungerBridge` (alias), `units.convert` / `convert_time`
(`convertBytes` remains), all snake_case function names (now camelCase: `stringToSign`,
`buildAuthHeaders`, `canonicalJson`, `normalizePath`, `parseSse`, ...).
