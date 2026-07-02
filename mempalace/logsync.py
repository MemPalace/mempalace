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

Pure stdlib (urllib) — the sync layer takes no new dependencies.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger("mempalace.logsync")

PEERS_FILENAME = "peers.json"
_HTTP_TIMEOUT_S = 30
_PULL_BATCH = 500


def _http_timeout_s() -> float:
    """Per-request timeout for peer HTTP calls, env-tunable.

    The default suits logstream syncs (small pages), but a deep-offset
    snapshot page — Chroma's get(offset=N) scans N rows — behind a TLS
    proxy can legitimately exceed 30s on a large palace (first hit in
    production: a 31k-drawer bootstrap timing out at offset 27500). Set
    MEMPALACE_SYNC_HTTP_TIMEOUT to raise it for bootstrap pulls.
    """
    try:
        return float(os.environ.get("MEMPALACE_SYNC_HTTP_TIMEOUT", "") or _HTTP_TIMEOUT_S)
    except ValueError:
        return float(_HTTP_TIMEOUT_S)


class SyncPeerError(Exception):
    """A peer was unreachable or answered outside the protocol."""


def load_peers(palace_path: str) -> list[dict]:
    """Read peers.json; [] when absent. Malformed files fail loudly."""
    path = Path(os.path.expanduser(palace_path)) / PEERS_FILENAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        peers = data["peers"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"{path} is malformed: {exc}") from None
    if not isinstance(peers, list):
        raise ValueError(f"{path}: 'peers' must be a list")
    for peer in peers:
        if not isinstance(peer, dict) or not peer.get("url"):
            raise ValueError(f"{path}: every peer needs at least a 'url'")
    return peers


def _peer_get(base_url: str, token: str, path: str, params: dict = None) -> dict:
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=_http_timeout_s()) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise SyncPeerError(f"{url}: {exc}") from None


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
    }


def sync_all(ls, palace_path: str) -> list[dict]:
    """One round against every configured peer; per-peer errors are
    reported, never raised — one dead peer must not block the others (R1:
    only convergence waits)."""
    results = []
    for peer in load_peers(palace_path):
        name = peer.get("name") or peer["url"]
        try:
            stats = sync_with_peer(ls, peer["url"], peer.get("token", ""))
            stats["peer_name"] = name
            results.append(stats)
        except (SyncPeerError, ValueError) as exc:
            results.append({"peer_name": name, "peer_url": peer["url"], "error": str(exc)})
    return results
