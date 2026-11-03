# HungerLib

A powerful automation library for Pterodactyl panels.
Provides clean APIs for RCON, scheduling, panel integration, and server orchestration.

## Features

- Simple RCON wrapper
- Pterodactyl API helpers
- Server orchestration utilities
- Scheduler utilities
- Logging helpers
- Zero external configuration required

## Installation

```bash
pip install hungerlib
```

## HungerBridge client

 The `BridgeClient` talks to the HungerBridge API at the root URL, using endpoints such as `/ping`, `/server/info`, `/server/status`, `/world/tps`, `/players/list`, `/server/run`, `/server/log`, and `/server/stream`.

```python
from hungerlib.bridgeclient import BridgeClient

client = BridgeClient('http://localhost:1913', 'abcd1234:secret')
print(client.getPing())
print(client.runCommand('say hello'))
```

The client exposes root-level methods like `ping()`, `info()`, `status()`, `tps()`, and `players()`, without any `/v2` path prefix.
