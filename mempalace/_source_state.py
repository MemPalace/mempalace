"""Exact filesystem state used to track conversation source snapshots."""

import os


class SourceChangedDuringReadError(OSError):
    """A source changed while it was being read; retry on a later pass."""


def source_fingerprint(file_stat: os.stat_result) -> str:
    """Encode the source's identity and change state as backend-safe metadata.

    POSIX ctime detects in-place rewrites that preserve size and restore mtime.
    This guarantee is POSIX-only: Windows exposes creation time in that field
    and an in-place same-size rewrite with restored mtime is not detected
    there. Windows still tracks inode, size and mtime changes; this is a stat
    fingerprint, not a content hash. Device numbers are deliberately excluded:
    remounting the same volume can change st_dev without changing its files.
    Reading a file changes neither fingerprint.
    """
    values: tuple[int, ...] = (
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )
    if os.name == "posix":
        values += (file_stat.st_ctime_ns,)
    return ":".join(str(value) for value in values)
