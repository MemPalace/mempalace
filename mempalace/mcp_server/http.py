# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


# Module-level constants for the HTTP transport.
# Defined here (not inside main()) so _serve_http() / _build_http_server()
# can reference them as free names without a NameError.
#
# ThreadingHTTPServer can run requests in parallel, but an exclusive lock
# around every JSON-RPC method made a slow palace search stall handshakes and
# every other reader. Protocol methods and independent stores bypass this
# lock; palace reads share it; palace writes take it exclusively.
class _RWLock:
    """Writer-preferring readers-writer lock."""

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    def acquire_read(self) -> None:
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1

    def release_write(self) -> None:
        with self._cond:
            self._writer = False
            self._cond.notify_all()

    def read_lock(self):
        lock = self

        class _Read:
            def __enter__(self):
                lock.acquire_read()
                return lock

            def __exit__(self, exc_type, exc, tb):
                lock.release_read()
                return False

        return _Read()

    def __enter__(self):
        self.acquire_write()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release_write()
        return False


_HTTP_REQUEST_LOCK = _RWLock()
_HTTP_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_HTTP_ACTIVE_CLIENT_WINDOW_S = 120.0

_HTTP_PROTOCOL_METHODS = frozenset({"initialize", "ping", "tools/list"})

# RFC 003 phase 5: logstream tools touch only logstream.sqlite3 (its own WAL
# database with internal locking) — never Chroma or the KG. Dispatching them
# outside _HTTP_REQUEST_LOCK keeps a five-minute mempalace_event_wait
# long-poll from stalling every other agent on a shared hub, and lets the
# SSE stream coexist with normal tool traffic.
# Knowledge-graph tools use their own SQLite database and lock. Mesh peers
# and the AAAK spec are process-local reads.
_HTTP_LOCK_FREE_TOOLS = frozenset(
    {
        "mempalace_event_append",
        "mempalace_task_create",
        "mempalace_event_list",
        "mempalace_event_wait",
        "mempalace_event_ack",
        "mempalace_artifact_put",
        "mempalace_artifact_get",
        "mempalace_patch_submit",
        "mempalace_kg_query",
        "mempalace_kg_add",
        "mempalace_kg_invalidate",
        "mempalace_kg_supersede",
        "mempalace_kg_timeline",
        "mempalace_kg_stats",
        "mempalace_mesh_peers",
        "mempalace_get_aaak_spec",
    }
)

# SSE stream limits (GET /logstream/stream). Each connected client holds one
# handler thread, so the cap is a thread-exhaustion guard, not a rate limit.
_SSE_MAX_CLIENTS_ENV = "MEMPALACE_SSE_MAX_CLIENTS"
_SSE_MAX_CLIENTS_DEFAULT = 8
_SSE_HEARTBEAT_S = 15.0
_SSE_POLL_BASE_S = 0.3
_SSE_POLL_JITTER_S = 0.4
_SSE_BATCH_LIMIT = 500
_HTTP_RECENT_CLIENT_LIMIT = 50
# Host literals that always denote this machine. Used both to decide whether a
# bind is loopback (skip the network-exposure warning) and to pin the Host
# header against DNS rebinding when serving on loopback.
_HTTP_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
_HTTP_ALLOW_INSECURE_NO_TOKEN_ENV = "MEMPALACE_MCP_HTTP_ALLOW_INSECURE_NO_TOKEN"


def _resolve_tls_paths() -> tuple:
    """Resolve the TLS cert/key from --tls-cert/--tls-key or env, or (None, None).

    Flags take precedence over ``MEMPALACE_MCP_TLS_CERT`` / ``MEMPALACE_MCP_TLS_KEY``.
    Both must be given together; one without the other is a configuration error
    (raised here, before any bind, so it fails loudly at startup).
    """
    cert = (
        getattr(_args, "tls_cert", None) or os.environ.get("MEMPALACE_MCP_TLS_CERT", "")
    ).strip()
    key = (getattr(_args, "tls_key", None) or os.environ.get("MEMPALACE_MCP_TLS_KEY", "")).strip()
    if bool(cert) != bool(key):
        raise ValueError("TLS requires both --tls-cert and --tls-key (or the matching env vars)")
    if not cert:
        return None, None
    for label, path in (("--tls-cert", cert), ("--tls-key", key)):
        if not os.path.isfile(path):
            raise ValueError(f"{label} file not found: {path!r}")
    return cert, key


def _wrap_tls(sock, cert: str, key: str):
    """Wrap a server socket in a TLS 1.2+ context. Raises on bad cert/key."""
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx.wrap_socket(sock, server_side=True)


def _http_is_loopback(host: str) -> bool:
    """Whether ``host`` binds only to this machine."""
    return (host or "").strip().lower() in _HTTP_LOOPBACK_HOSTS


_HTTP_EXTRA_ALLOWED_HOSTS_ENV = "MEMPALACE_MCP_EXTRA_ALLOWED_HOSTS"


def _http_allowed_host_values(bind_host: str, port: int) -> set:
    """Host-header values accepted when Host pinning is enforced.

    DNS-rebinding defense: a browser tricked into POSTing to ``127.0.0.1`` by a
    malicious page still carries the *attacker's* domain in the ``Host`` header,
    so we pin ``Host`` to the loopback literals (and the bound host) with and
    without the port. Computed from the *actual* bound port so an ephemeral
    ``port=0`` bind (tests) still matches.

    ``MEMPALACE_MCP_EXTRA_ALLOWED_HOSTS`` (comma-separated ``host`` or
    ``host:port`` values) extends the pin for the documented fronting-proxy
    pattern ("terminate TLS at a proxy"): a loopback-bound server behind
    ``tailscale serve``/nginx receives the *public* name in ``Host``, which
    the loopback pin would otherwise reject. Entries are matched exactly
    (lowercased); a bare hostname also matches ``host:<bound port>``.
    """
    names = set(_HTTP_LOOPBACK_HOSTS)
    if bind_host:
        names.add(bind_host.strip().lower())
    values = set()
    for name in names:
        values.add(name)
        values.add(f"{name}:{port}")
    for raw in os.environ.get(_HTTP_EXTRA_ALLOWED_HOSTS_ENV, "").split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        values.add(entry)
        if ":" not in entry:
            values.add(f"{entry}:{port}")
    return values


def _http_origin_allowed(origin: str) -> bool:
    """Whether a browser ``Origin`` header may call the transport.

    Non-browser MCP clients omit ``Origin`` entirely (allowed). When an
    ``Origin`` *is* present it must be a loopback origin — this is what stops a
    page at ``https://evil.example`` from reaching a DNS-rebound localhost
    server and reading the palace.
    """
    from urllib.parse import urlparse

    try:
        host = (urlparse(origin).hostname or "").strip().lower()
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1")


def _http_client_identity(handler) -> tuple[str, dict]:
    headers = handler.headers
    peer = handler.client_address[0] if handler.client_address else ""
    forwarded_for = (headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    real_ip = (headers.get("X-Real-IP") or "").strip()
    tailnet_user = (headers.get("Tailscale-User-Login") or "").strip()
    user_agent = (headers.get("User-Agent") or "").strip()
    host_hdr = (headers.get("Host") or "").strip()
    peer_hint = forwarded_for or real_ip or peer
    basis = "|".join([peer_hint, tailnet_user, user_agent, host_hdr])
    client_id = hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()[:16]
    return client_id, {
        "client_id": client_id,
        "peer": peer,
        "peer_hint": peer_hint,
        "host": host_hdr,
        "user_agent": user_agent[:160],
        "tailscale_user": tailnet_user[:160],
    }


def _http_record_request(httpd, handler, status: int) -> None:
    now = time.time()
    now_iso = datetime.now().isoformat()
    client_id, identity = _http_client_identity(handler)
    with httpd.stats_lock:
        httpd.request_count += 1
        httpd.status_counts[str(status)] = httpd.status_counts.get(str(status), 0) + 1
        entry = dict(httpd.recent_clients.get(client_id, identity))
        entry.update(identity)
        entry["last_seen"] = now_iso
        entry["last_seen_monotonic"] = now
        entry["request_count"] = int(entry.get("request_count", 0)) + 1
        entry["last_method"] = handler.command
        entry["last_path"] = urlparse(handler.path).path
        entry["last_status"] = status
        entry["authenticated"] = bool(
            httpd.auth_token
            and hmac.compare_digest(
                handler.headers.get("Authorization", ""), f"Bearer {httpd.auth_token}"
            )
        )
        httpd.recent_clients[client_id] = entry
        overflow = len(httpd.recent_clients) - _HTTP_RECENT_CLIENT_LIMIT
        if overflow > 0:
            oldest = sorted(
                httpd.recent_clients.items(),
                key=lambda item: item[1].get("last_seen_monotonic", 0.0),
            )
            for stale_id, _entry in oldest[:overflow]:
                httpd.recent_clients.pop(stale_id, None)


def _http_status_payload(httpd) -> dict:
    now = time.time()
    with httpd.stats_lock:
        recent = sorted(
            (dict(entry) for entry in httpd.recent_clients.values()),
            key=lambda entry: entry.get("last_seen_monotonic", 0.0),
            reverse=True,
        )
        request_count = httpd.request_count
        status_counts = dict(httpd.status_counts)

    active = []
    for entry in recent:
        last_seen_monotonic = entry.pop("last_seen_monotonic", 0.0)
        if now - last_seen_monotonic <= _HTTP_ACTIVE_CLIENT_WINDOW_S:
            active.append(dict(entry))

    writer = {
        "read_only": _READ_ONLY,
        "peer_writer_read_only": _MCP_WRITER_READ_ONLY,
        "peer_writer_lock_failed": _MCP_WRITER_LOCK_FAILED,
    }
    if _MCP_WRITER_LOCK_ERROR:
        writer["peer_writer_lock_error"] = _MCP_WRITER_LOCK_ERROR

    integrity = _sqlite_integrity_payload()
    palace_path = (
        os.path.abspath(os.path.expanduser(_config.palace_path)) if _config.palace_path else ""
    )
    return {
        # `ok` is None in the two cases the payload reports as an absent
        # verdict: a non-chroma backend (#1931), and a chroma palace with no
        # database file yet. That is an absence, not a failure, and collapsing
        # it with bool() would report a freshly installed server as unhealthy.
        # A missing key is not one of those cases and still fails closed.
        "ok": integrity.get("ok", False) is not False,
        "server": {
            "name": "mempalace",
            "version": __version__,
            "transport": "http",
            "scheme": getattr(httpd, "scheme", "http"),
            "bind_host": httpd.bind_host,
            "port": httpd.server_address[1],
            "started_at": httpd.started_at,
            "uptime_seconds": round(time.monotonic() - httpd.started_monotonic, 3),
        },
        "palace": {
            "path_hash": hashlib.sha256(palace_path.encode("utf-8")).hexdigest()[:16]
            if _config.palace_path
            else "",
            "backend": _config.backend,
            "sqlite_integrity": integrity,
        },
        "writer": writer,
        "requests": {
            "total": request_count,
            "by_status": status_counts,
        },
        "clients": {
            "active_window_seconds": _HTTP_ACTIVE_CLIENT_WINDOW_S,
            "active": active,
            "recent": recent,
        },
    }


def _http_request_rejected(handler, require_auth: bool) -> bool:
    """Enforce HTTP Host/Origin/auth policy before dispatching a request."""
    srv = handler.server
    if srv.enforce_host_pin:
        host_hdr = (handler.headers.get("Host") or "").strip().lower()
        if host_hdr not in srv.allowed_hosts:
            logger.warning("HTTP request rejected: Host %r not allowed", host_hdr)
            handler.send_error(403, "Forbidden")
            return True
    origin = handler.headers.get("Origin")
    if origin and not _http_origin_allowed(origin):
        logger.warning("HTTP request rejected: cross-origin %r", origin)
        handler.send_error(403, "Forbidden")
        return True
    if require_auth and srv.auth_token:
        provided = handler.headers.get("Authorization", "")
        if not hmac.compare_digest(provided, f"Bearer {srv.auth_token}"):
            logger.warning("HTTP request rejected: missing/invalid bearer token")
            handler.send_error(401, "Unauthorized")
            return True
    return False


def _sse_max_clients() -> int:
    try:
        return max(0, int(os.environ.get(_SSE_MAX_CLIENTS_ENV, "") or _SSE_MAX_CLIENTS_DEFAULT))
    except ValueError:
        return _SSE_MAX_CLIENTS_DEFAULT


def _http_dispatch(request):
    """Dispatch one JSON-RPC request with the transport's locking policy.

    Protocol methods and independent stores are lock-free. Palace reads share
    the lock; palace writes take it exclusively. Unclassified tools fail
    closed onto the exclusive side.
    """
    method = request.get("method") or "" if isinstance(request, dict) else ""
    if method in _HTTP_PROTOCOL_METHODS or method.startswith("notifications/"):
        return handle_request(request)
    tool_name = None
    if method == "tools/call" and isinstance(request.get("params"), dict):
        tool_name = request["params"].get("name")
    if tool_name in _HTTP_LOCK_FREE_TOOLS:
        return handle_request(request)
    # service.classify_tool is the authoritative read/write registry. The
    # lock-free set above is a storage-boundary override for independent DBs.
    from ..service import classify_tool

    if classify_tool(tool_name) == "read":
        with _HTTP_REQUEST_LOCK.read_lock():
            return handle_request(request)
    with _HTTP_REQUEST_LOCK:
        return handle_request(request)


def _http_handle_get(handler) -> None:
    """Route GET requests. Module-level (like the other _http_* helpers) to
    keep _build_http_server under the C901 complexity ceiling.

    /healthz is the only credential-free route (liveness probes); everything
    else follows the bearer policy because it exposes palace/ops metadata.
    """
    path = urlparse(handler.path).path
    if path == "/healthz":
        if not handler._request_rejected(require_auth=False):
            handler._send_bytes(200, b"ok\n", "text/plain; charset=utf-8")
        return
    if path == "/statusz":
        if not handler._request_rejected(require_auth=True):
            handler._send_json(200, handler._status_payload())
        return
    if path == "/logstream/stream":
        # Events and artifacts expose work metadata and patch contents;
        # the stream always follows the bearer policy.
        if not handler._request_rejected(require_auth=True):
            _http_serve_logstream_stream(handler)
        return
    if path.startswith("/sync/"):
        if handler._request_rejected(require_auth=True):
            return
        if not _http_serve_sync(handler, path):
            handler.send_error(404, "Not Found")
        return
    if handler._request_rejected(require_auth=False):
        return
    handler.send_error(404, "Not Found")


def _http_serve_sync(handler, path: str) -> bool:
    """RFC 004 step 0: pull-based anti-entropy endpoints for peer replicas.

    GET /sync/version_vector           → {replica_id, version_vector}
    GET /sync/ops?origin=&after=&limit → {origin, events, count} (author order)
    GET /sync/artifact?id=             → {artifact} (exact content + sha256)

    Bearer policy enforced by the caller; served lock-free like all
    logstream traffic. Returns False when the path is not a sync route.
    """
    from urllib.parse import parse_qsl

    query = dict(parse_qsl(urlparse(handler.path).query))
    try:
        if path == "/sync/version_vector":
            handler._send_json(
                200,
                {
                    "replica_id": _call_logstream(lambda ls: ls.replica_id),
                    "version_vector": _call_logstream(lambda ls: ls.version_vector()),
                    # Node-profile advertisement (additive): our own
                    # self-description plus every profile we can relay, so
                    # carriers propagate profiles for transitively-known
                    # origins. Old peers ignore these fields.
                    "profile": _node_profile(),
                    "profiles": _known_profiles_snapshot(),
                },
            )
            return True
        if path == "/sync/ops":
            origin = query.get("origin") or ""
            after = int(query.get("after", "0") or 0)
            limit = int(query.get("limit", "500") or 500)
            events = _call_logstream(lambda ls: ls.list_ops(origin, after_seq=after, limit=limit))
            handler._send_json(200, {"origin": origin, "events": events, "count": len(events)})
            return True
        if path == "/sync/artifact":
            artifact_id = query.get("id") or ""
            artifact = _call_logstream(lambda ls: ls.get_artifact(artifact_id))
            if artifact is None:
                handler._send_json(404, {"error": f"artifact {artifact_id!r} not found"})
            else:
                handler._send_json(200, {"artifact": artifact})
            return True
        if path == "/sync/peers":
            handler._send_json(200, _mesh_peers_payload())
            return True
    except (ValueError, TypeError) as exc:
        handler._send_json(400, {"error": str(exc)})
        return True
    return False


# Estate data (RFC 004 A.1): what the mesh looks like from THIS replica.
# Written by the sync loop after every round, read by /sync/peers. Values
# are whole-entry replacements under the GIL, so readers never see a
# half-updated peer record.
_PEER_SYNC_STATE: dict = {}

# Node profiles learned from peers (their self-descriptions, relayed
# transitively), keyed by replica_id. LWW by advertised_at. The estate
# renders roles/accelerator/counts from these instead of UI guesses.
_KNOWN_PROFILES: dict = {}

# Self profile is recomputed at most every TTL seconds — peers request it
# every sync round and the drawer count / provider probe should not run
# per request.
_NODE_PROFILE_TTL_S = 60.0
_node_profile_cache: dict = {}

_ACCELERATOR_NAMES = {"cuda": "CUDA", "dml": "DirectML", "coreml": "CoreML", "cpu": "CPU"}


def _node_profile() -> dict:
    """This node's self-described profile — every field is pure derivation
    (palace presence, logstream authorship, resolved onnxruntime provider),
    never configuration, so the estate can render truth without guesses."""
    import platform

    cached = _node_profile_cache.get("profile")
    if cached and time.monotonic() - _node_profile_cache.get("at", 0) < _NODE_PROFILE_TTL_S:
        return cached

    roles = []
    drawers = None
    try:
        col = _get_collection(create=False)
        if col is not None:
            drawers = col.count()
            roles.append("replica")
    except Exception:
        logger.debug("node profile: drawer count unavailable", exc_info=True)
    try:
        authored = _call_logstream(lambda ls: ls.version_vector().get(ls.replica_id, 0))
        if authored:
            roles.append("agents")
    except Exception:
        logger.debug("node profile: logstream authorship unavailable", exc_info=True)
    accelerator = None
    try:
        from ..embedding import _resolve_providers, current_model_name

        device = getattr(_config, "embedding_device", None) or "auto"
        _providers, effective = _resolve_providers(device)
        accelerator = {
            "provider": _ACCELERATOR_NAMES.get(effective, effective),
            "embedder": current_model_name(),
        }
        roles.append("compute")
    except Exception:
        logger.debug("node profile: embedder resolution unavailable", exc_info=True)

    profile = {
        "roles": roles,
        "accelerator": accelerator,
        "drawers": drawers,
        "hardware": platform.platform(),
        "advertised_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _node_profile_cache["profile"] = profile
    _node_profile_cache["at"] = time.monotonic()
    return profile


def _merge_known_profiles(profiles: dict) -> None:
    """Fold relayed profiles in, last-writer-wins by advertised_at."""
    if not isinstance(profiles, dict):
        return
    for origin, profile in profiles.items():
        if not isinstance(origin, str) or not isinstance(profile, dict):
            continue
        prior = _KNOWN_PROFILES.get(origin)
        if prior and (prior.get("advertised_at") or "") >= (profile.get("advertised_at") or ""):
            continue
        _KNOWN_PROFILES[origin] = profile


def _known_profiles_snapshot(published: dict = None) -> dict:
    """Every origin profile this node can vouch for having seen.

    Published profiles first (the hub's, when this process is not the one
    syncing), then any learned in this process, then our own fresh
    self-profile last so it always wins for self.
    """
    if published is None:
        published = _published_mesh_state()
    snapshot = dict(published.get("profiles") or {})
    snapshot.update(_KNOWN_PROFILES)
    try:
        snapshot[_call_logstream(lambda ls: ls.replica_id)] = _node_profile()
    except Exception:
        logger.debug("node profile: self profile unavailable", exc_info=True)
    return snapshot


def _publish_mesh_state(palace_path: str) -> None:
    """Write this process's estate where other local processes can read it.

    The sync loop runs only in the HTTP transport, so without this the stdio
    MCP servers agents connect through answer ``mempalace_mesh_peers`` from a
    permanently empty ``_PEER_SYNC_STATE`` -- peers with a name and a url and
    nothing else, and ``origin_profiles`` holding only this node. Never fatal:
    a publish failure costs other processes their estate view, not this
    process's convergence.
    """
    from .. import server_registry

    try:
        server_registry.write_mesh_state(
            palace_path,
            peers=dict(_PEER_SYNC_STATE),
            profiles=dict(_KNOWN_PROFILES),
        )
    except Exception:
        logger.debug("mesh state publish failed", exc_info=True)


def _published_mesh_state() -> dict:
    """Read the estate published by this palace's hub, if any."""
    from .. import server_registry

    palace_path = getattr(_config, "palace_path", None)
    if not palace_path:
        return {"peers": {}, "profiles": {}, "written_at": None, "writer_alive": False}
    try:
        return server_registry.read_mesh_state(palace_path)
    except Exception:
        logger.debug("mesh state read failed", exc_info=True)
        return {"peers": {}, "profiles": {}, "written_at": None, "writer_alive": False}


def _record_peer_sync(stats: dict) -> None:
    """Fold one peer's round outcome into the estate state."""
    name = stats.get("peer_name") or stats.get("peer_url") or "?"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    prior = _PEER_SYNC_STATE.get(name) or {}
    if stats.get("error"):
        entry = {
            "url": stats.get("peer_url") or prior.get("url"),
            "replica_id": prior.get("replica_id"),
            "reachable": False,
            "last_error": stats["error"],
            "last_error_at": now,
            "last_success_at": prior.get("last_success_at"),
            "remote_version_vector": prior.get("remote_version_vector"),
            # Unreachable peers keep their last advertised profile — the
            # estate renders "last seen as", never a blank node.
            "profile": prior.get("profile"),
        }
    else:
        entry = {
            "url": stats.get("peer_url"),
            "replica_id": stats.get("peer_replica"),
            "reachable": True,
            "last_error": None,
            "last_error_at": prior.get("last_error_at"),
            "last_success_at": now,
            "last_pulled_events": stats.get("pulled_events", 0),
            "last_pulled_artifacts": stats.get("pulled_artifacts", 0),
            "remote_version_vector": stats.get("remote_version_vector") or {},
            "profile": stats.get("remote_profile") or prior.get("profile"),
        }
        _merge_known_profiles(stats.get("remote_profiles") or {})
        if stats.get("remote_profile") and stats.get("peer_replica"):
            _merge_known_profiles({stats["peer_replica"]: stats["remote_profile"]})
    _PEER_SYNC_STATE[name] = entry


def _mesh_peers_payload() -> dict:
    """The estate answer for one replica: who its peers are, whether they
    were reachable last round, their vectors (drift is peer vector vs
    self vector — computed by the consumer), and origins known only
    transitively. peers.json tokens are NEVER included.
    """
    import socket

    from ..logsync import load_peers

    replica_id = _call_logstream(lambda ls: ls.replica_id)
    local_vector = _call_logstream(lambda ls: ls.version_vector())
    try:
        configured = load_peers(getattr(_config, "palace_path", None) or "")
    except (ValueError, TypeError):
        configured = []
    # The peer sync loop lives in the HTTP transport, so in every other
    # process _PEER_SYNC_STATE is empty and the only honest source is what
    # the hub published. Prefer our own state when we have it (this process
    # is the one syncing, so it is fresher than anything on disk) and fall
    # back to the published estate per peer.
    published = _published_mesh_state()
    published_peers = published.get("peers") or {}
    named_origins = {replica_id}
    peers = []
    for peer in configured:
        name = peer.get("name") or peer["url"]
        state = dict(_PEER_SYNC_STATE.get(name) or published_peers.get(name) or {})
        state.pop("url", None)  # peers.json is authoritative for the url
        if state.get("replica_id"):
            named_origins.add(state["replica_id"])
        peers.append({"name": name, "url": peer["url"], **state})
    return {
        "self": {
            "replica_id": replica_id,
            "name": socket.gethostname(),
            "version_vector": local_vector,
            "profile": _node_profile(),
        },
        "peers": peers,
        # Origins present in the local log but not configured as peers —
        # replicas this node only knows transitively (gossip carriers).
        "unnamed_origins": sorted(set(local_vector) - named_origins),
        # Every self-described profile known here, keyed by replica_id —
        # including profiles of unnamed origins relayed through carriers.
        "origin_profiles": _known_profiles_snapshot(published),
        "sync_interval_s": _peer_sync_interval_s(),
        # Where the peer status above came from. in_process means this
        # process runs the sync loop; otherwise the estate was published by
        # the hub at published_at, and writer_alive says whether that hub is
        # still running (a dead hub leaves a last-known-good reading).
        "estate_source": {
            "in_process": bool(_PEER_SYNC_STATE),
            "published_at": published.get("written_at"),
            "writer_alive": published.get("writer_alive", False),
        },
    }


def _peer_sync_interval_s() -> float:
    try:
        return float(os.environ.get("MEMPALACE_SYNC_INTERVAL", "") or 15)
    except ValueError:
        return 15.0


def _start_peer_sync_thread() -> None:
    """Background anti-entropy loop for the logstream (RFC 004 step 0).

    Runs in the serving process so a hub with configured peers converges
    with zero extra processes. Interval via MEMPALACE_SYNC_INTERVAL
    (seconds, default 15; 0 disables). Errors are logged, never fatal —
    a dead peer must not take the loop down (R1).

    Membership is re-read from peers.json every round, never latched at
    startup: joining the mesh is "write peers.json", and requiring a hub
    restart for a file the docs describe as picked up "within one sync
    cycle" is a silent no-op for anyone following the guide in order (hub
    first, peers after). A malformed peers.json logs once per transition
    rather than every round, so a typo is visible without flooding the log.
    """
    from ..logsync import sync_all

    palace_path = getattr(_config, "palace_path", None)
    if not palace_path:
        return
    interval = _peer_sync_interval_s()
    if interval <= 0:
        return

    def _loop():
        malformed_logged = False
        while True:
            time.sleep(interval)
            try:
                ls = _get_logstream()
                for stats in sync_all(ls, palace_path):
                    _record_peer_sync(stats)
                    if stats.get("error"):
                        logger.warning("peer sync %s: %s", stats.get("peer_name"), stats["error"])
                    elif stats.get("pulled_events"):
                        logger.info(
                            "peer sync %s: pulled %d event(s), %d artifact(s)",
                            stats.get("peer_name"),
                            stats["pulled_events"],
                            stats["pulled_artifacts"],
                        )
                # Publish once per round, not per peer: the estate is only
                # coherent after every configured peer has been attempted.
                _publish_mesh_state(palace_path)
                malformed_logged = False
            except ValueError as exc:
                if not malformed_logged:
                    logger.error("peers.json is malformed; skipping peer sync: %s", exc)
                    malformed_logged = True
            except Exception:
                logger.warning("peer sync round failed", exc_info=True)

    thread = threading.Thread(target=_loop, name="mempalace-logsync", daemon=True)
    thread.start()
    logger.info("peer sync thread started (interval %.0fs)", interval)


def _sse_acquire_slot(httpd) -> bool:
    with httpd.sse_lock:
        if httpd.sse_clients >= httpd.sse_max_clients:
            return False
        httpd.sse_clients += 1
        return True


def _sse_release_slot(httpd) -> None:
    with httpd.sse_lock:
        httpd.sse_clients -= 1


def _http_serve_logstream_stream(handler) -> None:
    """RFC 003 phase 5: live logstream tail over Server-Sent Events.

    Query params are the live-tail subset of ``event_list`` filters (stream,
    room, topic, type, to/from agent, correlation id, and status), plus
    ``since_event_id`` (or the standard ``Last-Event-ID`` header): with a
    cursor the server replays everything after it, then tails live; without
    one it tails only post-connect events. Each event is one SSE frame
    (``id:`` = event id, ``event: logstream``, ``data:`` = the same JSON
    envelope event_list returns), with a ``: ping`` comment every ~15s so
    proxies keep the pipe open. Never touches _HTTP_REQUEST_LOCK — the
    stream must coexist with normal tool traffic, not starve it. Defined at
    module level (like the other _http_* helpers) to keep
    _build_http_server under the C901 complexity ceiling.
    """
    import random
    from urllib.parse import parse_qsl

    global _last_request_time

    query = dict(parse_qsl(urlparse(handler.path).query))
    filters = {
        key: (query.get(key) or None)
        for key in (
            "stream",
            "room",
            "topic",
            "type",
            "to_agent",
            "from_agent",
            "correlation_id",
            "status",
        )
    }
    cursor = query.get("since_event_id") or handler.headers.get("Last-Event-ID") or None

    def _list_after(after_id):
        return _call_logstream(
            lambda ls: ls.list_events(since_event_id=after_id, limit=_SSE_BATCH_LIMIT, **filters)
        )

    try:
        if cursor is None:
            # Live tail: only post-connect events. On an empty log the
            # cursor stays None and the whole log is post-connect.
            cursor = _call_logstream(lambda ls: ls.latest_event_id())
        # Validate filters (and an explicit cursor) before committing to a
        # streaming response — errors must be a clean 400, not a half-open
        # stream.
        if cursor:
            _list_after(cursor)
        else:
            _call_logstream(lambda ls: ls.list_events(limit=1, **filters))
    except ValueError as exc:
        handler._send_json(400, {"error": str(exc)})
        return

    if not _sse_acquire_slot(handler.server):
        handler._record_request(503)
        handler.send_response(503)
        handler.send_header("Retry-After", "5")
        handler.send_header("Content-Length", "0")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        return

    try:
        handler._record_request(200)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()

        last_write = time.monotonic()
        while True:
            _last_request_time = time.monotonic()
            events = (
                _list_after(cursor)
                if cursor
                else _call_logstream(lambda ls: ls.list_events(limit=_SSE_BATCH_LIMIT, **filters))
            )
            for event in events:
                frame = (
                    f"id: {event['id']}\n"
                    f"event: logstream\n"
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                )
                handler.wfile.write(frame.encode("utf-8"))
                cursor = event["id"]
                last_write = time.monotonic()
            if events:
                handler.wfile.flush()
            elif time.monotonic() - last_write >= _SSE_HEARTBEAT_S:
                handler.wfile.write(b": ping\n\n")
                handler.wfile.flush()
                last_write = time.monotonic()
            time.sleep(_SSE_POLL_BASE_S + random.random() * _SSE_POLL_JITTER_S)
    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
        pass  # client went away — the normal way an SSE stream ends
    finally:
        _sse_release_slot(handler.server)
        handler.close_connection = True


def _build_http_server(host: str, port: int):
    """Construct (but do not start) the MCP HTTP server.

    Split out from :func:`_serve_http` so tests can bind an ephemeral port,
    exercise the *real* handler, and shut it down — the previous test reached
    for Starlette/uvicorn (neither a dependency) and so was silently skipped in
    CI. Returns a bound ``ThreadingHTTPServer`` whose request policy (Host
    allowlist, Origin check, optional bearer token) is attached as attributes.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    auth_token = os.environ.get("MEMPALACE_MCP_HTTP_TOKEN", "").strip()
    if (
        not _http_is_loopback(host)
        and not auth_token
        and not _truthy_env(_HTTP_ALLOW_INSECURE_NO_TOKEN_ENV)
    ):
        raise ValueError(
            "MEMPALACE_MCP_HTTP_TOKEN is required when binding MCP HTTP to a "
            f"non-loopback host. Set {_HTTP_ALLOW_INSECURE_NO_TOKEN_ENV}=1 only "
            "when a trusted fronting layer provides access control."
        )

    # Resolve TLS before bind so a bad cert/key fails loudly rather than at the
    # first request. TLS is transport encryption only — the bearer-token guard
    # above still applies on a non-loopback bind.
    tls_cert, tls_key = _resolve_tls_paths()

    class _MCPHTTPServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address):
            # A client hanging up mid-response makes the send path raise
            # ConnectionError (BrokenPipeError / ConnectionResetError), or
            # ssl.SSLEOFError over TLS. That is a routine disconnect, not a
            # server fault, so log it at DEBUG rather than let the default
            # handler dump a per-request traceback. Real errors (including
            # genuine TLS handshake/cert failures) still reach that handler.
            exc = sys.exc_info()[1]
            is_disconnect = isinstance(exc, ConnectionError)
            if not is_disconnect:
                import ssl

                # Only the abrupt-EOF SSLError; genuine TLS errors must surface.
                is_disconnect = isinstance(exc, ssl.SSLEOFError)
            if is_disconnect:
                logger.debug(
                    "HTTP client %s disconnected before the response completed",
                    client_address,
                )
                return
            super().handle_error(request, client_address)

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 10

        def log_message(self, fmt, *args):
            logger.info("HTTP %s - " + fmt, self.client_address[0], *args)

        def send_error(self, code, message=None, explain=None):
            self._record_request(code)
            return super().send_error(code, message, explain)

        def _record_request(self, status: int) -> None:
            _http_record_request(self.server, self, status)

        def _status_payload(self) -> dict:
            return _http_status_payload(self.server)

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self._record_request(status)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, body, "application/json; charset=utf-8")

        def _request_rejected(self, require_auth: bool) -> bool:
            return _http_request_rejected(self, require_auth)

        def do_GET(self):
            _http_handle_get(self)

        def do_POST(self):
            if self._request_rejected(require_auth=True):
                return
            path = urlparse(self.path).path
            if path != "/mcp":
                self.send_error(404, "Not Found")
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0") or "0")
            except (TypeError, ValueError):
                content_length = 0

            if content_length < 0 or content_length > _HTTP_MAX_REQUEST_BYTES:
                self._send_json(
                    413,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Request too large"},
                    },
                )
                return

            try:
                raw = self.rfile.read(content_length)
                request = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                logger.warning("HTTP JSON-RPC read or parse error: %s", exc)
                self._send_json(400, _json_rpc_parse_error())
                return

            # Locking policy lives in _http_dispatch: global lock for
            # Chroma-touching tools, lock-free for logstream tools.
            response = _http_dispatch(request)

            if response is None:
                # JSON-RPC notifications intentionally have no response body.
                self._record_request(202)
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return

            self._send_json(200, response)

    httpd = _MCPHTTPServer((host, port), _Handler)
    bound_port = httpd.server_address[1]
    # Pin Host only on a loopback bind (the security-critical default). A
    # deliberately network-exposed bind is the operator's call and may sit
    # behind a proxy that rewrites Host, so we relax the pin there and lean on
    # the Origin check + optional token instead.
    httpd.enforce_host_pin = _http_is_loopback(host)
    httpd.allowed_hosts = _http_allowed_host_values(host, bound_port)
    httpd.auth_token = auth_token
    httpd.scheme = "http"
    httpd.bind_host = host
    httpd.started_at = datetime.now().isoformat()
    httpd.started_monotonic = time.monotonic()
    httpd.stats_lock = threading.Lock()
    httpd.request_count = 0
    httpd.status_counts = {}
    httpd.recent_clients = {}
    httpd.sse_lock = threading.Lock()
    httpd.sse_clients = 0
    httpd.sse_max_clients = _sse_max_clients()
    if tls_cert:
        httpd.socket = _wrap_tls(httpd.socket, tls_cert, tls_key)
        httpd.scheme = "https"
    return httpd


def _serve_http(host: str, port: int) -> None:
    """Serve JSON-RPC over HTTP in-process.

    This transport intentionally reuses the same ``handle_request`` dispatcher
    as stdio. The only change is the framing layer: HTTP mode avoids a
    long-lived stdout pipe for operators who run MemPalace behind an HTTP MCP
    client/proxy for days at a time.
    """
    try:
        httpd = _build_http_server(host, port)
    except (OSError, ValueError) as exc:
        logger.error("Failed to start MCP HTTP server on %s:%s: %s", host, port, exc)
        sys.exit(1)

    bound_port = httpd.server_address[1]

    # Register this process as the palace's hub so local short-lived writers
    # (save hooks, plain `mempalace mine`) forward their writes here instead
    # of colliding with the MCP writer lease. Best-effort: discovery is an
    # optimization, serving must not fail because the record could not be
    # written.
    _registered_palace = _config.palace_path
    if _registered_palace:
        try:
            from .. import server_registry

            server_registry.write_serverinfo(
                _registered_palace,
                host=host,
                port=bound_port,
                scheme=getattr(httpd, "scheme", "http"),
                read_only=_READ_ONLY,
                capabilities=["search_cli_compatible"],
                search_config_fingerprint=_config.search_config_fingerprint,
            )
            import atexit

            atexit.register(server_registry.clear_serverinfo, _registered_palace)
        except Exception:
            logger.debug("Failed to write hub serverinfo", exc_info=True)
            _registered_palace = None

    if not _http_is_loopback(host):
        if httpd.auth_token:
            logger.warning(
                "MemPalace MCP HTTP server bound to non-loopback host %s; /mcp "
                "requires the configured bearer token.",
                host,
            )
        else:
            logger.warning(
                "MemPalace MCP HTTP server bound to non-loopback host %s without "
                "a bearer token because %s is set.",
                host,
                _HTTP_ALLOW_INSECURE_NO_TOKEN_ENV,
            )

    # RFC 004 step 0: converge with peer replicas when peers.json exists.
    _start_peer_sync_thread()

    with httpd:
        logger.info(
            "MemPalace MCP HTTP server listening on %s://%s:%s/mcp%s%s",
            getattr(httpd, "scheme", "http"),
            host,
            bound_port,
            " (TLS)" if getattr(httpd, "scheme", "http") == "https" else "",
            " (read-only)" if _READ_ONLY else "",
        )
        try:
            httpd.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            logger.info("MemPalace MCP HTTP server shutting down")
        finally:
            if _registered_palace:
                try:
                    from .. import server_registry

                    server_registry.clear_serverinfo(_registered_palace)
                except Exception:
                    logger.debug("Failed to clear hub serverinfo", exc_info=True)
