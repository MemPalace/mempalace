"""
replica.py — Per-palace replica identity (RFC 004 transport seam / provenance)

Every palace replica has one stable ``ReplicaId``, stamped as
``origin_replica`` into every op it authors. For the step-0 pilot the id is
minted locally on first use and persisted in ``replica.json`` inside the
palace directory. Existing 12-hex pilot ids remain valid, but new ids use
128 bits of entropy. When the transport layer lands (MeshGuard), the mesh
Ed25519 identity supersedes it via an alias op — RFC 004 Appendix A.4:
"a rename is just another provenance fact."

The id never syncs and never rotates silently; it names the seat (this
machine's copy of the palace), not the model or the agent.
"""

import json
import os
import re
import secrets
import tempfile
from pathlib import Path

REPLICA_FILENAME = "replica.json"

_REPLICA_ID_RE = re.compile(r"^rep_(?:[0-9a-f]{12}|[0-9a-f]{32})$")


def _mint() -> str:
    return f"rep_{secrets.token_hex(16)}"


def _read_replica_id(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        replica_id = data["replica_id"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            f"{path} is corrupt ({exc}); refusing to mint a second replica "
            "identity for this palace — restore or delete the file explicitly"
        ) from None
    if not isinstance(replica_id, str) or not _REPLICA_ID_RE.match(replica_id):
        raise ValueError(
            f"{path} holds an invalid replica_id {replica_id!r}; refusing to "
            "mint a second identity — restore or delete the file explicitly"
        )
    return replica_id


def _fsync_directory(path: Path) -> None:
    """Best-effort persistence for a newly linked directory entry."""
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Windows and some special filesystems reject directory fds.
        pass


def get_replica_id(palace_path: str) -> str:
    """Return this palace's stable replica id, minting it on first use.

    The file is published atomically from a fully written unique temp file.
    Concurrent first callers all adopt the one no-clobber winner, while a
    corrupt or foreign-shaped file fails loudly rather than silently minting
    a second identity — two ids for one replica would fork its op-log
    provenance.
    """
    path = Path(os.path.expanduser(palace_path)) / REPLICA_FILENAME
    try:
        return _read_replica_id(path)
    except FileNotFoundError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    replica_id = _mint()
    payload = (
        json.dumps({"replica_id": replica_id, "minted_at_note": "RFC 004 step 0"}, indent=2) + "\n"
    )
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            os.link(tmp, path)
        except FileExistsError:
            return _read_replica_id(path)

        _fsync_directory(path.parent)
        return replica_id
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink()
        except OSError:
            pass
