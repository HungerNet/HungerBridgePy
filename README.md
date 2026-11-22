# HungerBridgePy

Python client for [HungerBridge](https://modrinth.com/project/hungerbridge), the HTTP bridge
plugin/mod for **Paper, Purpur, Folia and Fabric** Minecraft servers (1.21.11, 26.1.2, 26.2,
26.3). HungerBridgePy 2 speaks protocol v2 (HungerBridge 5.x).

```bash
pip install hungerbridge
```

Upgrading from 1.x: [MIGRATING.md](MIGRATING.md).

## Quick start

1. On the server console create a token for a policy from `policies.yaml`:

   ```text
   /hb token create my-bot admin
   HungerBridge » Token created
     ✔ my-bot uses policy admin
     • Pickup URL  /pickup/<uuid>?passkey=<hex>
   ```

2. Redeem the single-use pickup and talk to the server:

```python
from hungerbridge import BridgeClient

client = BridgeClient.fromPickup("http://127.0.0.1:1913/pickup/<uuid>?passkey=<hex>")
print(client.credentials.token_id)           # store token_id and token_secret
# later:
client = BridgeClient("http://127.0.0.1:1913", token_id="my-bot", token_secret="<64 hex chars>")

print(client.getServerMeta())                # platform, minecraft_version, bridge_version, port, protocol_version
print(client.getTPS("1m"), client.getMSPT(), client.getPlayers())
print(client.runCommand("list"))             # the command's output; not printed on the server console
client.log(message="example message", level="CUSTOMLEVEL", thread="CustomThread", logger="CustomLogger")
#  → [10:53:54] [CustomThread/CUSTOMLEVEL]: [CustomLogger] example message
```

The token id and secret are always two separate values; the constructor takes them as
keyword arguments and validates them (`ValueError` otherwise).

## API

| Method | Endpoint | Returns |
|---|---|---|
| `isOk()`, `getServerTime()`, `getPing()` | `GET /ping` | bool, epoch ms, RTT ms |
| `getTokenInfo()` | `GET /auth/check` | `token_id`, `policy_id`, `permissions`, `expiry` |
| `getServerMeta()`, `getPlatform()`, `getMinecraftVersion()`, `getBridgeVersion()` | `GET /server/meta` | dict / str |
| `getPlayers()`, `getPlayerCount()`, `getMaxPlayers()`, `getPlayerStats()` | `GET /players/list` | names, counts |
| `getTPS(window='current'\|'1m'\|'5m'\|'15m')`, `getTPSStats()` | `GET /world/tps` | float, `-1` if unavailable |
| `getMSPT()` | `GET /world/mspt` | float |
| `getWorldTime()`, `getWorldTimeInfo()`, `getWorldWeather()` | `GET /world/time`, `/world/weather` | tick of day / dict / `clear`/`rain`/`thunder` |
| `getLoadedChunks()`, `getLoadedEntities()` | `GET /world/chunks`, `/world/entities` | `total`, `world`, `world_nether`, `world_the_end`, … |
| `getUptime()`, `getCPUStats()`, `getMemoryStats(unit='mib')`, `getGCStats()`, `getThreadStats()`, `getNetworkStats()` | `GET /system/*` | |
| `runCommand(command, show_console=False)` | `POST /server/run` | output text |
| `broadcast(message, prefix=None)` | `POST /server/run` (`tellraw @a`) | |
| `log(message, level='INFO', thread=None, logger=None, normalize=False)` | `POST /server/log` | the logged text |
| `stopServer()` | `POST /server/stop` | |
| `streamLogs(history=None, *, timeout=None, reconnect=True)` | `GET /server/stream` | iterator of `LogEvent` |
| `openStream(*, on_event, on_history, on_close)` | `GET /server/stream` | `Stream` |
| `BridgeClient.fromPickup(url, **options)`, `redeemPickup(url)` | `GET /pickup/<id>` | client / `Credentials` |
| `findRelease(minecraft_version, loader, *, version=None)` | Modrinth | `BridgeRelease` |

Constructor options: `max_retries` (after HTTP 429, default 2), `retry_backoff` (seconds),
`min_interval` (seconds between requests), `timeout` (seconds or `(connect, read)`, default
`(5, 30)`), `session`.

### Commands

`runCommand()` returns the command's feedback. It is captured directly by HungerBridge and
is **not** printed on the server console unless you ask for it:

```python
client.runCommand("seed")                       # returned, console stays quiet
client.runCommand("save-all", show_console=True)  # returned and shown on the console
```

A command the server thread does not run within 5 seconds raises `HungerBridgeError` with
`code == "command_timeout"` (HTTP 504).

### Logging

`log()` writes to the server log with any level, thread label and logger name (defaults:
`INFO`, `HungerBridge`, `HungerBridge`). Custom level names use letters, digits and `_`.
`normalize=True` makes the server reduce the message to plain text first: JSON text
components are flattened, formatting codes and chat decorations are removed:

```python
client.log("System chat: Hello", normalize=True)     # logs and returns "Hello"
```

### Log streaming

```python
for event in client.streamLogs(history=50):
    if event.logger == "CustomLogger":
        print(event.level, event.thread, event.message)
    print(event)                                  # the console line
```

Each `LogEvent` has `seq`, `time` (epoch ms), `level`, `thread`, `logger`, `message`,
`throwable` and `line` (`[HH:mm:ss] [thread/LEVEL]: [logger] message`, rendered by the
server, identical on every loader). `history=True` replays everything the server buffers
(1000 events). The iterator ends when nothing arrives for `timeout` seconds, raises the
error that ended the stream, and disconnects when you leave the loop.

Reconnects resume exactly: the stream sends `Last-Event-ID` and the server replays only the
events that were missed, so nothing is lost or duplicated. If the server can no longer
replay them (it restarted, or more than 1000 events passed), the stream logs a warning on
the `hungerbridge` logger and counts it in `stream.gaps`. Authentication, permission and
protocol errors end the stream instead of being retried.

For callbacks in a background thread:

```python
stream = client.openStream(on_event=print, on_history=lambda events: None, on_close=print)
stream.connect(history=20)
stream.waitUntilConnected(10)
...
stream.disconnect()
```

### Releases (Modrinth)

```python
from hungerbridge import findRelease

release = findRelease(minecraft_version="1.21.11", loader="paper")
print(release.version_number, release.filename, release.url, release.sha512, release.directory)   # /plugins
```

Releases are resolved only through [Modrinth](https://modrinth.com/project/hungerbridge),
by its loader and game-version metadata. Only releases of HungerBridge 5.x (the protocol
this client speaks) are offered. Unknown loaders or Minecraft versions raise `ValueError`
listing what is published; no compatible release raises `HungerBridgeNotFound`.

### Errors

All errors derive from `HungerBridgeError` (`.status`, `.code`, `.reason`, `.body`):
`HungerBridgeAuthError` (401; `reason` is `invalid_credentials`, `timestamp_out_of_range`,
`nonce_replay`, `token_revoked`, `token_expired`, `missing_credentials`),
`HungerBridgeForbidden` (403), `HungerBridgeNotFound` (404), `HungerBridgeRateLimit` (429,
`.retry_after`), `HungerBridgeConnectionError` (unreachable/timeout) and
`HungerBridgeProtocolError` (the server speaks another protocol version, or sent invalid data).

Diagnostics (reconnects, gaps, retries) go to the standard `logging` logger `hungerbridge`.

## Protocol

Requests are signed with HMAC-SHA256 over `METHOD\npath\ntimestamp\nnonce\ncanonical_json_body`
(headers `X-Auth-Id`, `X-Auth-Timestamp`, `X-Auth-Nonce`, `X-Auth-Signature`). The canonical
JSON rules are in `hungerbridge/canonical.py`; `tests/data/protocol-vectors.json` holds golden
vectors shared with the Java server's tests. The full wire format is HungerBridge's
[ENDPOINTS.md](https://github.com/HungerNet/HungerBridge/blob/main/ENDPOINTS.md).

## Package layout

| Module | Responsibility |
|---|---|
| `client.py` | `BridgeClient`, `redeemPickup()` |
| `transport.py` | HTTP session, timeouts, throttling, 429 retry, protocol check, error mapping |
| `auth.py` | `Credentials`, HMAC signing |
| `canonical.py` | canonical JSON + path normalization (wire format) |
| `stream.py` | SSE parser, `LogEvent`, `Stream` |
| `releases.py` | Modrinth release resolution (`findRelease`, `BridgeRelease`) |
| `exceptions.py`, `units.py`, `protocol.py` | errors, byte units, protocol constants |

## Development

```bash
pip install -e .[test]
pytest                                                    # unit + protocol tests (no server needed)
HB_PICKUP_URL=http://127.0.0.1:1913/pickup/<id>?passkey=<hex> pytest -m live tests/test_live.py
HB_MODRINTH=1 pytest -m modrinth                          # against the real Modrinth API
```

HungerBridge's `./gradlew :common:standaloneBridge` runs the real server HTTP stack without
Minecraft for the live suite.
