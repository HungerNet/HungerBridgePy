"""Find HungerBridge release jars on GitHub.

HungerBridge publishes one jar per Minecraft version and loader. Release assets are
named ``HungerBridge-<loader>-<minecraft>-<version>.jar`` (for example
``HungerBridge-paper-26.2-4-alpha.94.jar``). Releases built before that naming used the
Gradle project name, ``<minecraft>-<loader>-<version>.jar`` (``26.2-paper-4-alpha.93.jar``);
both forms are understood.

The Paper jar runs on Paper, Purpur and Folia; the Fabric jar needs only Fabric Loader.

Example::

    from hungerbridge.releases import find_release

    asset = find_release("1.21.11", "paper")
    print(asset.name, asset.url)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

import requests

from .exceptions import HungerBridgeConnectionError, HungerBridgeError, HungerBridgeNotFound
from .transport import DEFAULT_TIMEOUT, Timeout

__all__ = [
    "DEFAULT_REPOSITORY",
    "GITHUB_API",
    "LOADERS",
    "LOADER_DIRECTORIES",
    "ReleaseAsset",
    "loader_for",
    "parse_asset_name",
    "list_release_assets",
    "find_release",
]

DEFAULT_REPOSITORY = "HungerNet/HungerBridge"
GITHUB_API = "https://api.github.com"

#: Server software (as reported by e.g. the Calagopus Version Changer or ``/server/meta``)
#: -> HungerBridge loader whose jar runs on it.
LOADERS = {
    "paper": "paper",
    "purpur": "paper",
    "folia": "paper",
    "pufferfish": "paper",
    "leaves": "paper",
    "spigot": "paper",
    "bukkit": "paper",
    "fabric": "fabric",
    "quilt": "fabric",
}

#: Where each loader loads the jar from, relative to the server root.
LOADER_DIRECTORIES = {"paper": "/plugins", "fabric": "/mods"}

_MC = r"\d+\.\d+(?:\.\d+)?"
_NEW_NAME = re.compile(rf"^hungerbridge-(?P<loader>paper|fabric)-(?P<mc>{_MC})-(?P<version>.+)\.jar$", re.IGNORECASE)
_OLD_NAME = re.compile(rf"^(?:.*?-)?(?P<mc>{_MC})-(?P<loader>paper|fabric)-(?P<version>.+)\.jar$", re.IGNORECASE)
_SKIP = ("-sources", "-javadoc", "-dev", "-all-dev")


@dataclass(frozen=True)
class ReleaseAsset:
    """One downloadable HungerBridge jar."""

    name: str
    url: str
    loader: str
    minecraft_version: str
    release: Optional[str] = None

    @property
    def directory(self) -> str:
        """The server directory the jar belongs in (``/plugins`` or ``/mods``)."""
        return LOADER_DIRECTORIES[self.loader]


def loader_for(server_type: Optional[str]) -> Optional[str]:
    """Return ``"paper"`` or ``"fabric"`` for a server type such as ``"PURPUR"``, else ``None``."""
    return LOADERS.get(str(server_type or "").strip().lower())


def _version_key(version: str):
    return tuple(int(part) for part in version.split("."))


def parse_asset_name(name: str):
    """Return ``(loader, minecraft_version)`` for a release jar name, or ``None``.

    Sources, javadoc and dev jars are ignored.
    """
    lowered = name.lower()
    if not lowered.endswith(".jar") or any(marker in lowered for marker in _SKIP):
        return None
    match = _NEW_NAME.match(name) or _OLD_NAME.match(name)
    if match is None:
        return None
    return match.group("loader").lower(), match.group("mc")


def _get_release(release: str, repository: str, api_url: str, timeout: Timeout, session) -> dict:
    path = "releases/latest" if release == "latest" else f"releases/tags/{release}"
    url = f"{api_url.rstrip('/')}/repos/{repository}/{path}"
    http = session or requests
    try:
        response = http.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise HungerBridgeConnectionError(f"Cannot reach GitHub to list HungerBridge releases: {exc}") from exc
    if response.status_code == 404:
        raise HungerBridgeNotFound(f"HungerBridge release {release!r} not found in {repository}", status=404)
    if not response.ok:
        raise HungerBridgeError(
            f"GitHub answered HTTP {response.status_code} for HungerBridge release {release!r}",
            status=response.status_code,
        )
    return response.json()


def list_release_assets(
    release: str = "latest",
    *,
    repository: str = DEFAULT_REPOSITORY,
    api_url: str = GITHUB_API,
    timeout: Timeout = DEFAULT_TIMEOUT,
    session: Optional[requests.Session] = None,
) -> List[ReleaseAsset]:
    """Return every HungerBridge jar of a release (``"latest"`` or a tag such as ``"v4-alpha.93"``)."""
    data = _get_release(release, repository, api_url, timeout, session)
    tag = data.get("tag_name")
    assets = []
    for asset in data.get("assets", []):
        parsed = parse_asset_name(asset.get("name", ""))
        if parsed is not None:
            loader, mc = parsed
            assets.append(ReleaseAsset(asset["name"], asset.get("browser_download_url", ""), loader, mc, tag))
    return assets


def find_release(
    minecraft_version: str,
    loader: str,
    release: str = "latest",
    *,
    repository: str = DEFAULT_REPOSITORY,
    api_url: str = GITHUB_API,
    timeout: Timeout = DEFAULT_TIMEOUT,
    session: Optional[requests.Session] = None,
) -> ReleaseAsset:
    """Return the jar of ``release`` for a Minecraft version and loader.

    Args:
        minecraft_version: e.g. ``"1.21.11"``, or ``"latest"`` for the newest version
            the release supports.
        loader: ``"paper"`` or ``"fabric"`` (server types like ``"PURPUR"`` are accepted).
        release: ``"latest"`` or a release tag.

    Raises:
        HungerBridgeNotFound: The release, or a jar for that version and loader, does not exist.
        HungerBridgeConnectionError: GitHub could not be reached.
    """
    resolved = loader_for(loader)
    if resolved is None:
        raise ValueError(f"loader must be 'paper' or 'fabric' (or a server type such as 'PURPUR'), got {loader!r}")
    assets = [a for a in list_release_assets(release, repository=repository, api_url=api_url, timeout=timeout, session=session)
              if a.loader == resolved]
    if not assets:
        raise HungerBridgeNotFound(f"HungerBridge release {release!r} has no {resolved} jar", status=404)
    if minecraft_version == "latest":
        return max(assets, key=lambda a: _version_key(a.minecraft_version))
    for asset in assets:
        if asset.minecraft_version == minecraft_version:
            return asset
    supported = ", ".join(sorted({a.minecraft_version for a in assets}, key=_version_key))
    raise HungerBridgeNotFound(
        f"HungerBridge {assets[0].release or release} has no {resolved} jar for Minecraft {minecraft_version}. "
        f"Available: {supported}",
        status=404,
    )
