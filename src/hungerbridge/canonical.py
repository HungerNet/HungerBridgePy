'''Canonical request encoding shared with the HungerBridge server.

Everything in this module is part of the wire protocol: the output must match
``com.hungerbridge.common.security.TokenManager`` (``canonicalizeJson`` and
``normalizePath``) byte for byte, otherwise HMAC signatures will not verify.

Rules (protocol v1):

* Objects: keys sorted by UTF-16 code unit order (Java ``TreeMap<String>``),
  no whitespace, ``key:value`` pairs joined with ``,``.
* Strings: JSON escapes for ``'`` ``\\`` ``\\b`` ``\\f`` ``\\n`` ``\\r`` ``\\t``;
  every other code unit below U+0020 or above U+007F is written as a
  lower-case ``\\uXXXX`` escape. Characters outside the BMP are written as a
  UTF-16 surrogate pair (two escapes), exactly like Java does.
* Numbers are emitted with :func:`json.dumps`; the body that is *sent* is the
  canonical string itself, so the server canonicalizes the very same lexeme.
  NaN and infinities are not valid JSON and are rejected.
* Paths: query string and fragment removed, leading ``/`` enforced, trailing
  ``/`` removed (except for the root path).
'''

from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import urlparse

__all__ = ['canonical_json', 'canonical_body', 'normalize_path', 'quote_string']

_SIMPLE_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def quote_string(value: str | None) -> str:
    '''Quote ``value`` as an ASCII-only JSON string literal (Java-compatible).'''
    if value is None:
        return '""'
    out = ['"']
    for ch in value:
        simple = _SIMPLE_ESCAPES.get(ch)
        if simple is not None:
            out.append(simple)
            continue
        code = ord(ch)
        if code > 0xFFFF:
            # Java strings are UTF-16: emit the surrogate pair, not a 5+ digit escape.
            code -= 0x10000
            out.append('\\u%04x\\u%04x' % (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)))
        elif code < 0x20 or code > 0x7F:
            out.append('\\u%04x' % code)
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def _utf16_sort_key(key: str) -> bytes:
    # Java's TreeMap<String> orders by UTF-16 code units; Python orders by code
    # points. They differ only when non-BMP characters are compared with
    # characters in U+E000..U+FFFF. Encoding as UTF-16-BE gives Java's order.
    return key.encode('utf-16-be', 'surrogatepass')


def canonical_json(value: Any) -> str:
    '''Return the canonical JSON encoding of ``value``.

    ``None`` encodes as ``null``. Unknown objects are encoded as their ``str()``
    (quoted), matching the historical client behavior.
    '''
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('NaN and infinite floats cannot be encoded as JSON')
        return json.dumps(value)
    if isinstance(value, str):
        return quote_string(value)
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(canonical_json(v) for v in value) + ']'
    if isinstance(value, dict):
        items = sorted(((str(k), v) for k, v in value.items()), key=lambda kv: _utf16_sort_key(kv[0]))
        return '{' + ','.join(quote_string(k) + ':' + canonical_json(v) for k, v in items) + '}'
    return quote_string(str(value))


def canonical_body(payload: Any) -> str:
    '''Canonical request body: empty string when there is no payload.'''
    if payload is None:
        return ''
    return canonical_json(payload)


def normalize_path(path: str | None) -> str:
    '''Normalize a request path exactly like ``TokenManager.normalizePath``.'''
    if not path:
        return '/'
    normalized = path.strip()
    if not normalized:
        return '/'
    if '://' in normalized:
        try:
            normalized = urlparse(normalized).path or '/'
        except ValueError:
            normalized = normalized.split('://', 1)[1]
            normalized = '/' + normalized.split('/', 1)[1] if '/' in normalized else '/'
    normalized = normalized.split('?', 1)[0].split('#', 1)[0]
    if not normalized.startswith('/'):
        normalized = '/' + normalized
    while len(normalized) > 1 and normalized.endswith('/'):
        normalized = normalized[:-1]
    return normalized or '/'
