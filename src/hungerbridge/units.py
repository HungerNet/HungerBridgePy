'''Unit conversion helpers (byte and time units).

Semantics match ``hungerlib.utils.convert``, which the original client imported:
values are converted through a base unit and returned as ``float`` (optionally
rounded).
'''

from __future__ import annotations

from types import SimpleNamespace

__all__ = ['BYTE_UNITS', 'TIME_UNITS', 'convert_bytes', 'convert_time', 'convert']

BYTE_UNITS = {
    'b': 1,
    'kb': 1_000,
    'mb': 1_000_000,
    'gb': 1_000_000_000,
    'tb': 1_000_000_000_000,
    'kib': 1024,
    'mib': 1024**2,
    'gib': 1024**3,
    'tib': 1024**4,
}

TIME_UNITS = {
    'ns': 1e-9,
    'us': 1e-6,
    'ms': 1e-3,
    's': 1.0,
    'min': 60.0,
    'h': 3600.0,
    'day': 86400.0,
}


def _factor(table: dict, unit: str) -> float:
    try:
        return float(table[unit.lower()])
    except (KeyError, AttributeError):
        raise ValueError(f'Unknown unit: {unit!r} (expected one of {', '.join(table)})') from None


def convert_bytes(value, src: str = 'b', dst: str = 'mib', rounding: int | None = None) -> float:
    result = float(value) * _factor(BYTE_UNITS, src) / _factor(BYTE_UNITS, dst)
    return round(result, rounding) if rounding is not None else result


def convert_time(value, src: str = 's', dst: str = 'ms', rounding: int | None = None) -> float:
    result = float(value) * _factor(TIME_UNITS, src) / _factor(TIME_UNITS, dst)
    return round(result, rounding) if rounding is not None else result


# Backwards-compatible namespace: ``convert.byte(value, 'b', 'mib')``.
convert = SimpleNamespace(byte=convert_bytes, time=convert_time)
