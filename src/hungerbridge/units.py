"""Byte unit conversion for the memory getters."""

from __future__ import annotations

__all__ = ["BYTE_UNITS", "convertBytes"]

BYTE_UNITS = {
    "b": 1,
    "kb": 1_000,
    "mb": 1_000_000,
    "gb": 1_000_000_000,
    "tb": 1_000_000_000_000,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def convertBytes(value, unit: str = "mib"):
    """Convert ``value`` bytes to ``unit`` (``b`` returns the integer unchanged; ``None`` stays ``None``).

    Raises:
        ValueError: unknown unit.
    """
    factor = BYTE_UNITS.get(str(unit).lower())
    if factor is None:
        raise ValueError(f"Unknown unit {unit!r}, expected one of: {', '.join(BYTE_UNITS)}")
    if value is None or factor == 1:
        return value
    return float(value) / factor
