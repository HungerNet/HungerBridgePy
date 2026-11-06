"""End-to-end protocol tests against a real HungerBridge server.

Skipped unless one of these is set:

* ``HB_PICKUP_URL`` – a fresh ``/pickup/<id>?passkey=...`` URL (redeemed once), or
* ``HB_URL`` and ``HB_TOKEN`` (``"id:secret"``).

Optional: ``HB_EXPECT_PLATFORM`` (paper|folia|fabric), ``HB_EXPECT_MC`` (e.g. 26.3),
``HB_ALLOW_STOP=1`` to also exercise ``/server/stop`` as the very last test.
"""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
import requests

from hungerbridge import (
    BridgeClient,
    HungerBridgeAuthError,
    HungerBridgeNotFound,
    HungerBridgeRateLimit,
    PROTOCOL_VERSION,
)
from hungerbridge.auth import Credentials, build_auth_headers

pytestmark = pytest.mark.live

_PICKUP = os.environ.get("HB_PICKUP_URL")
_URL = os.environ.get("HB_URL")
_TOKEN = os.environ.get("HB_TOKEN")

if not (_PICKUP or (_URL and _TOKEN)):
    pytest.skip("no live HungerBridge server configured", allow_module_level=True)


@pytest.fixture(scope="module")
def client() -> BridgeClient:
    if _PICKUP:
        c = BridgeClient.fromPickup(_PICKUP, max_retries=5, retry_backoff_sec=0.5, static_delay=0.11)
        # make the redeemed token available to later processes / debugging
        os.environ["HB_URL"], os.environ["HB_TOKEN"] = c.base, f"{c._token_id}:{c._token_secret}"
        return c
    return BridgeClient(_URL, _TOKEN, max_retries=5, retry_backoff_sec=0.5)


def test_auth_check(client):
    info = client.getTokenInfo()
    assert info["token_id"] == client._token_id
    assert isinstance(info["permissions"], list) and info["permissions"]


def test_ping_and_clock(client):
    assert client.isOk() is True
    server_ms = client.getServerTime()
    assert abs(server_ms / 1000 - time.time()) < 120
    assert isinstance(client.getPing(), int)


def test_meta_and_protocol_negotiation(client):
    meta = client.getServerMeta()
    assert meta["platform"] in {"paper", "folia", "fabric", "standalone"}
    assert meta["minecraft_version"] and meta["minecraft_version"] != "unknown"
    assert meta["bridge_version"] and meta["bridge_version"] != "unknown"
    assert meta["protocol_version"] == PROTOCOL_VERSION
    assert client.transport.server_protocol_version == PROTOCOL_VERSION
    if os.environ.get("HB_EXPECT_PLATFORM"):
        assert meta["platform"] == os.environ["HB_EXPECT_PLATFORM"]
    if os.environ.get("HB_EXPECT_MC"):
        assert meta["minecraft_version"] == os.environ["HB_EXPECT_MC"]


def test_players(client):
    assert isinstance(client.getPlayers("count"), int)
    assert isinstance(client.getPlayers("list"), list)
    assert client.getMaxPlayers() > 0


def test_world_metrics(client):
    for mode in ("current", "1m", "5m", "15m"):
        tps = client.getTPS(mode)
        assert isinstance(tps, (int, float)) and (tps == -1 or 0 < tps <= 20.0), (mode, tps)
    mspt = client.getMSPT()
    assert isinstance(mspt, (int, float)) and (mspt == -1 or mspt >= 0)
    info = client.getWorldTimeInfo()
    assert 0 <= info["time"] < 24000, info
    assert info["full_time"] >= info["time"] or info["full_time"] == -1
    assert client.getWorldWeather() in {"clear", "rain", "thunder"}
    for counts in (client.getLoadedChunks(), client.getLoadedEntities()):
        assert set(counts) == {"total", "world", "world_nether", "world_the_end"}
        assert all(isinstance(v, int) for v in counts.values()), counts
        assert counts["total"] >= counts["world"] + counts["world_nether"] + counts["world_the_end"] or counts["total"] == -1
    # Recent versions keep no spawn chunks loaded while nobody is online, so force-load one
    # chunk to check that counting really reads the overworld.
    client.runCommand("forceload add 0 0")
    try:
        deadline = time.monotonic() + 10
        while client.getLoadedChunks()["world"] < 1 and time.monotonic() < deadline:
            time.sleep(0.25)
        assert client.getLoadedChunks()["world"] >= 1, "force-loaded overworld chunk not counted"
    finally:
        client.runCommand("forceload remove 0 0")


def test_system_metrics(client):
    assert client.getUptime() > 0
    cpu = client.getCPUStats()
    assert cpu["processors"] >= 1 and 0.0 <= cpu["load"] <= 1.0
    mem = client.getMemoryStats("b")
    assert mem["heap_used"] > 0 and mem["heap_max"] >= mem["heap_committed"] >= mem["heap_used"]
    assert client.getGCStats()["gc_count"] >= 0
    threads = client.getThreadStats()
    assert threads["peak"] >= threads["current"] > 0
    assert set(client.getNetworkStats()) == {"bytes_in_per_sec", "bytes_out_per_sec", "total_bytes_in", "total_bytes_out"}


def test_run_command_with_non_ascii_payload(client):
    # Non-BMP characters exercise the canonical-JSON surrogate encoding end to end.
    out = client.runCommand("say hungerbridgepy café \U0001F600", showConsole=True)
    assert out is not None
    listing = client.runCommand("list")
    assert listing and "players" in listing.lower(), listing


def test_run_command_silent_returns_no_output(client):
    resp = client.runCommand("list", silent=True, normalize=False)
    assert resp["ok"] is True and "output" not in resp


def test_log_reaches_sse_stream(client):
    marker = f"hb-live-{uuid.uuid4().hex[:8]}"
    received = []
    histories = []
    stream = client.stream
    stream.history_handler = histories.append
    stream.new_log_handler = received.append
    # reconnect=True also backs off on 429 (Retry-After) if earlier tests drained the bucket
    stream.connect(history=5, reconnect=True)
    try:
        assert stream.wait_until_connected(15), stream.last_error
        deadline = time.monotonic() + 3
        while not histories and time.monotonic() < deadline:
            time.sleep(0.05)
        assert histories, "server did not send history-end marker"
        client.log(f"{marker} é\U0001F600", level="INFO", thread="hb-live")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not any(marker in line for line in received):
            time.sleep(0.1)
        hits = [line for line in received if marker in line]
        assert hits, received[-10:]
        assert "é\U0001F600" in hits[0]
        assert stream.extractTimestamp(stream.sanitize(hits[0])) is not None, hits[0]
    finally:
        stream.disconnect()
        stream.history_handler = stream.new_log_handler = None


def test_custom_log_level(client):
    assert client.log("custom level from hungerbridgepy", level="BRIDGE")["ok"] is True


# -- negative paths -----------------------------------------------------------------------


def _raw(client, method, path, headers, body=None):
    return requests.request(method, client.base + path, headers=headers, data=body, timeout=10)


def test_unauthenticated_is_rejected(client):
    with pytest.raises(HungerBridgeAuthError):
        BridgeClient(client.base).isOk()


def test_wrong_secret_is_rejected(client):
    bad = BridgeClient(client.base, client._token_id, "00" * 32)
    with pytest.raises(HungerBridgeAuthError) as info:
        bad.isOk()
    assert info.value.reason == "invalid_credentials"


def test_nonce_replay_is_rejected(client):
    headers = build_auth_headers(client.credentials, "GET", "/ping")
    assert _raw(client, "GET", "/ping", headers).status_code == 200
    second = _raw(client, "GET", "/ping", headers)
    assert second.status_code == 401
    assert second.json().get("reason") == "nonce_replay"


def test_clock_skew_is_reported(client):
    headers = build_auth_headers(client.credentials, "GET", "/ping", timestamp=int(time.time()) - 3600)
    resp = _raw(client, "GET", "/ping", headers)
    assert resp.status_code == 401 and resp.json().get("reason") == "timestamp_out_of_range"


def test_tampered_body_is_rejected(client):
    headers = build_auth_headers(client.credentials, "POST", "/server/log", {"message": "a"})
    resp = _raw(client, "POST", "/server/log", headers, body='{"message":"b"}')
    assert resp.status_code == 401


def test_unknown_endpoint_is_json_404(client):
    with pytest.raises(HungerBridgeNotFound):
        client._get("does/not/exist")


def test_protocol_header_on_errors(client):
    resp = requests.get(client.base + "/ping", timeout=10)
    assert resp.status_code == 401
    assert resp.headers.get("X-HungerBridge-Protocol") == str(PROTOCOL_VERSION)
    assert resp.json()["ok"] is False


def test_bad_request_is_400_not_dropped_connection(client):
    headers = build_auth_headers(client.credentials, "POST", "/server/log", {"level": None})
    resp = _raw(client, "POST", "/server/log", headers, body='{"level":null}')
    assert resp.status_code == 400, resp.text


# -- rate limiting (runs late: exhausts the buckets) ----------------------------------------


def test_zz_rate_limit_sends_retry_after_and_client_recovers(client):
    hit = None

    def ping_without_retries():
        hasty = BridgeClient(client.base, client._token_id, client._token_secret, max_retries=0)
        return hasty.isOk()

    with ThreadPoolExecutor(max_workers=100) as pool:
        requests_in_burst = [pool.submit(ping_without_retries) for _ in range(100)]
        for request in as_completed(requests_in_burst):
            try:
                request.result()
            except HungerBridgeRateLimit as exc:
                hit = exc
                break
    assert hit is not None, "rate limit never triggered"
    assert isinstance(hit.retry_after, int) and hit.retry_after >= 1
    # A retrying client recovers transparently (fresh nonce per attempt).
    patient = BridgeClient(client.base, client._token_id, client._token_secret, max_retries=10, retry_backoff_sec=0.5)
    assert patient.isOk() is True
    time.sleep(3)  # let the buckets refill for anything that runs after


@pytest.mark.skipif(os.environ.get("HB_ALLOW_STOP") != "1", reason="set HB_ALLOW_STOP=1 to stop the server")
def test_zzz_stop_server(client):
    resp = client.stopServer()
    assert resp["ok"] is True and resp["stopped"] is True
