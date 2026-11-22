# PLAN: HungerBridgePy 2.0 (protocol v2)

The ecosystem plan lives in HungerBridge, the source of truth:
[HungerBridge/PLAN.md](https://github.com/HungerNet/HungerBridge/blob/main/PLAN.md)
(locally `../HungerBridge-1/PLAN.md`). This file lists this repository's part of it.

## What changes here

| Area | Change | Plan section |
|---|---|---|
| Authentication | `BridgeClient(url, *, token_id, token_secret)` only; `"id:secret"`, the UTF-8 secret fallback and `Credentials.parse` are removed; the secret must be 64 hex characters | §8, §13.2 |
| Commands | `runCommand(command, show_console=False) -> str`; `silent`/`normalize` removed; HTTP 504 `command_timeout` raised | §7 |
| Logging | `log(message, level="INFO", thread=None, logger=None, normalize=False)` | §6.2, §6.4 |
| Streaming | protocol v2 SSE: `LogEvent` objects, sequence ids, `Last-Event-ID` resume, gap reporting, no implicit buffers; `openStream(on_event, on_history, on_close)`, `streamLogs()` | §4 |
| Releases | Modrinth only: `findRelease(minecraft_version, loader, *, version=None) -> BridgeRelease`; GitHub code and file-name parsing removed | §5 |
| Transport | protocol mismatch raises `HungerBridgeProtocolError`; `swallow_rate_limit` removed; monotonic throttle (`min_interval`) | §4.1 |
| Naming | camelCase functions, snake_case parameters (§8 table) | §8 |
| Legacy | `hungerbridge.bridge`, `HungerBridge` alias, `units.convert`, legacy SSE heuristics removed | §1.4 |
| Hygiene | committed `__pycache__` and `egg-info` untracked; Python `.gitignore` | §9 |

Breaking changes: [MIGRATING.md](MIGRATING.md) and the plan's §14.
