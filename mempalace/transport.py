"""
transport.py — The RFC 004 transport seam (Layer 1).

Layer 2 (anti-entropy sync of the logstream and, in step 2a, the memory
op-log) is allowed to see peers ONLY through this interface. Everything
about networks, addressing, keys, and which machines are awake lives
below the seam; nothing about merge semantics leaks down into it. The
full contract, from the RFC:

    self_id()                    stable ReplicaId of this node
    peers()                      membership snapshot
    request(peer, path, params)  one authenticated round-trip
    open_stream(peer, path)      long-lived tail, resumable by cursor
    on_presence_change(cb)       liveness deltas (R5)
    on_inbound(path, handler)    serve anti-entropy pulls

v1 ships the pull half — ``self_id``/``peers``/``request`` — because that
is all the shipping sync engine consumes. The push half raises
``NotImplementedError`` here and lands with the MeshGuard binding, where
SWIM membership supplies presence, the Ed25519 node identity IS the
ReplicaId (provenance and authentication as one fact), and membership in
the mesh replaces bearer tokens as the only ACL.

Two implementations are planned:

- :class:`HttpsBearerTransport` (this module, shipping): tailnet HTTPS
  with per-hub bearer tokens from ``peers.json``. Address discovery and
  channel encryption come from the tailnet; authorization is an
  app-layer token that a human must relay out-of-band for every edge —
  the N-squared-token model the MeshGuard transport retires.
- ``MeshGuardTransport`` (next): binds the meshguard daemon over its FFI.
  Selected via ``MEMPALACE_TRANSPORT=meshguard`` once it exists; the
  factory below is the single swap point, so Layer 2 never changes.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

PEERS_FILENAME = "peers.json"

_HTTP_TIMEOUT_S = 30


class TransportError(Exception):
    """A peer was unreachable or answered outside the protocol."""


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


def load_peers(palace_path: str) -> list[dict]:
    """Read peers.json; [] when absent. Malformed files fail loudly.

    peers.json is TRANSPORT configuration — addressing plus app-layer
    authorization — which is why it lives on this side of the seam.
    """
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


def http_request(base_url: str, token: str, path: str, params: dict = None) -> dict:
    """The HTTPS wire primitive: one bearer-authenticated GET, JSON back."""
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
        raise TransportError(f"{url}: {exc}") from None


class Transport(ABC):
    """The seam. Layer 2 holds one of these and nothing else network-shaped."""

    @abstractmethod
    def self_id(self) -> str:
        """Stable ReplicaId of this node."""

    @abstractmethod
    def peers(self) -> list[dict]:
        """Current membership snapshot. Each peer dict carries at least
        ``name``; transport-specific addressing fields are opaque to
        Layer 2, which only ever hands the dict back to :meth:`request`."""

    @abstractmethod
    def request(self, peer: dict, path: str, params: Optional[dict] = None) -> dict:
        """One authenticated round-trip to ``peer``. Raises TransportError."""

    # ── Push half — defined by the contract, lands with MeshGuard ────────

    def open_stream(self, peer: dict, path: str):
        raise NotImplementedError("open_stream lands with the MeshGuard transport binding")

    def on_presence_change(self, callback):
        raise NotImplementedError("presence lands with the MeshGuard transport binding")

    def on_inbound(self, path: str, handler):
        raise NotImplementedError("inbound serving lands with the MeshGuard transport binding")


class HttpsBearerTransport(Transport):
    """Shipping transport: tailnet HTTPS + peers.json bearer tokens."""

    def __init__(self, palace_path: str):
        self._palace_path = palace_path

    def self_id(self) -> str:
        from .replica import get_replica_id

        return get_replica_id(self._palace_path)

    def peers(self) -> list[dict]:
        return load_peers(self._palace_path)

    def request(self, peer: dict, path: str, params: Optional[dict] = None) -> dict:
        return http_request(peer["url"], peer.get("token", ""), path, params)


def get_transport(palace_path: str) -> Transport:
    """The single swap point (MEMPALACE_TRANSPORT; default https).

    ``meshguard`` is reserved: selecting it before the binding lands
    fails loudly rather than silently degrading to bearer tokens —
    a user who asked for mesh-identity auth must never unknowingly
    run on the token model.
    """
    selected = (os.environ.get("MEMPALACE_TRANSPORT") or "https").strip().lower()
    if selected == "https":
        return HttpsBearerTransport(palace_path)
    if selected == "meshguard":
        raise NotImplementedError(
            "MEMPALACE_TRANSPORT=meshguard is reserved for the MeshGuard binding "
            "(RFC 004 transport seam); it has not shipped yet"
        )
    raise ValueError(f"unknown MEMPALACE_TRANSPORT {selected!r} (expected: https, meshguard)")
