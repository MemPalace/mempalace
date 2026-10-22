"""Environment variable parsing shared by every MemPalace setting.

One vocabulary for flags, one rule for numbers: a blank variable counts as
unset, and a value that does not parse logs one warning per variable and value
and falls back to the caller's default instead of raising. Kept import-light
because hooks and startup paths read their switches through it.
"""

import logging
import math
import os
from typing import Optional

from ._once import first_time

logger = logging.getLogger(__name__)

TRUTHY = frozenset({"1", "true", "yes", "y", "on"})
FALSY = frozenset({"0", "false", "no", "n", "off"})


def _warn_once(name: str, raw: str, expected: str) -> None:
    if not first_time("env", name, raw):
        return
    logger.warning("%s=%r is not recognized (expected %s); using the default", name, raw, expected)


def parse_bool(value) -> Optional[bool]:
    """``True``/``False`` for a recognised flag value, ``None`` for anything else."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    return None


def parse_number(value, kind=float):
    """``value`` as ``kind`` (``int`` or ``float``), or ``None`` if it is not one.

    Booleans and non-finite floats are not numbers here.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = kind(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if kind is float and not math.isfinite(number):
        return None
    return number


def env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    """The stripped value of ``name``, or ``default`` when it is unset or blank."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def env_flag(name: str) -> Optional[bool]:
    """The flag in ``name``, or ``None`` when it is unset, blank or unrecognised."""
    raw = env_str(name)
    if raw is None:
        return None
    value = parse_bool(raw)
    if value is None:
        _warn_once(name, raw, "1/true/yes/on or 0/false/no/off")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = env_flag(name)
    return default if value is None else value


def _env_number(name, default, kind, expected):
    raw = env_str(name)
    if raw is None:
        return default
    value = parse_number(raw, kind)
    if value is None:
        _warn_once(name, raw, expected)
        return default
    return value


def env_int(name: str, default=None):
    """The integer in ``name``, or ``default`` when it is unset, blank or not an integer."""
    return _env_number(name, default, int, "an integer")


def env_float(name: str, default=None):
    """The finite number in ``name``, or ``default`` when it is unset, blank or not one."""
    return _env_number(name, default, float, "a number")


def env_list(name: str) -> list:
    """The non-empty, stripped, comma-separated items of ``name``."""
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]
