"""
opsync.py — Anti-entropy sync engine for the memory op-log (RFC 004 step 2a)

The logstream sync engine (logsync.py) generalized to memory ops: diff
version vectors, pull missing per-origin op ranges, fold with
``OpLog.apply_remote_op`` — idempotent, so a crash mid-round just means
the next round re-pulls the tail.

This module moves OPS between op-logs; it does not touch the vector
store. Applying pulled ops to the local store (drawer upserts, KG rows)
is the fold consumer's job — a separate, deliberately decoupled stage,
because during the dual-write shadow the store is still written by the
origin's own dual-write and remote ops are held for the cutover.

Rides the RFC 004 transport seam: peers come from the transport's
membership snapshot and every wire call goes through its ``request``.
"""

import logging

from .oplog import OpLog, get_oplog
from .transport import TransportError, get_transport

logger = logging.getLogger("mempalace.opsync")

_PULL_BATCH = 500


def sync_memops_with_peer(oplog: OpLog, transport, peer: dict) -> dict:
    """One anti-entropy round for memory ops against one peer."""
    remote = transport.request(peer, "/sync/memops/version_vector")
    remote_vector = remote.get("version_vector") or {}
    remote_replica = remote.get("replica_id", "?")
    local_vector = oplog.version_vector()

    pulled_ops = 0
    per_origin: dict = {}

    for origin, remote_top in remote_vector.items():
        if origin == oplog.replica_id:
            continue  # nobody knows more of our own ops than we do
        after = local_vector.get(origin, 0)
        origin_pulled = 0
        while after < remote_top:
            batch = (
                transport.request(
                    peer,
                    "/sync/memops",
                    {"origin": origin, "after": after, "limit": _PULL_BATCH},
                ).get("ops")
                or []
            )
            if not batch:
                break  # peer's vector promised more than it served; stop cleanly
            for op in batch:
                if oplog.apply_remote_op(op):
                    pulled_ops += 1
                    origin_pulled += 1
                after = max(after, op.get("origin_seq") or 0)
        if origin_pulled:
            per_origin[origin] = origin_pulled

    return {
        "peer_url": peer.get("url"),
        "peer_replica": remote_replica,
        "pulled_ops": pulled_ops,
        "per_origin": per_origin,
        "remote_version_vector": remote_vector,
    }


def sync_all_memops(palace_path: str, oplog: OpLog = None, transport=None) -> list:
    """One memory-op round against every peer; per-peer errors are
    reported, never raised (R1: only convergence waits)."""
    oplog = oplog or get_oplog(palace_path)
    transport = transport or get_transport(palace_path)
    results = []
    for peer in transport.peers():
        name = peer.get("name") or peer.get("url") or "?"
        try:
            stats = sync_memops_with_peer(oplog, transport, peer)
            stats["peer_name"] = name
            results.append(stats)
        except (TransportError, ValueError) as exc:
            results.append({"peer_name": name, "peer_url": peer.get("url"), "error": str(exc)})
    return results
