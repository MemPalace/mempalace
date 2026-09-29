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
from pathlib import Path

REPLICA_FILENAME = "replica.json"

_REPLICA_ID_RE = re.compile(r"^rep_(?:[0-9a-f]{12}|[0-9a-f]{32})$")


def _mint() -> str:
    return f"rep_{secrets.token_hex(16)}"


def get_replica_id(palace_path: str) -> str:
    """Return this palace's stable replica id, minting it on first use.

    The file is written atomically (tmp + rename) so a crash mid-mint can
    never leave a half-written identity; a corrupt or foreign-shaped file
    fails loudly rather than silently minting a second identity — two ids
    for one replica would fork its op-log provenance.
    """
    path = Path(os.path.expanduser(palace_path)) / REPLICA_FILENAME
    if path.exists():
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

    path.parent.mkdir(parents=True, exist_ok=True)
    replica_id = _mint()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"replica_id": replica_id, "minted_at_note": "RFC 004 step 0"}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return replica_id
