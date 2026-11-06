'''High-level HungerBridge API client.'''

from __future__ import annotations

import json
import time
from typing import Any, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from .auth import Credentials
from .exceptions import HungerBridgeError, InvalidModeError
from .stream import Stream
from .transport import DEFAULT_TIMEOUT, Timeout, Transport, _json_or_none
from .units import BYTE_UNITS, convert_bytes

__all__ = ['BridgeClient', 'redeem_pickup']


def _extract(response: Any, path: Optional[str]) -> Any:
    '''Return ``response[a][b]...`` for a dotted ``path`` (``None`` if absent).'''
    if response is None:
        return None
    if not path:
        return response
    cur = response
    for part in path.split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def redeem_pickup(
    url: str,
    pickup_id: Optional[str] = None,
    passkey: Optional[str] = None,
    *,
    timeout: Timeout = DEFAULT_TIMEOUT,
) -> Tuple[str, str]:
    '''Redeem a one-time token pickup and return ``(token_id, token_secret)``.

    Accepts either the full URL printed by ``/hb token create``
    (``http://host:1913/pickup/<id>?passkey=<hex>``) or a base URL plus
    ``pickup_id`` and ``passkey``. Pickups are single-use.
    '''
    if pickup_id is None:
        parsed = urlparse(url)
        pickup_id = parsed.path.rstrip('/').rsplit('/', 1)[-1]
        passkey = passkey or (parse_qs(parsed.query).get('passkey') or [None])[0]
        url = f'{parsed.scheme}://{parsed.netloc}'
    if not pickup_id or not passkey:
        raise ValueError('pickup id and passkey are required')
    response = requests.get(f'{url.rstrip("/")}/pickup/{pickup_id}', params={'passkey': passkey}, timeout=timeout)
    if not response.ok:
        raise Transport._error_for(response)
    data = _json_or_none(response) or {}
    token_id, secret = data.get('token_id'), data.get('token_secret')
    if not token_id or not secret:
        raise HungerBridgeError('Pickup response did not contain a token', status=response.status_code, body=data)
    return token_id, secret


class BridgeClient:
    '''Client for the HungerBridge HTTP API.

    ``token_id``/``token_secret`` may also be given as a single ``'id:secret'``
    string in ``token_id``. All getters return plain Python values/dicts.
    '''

    def __init__(
        self,
        url: str,
        token_id: Optional[str] = None,
        token_secret: Optional[str] = None,
        history_handler=None,
        new_log_handler=None,
        max_retries: int = 0,
        retry_backoff_sec: float = 1.0,
        swallow_rate_limit: bool = False,
        static_delay: float = 0.0,
        *,
        timeout: Timeout = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
    ):
        self.base = url.rstrip('/')
        self.credentials = Credentials.parse(token_id, token_secret)
        self.transport = Transport(
            self.base,
            self.credentials,
            max_retries=max_retries,
            retry_backoff_sec=retry_backoff_sec,
            swallow_rate_limit=swallow_rate_limit,
            static_delay=static_delay,
            timeout=timeout,
            session=session,
        )
        self.stream = Stream(
            base_url=self.base,
            headers_provider=lambda: self.transport.auth_headers('GET', '/server/stream'),
            history_handler=history_handler,
            new_log_handler=new_log_handler,
        )

    # -- backwards-compatible internals ---------------------------------------------

    @property
    def _token_id(self) -> Optional[str]:
        return self.credentials.token_id if self.credentials else None

    @property
    def _token_secret(self) -> Optional[str]:
        return self.credentials.secret if self.credentials else None

    def _get(self, path: str) -> Any:
        return self.transport.get(path)

    def _post(self, path: str, payload: Any) -> Any:
        return self.transport.post(path, payload)

    def _build_auth_headers(self, method: str, path: str, body: Any) -> dict:
        return self.transport.auth_headers(method, path, body)

    def _extract(self, response: Any, path: Optional[str]) -> Any:
        return _extract(response, path)

    def _convert(self, value: Any, unit: Optional[str]) -> Any:
        # Unknown units (and 'b') return the raw byte value, as before.
        if value is None or not isinstance(unit, str) or unit.lower() not in BYTE_UNITS or unit.lower() == 'b':
            return value
        return convert_bytes(value, 'b', unit)

    # -- connectivity ---------------------------------------------------------------

    def isOk(self) -> bool:
        return str(_extract(self._get('ping'), 'ok')).lower() == 'true'

    def getServerTime(self):
        '''Server wall-clock time in epoch milliseconds.'''
        return _extract(self._get('ping'), 'server_time')

    def getPing(self) -> int:
        '''Round-trip time of an authenticated ``/ping`` in milliseconds.'''
        start = time.perf_counter()
        self._get('ping')
        return int((time.perf_counter() - start) * 1000)

    def getTokenInfo(self) -> dict:
        resp = self._get('auth/check')
        return {
            'token_id': _extract(resp, 'tokenId'),
            'policy_id': _extract(resp, 'policyId'),
            'permissions': _extract(resp, 'permissions'),
        }

    # -- server metadata ------------------------------------------------------------

    def getServerMeta(self) -> dict:
        resp = self._get('server/meta')
        return {
            'platform': _extract(resp, 'platform'),
            'minecraft_version': _extract(resp, 'minecraft_version'),
            'bridge_version': _extract(resp, 'bridge_version'),
            'port': _extract(resp, 'port'),
            'protocol_version': _extract(resp, 'protocol_version'),
        }

    def getPlatform(self):
        return self.getServerMeta()['platform']

    def getMinecraftVersion(self):
        return self.getServerMeta()['minecraft_version']

    def getBridgeVersion(self):
        return self.getServerMeta()['bridge_version']

    def getBridgePort(self):
        return self.getServerMeta()['port']

    def getProtocolVersion(self) -> int:
        '''Protocol version spoken by the server (1 for servers that predate versioning).'''
        version = self.getServerMeta().get('protocol_version')
        return int(version) if version is not None else 1

    # -- players --------------------------------------------------------------------

    def getPlayers(self, mode: str = 'count'):
        resp = self._get('players/list')
        if mode == 'count':
            return _extract(resp, 'count')
        if mode == 'list':
            return _extract(resp, 'players')
        raise InvalidModeError(f'Invalid mode: "{mode}"')

    def getMaxPlayers(self) -> int:
        max_players = _extract(self._get('players/list'), 'max')
        try:
            return int(max_players) if max_players is not None else 0
        except (TypeError, ValueError):
            return 0

    # -- world ----------------------------------------------------------------------

    _TPS_FIELDS = {'current': 'tps', '1m': 'tps_1m', '5m': 'tps_5m', '15m': 'tps_15m'}

    def getTPS(self, mode: str = 'current'):
        field = self._TPS_FIELDS.get(mode)
        if field is None:
            raise InvalidModeError(f'Invalid mode: "{mode}"')
        return _extract(self._get('world/tps'), field)

    def getMSPT(self):
        return _extract(self._get('world/mspt'), 'mspt')

    def getWorldTime(self):
        '''Overworld time of day in ticks (0-23999); ``-1`` if unavailable.'''
        return _extract(self._get('world/time'), 'time')

    def getWorldTimeInfo(self) -> dict:
        resp = self._get('world/time')
        return {key: _extract(resp, key) for key in ('time', 'full_time', 'day', 'server_time')}

    def getWorldWeather(self):
        return _extract(self._get('world/weather'), 'weather')

    def _world_counts(self, path: str) -> dict:
        resp = self._get(path)
        return {key: _extract(resp, key) for key in ('total', 'world', 'world_nether', 'world_the_end')}

    def getLoadedChunks(self) -> dict:
        return self._world_counts('world/chunks')

    def getLoadedEntities(self) -> dict:
        return self._world_counts('world/entities')

    # -- system ---------------------------------------------------------------------

    def getUptime(self):
        return _extract(self._get('system/uptime'), 'uptime_ms')

    def getCPUStats(self) -> dict:
        resp = self._get('system/cpu')
        return {'load': _extract(resp, 'cpu_load'), 'processors': _extract(resp, 'processors')}

    _MEMORY_FIELDS = (
        'heap_used',
        'heap_committed',
        'heap_max',
        'nonheap_used',
        'nonheap_committed',
        'nonheap_max',
        'jvm_used',
        'jvm_committed',
        'jvm_max',
        'process_used',
        'process_virtual',
    )

    def getMemoryStats(self, unit: str = 'mib') -> dict:
        resp = self._get('system/memory')
        raw = {name: _extract(resp, name + '_bytes') for name in self._MEMORY_FIELDS}
        out = {name: self._convert(value, unit) for name, value in raw.items()}
        used, committed = raw['heap_used'], raw['heap_committed']
        out.update(
            {
                'used': self._convert(used, unit),
                'total': self._convert(committed, unit),
                'free': self._convert(max(0, committed - used) if committed is not None and used is not None else None, unit),
                'max': self._convert(raw['heap_max'], unit),
            }
        )
        return out

    def getProcessMemory(self, unit: str = 'mib') -> dict:
        resp = self._get('system/memory')
        return {
            'process_used': self._convert(_extract(resp, 'process_used_bytes'), unit),
            'process_virtual': self._convert(_extract(resp, 'process_virtual_bytes'), unit),
        }

    def getGCStats(self) -> dict:
        resp = self._get('system/gc')
        return {k: _extract(resp, k) for k in ('gc_type', 'gc_count', 'gc_time_ms', 'last_gc_pause_ms', 'avg_gc_pause_ms')}

    def getThreadStats(self) -> dict:
        resp = self._get('system/threads')
        return {k: _extract(resp, k) for k in ('current', 'peak', 'daemon')}

    def getNetworkStats(self) -> dict:
        resp = self._get('system/network')
        return {k: _extract(resp, k) for k in ('bytes_in_per_sec', 'bytes_out_per_sec', 'total_bytes_in', 'total_bytes_out')}

    # -- actions --------------------------------------------------------------------

    def log(self, message: str, level: Optional[str] = 'info', thread: Optional[str] = None):
        '''``POST /server/log`` and return the server's response dict.

        ``level`` accepts builtin or custom level names. ``level=None`` prefixes
        the message with backspaces so consoles render it without a level tag.
        ``thread`` sets the logger/thread label of the emitted event.
        '''
        if level is not None:
            body = {'level': level, 'message': message}
        else:
            body = {'level': 'info', 'message': ('\b' * 50) + message}
        if thread is not None:
            body['thread'] = thread
        return self._post('server/log', body)

    def runCommand(self, command: str, showConsole: bool = False, silent: bool = False, normalize: bool = True):
        '''``POST /server/run``.

        With ``normalize=False`` the full response dict is returned; otherwise the
        captured output lines joined with ``\\n`` (or ``None``).
        '''
        resp = self._post('server/run', {'command': command, 'silent': silent, 'show_console': showConsole})
        if not normalize:
            return resp
        if isinstance(resp, dict):
            out = resp.get('output')
            if isinstance(out, list):
                return '\n'.join(str(x) for x in out)
            if isinstance(out, (str, bytes)):
                return out
            return None
        if isinstance(resp, list):
            return '\n'.join(str(x) for x in resp)
        if isinstance(resp, (str, bytes)):
            return resp
        return None

    def broadcast(self, msg: str, prefix: Optional[str] = None):
        '''Broadcast ``msg`` (optionally prefixed) to all players via ``tellraw @a``.'''
        component = json.dumps({'text': (prefix or '') + msg}, ensure_ascii=False)
        return self.runCommand(f'tellraw @a {component}', showConsole=True, silent=False, normalize=True)

    def stopServer(self):
        '''``POST /server/stop`` — stops the Minecraft server; returns the response dict.'''
        return self._post('server/stop', {})

    # -- token pickup -----------------------------------------------------------------

    @staticmethod
    def redeemPickup(url: str, pickup_id: Optional[str] = None, passkey: Optional[str] = None) -> Tuple[str, str]:
        return redeem_pickup(url, pickup_id, passkey)

    @classmethod
    def fromPickup(cls, pickup_url: str, **kwargs: Any) -> 'BridgeClient':
        '''Redeem ``pickup_url`` and return a client authenticated with the new token.'''
        token_id, secret = redeem_pickup(pickup_url)
        parsed = urlparse(pickup_url)
        return cls(f'{parsed.scheme}://{parsed.netloc}', token_id, secret, **kwargs)
