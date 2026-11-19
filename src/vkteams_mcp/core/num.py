"""Numbers from outside: a header, an upstream field, a setting."""

from __future__ import annotations

import re


def as_int(value: object) -> int | None:
    """An int for an int or a string of ASCII digits (optionally signed), else None. `str.isdigit()` is not a test
    for "int() will accept it": it is true for "²" and "١٢", which int() refuses with a ValueError."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        return int(text) if re.fullmatch(r"[+-]?[0-9]{1,19}", text) else None  # not "1_000", not "²"
    return None
