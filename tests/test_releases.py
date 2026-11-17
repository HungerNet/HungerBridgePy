import pytest

from hungerbridge import HungerBridgeNotFound
from hungerbridge.releases import ReleaseAsset, find_release, list_release_assets, loader_for, parse_asset_name

NEW = ["HungerBridge-paper-1.21.11-4-alpha.94.jar", "HungerBridge-paper-26.2-4-alpha.94.jar",
       "HungerBridge-fabric-26.1.2-4-alpha.94.jar", "HungerBridge-fabric-26.3-4-alpha.94.jar",
       "HungerBridge-paper-26.2-4-alpha.94-sources.jar"]
OLD = ["26.2-paper-4-alpha.93.jar", "1.21.11-fabric-4-alpha.93.jar", "26.2-fabric-4-alpha.93-dev.jar"]


@pytest.mark.parametrize("name, expected", [
    ("HungerBridge-paper-26.2-4-alpha.94.jar", ("paper", "26.2")),
    ("HungerBridge-fabric-1.21.11-4-alpha.94.jar", ("fabric", "1.21.11")),
    ("26.2-paper-4-alpha.93.jar", ("paper", "26.2")),
    ("1.21.11-fabric-4-alpha.93.jar", ("fabric", "1.21.11")),
    ("HungerBridge-paper-26.2-4-alpha.94-sources.jar", None),
    ("26.2-fabric-4-alpha.93-dev.jar", None),
    ("README.md", None),
])
def test_parse_asset_name(name, expected):
    assert parse_asset_name(name) == expected


@pytest.mark.parametrize("server_type, loader", [
    ("PAPER", "paper"), ("Purpur", "paper"), ("folia", "paper"), ("FABRIC", "fabric"), ("QUILT", "fabric"),
    ("VANILLA", None), (None, None),
])
def test_loader_for(server_type, loader):
    assert loader_for(server_type) == loader


@pytest.fixture
def github(fake_server):
    def release(names, tag):
        return {"tag_name": tag, "assets": [{"name": n, "browser_download_url": f"https://dl.example/{n}"} for n in names]}

    fake_server.json_route("GET", "/repos/HungerNet/HungerBridge/releases/latest", release(NEW, "v4-alpha.94"))
    fake_server.json_route("GET", "/repos/HungerNet/HungerBridge/releases/tags/v4-alpha.93", release(OLD, "v4-alpha.93"))
    return fake_server.url


def test_list_release_assets(github):
    assets = list_release_assets(api_url=github)
    assert [a.name for a in assets] == NEW[:4]
    assert all(a.release == "v4-alpha.94" for a in assets)


def test_find_exact_and_latest(github):
    asset = find_release("1.21.11", "PURPUR", api_url=github)
    assert asset == ReleaseAsset("HungerBridge-paper-1.21.11-4-alpha.94.jar",
                                 "https://dl.example/HungerBridge-paper-1.21.11-4-alpha.94.jar",
                                 "paper", "1.21.11", "v4-alpha.94")
    assert asset.directory == "/plugins"
    assert find_release("latest", "fabric", api_url=github).minecraft_version == "26.3"


def test_old_release_names_still_work(github):
    asset = find_release("26.2", "paper", "v4-alpha.93", api_url=github)
    assert asset.name == "26.2-paper-4-alpha.93.jar" and asset.release == "v4-alpha.93"


def test_errors(github):
    with pytest.raises(HungerBridgeNotFound, match="Available: 1.21.11, 26.2"):
        find_release("1.20.4", "paper", api_url=github)
    with pytest.raises(HungerBridgeNotFound, match="not found"):
        find_release("26.2", "paper", "v0", api_url=github)
    with pytest.raises(ValueError, match="loader"):
        find_release("26.2", "forge", api_url=github)
