"""
logsync.py — Anti-entropy sync engine for the logstream (RFC 004 step 0)

Pull-based convergence between palace replicas: diff version vectors, pull
missing per-origin op ranges (artifacts first, so referenced ids never
dangle), fold with the idempotent apply primitives. Each replica pulls from
its peers; two replicas pulling from each other converge without any push
path or coordinator.

Transport for the pilot rides the RFC 004 transport seam's `request` shape over
plain HTTPS (Tailscale or any mutually-reachable network) using the peer's
hub bearer token. Peers are configured in ``peers.json`` in the palace dir:

    {
      "peers": [
        {"name": "windows", "url": "https://host.example.ts.net", "token": "..."}
      ]
    }

This module lives ABOVE the RFC 004 transport seam (mempalace/transport.py):
peers come from the transport's membership snapshot and every wire call goes
through its ``request``. Swapping the link (HTTPS bearer today, MeshGuard
next) never touches the sync logic here.
"""

import logging

from .transport import (
    PEERS_FILENAME,  # noqa: F401 — re-exported for existing importers
    TransportError,
    get_transport,
    http_request,
    load_peers,
)

logger = logging.getLogger("mempalace.logsync")

_PULL_BATCH = 500

# Historical names, kept so existing importers (replica_sync, CLI, tests)
# keep working: the error class moved below the seam with the wire code.
SyncPeerError = TransportError
__all__ = ["PEERS_FILENAME", "SyncPeerError", "load_peers", "sync_all", "sync_with_peer"]


def _peer_get(base_url: str, token: str, path: str, params: dict = None) -> dict:
    """Compat shim for raw url+token callers (replica_sync, CLI --peer)."""
    return http_request(base_url, token, path, params)


def sync_with_peer(ls, url: str, token: str = "") -> dict:
    """One anti-entropy round against one peer. Returns pull stats.

    Never partially applies an event: artifacts are fetched and folded
    before the event that references them, and every apply is idempotent,
    so a crash mid-round just means the next round re-pulls the tail.
    """
    remote = _peer_get(url, token, "/sync/version_vector")
    remote_vector = remote.get("version_vector") or {}
    remote_replica = remote.get("replica_id", "?")
    local_vector = ls.version_vector()

    pulled_events = 0
    pulled_artifacts = 0
    per_origin = {}

    for origin, remote_top in remote_vector.items():
        if origin == ls.replica_id:
            continue  # nobody knows more of our own ops than we do
        after = local_vector.get(origin, 0)
        origin_pulled = 0
        while after < remote_top:
            batch = (
                _peer_get(
                    url,
                    token,
                    "/sync/ops",
                    {"origin": origin, "after": after, "limit": _PULL_BATCH},
                ).get("events")
                or []
            )
            if not batch:
                break  # peer's vector promised more than it served; stop cleanly
            for event in batch:
                for artifact_id in event.get("artifact_ids") or []:
                    if ls.has_artifact(artifact_id):
                        continue
                    artifact = _peer_get(url, token, "/sync/artifact", {"id": artifact_id}).get(
                        "artifact"
                    )
                    if not artifact:
                        raise SyncPeerError(
                            f"{url}: peer served event {event.get('id')!r} but not "
                            f"its artifact {artifact_id!r}"
                        )
                    if ls.apply_remote_artifact(artifact):
                        pulled_artifacts += 1
                if ls.apply_remote_event(event):
                    pulled_events += 1
                    origin_pulled += 1
                after = max(after, event.get("origin_seq") or 0)
        if origin_pulled:
            per_origin[origin] = origin_pulled

    return {
        "peer_url": url,
        "peer_replica": remote_replica,
        "pulled_events": pulled_events,
        "pulled_artifacts": pulled_artifacts,
        "per_origin": per_origin,
        # The peer's vector at round start — consumers (the /sync/peers
        # estate endpoint, PalaceMind's mesh view) compute drift from it.
        "remote_version_vector": remote_vector,
        # Node-profile advertisement riding the same fetch: the peer's own
        # self-description plus profiles it relays for origins it knows
        # (absent from pre-profile peers — consumers treat None as unknown).
        "remote_profile": remote.get("profile"),
        "remote_profiles": remote.get("profiles") or {},
    }


def sync_all(ls, palace_path: str, transport=None) -> list[dict]:
    """One round against every peer in the transport's membership snapshot;
    per-peer errors are reported, never raised — one dead peer must not
    block the others (R1: only convergence waits)."""
    transport = transport or get_transport(palace_path)
    results = []
    for peer in transport.peers():
        name = peer.get("name") or peer.get("url") or "?"
        try:
            stats = sync_with_peer(ls, peer["url"], peer.get("token", ""))
            stats["peer_name"] = name
            results.append(stats)
        except (TransportError, ValueError) as exc:
            results.append({"peer_name": name, "peer_url": peer.get("url"), "error": str(exc)})
    return results
