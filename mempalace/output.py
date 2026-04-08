"""
output.py — Shared terminal output helpers.
"""

import sys


def safe_separator(width: int = 56) -> str:
    """
    Return a separator line safe for the active stdout encoding.

    Windows cp1252 terminals cannot encode the Unicode box-drawing char
    U+2500 (─), so we fall back to plain hyphens when needed.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "─".encode(encoding)
        return "─" * width
    except (UnicodeEncodeError, LookupError):
        return "-" * width
