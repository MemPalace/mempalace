"""Internal bounded allocation for adjacent atomic-write temporary files."""

from __future__ import annotations

import errno
import os
import secrets

_TEMP_NAME_ATTEMPTS = 8


def create_temp_file(directory: str, prefix: str, suffix: str = "") -> tuple[int, str]:
    """Exclusively create a private temporary file, returning its descriptor and path.

    Only genuine name collisions are retried, at most eight times. Other
    filesystem errors propagate immediately: Windows ``tempfile.mkstemp``
    can instead treat permission failures as collisions and retry billions
    of names. The caller owns the returned descriptor and file, and must
    close it before removal or atomic publication.
    """
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    for _ in range(_TEMP_NAME_ATTEMPTS):
        candidate = os.path.join(directory, f"{prefix}{secrets.token_hex(16)}{suffix}")
        try:
            return os.open(candidate, flags, 0o600), candidate
        except FileExistsError:
            continue
    raise FileExistsError(
        errno.EEXIST,
        f"No unused temporary file name after {_TEMP_NAME_ATTEMPTS} attempts",
        directory,
    )
