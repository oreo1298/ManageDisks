"""Size formatting, parsing and sector alignment helpers."""

from __future__ import annotations

import re

KiB = 1024
MiB = 1024 ** 2
GiB = 1024 ** 3
TiB = 1024 ** 4

_IEC = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"]

_SUFFIXES = {
    "": None,  # caller-defined default unit
    "b": 1,
    "s": "sector",
    "k": KiB, "kib": KiB, "kb": 1000,
    "m": MiB, "mib": MiB, "mb": 1000 ** 2,
    "g": GiB, "gib": GiB, "gb": 1000 ** 3,
    "t": TiB, "tib": TiB, "tb": 1000 ** 4,
    "%": "percent",
}

_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z%]*)\s*$")


class SizeError(ValueError):
    pass


def format_size(n: int | float | None, precision: int = 1) -> str:
    """Human readable IEC size, e.g. ``format_size(1536) == '1.5 KiB'``."""
    if n is None:
        return "—"
    n = float(n)
    neg = n < 0
    n = abs(n)
    i = 0
    while n >= 1024 and i < len(_IEC) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        text = f"{int(n)} B"
    else:
        text = f"{n:.{precision}f}".rstrip("0").rstrip(".") + f" {_IEC[i]}"
    return ("-" if neg else "") + text


def parse_size(text: str, *, default_unit: int = MiB, total: int | None = None,
               sector_size: int = 512) -> int:
    """Parse a user supplied size into bytes.

    Accepts ``10G``, ``10GiB`` (binary), ``10GB`` (decimal), ``2048s``
    (sectors), ``50%`` (of ``total``) and bare numbers (``default_unit``).
    """
    m = _SIZE_RE.match(str(text))
    if not m:
        raise SizeError(f"Invalid size: {text!r}")
    value = float(m.group(1))
    suffix = m.group(2).lower()
    if suffix not in _SUFFIXES:
        raise SizeError(f"Unknown size unit {m.group(2)!r} in {text!r}")
    unit = _SUFFIXES[suffix]
    if unit is None:
        unit = default_unit
    if unit == "sector":
        return int(value) * sector_size
    if unit == "percent":
        if total is None:
            raise SizeError("Percent sizes are not allowed here")
        if value > 100:
            raise SizeError("Percentage cannot exceed 100%")
        return int(total * value / 100.0)
    return int(value * unit)


def align_up(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    return ((value + alignment - 1) // alignment) * alignment


def align_down(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    return (value // alignment) * alignment


def percent(part: int | float | None, whole: int | float | None) -> float | None:
    if part is None or not whole:
        return None
    return max(0.0, min(1.0, float(part) / float(whole)))
