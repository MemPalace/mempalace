"""Owner-only permissions for local artifacts containing private palace metadata."""

import os
import subprocess
from pathlib import Path

_WINDOWS = os.name == "nt"


def restrict_private(path: Path, *, directory: bool = False) -> None:
    """Establish owner-only access, including a verified ACL on Windows."""
    if not _WINDOWS:
        path.chmod(0o700 if directory else 0o600)
        if path.stat().st_mode & 0o077:
            raise PermissionError(f"Could not restrict private state: {path}")
        return
    identity = subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()
    if not identity:
        raise PermissionError("Could not identify the owner of private state")
    grant = f"{identity}:{'(OI)(CI)' if directory else ''}F"
    for args in (("/reset",), ("/inheritance:r",), ("/grant:r", grant)):
        subprocess.run(["icacls", str(path), *args], capture_output=True, text=True, check=True)
    acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
    principals = set()
    for line in acl.splitlines():
        if ":(" not in line:
            continue
        line = line.strip()
        if line.casefold().startswith(str(path).casefold()):
            line = line[len(str(path)) :].lstrip()
        principals.add(line.split(":(", 1)[0].strip().casefold())
    if principals != {identity.casefold()} or "(i)" in acl.casefold():
        raise PermissionError(f"Could not verify private state ACL: {path}")
