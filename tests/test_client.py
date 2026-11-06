import json

import pytest

import hungerbridge
from hungerbridge import BridgeClient, HungerBridgeError, HungerBridgeNotFound, InvalidModeError, redeem_pickup
from hungerbridge.units import convert, convert_bytes

SECRET = "22" * 32


@pytest.fixture
def client(fake_server):
    return BridgeClient(fake_server.url, "tok", SECRET)


def test_public_api_and_compat_imports():
    from hungerbridge.bridge import BridgeClient as Legacy, Stream, _canonicalize_json_body, _normalize_path

    assert Legacy is BridgeClient
    assert hungerbridge.HungerBridge is BridgeClient
    assert Stream is hungerbridge.Stream
    assert _canonicalize_json_body(None) == ""
    assert _normalize_path("x/") == "/x"


def test_id_colon_secret_constructor():
    c = BridgeClient("http://h:1", f"tok:{SECRET}")
    assert c._token_id == "tok" and c._token_secret == SECRET


def test_tps_modes(client, fake_server):
    fake_server.json_route("GET", "/world/tps", {"ok": True, "tps": 20.0, "tps_1m": 19.5, "tps_5m": 19.0, "tps_15m": 18.0})
    assert [client.getTPS(m) for m in ("current", "1m", "5m", "15m")] == [20.0, 19.5, 19.0, 18.0]
    with pytest.raises(InvalidModeError):
        client.getTPS("bogus")


def test_players(client, fake_server):
    fake_server.json_route("GET", "/players/list", {"ok": True, "count": 2, "max": 20, "players": ["a", "b"]})
    assert client.getPlayers() == 2
    assert client.getPlayers("list") == ["a", "b"]
    assert client.getMaxPlayers() == 20
    with pytest.raises(InvalidModeError):
        client.getPlayers("x")


def test_memory_units(client, fake_server):
    mib = 1024 * 1024
    fake_server.json_route("GET", "/system/memory", {
        "ok": True, "heap_used_bytes": 100 * mib, "heap_committed_bytes": 300 * mib, "heap_max_bytes": 1024 * mib,
        "nonheap_used_bytes": 10 * mib, "nonheap_committed_bytes": 20 * mib, "nonheap_max_bytes": None,
        "jvm_used_bytes": 110 * mib, "jvm_committed_bytes": 320 * mib, "jvm_max_bytes": 1024 * mib,
        "process_used_bytes": 500 * mib, "process_virtual_bytes": 0,
    })
    stats = client.getMemoryStats()
    assert stats["heap_used"] == 100.0 and stats["free"] == 200.0 and stats["max"] == 1024.0
    assert stats["nonheap_max"] is None
    assert client.getMemoryStats("gib")["heap_max"] == 1.0
    assert client.getMemoryStats("b")["heap_used"] == 100 * mib
    assert client.getProcessMemory()["process_used"] == 500.0


def test_world_time_info(client, fake_server):
    fake_server.json_route("GET", "/world/time", {"ok": True, "time": 6000, "full_time": 30000, "day": 1, "server_time": 1700000000})
    assert client.getWorldTime() == 6000
    assert client.getWorldTimeInfo() == {"time": 6000, "full_time": 30000, "day": 1, "server_time": 1700000000}


def test_meta_and_protocol(client, fake_server):
    fake_server.json_route("GET", "/server/meta", {"ok": True, "platform": "paper", "minecraft_version": "26.3", "bridge_version": "4", "port": 1913, "protocol_version": 1})
    assert client.getPlatform() == "paper"
    assert client.getProtocolVersion() == 1


def test_meta_from_legacy_server_defaults_protocol_1(client, fake_server):
    fake_server.json_route("GET", "/server/meta", {"ok": True, "platform": "fabric"})
    assert client.getProtocolVersion() == 1


def test_run_command_normalization(client, fake_server):
    fake_server.json_route("POST", "/server/run", {"ok": True, "output": ["line 1", "line 2"]})
    assert client.runCommand("list") == "line 1\nline 2"
    assert client.runCommand("list", normalize=False) == {"ok": True, "output": ["line 1", "line 2"]}
    assert fake_server.requests[0].json() == {"command": "list", "silent": False, "show_console": False}


def test_broadcast_builds_valid_tellraw_json(client, fake_server):
    # Regression: backslashes/newlines used to produce invalid tellraw JSON.
    fake_server.json_route("POST", "/server/run", {"ok": True, "output": []})
    client.broadcast('a "quoted" \\ path\nnext', prefix="[HB] ")
    cmd = fake_server.requests[0].json()["command"]
    assert cmd.startswith("tellraw @a ")
    assert json.loads(cmd[len("tellraw @a "):]) == {"text": '[HB] a "quoted" \\ path\nnext'}


def test_log_variants(client, fake_server):
    fake_server.json_route("POST", "/server/log", {"ok": True})
    assert client.log("hi", level="BRIDGE", thread="worker") == {"ok": True}
    client.log("bare", level=None)
    first, second = (r.json() for r in fake_server.requests)
    assert first == {"level": "BRIDGE", "message": "hi", "thread": "worker"}
    assert second == {"level": "info", "message": "\b" * 50 + "bare"}


def test_token_info(client, fake_server):
    fake_server.json_route("GET", "/auth/check", {"ok": True, "tokenId": "tok", "policyId": "admin", "permissions": ["*"]})
    assert client.getTokenInfo() == {"token_id": "tok", "policy_id": "admin", "permissions": ["*"]}


def test_system_getters(client, fake_server):
    fake_server.json_route("GET", "/system/cpu", {"ok": True, "cpu_load": 0.5, "processors": 8})
    fake_server.json_route("GET", "/system/gc", {"ok": True, "gc_type": "G1", "gc_count": 3, "gc_time_ms": 9, "last_gc_pause_ms": 4, "avg_gc_pause_ms": 3})
    fake_server.json_route("GET", "/system/threads", {"ok": True, "current": 1, "peak": 2, "daemon": 3})
    fake_server.json_route("GET", "/system/network", {"ok": True, "bytes_in_per_sec": 1, "bytes_out_per_sec": 2, "total_bytes_in": 3, "total_bytes_out": 4})
    fake_server.json_route("GET", "/system/uptime", {"ok": True, "uptime_ms": 1234})
    fake_server.json_route("GET", "/world/chunks", {"ok": True, "total": 3, "world": 1, "world_nether": 1, "world_the_end": 1})
    assert client.getCPUStats() == {"load": 0.5, "processors": 8}
    assert client.getGCStats()["gc_type"] == "G1"
    assert client.getThreadStats() == {"current": 1, "peak": 2, "daemon": 3}
    assert client.getNetworkStats()["total_bytes_out"] == 4
    assert client.getUptime() == 1234
    assert client.getLoadedChunks() == {"total": 3, "world": 1, "world_nether": 1, "world_the_end": 1}


def test_stream_requests_are_signed_for_stream_path(fake_server):
    seen = []

    def handler(h, rec):
        seen.append(rec.headers)
        h.send_json(401, {"ok": False})

    fake_server.route("GET", "/server/stream", handler)
    c = BridgeClient(fake_server.url, "tok", SECRET)
    c.stream.connect(history=5)
    c.stream.disconnect()
    import time
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    assert seen and seen[0]["X-Auth-Id"] == "tok" and "X-Auth-Signature" in seen[0]


def test_redeem_pickup_full_url(fake_server):
    def handler(h, rec):
        assert "passkey=pk" in rec.path
        h.send_json(200, {"ok": True, "token_id": "new", "token_secret": SECRET})

    fake_server.route("GET", "/pickup/abc", handler)
    assert redeem_pickup(f"{fake_server.url}/pickup/abc?passkey=pk") == ("new", SECRET)
    client = BridgeClient.fromPickup(f"{fake_server.url}/pickup/abc?passkey=pk")
    assert client._token_id == "new" and client.base == fake_server.url


def test_redeem_pickup_not_found(fake_server):
    with pytest.raises(HungerBridgeNotFound):
        redeem_pickup(fake_server.url, "missing", "pk")
    with pytest.raises(ValueError):
        redeem_pickup(fake_server.url + "/pickup/x")


def test_swallowed_rate_limit_does_not_crash_meta(fake_server):
    fake_server.json_route("GET", "/server/meta", {}, status=429)
    c = BridgeClient(fake_server.url, "tok", SECRET, swallow_rate_limit=True)
    assert c.getServerMeta()["platform"] is None


def test_units_compat():
    assert convert.byte(1024, "b", "kib") == 1.0
    assert convert_bytes(1_500_000, "b", "mb", rounding=1) == 1.5
    assert convert.time(1, "s", "ms") == 1000.0
    with pytest.raises(ValueError):
        convert_bytes(1, "b", "parsecs")


def test_errors_are_hungerbridge_errors(fake_server):
    fake_server.json_route("GET", "/ping", {"ok": False, "error": "unauthorized"}, status=401)
    with pytest.raises(HungerBridgeError):
        BridgeClient(fake_server.url).isOk()
