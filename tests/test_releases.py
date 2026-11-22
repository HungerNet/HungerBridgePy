"""Release resolution against a fake Modrinth API (metadata only, no file-name parsing)."""

import json
import os

import pytest

from hungerbridge import BridgeRelease, HungerBridgeNotFound, findRelease
from hungerbridge.releases import listReleases

PROJECT = {"slug": "hungerbridge", "loaders": ["fabric", "folia", "paper", "purpur", "quilt"],
           "game_versions": ["1.21.11", "26.1.2", "26.2", "26.3"]}


def version(vid, number, loaders, games, date, filename="whatever-name.jar", status="listed"):
    return {"id": vid, "version_number": number, "version_type": "alpha", "status": status, "loaders": loaders,
            "game_versions": games, "date_published": date,
            "files": [{"filename": filename + ".sources", "url": "x", "primary": False},
                      {"filename": filename, "url": f"https://cdn.modrinth.com/{vid}/{filename}", "primary": True,
                       "size": 123, "hashes": {"sha512": "ff" * 64}}]}


VERSIONS = [
    version("new", "5-alpha.2", ["paper", "purpur", "folia"], ["26.3"], "2026-12-02T00:00:00Z", "HungerBridge-paper-26.3.jar"),
    version("old", "5-alpha.1", ["paper", "purpur", "folia"], ["26.3"], "2026-12-01T00:00:00Z"),
    version("v4", "4-alpha.97", ["paper", "purpur", "folia"], ["26.3"], "2026-11-01T00:00:00Z"),
    version("v3", "3.0-alpha", ["paper"], ["26.3"], "2026-06-20T00:00:00Z"),
    version("gone", "5-alpha.3", ["paper"], ["26.3"], "2026-12-03T00:00:00Z", status="archived"),
]


@pytest.fixture
def modrinth(fake_server):
    fake_server.json_route("GET", "/project/hungerbridge", PROJECT, protocol=None)

    def versions(h, rec):
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(rec.path).query)
        loaders = json.loads(query["loaders"][0])
        games = json.loads(query["game_versions"][0])
        matching = [v for v in VERSIONS if set(loaders) & set(v["loaders"]) and set(games) & set(v["game_versions"])]
        h.send_json(200, matching, protocol=None)

    fake_server.route("GET", "/project/hungerbridge/version", versions)
    return fake_server


def test_newest_compatible_release(modrinth):
    release = findRelease(minecraft_version="26.3", loader="purpur", api_url=modrinth.url)
    assert release == BridgeRelease(
        version_id="new", version_number="5-alpha.2", version_type="alpha", loader="purpur",
        loaders=("paper", "purpur", "folia"), game_versions=("26.3",), filename="HungerBridge-paper-26.3.jar",
        url="https://cdn.modrinth.com/new/HungerBridge-paper-26.3.jar", size=123, sha512="ff" * 64,
        date_published="2026-12-02T00:00:00Z")
    assert release.directory == "/plugins"
    query = modrinth.requests[1].path
    assert "loaders=%5B%22purpur%22%5D" in query and "game_versions=%5B%2226.3%22%5D" in query
    assert modrinth.requests[0].headers["User-Agent"].startswith("hungerbridge-py/")


def test_only_listed_releases_of_the_compatible_major(modrinth):
    numbers = [r.version_number for r in listReleases("26.3", "paper", api_url=modrinth.url)]
    assert numbers == ["5-alpha.2", "5-alpha.1"]


def test_pinned_version(modrinth):
    assert findRelease("26.3", "paper", version="5-alpha.1", api_url=modrinth.url).version_id == "old"
    with pytest.raises(HungerBridgeNotFound, match="version 4-alpha.97"):
        findRelease("26.3", "paper", version="4-alpha.97", api_url=modrinth.url)


def test_validation_uses_project_metadata(modrinth):
    with pytest.raises(ValueError, match="Loaders on Modrinth: fabric, folia, paper, purpur, quilt"):
        findRelease("26.3", "forge", api_url=modrinth.url)
    with pytest.raises(ValueError, match="Versions on Modrinth: 1.21.11, 26.1.2, 26.2, 26.3"):
        findRelease("1.20.4", "paper", api_url=modrinth.url)
    for missing in ({"minecraft_version": "", "loader": "paper"}, {"minecraft_version": "26.3", "loader": None}):
        with pytest.raises(ValueError, match="required"):
            findRelease(api_url=modrinth.url, **missing)


def test_no_compatible_release(modrinth):
    with pytest.raises(HungerBridgeNotFound, match="HungerBridge 5.x"):
        findRelease("1.21.11", "fabric", api_url=modrinth.url)


def test_mod_loaders_install_into_mods():
    release = BridgeRelease("i", "5-alpha.1", "alpha", "fabric", ("fabric",), ("26.3",), "f.jar", "u", 1, "s", "d")
    assert release.directory == "/mods"


@pytest.mark.modrinth
@pytest.mark.skipif(os.environ.get("HB_MODRINTH") != "1", reason="set HB_MODRINTH=1 to query the real Modrinth API")
def test_real_modrinth_project_metadata():
    from hungerbridge.releases import getProject

    project = getProject()
    assert project["slug"] == "hungerbridge"
    assert {"paper", "fabric"} <= set(project["loaders"])
