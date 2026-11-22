import importlib
import json

import pytest

import hungerbridge
from hungerbridge import (
    BridgeClient,
    Credentials,
    HungerBridgeError,
    HungerBridgeNotFound,
    HungerBridgeProtocolError,
    redeemPickup,
)

from conftest import SECRET


@pytest.fixture
def client(fake_server):
    return BridgeClient(fake_server.url, token_id="tok", token_secret=SECRET)


def test_public_api():
    assert hungerbridge.PROTOCOL_VERSION == 2 and hungerbridge.BRIDGE_MAJOR == 5
    for removed in ("HungerBridge", "redeem_pickup", "find_release", "ReleaseAsset", "MAX_HISTORY", "InvalidModeError"):
        assert not hasattr(hungerbridge, removed), removed
    with pytest.raises(ImportError):
        importlib.import_module("hungerbridge.bridge")


def test_constructor_requires_explicit_credentials(fake_server):
    with pytest.raises(TypeError):
        BridgeClient(fake_server.url, "tok", SECRET)                 # positional: not allowed
    with pytest.raises(TypeError):
        BridgeClient(fake_server.url, token_id="tok")                # secret missing
    with pytest.raises(ValueError, match="64 hex"):
        BridgeClient(fake_server.url, token_id="tok", token_secret=f"tok:{SECRET}")
    with pytest.raises(ValueError, match="http"):
        BridgeClient("127.0.0.1:1913", token_id="tok", token_secret=SECRET)
    client = BridgeClient(fake_server.url + "/", token_id="tok", token_secret=SECRET)
    assert client.base == fake_server.url and "tok" in repr(client) and SECRET not in repr(client)


def test_tps(client, fake_server):
    fake_server.json_route("GET", "/world/tps", {"ok": True, "tps": 20.0, "tps_1m": 19.5, "tps_5m": 19.0, "tps_15m": 18.0})
    assert [client.getTPS(w) for w in ("current", "1m", "5m", "15m")] == [20.0, 19.5, 19.0, 18.0]
    assert client.getTPSStats() == {"current": 20.0, "1m": 19.5, "5m": 19.0, "15m": 18.0}
    with pytest.raises(ValueError, match="window"):
        client.getTPS("bogus")


def test_players(client, fake_server):
    fake_server.json_route("GET", "/players/list", {"ok": True, "count": 2, "max": 20, "players": ["a", "b"]})
    assert client.getPlayers() == ["a", "b"]
    assert client.getPlayerCount() == 2 and client.getMaxPlayers() == 20
    assert client.getPlayerStats() == {"count": 2, "max": 20, "players": ["a", "b"]}


def test_memory_units(client, fake_server):
    mib = 1024 * 1024
    fake_server.json_route("GET", "/system/memory", {
        "ok": True, "heap_used_bytes": 100 * mib, "heap_committed_bytes": 300 * mib, "heap_max_bytes": 1024 * mib,
        "nonheap_used_bytes": 10 * mib, "nonheap_committed_bytes": 20 * mib, "nonheap_max_bytes": None,
        "jvm_used_bytes": 110 * mib, "jvm_committed_bytes": 320 * mib, "jvm_max_bytes": 1024 * mib,
        "process_used_bytes": 500 * mib, "process_virtual_bytes": 0,
    })
    stats = client.getMemoryStats()
    assert stats["heap_used"] == 100.0 and stats["heap_free"] == 200.0 and stats["heap_max"] == 1024.0
    assert stats["nonheap_max"] is None and "used" not in stats
    assert client.getMemoryStats("gib")["heap_max"] == 1.0
    assert client.getMemoryStats("b")["heap_used"] == 100 * mib
    with pytest.raises(ValueError, match="Unknown unit"):
        client.getMemoryStats("parsecs")


def test_meta_world_and_system(client, fake_server):
    fake_server.json_route("GET", "/server/meta", {"ok": True, "platform": "paper", "minecraft_version": "26.3",
                                                    "bridge_version": "5-alpha.1", "port": 1913, "protocol_version": 2})
    fake_server.json_route("GET", "/world/time", {"ok": True, "time": 6000, "full_time": 30000, "day": 1, "server_time": 1700000000})
    fake_server.json_route("GET", "/world/chunks", {"ok": True, "total": 4, "world": 1, "world_nether": 1, "world_the_end": 1, "custom:dim": 1})
    fake_server.json_route("GET", "/system/cpu", {"ok": True, "cpu_load": 0.5, "processors": 8})
    fake_server.json_route("GET", "/system/gc", {"ok": True, "gc_type": "G1", "gc_count": 3, "gc_time_ms": 9, "last_gc_pause_ms": 4, "avg_gc_pause_ms": 3})
    fake_server.json_route("GET", "/system/threads", {"ok": True, "current": 1, "peak": 2, "daemon": 3})
    fake_server.json_route("GET", "/system/uptime", {"ok": True, "uptime_ms": 1234})
    fake_server.json_route("GET", "/auth/check", {"ok": True, "token_id": "tok", "policy_id": "admin", "permissions": ["*"], "expiry": 0})
    assert client.getPlatform() == "paper" and client.getBridgeVersion() == "5-alpha.1"
    assert client.getServerMeta()["protocol_version"] == 2
    assert client.getWorldTime() == 6000 and client.getWorldTimeInfo()["day"] == 1
    assert client.getLoadedChunks() == {"total": 4, "world": 1, "world_nether": 1, "world_the_end": 1, "custom:dim": 1}
    assert client.getCPUStats() == {"load": 0.5, "processors": 8}
    assert client.getGCStats()["gc_type"] == "G1"
    assert client.getThreadStats() == {"current": 1, "peak": 2, "daemon": 3}
    assert client.getUptime() == 1234
    assert client.getTokenInfo() == {"token_id": "tok", "policy_id": "admin", "permissions": ["*"], "expiry": 0}


def test_run_command_wire_format_and_suppression_default(client, fake_server):
    fake_server.json_route("POST", "/server/run", {"ok": True, "output": ["line 1", "line 2"]})
    assert client.runCommand("list") == "line 1\nline 2"
    client.runCommand("text", False)                 # the reported call: positional show_console
    client.runCommand("say hi", show_console=True)
    bodies = [r.json() for r in fake_server.requests]
    assert bodies == [{"command": "list", "show_console": False}, {"command": "text", "show_console": False},
                      {"command": "say hi", "show_console": True}]


def test_run_command_timeout_is_raised(client, fake_server):
    fake_server.json_route("POST", "/server/run", {"ok": False, "error": "command_timeout",
                                                    "message": "Command did not complete within 5000 ms: x"}, status=504)
    with pytest.raises(HungerBridgeError) as info:
        client.runCommand("x")
    assert info.value.status == 504 and info.value.code == "command_timeout"


def test_broadcast_builds_valid_tellraw_json(client, fake_server):
    fake_server.json_route("POST", "/server/run", {"ok": True, "output": []})
    client.broadcast('a "quoted" \\ path\nnext', prefix="[HB] ")
    cmd = fake_server.requests[0].json()["command"]
    assert json.loads(cmd[len("tellraw @a "):]) == {"text": '[HB] a "quoted" \\ path\nnext'}


def test_log_with_logger_and_normalize(client, fake_server):
    def handler(h, rec):
        body = rec.json()
        h.send_json(200, {"ok": True, "message": "Hello" if body.get("normalize") else body["message"]})

    fake_server.route("POST", "/server/log", handler)
    assert client.log(message="example message", level="CUSTOMLEVEL", thread="CustomThread", logger="CustomLogger") == "example message"
    assert client.log("System chat: Hello", normalize=True) == "Hello"
    first, second = (r.json() for r in fake_server.requests)
    assert first == {"message": "example message", "level": "CUSTOMLEVEL", "thread": "CustomThread",
                     "logger": "CustomLogger", "normalize": False}
    assert second == {"message": "System chat: Hello", "level": "INFO", "normalize": True}


def test_stop_server(client, fake_server):
    fake_server.json_route("POST", "/server/stop", {"ok": True, "stopped": True})
    assert client.stopServer() is None
    assert fake_server.requests[0].json() == {}


def test_redeem_pickup(fake_server):
    def handler(h, rec):
        assert "passkey=pk" in rec.path
        h.send_json(200, {"ok": True, "token_id": "new", "token_secret": SECRET})

    fake_server.route("GET", "/pickup/abc", handler)
    assert redeemPickup(f"{fake_server.url}/pickup/abc?passkey=pk") == Credentials(token_id="new", token_secret=SECRET)
    client = BridgeClient.fromPickup(f"{fake_server.url}/pickup/abc?passkey=pk", max_retries=0)
    assert client.credentials.token_id == "new" and client.base == fake_server.url


def test_redeem_pickup_errors(fake_server):
    with pytest.raises(HungerBridgeNotFound):
        redeemPickup(f"{fake_server.url}/pickup/missing?passkey=pk")
    with pytest.raises(ValueError):
        redeemPickup(f"{fake_server.url}/pickup/x")
    fake_server.json_route("GET", "/pickup/old", {"ok": True, "token_id": "a", "token_secret": SECRET}, protocol="1")
    with pytest.raises(HungerBridgeProtocolError):
        redeemPickup(f"{fake_server.url}/pickup/old?passkey=pk")

