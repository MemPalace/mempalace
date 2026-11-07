"""Declared types for the drawer metadata fields MemPalace compares.

Every backend write runs through :func:`coerce_metadata_types`, so a value
that arrives in the wrong shape (``"3"`` for a chunk index, ``"1.7e9"`` for
an mtime, ``"true"`` for a flag, a ``datetime`` for a timestamp) is stored as
its declared type and range filters and the mined-set checks compare numbers
with numbers. A value that does not parse cleanly is stored as given: these
fields are bookkeeping, and nothing is dropped to make them fit. Readers that
compare these fields call the same function on legacy rows.
"""

import math
import re
from datetime import datetime

INT_FIELDS = frozenset(
    {
        "chunk_index",
        "chunk_total",
        "line_start",
        "line_end",
        "entry_index",
        "normalize_version",
        "convo_chunker_version",
        "general_extractor_version",
    }
)
FLOAT_FIELDS = frozenset({"source_mtime", "compression_ratio"})
BOOL_FIELDS = frozenset({"is_sentinel"})
TIMESTAMP_FIELDS = frozenset({"filed_at", "created_at"})

_INT_TEXT = re.compile(r"[+-]?[0-9]+")
_FLOAT_TEXT = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_BOOL_TEXT = {"true": True, "1": True, "false": False, "0": False}
# Backends store integers as signed 64-bit; a bigger one stays as given.
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def _int64(number, value):
    return number if _INT64_MIN <= number <= _INT64_MAX else value


def _as_int(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return _int64(int(value), value)
    if isinstance(value, str) and _INT_TEXT.fullmatch(value.strip()):
        return _int64(int(value.strip()), value)
    return value


def _as_float(value):
    if isinstance(value, str) and _FLOAT_TEXT.fullmatch(value.strip()):
        number = float(value.strip())
        return number if math.isfinite(number) else value
    return value


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        return _BOOL_TEXT.get(value.strip().lower(), value)
    return value


def _as_timestamp(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _coerce(key, value):
    if value is None:
        return value
    if key in INT_FIELDS:
        return _as_int(value)
    if key in FLOAT_FIELDS:
        return _as_float(value)
    if key in BOOL_FIELDS:
        return _as_bool(value)
    if key in TIMESTAMP_FIELDS:
        return _as_timestamp(value)
    return value


def coerce_metadata_types(metadata):
    """Return ``metadata`` with declared fields in their declared types.

    The same dict comes back when nothing changed, so a write batch is not
    copied for the common case.
    """
    if not metadata:
        return metadata
    coerced = None
    for key, value in metadata.items():
        new = _coerce(key, value)
        if new is not value:
            if coerced is None:
                coerced = dict(metadata)
            coerced[key] = new
    return coerced if coerced is not None else metadata


def is_sentinel(metadata):
    """Whether a drawer's metadata marks it a sentinel.

    Legacy rows can hold ``"false"`` or ``"0"``, which plain truthiness reads
    as a sentinel; any other value keeps its usual truthiness.
    """
    flag = _as_bool((metadata or {}).get("is_sentinel"))
    return flag is not False and bool(flag)


def is_bookkeeping(metadata, drawer_id=None):
    """Whether a row tracks a source instead of holding any of its text.

    That is the convo miner's registry row (``ingest_mode="registry"``, an id
    starting ``_reg_``) or a miner's sentinel. The mining checks read these
    rows; nothing a user sees should. A room name alone proves nothing: a
    project can name a real room ``_registry``.
    """
    meta = metadata or {}
    if is_sentinel(meta) or meta.get("ingest_mode") == "registry":
        return True
    return isinstance(drawer_id, str) and drawer_id.startswith(("_reg_", "sentinel_"))


# A ``where`` filter matching the bookkeeping rows ``is_bookkeeping`` names by
# metadata, for readers that can only subtract them after counting.
BOOKKEEPING_WHERE = {"$or": [{"ingest_mode": "registry"}, {"is_sentinel": True}]}
