"""Exact filesystem state used to track conversation source snapshots."""

import os


def source_fingerprint(file_stat: os.stat_result) -> str:
    """Encode identity, size and nanosecond mtime as backend-safe metadata."""
    return ":".join(
        str(value)
        for value in (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
        )
    )
