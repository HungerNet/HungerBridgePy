"""End-to-end tests against a real HungerBridge server (protocol v2).

Skipped unless one of these is set:

* ``HB_PICKUP_URL`` – a fresh ``/pickup/<id>?passkey=...`` URL (redeemed once), or
* ``HB_URL``, ``HB_TOKEN_ID`` and ``HB_TOKEN_SECRET``.

Optional: ``HB_EXPECT_PLATFORM`` (paper|folia|fabric|standalone), ``HB_EXPECT_MC`` (e.g. 26.3),
``HB_ALLOW_STOP=1`` to also exercise ``/server/stop`` as the very last test.

Run with ``pytest -m live tests/test_live.py``. The same suite runs against
``./gradlew :common:standaloneBridge`` and, in HungerBridge's CI, against real Paper, Purpur,
Folia and Fabric servers.
"""

from __future__ import annotations

import os
import re
import time
import uuid

import pytest
import requests

from hungerbridge import (
    PROTOCOL_VERSION,
    BridgeClient,
    HungerBridgeAuthError,
    HungerBridgeError,
    HungerBridgeNotFound,
    HungerBridgeRateLimit,
)
from hungerbridge.auth import buildAuthHeaders

pytestmark = pytest.mark.live

_PICKUP = os.environ.get("HB_PICKUP_URL")
_URL = os.environ.get("HB_URL")
_ID = os.environ.get("HB_TOKEN_ID")
_SECRET = os.environ.get("HB_TOKEN_SECRET")

if not (_PICKUP or (_URL and _ID and _SECRET)):
    pytest.skip("no live HungerBridge server configured", allow_module_level=True)

LINE = re.compile(r"^\[\d{2}:\d{2}:\d{2}] \[(?P<thread>[^/\]]+)/(?P<level>[A-Z0-9_]+)]: (?P<rest>.*)$", re.S)


@pytest.fixture(scope="module")
def client() -> BridgeClient:
    if _PICKUP:
        c = BridgeClient.fromPickup(_PICKUP, max_retries=5, retry_backoff=0.5)
        os.environ.update(HB_URL=c.base, HB_TOKEN_ID=c.credentials.token_id, HB_TOKEN_SECRET=c.credentials.token_secret)
        return c
    return BridgeClient(_URL, token_id=_ID, token_secret=_SECRET, max_retries=5, retry_backoff=0.5)


class Collector:
    """A connected stream collecting every live event."""

    def __init__(self, client: BridgeClient):
        self.events = []
        self.stream = client.openStream(on_event=self.events.append, on_history=lambda events: None)

    def __enter__(self):
        self.stream.connect(history=1, reconnect=True)
        assert self.stream.waitUntilConnected(15), self.stream.last_error
        return self

    def __exit__(self, *exc):
        self.stream.disconnect()

    def waitFor(self, predicate, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hits = [e for e in list(self.events) if predicate(e)]
            if hits:
                return hits
            time.sleep(0.1)
        return []


def test_auth_check(client):
    info = client.getTokenInfo()
    assert info["token_id"] == client.credentials.token_id
    assert isinstance(info["permissions"], list) and info["permissions"]


def test_ping_and_clock(client):
    assert client.isOk() is True
    assert abs(client.getServerTime() / 1000 - time.time()) < 120
    assert isinstance(client.getPing(), int)


def test_meta_and_protocol(client):
    meta = client.getServerMeta()
    assert meta["platform"] in {"paper", "folia", "fabric", "standalone"}
    assert meta["minecraft_version"] and meta["minecraft_version"] != "unknown"
    assert meta["bridge_version"] and meta["bridge_version"] != "unknown"
    assert meta["protocol_version"] == PROTOCOL_VERSION
    if os.environ.get("HB_EXPECT_PLATFORM"):
        assert meta["platform"] == os.environ["HB_EXPECT_PLATFORM"]
    if os.environ.get("HB_EXPECT_MC"):
        assert meta["minecraft_version"] == os.environ["HB_EXPECT_MC"]


def test_players(client):
    assert isinstance(client.getPlayers(), list)
    assert isinstance(client.getPlayerCount(), int)
    assert client.getMaxPlayers() > 0


def test_world_metrics(client):
    for window in ("current", "1m", "5m", "15m"):
        tps = client.getTPS(window)
        assert isinstance(tps, (int, float)) and (tps == -1 or 0 < tps <= 20.0), (window, tps)
    mspt = client.getMSPT()
    assert isinstance(mspt, (int, float)) and (mspt == -1 or mspt >= 0)
    info = client.getWorldTimeInfo()
    assert 0 <= info["time"] < 24000, info
    assert client.getWorldWeather() in {"clear", "rain", "thunder"}
    for counts in (client.getLoadedChunks(), client.getLoadedEntities()):
        assert {"total", "world", "world_nether", "world_the_end"} <= set(counts)
        assert all(isinstance(v, int) for v in counts.values()), counts


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


def test_run_command_returns_feedback(client):
    assert client.runCommand("seed").startswith("Seed: [")
    listing = client.runCommand("list")
    assert "players" in listing.lower(), listing
    # non-BMP characters exercise the canonical-JSON surrogate encoding end to end
    assert client.runCommand("say hungerbridgepy café \U0001F600") is not None


def test_command_feedback_reaches_the_console_only_with_show_console(client):
    """The reported bug: runCommand("text", False) printed its output on the console.
    The console is observed through the stream, which carries every console event."""
    with Collector(client) as console:
        assert client.runCommand("seed", False).startswith("Seed: [")
        time.sleep(2.0)
        assert not [e for e in console.events if "Seed: [" in e.message], "suppressed feedback reached the console"
        client.runCommand("seed", show_console=True)
        assert console.waitFor(lambda e: "Seed: [" in e.message), "echoed feedback did not reach the console"


def test_custom_logger_level_and_thread(client):
    marker = f"hb-live-{uuid.uuid4().hex[:8]}"
    with Collector(client) as console:
        logged = client.log(message=f"{marker} é\U0001F600", level="CUSTOMLEVEL", thread="CustomThread", logger="CustomLogger")
        assert logged == f"{marker} é\U0001F600"
        hits = console.waitFor(lambda e: marker in e.message)
    assert hits, console.events[-10:]
    event = hits[0]
    assert (event.level, event.thread, event.logger) == ("CUSTOMLEVEL", "CustomThread", "CustomLogger")
    match = LINE.match(event.line)
    assert match and match.group("thread") == "CustomThread" and match.group("level") == "CUSTOMLEVEL", event.line
    assert match.group("rest") == f"[CustomLogger] {marker} é\U0001F600"


def test_normalization(client):
    marker = f"hb-norm-{uuid.uuid4().hex[:8]}"
    with Collector(client) as console:
        assert client.log(f"System chat: {marker}", normalize=True) == marker
        assert client.log('{"text":"' + marker + '-json","color":"gold"}', normalize=True) == f"{marker}-json"
        hits = console.waitFor(lambda e: e.message == marker)
    assert hits and hits[0].line.endswith(f"[HungerBridge] {marker}")


def test_stream_resumes_exactly_after_a_disconnect(client):
    marker = f"hb-resume-{uuid.uuid4().hex[:8]}"
    events = []
    stream = client.openStream(on_event=events.append)
    stream.connect(reconnect=False)
    assert stream.waitUntilConnected(15), stream.last_error
    stream.disconnect()
    for i in range(3):
        client.log(f"{marker} same")       # identical messages are distinct events
    stream.connect(reconnect=True)         # resumes after the last event id (and rides out a 429)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and len([e for e in events if e.message == f"{marker} same"]) < 3:
            time.sleep(0.1)
    finally:
        stream.disconnect()
    hits = [e for e in events if e.message == f"{marker} same"]
    assert len(hits) == 3 and len({e.seq for e in hits}) == 3
    assert stream.gaps == 0


def test_invalid_log_names_are_rejected(client):
    with pytest.raises(HungerBridgeError) as info:
        client.log("x", level="not a level")
    assert info.value.status == 400


# -- negative paths -----------------------------------------------------------------------


def _raw(client, method, path, headers, body=None):
    for attempt in range(3):
        response = requests.request(method, client.base + path, headers=headers, data=body, timeout=10)
        if response.status_code != 429 or attempt == 2:
            return response
        time.sleep(max(1, int(response.headers.get("Retry-After", "1") or 1)))


def test_wrong_secret_is_rejected(client):
    bad = BridgeClient(client.base, token_id=client.credentials.token_id, token_secret="00" * 32, max_retries=5, retry_backoff=0.5)
    with pytest.raises(HungerBridgeAuthError) as info:
        bad.isOk()
    assert info.value.reason == "invalid_credentials"


def test_nonce_replay_is_rejected(client):
    headers = buildAuthHeaders(client.credentials, "GET", "/ping")
    assert _raw(client, "GET", "/ping", headers).status_code == 200
    second = _raw(client, "GET", "/ping", headers)
    assert second.status_code == 401 and second.json().get("reason") == "nonce_replay"


def test_clock_skew_is_reported(client):
    headers = buildAuthHeaders(client.credentials, "GET", "/ping", timestamp=int(time.time()) - 3600)
    resp = _raw(client, "GET", "/ping", headers)
    assert resp.status_code == 401 and resp.json().get("reason") == "timestamp_out_of_range"


def test_tampered_body_is_rejected(client):
    headers = buildAuthHeaders(client.credentials, "POST", "/server/log", {"message": "a"})
    assert _raw(client, "POST", "/server/log", headers, body='{"message":"b"}').status_code == 401


def test_unknown_endpoint_is_json_404(client):
    with pytest.raises(HungerBridgeNotFound):
        client._get("does/not/exist")


def test_protocol_header_on_errors(client):
    resp = requests.get(client.base + "/ping", timeout=10)
    assert resp.status_code == 401
    assert resp.headers.get("X-HungerBridge-Protocol") == str(PROTOCOL_VERSION)
    assert resp.json()["ok"] is False and resp.json()["reason"] == "missing_credentials"


def test_bad_request_is_400_not_a_dropped_connection(client):
    headers = buildAuthHeaders(client.credentials, "POST", "/server/log", {"level": None})
    resp = _raw(client, "POST", "/server/log", headers, body='{"level":null}')
    assert resp.status_code == 400, resp.text


# -- rate limiting (runs late: exhausts the buckets) ----------------------------------------


def test_zz_rate_limit_sends_retry_after_and_client_recovers(client):
    creds = client.credentials
    hasty = BridgeClient(client.base, token_id=creds.token_id, token_secret=creds.token_secret, max_retries=0)
    hit = None
    for _ in range(60):
        try:
            hasty.isOk()
        except HungerBridgeRateLimit as exc:
            hit = exc
            break
    assert hit is not None, "rate limit never triggered"
    assert isinstance(hit.retry_after, int) and hit.retry_after >= 1
    patient = BridgeClient(client.base, token_id=creds.token_id, token_secret=creds.token_secret, max_retries=10, retry_backoff=0.5)
    assert patient.isOk() is True
    time.sleep(3)  # let the buckets refill for anything that runs after


@pytest.mark.skipif(os.environ.get("HB_ALLOW_STOP") != "1", reason="set HB_ALLOW_STOP=1 to stop the server")
def test_zzz_stop_server(client):
    assert client.stopServer() is None

