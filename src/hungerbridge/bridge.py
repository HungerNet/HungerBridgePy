import time
import threading
import re
import requests
import hmac
import hashlib
import json
import uuid
from urllib.parse import urlparse
from .utils.exceptions import HungerBridgeError, HungerBridgeRateLimit, InvalidModeError
from .utils.convert import convert


def _canonicalize_json_body(value):
    if value is None:
        return ''
    def _quote_string(s: str) -> str:
        if s is None:
            return '""'
        out = ['"']
        for ch in s:
            o = ord(ch)
            if ch == '"': out.append('\\"')
            elif ch == '\\': out.append('\\\\')
            elif ch == '\b': out.append('\\b')
            elif ch == '\f': out.append('\\f')
            elif ch == '\n': out.append('\\n')
            elif ch == '\r': out.append('\\r')
            elif ch == '\t': out.append('\\t')
            elif o < 0x20 or o > 0x7f:
                out.append('\\u%04x' % o)
            else:
                out.append(ch)
        out.append('"')
        return ''.join(out)

    def _canonicalize(obj):
        if obj is None:
            return 'null'
        if isinstance(obj, bool):
            return 'true' if obj else 'false'
        if isinstance(obj, (int, float)) and not isinstance(obj, bool):
            return json.dumps(obj, separators=(',', ':'))
        if isinstance(obj, str):
            return _quote_string(obj)
        if isinstance(obj, (list, tuple)):
            parts = [_canonicalize(v) for v in obj]
            return '[' + ','.join(parts) + ']'
        if isinstance(obj, dict):
            items = []
            for k in sorted(obj.keys()):
                key = _quote_string(str(k))
                val = _canonicalize(obj[k])
                items.append(key + ':' + val)
            return '{' + ','.join(items) + '}'
        return _quote_string(str(obj))

    try:
        return _canonicalize(value)
    except Exception:
        try:
            return json.dumps(value, separators=(',', ':'), sort_keys=True)
        except Exception:
            return str(value)


def _normalize_path(path: str | None) -> str:
    if not path:
        return '/'
    normalized = path.strip()
    if not normalized:
        return '/'
    if '://' in normalized:
        try:
            parsed = urlparse(normalized)
            normalized = parsed.path or '/'
        except Exception:
            normalized = normalized.split('://', 1)[1]
            if '/' in normalized:
                normalized = normalized.split('/', 1)[1]
    normalized = normalized.split('?', 1)[0].split('#', 1)[0]
    if not normalized.startswith('/'):
        normalized = '/' + normalized
    while len(normalized) > 1 and normalized.endswith('/'):
        normalized = normalized[:-1]
    return normalized or '/'


class Stream:
    '''SSE streaming wrapper for /server/stream.'''
    def __init__(self, base_url: str, headers_provider, history_handler=None, new_log_handler=None):
        self.url = base_url.rstrip('/') + '/server/stream'
        self.headers_provider = headers_provider
        self.history_handler = history_handler
        self.new_log_handler = new_log_handler

        self.raw_stream = []
        self.sanitized_stream = []
        self.timestamped_stream = {}

        self._thread = None
        self._stop_event = None
        self._session = None

    def _default_history_handler(self, historic_lines):
        for line in historic_lines:
            clean = self.sanitize(line)
            ts = self.extractTimestamp(clean)
            self.raw_stream.append(line)
            self.sanitized_stream.append(clean)
            if ts is not None:
                self.timestamped_stream[ts] = clean

    def _default_new_log_handler(self, line):
        clean = self.sanitize(line)
        ts = self.extractTimestamp(clean)
        self.raw_stream.append(line)
        self.sanitized_stream.append(clean)
        if ts is not None:
            self.timestamped_stream[ts] = clean

    def connect(self, keepalive: int = 15, history: int | None = None):
        if self._thread and self._thread.is_alive():
            return

        self._stop_event = threading.Event()
        self._session = requests.Session()

        def _build_url_with_history(url: str, history: int | None):
            if not history:
                return url
            sep = '&' if '?' in url else '?'
            return f"{url}{sep}history={history}"

        def _run(history: int | None = None):
            history_phase = True
            history_lines = []
            history_deadline = time.time() + (1.0 if history else keepalive)

            request_url = _build_url_with_history(self.url, history)
            try:
                req_headers = self.headers_provider() if callable(self.headers_provider) else self.headers_provider
                with self._session.get(request_url, headers=req_headers, stream=True) as r:
                    if not r.ok:
                        raise requests.HTTPError(f'{r.status_code}: {r.text}')
                    for raw in r.iter_lines(decode_unicode=True):
                        if self._stop_event.is_set():
                            break
                        if not raw or not raw.startswith('data:'):
                            continue

                        line = raw[len('data:'):].lstrip()
                        if history_phase:
                            history_lines.append(line)
                            if time.time() >= history_deadline:
                                try:
                                    (self.history_handler or self._default_history_handler)(history_lines)
                                except Exception:
                                    pass
                                history_phase = False
                            continue

                        try:
                            (self.new_log_handler or self._default_new_log_handler)(line)
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                if self._session:
                    try:
                        self._session.close()
                    except Exception:
                        pass
                self._thread = None
                self._session = None
                self._stop_event = None

        self._thread = threading.Thread(target=_run, kwargs={"history": history}, name='HungerBridgeStream', daemon=True)
        self._thread.start()

    def disconnect(self):
        if self._stop_event:
            self._stop_event.set()
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
        self._thread = None
        self._session = None
        self._stop_event = None

    def isConnected(self) -> bool: return self._thread is not None and self._thread.is_alive()
    def getRaw(self) -> list: return list(self.raw_stream)
    def getSanitized(self) -> list: return list(self.sanitized_stream)
    def getTimestamped(self) -> dict: return dict(self.timestamped_stream)

    @staticmethod
    def sanitize(line: str) -> str:
        ansi_re = re.compile(r'\x1b\[[0-9;]*m')
        clean = ansi_re.sub('', line)
        clean = clean.replace('\\n', '\n').replace('\\r', '\r')
        return clean.rstrip()

    @staticmethod
    def extractTimestamp(line: str) -> str | None:
        m = re.match(r'\[([0-9]{2}:[0-9]{2}:[0-9]{2})\]', line)
        return m.group(1) if m else None


class BridgeClient:
    def __init__(
        self,
        url: str,
        token_id: str | None = None,
        token_secret: str | None = None,
        history_handler=None,
        new_log_handler=None,
        max_retries: int = 0,
        retry_backoff_sec: float = 1.0,
        swallow_rate_limit: bool = False,
        static_delay: float = 0.0,  # NEW: seconds to wait between requests
    ):
        self.base = url.rstrip('/')
        self._token_id = token_id
        self._token_secret = token_secret

        self._static_headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}

        self._max_retries = int(max_retries or 0)
        self._retry_backoff_sec = float(retry_backoff_sec or 1.0)
        self._swallow_rate_limit = bool(swallow_rate_limit)

        # Throttle state
        self._static_delay = float(static_delay)
        self._last_request_time = 0.0
        self._throttle_lock = threading.Lock()

        header_provider = (lambda: self._build_auth_headers('GET', '/server/stream', None)) if (self._token_id and self._token_secret) else dict(self._static_headers)

        self.stream = Stream(base_url=self.base, headers_provider=header_provider, history_handler=history_handler, new_log_handler=new_log_handler)

    # Thread-safe throttle enforcing a minimum gap between requests
    def _throttle(self):
        if self._static_delay <= 0:
            return
        with self._throttle_lock:
            now = time.time()
            delta = now - self._last_request_time
            if delta < self._static_delay:
                time.sleep(self._static_delay - delta)
            # update last_request_time to the time we actually send the request
            self._last_request_time = time.time()

    # HTTP helpers
    def _post(self, path: str, payload):
        self._throttle()

        full_path = '/' + path.lstrip('/')
        body_str = _canonicalize_json_body(payload)
        headers = self._build_auth_headers('POST', full_path, payload)

        for attempt in range(self._max_retries + 1):
            r = requests.post(self.base + full_path, headers=headers, data=body_str)

            if r.status_code == 429:
                retry_after = None
                ra = r.headers.get('Retry-After')
                if ra:
                    try:
                        retry_after = int(float(ra))
                    except Exception:
                        retry_after = ra

                if attempt < self._max_retries:
                    sleep_sec = self._retry_backoff_sec * (attempt + 1)
                    try:
                        if isinstance(retry_after, (int, float)):
                            sleep_sec = max(sleep_sec, retry_after)
                    except Exception:
                        pass
                    time.sleep(sleep_sec)
                    continue

                if self._swallow_rate_limit:
                    return None

                raise HungerBridgeRateLimit(retry_after=retry_after)

            if not r.ok:
                raise HungerBridgeError(f'HungerBridge error {r.status_code}: {r.text}')

            try:
                return r.json()
            except Exception:
                return r.text

    def _get(self, path: str):
        self._throttle()

        full_path = '/' + path.lstrip('/')
        headers = self._build_auth_headers('GET', full_path, None)

        for attempt in range(self._max_retries + 1):
            r = requests.get(self.base + full_path, headers=headers)

            if r.status_code == 429:
                retry_after = None
                ra = r.headers.get('Retry-After')
                if ra:
                    try:
                        retry_after = int(float(ra))
                    except Exception:
                        retry_after = ra

                if attempt < self._max_retries:
                    sleep_sec = self._retry_backoff_sec * (attempt + 1)
                    try:
                        if isinstance(retry_after, (int, float)):
                            sleep_sec = max(sleep_sec, retry_after)
                    except Exception:
                        pass
                    time.sleep(sleep_sec)
                    continue

                if self._swallow_rate_limit:
                    return None

                raise HungerBridgeRateLimit(retry_after=retry_after)

            if not r.ok:
                raise HungerBridgeError(f'HungerBridge error {r.status_code}: {r.text}')

            try:
                return r.json()
            except Exception:
                return r.text

    def _build_auth_headers(self, method: str, path: str, body):
        headers = dict(self._static_headers)
        if self._token_secret and self._token_id:
            timestamp = str(int(time.time()))
            nonce = uuid.uuid4().hex
            normalized_path = _normalize_path(path)
            body_str = '' if body is None else _canonicalize_json_body(body)
            msg = f"{method.upper()}\n{normalized_path}\n{timestamp}\n{nonce}\n{body_str}"
            try:
                key_bytes = bytes.fromhex(self._token_secret)
            except Exception:
                key_bytes = self._token_secret.encode('utf-8')
            sig = hmac.new(key_bytes, msg.encode('utf-8'), hashlib.sha256).hexdigest()
            headers.update({'X-Auth-Id': self._token_id, 'X-Auth-Timestamp': timestamp, 'X-Auth-Nonce': nonce, 'X-Auth-Signature': sig})
        return headers

    def _extract(self, response, path: str):
        if response is None:
            return None
        if path is None or path == '':
            return response
        parts = path.split('.')
        cur = response
        for p in parts:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
        return cur

    def _convert(self, value, unit):
        if value is None:
            return None
        if unit == 'mib':
            return convert.byte(value, 'b', 'mib')
        if unit == 'gib':
            return convert.byte(value, 'b', 'gib')
        return value

    # Public API
    def isOk(self):
        return True if str(self._extract(self._get('ping'), 'ok')).lower() == 'true' else False

    def getServerTime(self):
        return self._extract(self._get('ping'), 'server_time')

    def getPing(self) -> int:
        start = time.time()
        self._get('ping')
        end = time.time()
        return int((end - start) * 1000)

    def getTokenInfo(self):
        resp = self._get('auth/check')
        return {
            'token_id': self._extract(resp, 'tokenId'),
            'policy_id': self._extract(resp, 'policyId'),
            'permissions': self._extract(resp, 'permissions'),
        }

    def getServerMeta(self):
        resp = self._get('server/meta')
        return {
            'platform': resp.get('platform'),
            'minecraft_version': resp.get('minecraft_version'),
            'bridge_version': resp.get('bridge_version'),
            'port': resp.get('port')
        }

    def getPlatform(self): return self.getServerMeta()['platform']
    def getMinecraftVersion(self): return self.getServerMeta()['minecraft_version']
    def getBridgeVersion(self): return self.getServerMeta()['bridge_version']
    def getBridgePort(self): return self.getServerMeta()['port']

    def getPlayers(self, mode: str='count'):
        resp = self._get('players/list')
        if mode == 'count':
            return self._extract(resp, 'count')
        if mode == 'list':
            return self._extract(resp, 'players')
        raise InvalidModeError(f"Invalid mode: '{mode}'")

    def getMaxPlayers(self) -> int:
        resp = self._get('players/list')
        max_players = self._extract(resp, 'max')
        if max_players is None:
            return 0
        try:
            return int(max_players)
        except (TypeError, ValueError):
            return 0

    def getTPS(self, mode: str='current'):
        resp = self._get('world/tps')
        if mode == 'current':
            return self._extract(resp, 'tps')
        if mode == '1m':
            return self._extract(resp, 'tps_1m')
        if mode == '5m':
            return self._extract(resp, 'tps_5m')
        if mode == '15m':
            return self._extract(resp, 'tps_15m')
        raise InvalidModeError(f"Invalid mode: '{mode}'")

    def getMSPT(self):
        return self._extract(self._get('world/mspt'), 'mspt')

    def getWorldTime(self):
        return self._extract(self._get('world/time'), 'time')

    def getWorldWeather(self):
        return self._extract(self._get('world/weather'), 'weather')

    def getLoadedChunks(self):
        resp = self._get('world/chunks')
        return {
            'total': self._extract(resp, 'total'),
            'world': self._extract(resp, 'world'),
            'world_nether': self._extract(resp, 'world_nether'),
            'world_the_end': self._extract(resp, 'world_the_end'),
        }

    def getLoadedEntities(self):
        resp = self._get('world/entities')
        return {
            'total': self._extract(resp, 'total'),
            'world': self._extract(resp, 'world'),
            'world_nether': self._extract(resp, 'world_nether'),
            'world_the_end': self._extract(resp, 'world_the_end'),
        }

    def getUptime(self):
        return self._extract(self._get('system/uptime'), 'uptime_ms')

    def getCPUStats(self):
        resp = self._get('system/cpu')
        return {
            'load': self._extract(resp, 'cpu_load'),
            'processors': self._extract(resp, 'processors'),
        }

    def getMemoryStats(self, unit='mib'):
        resp = self._get('system/memory')

        heap_used = self._extract(resp, 'heap_used_bytes')
        heap_committed = self._extract(resp, 'heap_committed_bytes')
        heap_max = self._extract(resp, 'heap_max_bytes')
        nonheap_used = self._extract(resp, 'nonheap_used_bytes')
        nonheap_committed = self._extract(resp, 'nonheap_committed_bytes')
        nonheap_max = self._extract(resp, 'nonheap_max_bytes')
        jvm_used = self._extract(resp, 'jvm_used_bytes')
        jvm_committed = self._extract(resp, 'jvm_committed_bytes')
        jvm_max = self._extract(resp, 'jvm_max_bytes')
        process_used = self._extract(resp, 'process_used_bytes')
        process_virtual = self._extract(resp, 'process_virtual_bytes')

        def _convert_value(value):
            return self._convert(value, unit)

        return {
            'heap_used': _convert_value(heap_used),
            'heap_committed': _convert_value(heap_committed),
            'heap_max': _convert_value(heap_max),
            'nonheap_used': _convert_value(nonheap_used),
            'nonheap_committed': _convert_value(nonheap_committed),
            'nonheap_max': _convert_value(nonheap_max),
            'jvm_used': _convert_value(jvm_used),
            'jvm_committed': _convert_value(jvm_committed),
            'jvm_max': _convert_value(jvm_max),
            'process_used': _convert_value(process_used),
            'process_virtual': _convert_value(process_virtual),
            'used': _convert_value(heap_used),
            'total': _convert_value(heap_committed),
            'free': _convert_value(max(0, heap_committed - heap_used) if heap_committed is not None and heap_used is not None else None),
            'max': _convert_value(heap_max),
        }

    def getProcessMemory(self, unit='mib'):
        resp = self._get('system/memory')
        process_used = self._extract(resp, 'process_used_bytes')
        process_virtual = self._extract(resp, 'process_virtual_bytes')
        return {
            'process_used': self._convert(process_used, unit),
            'process_virtual': self._convert(process_virtual, unit),
        }

    def getGCStats(self):
        resp = self._get('system/gc')
        return {
            'gc_type': self._extract(resp, 'gc_type'),
            'gc_count': self._extract(resp, 'gc_count'),
            'gc_time_ms': self._extract(resp, 'gc_time_ms'),
            'last_gc_pause_ms': self._extract(resp, 'last_gc_pause_ms'),
            'avg_gc_pause_ms': self._extract(resp, 'avg_gc_pause_ms'),
        }

    def getThreadStats(self):
        resp = self._get('system/threads')
        return {
            'current': self._extract(resp, 'current'),
            'peak': self._extract(resp, 'peak'),
            'daemon': self._extract(resp, 'daemon'),
        }

    def getNetworkStats(self):
        resp = self._get('system/network')
        return {
            'bytes_in_per_sec': self._extract(resp, 'bytes_in_per_sec'),
            'bytes_out_per_sec': self._extract(resp, 'bytes_out_per_sec'),
            'total_bytes_in': self._extract(resp, 'total_bytes_in'),
            'total_bytes_out': self._extract(resp, 'total_bytes_out'),
        }

    def log(self, message: str, level: str = 'info', thread: str | None = None):
        '''POST /server/log — preserve original semantics and return full dict.
        - Accepts arbitrary level strings (builtin or custom).
        - Optional `thread` allows specifying a custom thread name for the log event.
        - If `level` is None, apply the backspace trick to avoid explicit level.
        '''
        if level is not None:
            body = {'level': level, 'message': message}
        else:
            no_level_message = ('\b' * 50) + message
            body = {'level': 'info', 'message': no_level_message}
        if thread is not None:
            body['thread'] = thread
        return self._post('server/log', body)

    def runCommand(self, command: str, showConsole: bool = False, silent: bool = False, normalize: bool = True):
        '''POST /server/run. If normalize=False returns full dict; otherwise returns normalized string or None.'''
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

    def broadcast(self, msg: str, prefix: str | None = None):
        '''Broadcast a message to all players using tellraw @a.'''
        safe = msg.replace('"', '\\"')
        cmd = f'tellraw @a {{"text":"{prefix + safe if prefix else safe}"}}'
        return self.runCommand(cmd, showConsole=True, silent=False, normalize=True)

    def stopServer(self):
        '''POST /server/stop — returns full server response dict.'''
        return self._post('server/stop', {})
 
