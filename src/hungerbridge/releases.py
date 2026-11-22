"""Find HungerBridge releases on Modrinth, the only release source.

Releases are selected by Modrinth's structured metadata — the version's ``loaders`` and
``game_versions`` — never by file names::

    from hungerbridge import findRelease

    release = findRelease(minecraft_version="1.21.11", loader="paper")
    print(release.version_number, release.filename, release.url, release.directory)

Only releases whose major version is :data:`~hungerbridge.protocol.BRIDGE_MAJOR` are
offered: they speak the protocol this client implements.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests

from .exceptions import HungerBridgeConnectionError, HungerBridgeError, HungerBridgeNotFound
from .protocol import BRIDGE_MAJOR
from .transport import DEFAULT_TIMEOUT, Timeout

__all__ = [
    "MODRINTH_API",
    "PROJECT",
    "PLUGIN_LOADERS",
    "MOD_LOADERS",
    "BridgeRelease",
    "findRelease",
    "listReleases",
    "getProject",
]

MODRINTH_API = "https://api.modrinth.com/v2"
#: Modrinth project slug (https://modrinth.com/project/hungerbridge).
PROJECT = "hungerbridge"

#: Loaders that load jars from ``/plugins`` and from ``/mods``.
PLUGIN_LOADERS = frozenset({"paper", "purpur", "folia", "spigot", "bukkit"})
MOD_LOADERS = frozenset({"fabric", "quilt", "forge", "neoforge"})

_MAJOR = re.compile(r"^\s*v?(\d+)")


def _userAgent() -> str:
    from . import __version__

    return f"hungerbridge-py/{__version__} (+https://github.com/HungerNet/HungerBridgePy)"


@dataclass(frozen=True)
class BridgeRelease:
    """One downloadable HungerBridge jar (a Modrinth version's primary file) for one loader."""

    version_id: str
    version_number: str
    version_type: str
    loader: str
    loaders: Tuple[str, ...]
    game_versions: Tuple[str, ...]
    filename: str
    url: str
    size: int
    sha512: str
    date_published: str

    @property
    def directory(self) -> str:
        """Where the jar goes on the server: ``/plugins`` or ``/mods``."""
        return "/plugins" if self.loader in PLUGIN_LOADERS else "/mods"


def _get(path: str, params: Optional[dict], api_url: Optional[str], timeout: Timeout, session) -> object:
    url = f"{(api_url or MODRINTH_API).rstrip('/')}/{path}"
    http = session or requests
    try:
        response = http.get(url, params=params, headers={"User-Agent": _userAgent(), "Accept": "application/json"}, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise HungerBridgeConnectionError(f"Cannot reach Modrinth ({url}): {exc}") from exc
    if response.status_code == 404:
        raise HungerBridgeNotFound(f"Modrinth has no {path}", status=404)
    if not response.ok:
        raise HungerBridgeError(f"Modrinth answered HTTP {response.status_code} for {path}", status=response.status_code)
    return response.json()


def getProject(*, api_url: Optional[str] = None, timeout: Timeout = DEFAULT_TIMEOUT, session=None) -> dict:
    """The Modrinth project metadata (``loaders``, ``game_versions``, ...)."""
    return _get(f"project/{PROJECT}", None, api_url, timeout, session)


def _validate(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required, e.g. findRelease(minecraft_version='1.21.11', loader='paper')")
    return value.strip()


def listReleases(
    minecraft_version: str,
    loader: str,
    *,
    api_url: Optional[str] = None,
    timeout: Timeout = DEFAULT_TIMEOUT,
    session=None,
) -> List[BridgeRelease]:
    """Every listed release for ``loader`` and ``minecraft_version`` that speaks this
    client's protocol, newest first.

    Raises:
        ValueError: missing arguments, or a loader/Minecraft version the project does not
            support (the message lists what Modrinth has).
        HungerBridgeConnectionError: Modrinth could not be reached.
    """
    minecraft_version = _validate(minecraft_version, "minecraft_version")
    loader = _validate(loader, "loader").lower()
    project = getProject(api_url=api_url, timeout=timeout, session=session)
    loaders = sorted(project.get("loaders") or [])
    if loader not in loaders:
        raise ValueError(f"HungerBridge is not published for loader {loader!r}. Loaders on Modrinth: {', '.join(loaders)}")
    if loader not in PLUGIN_LOADERS | MOD_LOADERS:
        raise ValueError(f"Unknown install directory for loader {loader!r}")
    game_versions = project.get("game_versions") or []
    if minecraft_version not in game_versions:
        raise ValueError(f"HungerBridge is not published for Minecraft {minecraft_version}. "
                         f"Versions on Modrinth: {', '.join(game_versions)}")
    versions = _get(f"project/{PROJECT}/version",
                    {"loaders": json.dumps([loader]), "game_versions": json.dumps([minecraft_version])},
                    api_url, timeout, session)
    releases = []
    for v in versions or []:
        match = _MAJOR.match(str(v.get("version_number", "")))
        if v.get("status", "listed") != "listed" or match is None or int(match.group(1)) != BRIDGE_MAJOR:
            continue
        files = v.get("files") or []
        primary = next((f for f in files if f.get("primary")), files[0] if files else None)
        if primary is None:
            continue
        releases.append(BridgeRelease(
            version_id=v["id"],
            version_number=v["version_number"],
            version_type=v.get("version_type", "release"),
            loader=loader,
            loaders=tuple(v.get("loaders") or ()),
            game_versions=tuple(v.get("game_versions") or ()),
            filename=primary["filename"],
            url=primary["url"],
            size=int(primary.get("size") or 0),
            sha512=(primary.get("hashes") or {}).get("sha512", ""),
            date_published=v.get("date_published", ""),
        ))
    releases.sort(key=lambda r: r.date_published, reverse=True)
    return releases


def findRelease(
    minecraft_version: str,
    loader: str,
    *,
    version: Optional[str] = None,
    api_url: Optional[str] = None,
    timeout: Timeout = DEFAULT_TIMEOUT,
    session=None,
) -> BridgeRelease:
    """The newest compatible release for ``loader`` and ``minecraft_version``, or the
    release with Modrinth version number ``version``.

    Raises:
        ValueError: see :func:`listReleases`.
        HungerBridgeNotFound: no compatible (or no matching ``version``) release exists.
        HungerBridgeConnectionError: Modrinth could not be reached.
    """
    releases = listReleases(minecraft_version, loader, api_url=api_url, timeout=timeout, session=session)
    if version is not None:
        releases = [r for r in releases if r.version_number == version]
    if not releases:
        wanted = f"version {version}" if version else f"a HungerBridge {BRIDGE_MAJOR}.x release"
        raise HungerBridgeNotFound(f"Modrinth has no {wanted} for {loader} on Minecraft {minecraft_version} "
                                   f"(this client speaks the protocol of HungerBridge {BRIDGE_MAJOR}.x)", status=404)
    return releases[0]
