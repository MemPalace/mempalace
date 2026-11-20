"""Exact filesystem state used to track conversation source snapshots."""

import os


class SourceChangedDuringReadError(OSError):
    """A source changed while it was being read; retry on a later pass."""


def source_fingerprint(file_stat: os.stat_result) -> str:
    """Encode the source's identity and change state as backend-safe metadata.

    POSIX ctime detects in-place rewrites that preserve size and restore mtime.
    Windows exposes creation time in that field, so retain the other checks
    there. Device numbers are deliberately excluded: remounting the same
    volume can change st_dev without changing its files. Inode, size and
    timestamps still detect replacements and edits. Reading a file changes
    neither fingerprint.
    """
    values: tuple[int, ...] = (
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )
    if os.name == "posix":
        values += (file_stat.st_ctime_ns,)
    return ":".join(str(value) for value in values)
