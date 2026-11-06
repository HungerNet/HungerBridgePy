# HungerBridgePy

Python client for [HungerBridge](https://github.com/HungerNet/HungerBridge), the HTTP
bridge plugin/mod for **Paper, Purpur, Folia and Fabric** Minecraft servers
(1.21.11, 26.1.2, 26.2, 26.3).

```bash
pip install hungerbridge
```

> The distribution was previously configured as `hungerlib`, which is a different
> PyPI project. The import name has always been `hungerbridge`.

## Quick start

1. On the server console create a token for a policy from `policies.yaml`:

   ```text
   /hb token create my-bot admin
   Pickup passkey: …
   Token created. Retrieve it at: /pickup/<uuid>?passkey=<hex>
   ```

2. Redeem the single-use pickup and talk to the server:

```python
from hungerbridge import BridgeClient

client = BridgeClient.fromPickup("http://127.0.0.1:1913/pickup/<uuid>?passkey=<hex>")
# later: BridgeClient("http://127.0.0.1:1913", "my-bot", "<64 hex chars>")
#    or: BridgeClient("http://127.0.0.1:1913", "my-bot:<64 hex chars>")

print(client.getServerMeta())          # platform, minecraft_version, bridge_version, port, protocol_version
print(client.getTPS("1m"), client.getMSPT())
print(client.runCommand("list"))       # captured console output
client.log("hello from python", level="BRIDGE", thread="bot")
```

## API

| Method | Endpoint | Returns |
|---|---|---|
| `isOk()`, `getServerTime()`, `getPing()` | `GET /ping` | bool, epoch ms, RTT ms |
| `getTokenInfo()` | `GET /auth/check` | `token_id`, `policy_id`, `permissions` |
| `getServerMeta()`, `getPlatform()`, `getMinecraftVersion()`, `getBridgeVersion()`, `getBridgePort()`, `getProtocolVersion()` | `GET /server/meta` | |
| `getPlayers('count'\|'list')`, `getMaxPlayers()` | `GET /players/list` | |
| `getTPS('current'\|'1m'\|'5m'\|'15m')` | `GET /world/tps` | float, `-1` if unavailable |
| `getMSPT()` | `GET /world/mspt` | float |
| `getWorldTime()`, `getWorldTimeInfo()` | `GET /world/time` | ticks of day (0–23999) / dict |
| `getWorldWeather()` | `GET /world/weather` | `clear`/`rain`/`thunder` |
| `getLoadedChunks()`, `getLoadedEntities()` | `GET /world/chunks`, `/world/entities` | `total`, `world`, `world_nether`, `world_the_end` |
| `getUptime()`, `getCPUStats()`, `getMemoryStats(unit)`, `getProcessMemory(unit)`, `getGCStats()`, `getThreadStats()`, `getNetworkStats()` | `GET /system/*` | |
| `runCommand(cmd, showConsole=False, silent=False, normalize=True)` | `POST /server/run` | output text / dict |
| `broadcast(msg, prefix=None)` | `POST /server/run` (`tellraw @a`) | |
| `log(message, level='info', thread=None)` | `POST /server/log` | dict |
| `stopServer()` | `POST /server/stop` | dict |
| `BridgeClient.redeemPickup(url)` / `fromPickup(url)` | `GET /pickup/<id>` | `(token_id, secret)` / client |

Constructor options: `max_retries`, `retry_backoff_sec`, `swallow_rate_limit`,
`static_delay` (minimum gap between requests), `timeout` (default `(5, 30)` seconds),
`session` (custom `requests.Session`).

### Log streaming (SSE)

```python
def on_history(lines): ...
def on_line(line): print(client.stream.sanitize(line))

client = BridgeClient(url, token, history_handler=on_history, new_log_handler=on_line)
client.stream.connect(history=50, reconnect=True)   # background thread
...
client.stream.disconnect()
```

Without handlers lines are buffered in `getRaw()`, `getSanitized()` and `getTimestamped()`.
With `reconnect=True` the stream reconnects with exponential back-off and replays only
the history lines it has not seen yet.

### Errors

All errors derive from `HungerBridgeError` (with `.status`, `.code`, `.reason`):
`HungerBridgeAuthError` (401 — `reason` is one of `invalid_credentials`,
`timestamp_out_of_range`, `nonce_replay`, `token_expired`), `HungerBridgeForbidden` (403),
`HungerBridgeNotFound` (404), `HungerBridgeRateLimit` (429, `.retry_after`),
`HungerBridgeConnectionError` (unreachable/timeout), `InvalidModeError`.

## Protocol

Requests are signed with HMAC-SHA256 over
`METHOD\npath\ntimestamp\nnonce\ncanonical_json_body` (headers `X-Auth-Id`,
`X-Auth-Timestamp`, `X-Auth-Nonce`, `X-Auth-Signature`). The canonical JSON rules are
documented in `hungerbridge/canonical.py`; `tests/data/protocol-vectors.json` holds golden
vectors that are shared with (and verified by) the Java server test suite. The server
advertises its protocol version in the `X-HungerBridge-Protocol` header; the client warns
if it differs from `hungerbridge.PROTOCOL_VERSION`.

## Package layout

| Module | Responsibility |
|---|---|
| `client.py` | `BridgeClient` endpoint wrappers, pickup redemption |
| `transport.py` | HTTP session, timeouts, throttling, 429 retry, error mapping |
| `auth.py` | credentials and HMAC signing |
| `canonical.py` | canonical JSON + path normalization (wire format) |
| `stream.py` | SSE parser and `Stream` |
| `exceptions.py`, `units.py`, `protocol.py` | errors, unit conversion, protocol constants |
| `bridge.py` | backwards-compatible re-exports of the old single-module API |

## Development

```bash
pip install -e .[test]
pytest                                   # unit + protocol tests (no server needed)
HB_PICKUP_URL=http://127.0.0.1:1913/pickup/<id>?passkey=<hex> pytest -m live   # against a real server
```
