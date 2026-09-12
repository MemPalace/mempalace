# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


def _get_result_ids(result) -> list:
    """Return ``get()`` result ids for both typed and dict-like collection results."""
    if result is None:
        return []
    ids = getattr(result, "ids", None)
    if ids is not None:
        return ids
    if isinstance(result, dict):
        return result.get("ids") or []
    getter = getattr(result, "get", None)
    if callable(getter):
        return getter("ids") or []
    return []


def _parse_args():
    parser = argparse.ArgumentParser(description="MemPalace MCP Server")
    parser.add_argument(
        "--palace",
        metavar="PATH",
        help="Path to the palace directory (overrides config file and env var)",
    )
    parser.add_argument(
        "--backend",
        metavar="NAME",
        help="Storage backend to use (default: config/env/detected/chroma)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Serve MCP over stdio (default) or in-process HTTP",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP host to bind when --transport=http (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="HTTP port to bind when --transport=http (default: 8765)",
    )
    parser.add_argument(
        "--tls-cert",
        metavar="PATH",
        help="PEM certificate to terminate TLS on the HTTP transport "
        "(requires --tls-key; env MEMPALACE_MCP_TLS_CERT)",
    )
    parser.add_argument(
        "--tls-key",
        metavar="PATH",
        help="PEM private key matching --tls-cert (env MEMPALACE_MCP_TLS_KEY)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Serve a read-only tool surface: the tools that change state are hidden "
        "from tools/list and refused at dispatch (env MEMPALACE_MCP_READ_ONLY)",
    )
    args, unknown = parser.parse_known_args()
    if unknown:
        logger.debug("Ignoring unknown args: %s", unknown)
    return args


_args = _parse_args()

if _args.palace:
    os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(_args.palace)
if _args.backend:
    backend_name = str(_args.backend).strip().lower()
    from ..backends import get_backend_class  # noqa: E402

    get_backend_class(backend_name)
    os.environ["MEMPALACE_BACKEND_EXPLICIT"] = backend_name
    os.environ["MEMPALACE_BACKEND"] = backend_name

_config = MempalaceConfig()

# Read-only server mode: when on, the tools in _READ_ONLY_REFUSED_TOOLS (defined
# below) are hidden from tools/list and refused at dispatch (-32003). That is a
# wider set than the _MUTATING_TOOLS the peer-writer guard uses. Resolved once at
# startup from --read-only or MEMPALACE_MCP_READ_ONLY. Computed inline (not via
# _truthy_env, defined below) so it is available to the request path regardless
# of import order.
_READ_ONLY = bool(getattr(_args, "read_only", False)) or os.environ.get(
    "MEMPALACE_MCP_READ_ONLY", ""
).strip().lower() in {"1", "true", "yes", "on"}

_kg_by_path: dict[str, KnowledgeGraph] = {}
_kg_cache_lock = threading.Lock()

_logstream_by_path: dict[str, Logstream] = {}
_logstream_cache_lock = threading.Lock()
# CLI-compatible search temporarily redirects process-wide stderr and fd 1.
# Keep that entire scope single-threaded even when tool_search is invoked
# outside the HTTP dispatch lock (tests, embedded hosts, future transports).
_cli_search_capture_lock = threading.Lock()
_palace_flag_given: bool = bool(_args.palace)

# MCP server idle auto-exit (#1552).  Stale MCP servers from ended Claude
# Code sessions do not self-terminate, accumulating ChromaDB/HNSW file
# handles on Windows.  When MEMPALACE_MCP_IDLE_HOURS is set (or defaults
# to 8 h), a background daemon thread exits the process once no request
# has been handled for that long.  Set to 0 to disable.
_MCP_IDLE_HOURS_ENV = "MEMPALACE_MCP_IDLE_HOURS"
_MCP_IDLE_HOURS_DEFAULT = 8.0
_last_request_time: float = time.monotonic()

# MCP startup/open SQLite integrity gate (#1818).
#
# The peer-writer guard prevents new concurrent writers, but an MCP server can
# still start against a palace that was already left corrupt by a prior writer
# crash/kill. Run the existing read-only SQLite quick_check once on startup/open
# and fail loudly instead of silently serving a malformed FTS5/HNSW index.
_sqlite_integrity_checked = False
_sqlite_integrity_errors: list[str] = []
_sqlite_integrity_check_error = ""
# Why no verdict exists, when none does. An empty _sqlite_integrity_errors is
# ambiguous on its own: quick_check found nothing wrong, or it never ran. Four
# exits in the refresh leave the list empty and only one of them means the
# database came back clean. This names one of the others, the palace with no
# chroma.sqlite3, so the status payload can report an absence rather than a
# clean bill of health.
_sqlite_integrity_no_verdict_reason = ""
# Serializes quick_check runs between the async startup preflight thread and
# lazy consumers on the protocol thread (double-checked in
# _ensure_sqlite_integrity_status) so the O(database size) probe never runs
# twice concurrently.
_sqlite_integrity_refresh_lock = threading.Lock()
_SQLITE_INTEGRITY_ERROR_CODE = -32002
_SQLITE_INTEGRITY_ALLOWED_TOOLS = frozenset(
    {
        "mempalace_status",
        "mempalace_reconnect",
        # RFC 003: logstream lives in its own logstream.sqlite3 with no
        # Chroma/FTS5 dependency, so agent coordination stays available
        # even while the main palace index is corrupt and under repair.
        "mempalace_event_append",
        "mempalace_task_create",
        "mempalace_event_list",
        "mempalace_event_wait",
        "mempalace_event_ack",
        "mempalace_artifact_put",
        "mempalace_artifact_get",
        "mempalace_patch_submit",
        # RFC 004: the estate is observability — logstream + sync state +
        # peers.json, no FTS5 dependency (the profile's drawer count
        # degrades gracefully). A damaged palace is exactly when mesh
        # visibility matters most; caught live on the third replica.
        "mempalace_mesh_peers",
    }
)

# The startup probe above runs PRAGMA quick_check, which reads every page of
# chroma.sqlite3 and is therefore O(database size). On multi-GB palaces it can
# exceed the MCP client's connection/handshake timeout, so the server never
# finishes starting and the client drops the connection (the peer-writer guard
# and lazy consumers all funnel through _refresh_sqlite_integrity_status). Skip
# the *startup* probe when the database exceeds this size (MB). `mempalace
# repair` still runs the full quick_check via repair.sqlite_integrity_errors
# before any destructive rebuild, so corruption is still caught where it
# matters. Set MEMPALACE_STARTUP_INTEGRITY_MAX_MB=0 to disable the gate and
# always run the startup probe.
_STARTUP_INTEGRITY_MAX_MB_ENV = "MEMPALACE_STARTUP_INTEGRITY_MAX_MB"
_STARTUP_INTEGRITY_MAX_MB_DEFAULT = 512.0


# MCP peer-writer guard (#1818).
#
# The existing per-operation palace lock serializes individual writes, but it
# cannot make another long-lived Chroma PersistentClient forget stale in-memory
# HNSW/FTS state. Hold the same per-palace mine lock for this MCP process
# lifetime. A peer MCP process can still serve read tools, but mutating tools
# refuse before touching Chroma or the knowledge graph.
_MCP_WRITER_LOCK_CM = None
_MCP_WRITER_READ_ONLY = False
_MCP_WRITER_LOCK_FAILED = False
_MCP_WRITER_LOCK_ERROR = ""
_MCP_WRITER_ATEXIT_REGISTERED = False
_MCP_ALLOW_PEER_WRITER_ENV = "MEMPALACE_MCP_ALLOW_PEER_WRITER"

_MUTATING_TOOLS = frozenset(
    {
        "mempalace_kg_add",
        "mempalace_kg_invalidate",
        "mempalace_kg_supersede",
        "mempalace_create_tunnel",
        "mempalace_delete_tunnel",
        "mempalace_delete_hallway",
        "mempalace_add_drawer",
        "mempalace_delete_drawer",
        "mempalace_checkpoint",
        "mempalace_delete_by_source",
        "mempalace_mine",
        "mempalace_sync",
        "mempalace_update_drawer",
        "mempalace_diary_write",
        "mempalace_event_append",
        "mempalace_task_create",
        "mempalace_event_ack",
        "mempalace_artifact_put",
        "mempalace_patch_submit",
    }
)

# Logstream mutating tools (RFC 003) write only to logstream.sqlite3 — an
# independent WAL database with no Chroma/HNSW in-memory state — so the
# peer-writer lease that protects Chroma does not apply to them. Exempting
# them keeps agent coordination alive while a CLI mine or a peer stdio
# writer holds the palace lock. They remain in _MUTATING_TOOLS so operator
# read-only mode (--read-only / MEMPALACE_MCP_READ_ONLY) still hides and
# refuses them.
_PEER_WRITER_EXEMPT_TOOLS = frozenset(
    {
        "mempalace_event_append",
        "mempalace_task_create",
        "mempalace_event_ack",
        "mempalace_artifact_put",
        "mempalace_patch_submit",
    }
)

# The subset of _MUTATING_TOOLS whose write path reaches the chroma vector
# segment. Deliberately narrower: the knowledge-graph and tunnel/hallway tools
# keep their own sqlite/JSON state and never touch HNSW, so an unusable vector
# index has no say over them.
#
# The distinction earns its keep because a write into a diverged HNSW segment
# does not fail — it blocks inside chromadb's Rust upsert with no timeout of its
# own, for the life of the process, while this server holds the palace mine lock
# and the writer lease. One stuck call becomes a palace-wide outage that a still
# healthy handshake hides.
_VECTOR_WRITE_TOOLS = frozenset(
    {
        "mempalace_add_drawer",
        "mempalace_update_drawer",
        "mempalace_delete_drawer",
        "mempalace_delete_by_source",
        "mempalace_diary_write",
        "mempalace_checkpoint",
        "mempalace_mine",
        "mempalace_sync",
    }
)

_DIVERGED_INDEX_ERROR_CODE = -32004

# Read-only mode (#1877) refuses a wider set than the peer-writer guard above.
#
# _MUTATING_TOOLS is the *palace-write* set: _mcp_peer_writer_refusal consults it
# to decide which calls need this process to hold the palace mine lock. A tool
# that never touches Chroma or the knowledge graph has to stay out of that set,
# or a server that lost the lease to a peer would start refusing calls the lease
# has no say over.
#
# Two tools are exactly that shape, and read-only has to name both because it is
# a capability boundary rather than a lock: it exists so a shared server can
# serve recall to a client that must not change server state.
#
#   mempalace_hook_settings, given an argument, writes the server's
#   ~/.mempalace/config.json through MempalaceConfig.set_hook_setting.
#   service.WRITE_TOOLS already classifies it as a write, which the daemon uses
#   as an allowlist, so read-only was the odd one out.
#
#   mempalace_memories_filed_away unlinks ~/.mempalace/hook_state/last_checkpoint
#   on both of its branches. Consuming the file is the contract of the tool, but
#   it is still a delete of state that outlives the process, on behalf of a
#   client with no write access. (service.classify_tool calls this one "read",
#   which is wrong for the same reason.)
#
# mempalace_reconnect is deliberately NOT here even though it is not write-free:
# it clears ChromaBackend._quarantined_paths, so the reopen that follows can let
# quarantine_stale_hnsw rename a segment directory. It is the only way to pick up
# an external writer's changes, and _SQLITE_INTEGRITY_ALLOWED_TOOLS already keeps
# it reachable for recovery, so gating it would strand a read-only server on a
# stale index. This set means "refuse what a client asked to change", not
# "nothing past here touches the disk" -- opening the palace or the knowledge
# graph materialises files on its own, which no name-based gate can express.
_READ_ONLY_REFUSED_TOOLS = _MUTATING_TOOLS | {
    "mempalace_hook_settings",
    "mempalace_memories_filed_away",
}


# Stale-library write gate (#899).
#
# A long-lived MCP server imports mempalace and its storage backend once and
# then serves from those in-memory modules for the whole life of the process.
# Upgrading the package on disk mid-session (`pip install -U mempalace`,
# `uv tool upgrade`, `pipx install --force`) cannot reach it: Python caches
# modules in sys.modules and never reloads them. The server keeps accepting
# writes, produced by code the user no longer has installed, and reports
# success for every one of them.
#
# #457 is the same condition with the volume turned up: upgrading mempalace
# tightened its ChromaDB pin and moved the installed ChromaDB across a
# storage-format boundary (downwards, 1.5.6 to 0.6.3), the running server kept
# serving the modules it had already loaded, and every tool call then failed
# with `no such column: collections.schema_str` until the host was restarted. That one
# announced itself. #899 reports the quiet variant, where the writes succeed in
# a format the newly-installed library may not read back and nothing surfaces
# until a fresh process opens the palace. Either way nothing told the running
# server to stop first; `mempalace migrate` (#502) only recovers such a palace
# after the fact.
#
# Watched distributions are the ones whose code writes the palace on this
# machine: mempalace itself, plus chromadb when chromadb is the backend
# actually serving. Its on-disk format has moved between releases, which is
# what makes a superseded copy of it dangerous. The networked backends
# (pgvector/qdrant/milvus) keep their format server-side, so a client-library
# upgrade does not rewrite local files the same way, and they are deliberately
# left out rather than gated on a guess.
#
# chromadb is a hard dependency rather than an extra, so it is installed even
# for someone serving from pgvector, and watching it unconditionally would
# refuse that person's writes over an upgrade to a library that touches nothing
# they own. The backend is read once here, at import, from the same config the
# server goes on to serve with. One that cannot be resolved counts as chroma:
# watching a distribution that turns out not to matter costs a restart, while
# not watching the one that does costs the silent corruption this exists to
# prevent.
#
# The resolved versions are cached behind a stat-only fingerprint, so the common
# case reads no metadata at all. That is not the same as free: building the
# fingerprint lists every search root once and stats the watched metadata files
# it finds there, and on a normal environment the listing is most of the cost.
# It is paid only by mutating tools — the refusal checks the tool name before
# anything else, so a read leaves the filesystem alone entirely. The one
# exception among reads is mempalace_status, which pays it deliberately, being
# the surface that reports this state.
#
# The cache is invalidated by that fingerprint and never by elapsed time,
# because a time window is precisely the gap a post-upgrade write slips
# through. The fingerprint covers what installers actually do: a renamed or
# removed metadata directory, and a metadata file rewritten in place. It cannot
# see a rewrite that leaves both the size and the recorded mtime untouched,
# which needs the replacement to be the same length AND to land inside the
# filesystem's timestamp granularity — nanoseconds on ext4, but roughly 15 ms
# on Windows and two seconds on FAT. An upgrade arriving minutes into a session
# clears that comfortably; an archive restore or a rewrite in the same tick as
# the previous reading does not, and that case fails open, leaving the gate no
# worse than its absence.
# -32004 belongs to the diverged-index gate; this one takes the next free code
# so a client can tell "restart the server" from "rebuild the index" without
# parsing the message.
_STALE_LIBRARY_ERROR_CODE = -32005


def _stale_library_watched_dists() -> tuple:
    """Distributions worth comparing, given the backend this server serves with."""
    watched = ["mempalace"]
    try:
        backend = str(_config.backend).strip().lower()
    except Exception:
        # Config trouble is not a reason to narrow the check.
        logger.debug("stale-library backend could not be resolved", exc_info=True)
        backend = "chroma"
    if backend == "chroma":
        watched.append("chromadb")
    return tuple(watched)


_STALE_LIBRARY_WATCHED_DISTS = _stale_library_watched_dists()
_MCP_ALLOW_STALE_LIBRARY_ENV = "MEMPALACE_MCP_ALLOW_STALE_LIBRARY"
# A distribution version is PEP 440 text. This metadata is a file on disk that
# the server does not own, and its value is quoted back to the client, so its
# shape is checked before it is echoed anywhere.
_VERSION_TEXT_RE = re.compile(r"\A[A-Za-z0-9._+!-]{1,64}\Z")
# Reported in place of a version when a distribution that was installed at
# startup is no longer installed at all. Not a valid PEP 440 version, so it can
# never collide with a real one.
_UNINSTALLED = "not installed"
_stale_library_cache_lock = threading.Lock()
_stale_library_cache: dict = {"signature": None, "versions": {}, "errors": {}}
# What has already been announced to the log, so a persistent condition is
# reported once rather than on every mutating call. These have their own lock
# because they are not part of the cached reading and outlive it: the cache is
# dropped whenever a reading fails, while what was last logged has to survive
# exactly that. Putting them behind the cache lock would also make the log
# bookkeeping contend for it on every mutating call.
_stale_library_log_lock = threading.Lock()
_stale_library_reported_errors: dict = {}
_stale_library_reported_drift: list = []


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _discard_mcp_storage_handles() -> None:
    """Close cached storage handles before changing writer-lease state.

    A stdio reader can hold a genuine read-only ``sqlite_exact`` collection
    while another process owns the palace. Once this process promotes to
    writer, that cached collection must not keep routing the mutating request
    through its ``query_only`` connection. The inverse matters for embedded
    HTTP: close writable handles before releasing the lifetime lease so no
    storage client survives beyond the ownership interval.

    Also clears per-process embedder-identity validation for this palace:
    a prior read-only open of an empty collection may have cached a "validated"
    key without recording identity on disk; promotion must re-run enforcement
    so the first writable open still labels drawers with the active model.
    """

    global \
        _client_cache, \
        _collection_cache, \
        _collection_cache_backend, \
        _collection_cache_palace, \
        _collection_open_error, \
        _palace_db_inode, \
        _palace_db_mtime, \
        _metadata_cache, \
        _metadata_cache_time

    cached_client = _client_cache
    try:
        from ..palace import clear_validated_embedder_identity, get_backend_for_palace

        backend = get_backend_for_palace(_config.palace_path)
        backend.close_palace(PalaceRef(id=_config.palace_path, local_path=_config.palace_path))
        clear_validated_embedder_identity(_config.palace_path)
    except Exception:
        logger.debug("Failed to close cached backend while changing MCP ownership", exc_info=True)
        try:
            from ..palace import clear_validated_embedder_identity

            clear_validated_embedder_identity(getattr(_config, "palace_path", None))
        except Exception:
            logger.debug(
                "Failed to clear embedder-identity cache while changing MCP ownership",
                exc_info=True,
            )

    if cached_client is not None:
        try:
            close = getattr(cached_client, "close", None)
            if callable(close):
                close()
        except Exception:
            logger.debug(
                "Failed to close MCP-local client while changing ownership",
                exc_info=True,
            )

    _client_cache = None
    _collection_cache = None
    _collection_cache_backend = None
    _collection_cache_palace = None
    _collection_open_error = None
    _palace_db_inode = 0
    _palace_db_mtime = 0.0
    _invalidate_overview_caches()


def _release_mcp_writer_lock() -> None:
    """Close writable handles and release this process's palace lease."""

    global _MCP_WRITER_LOCK_CM, _MCP_WRITER_READ_ONLY

    lock_cm = _MCP_WRITER_LOCK_CM
    if lock_cm is None:
        return

    try:
        _discard_mcp_storage_handles()
    finally:
        # Clear first so the atexit callback and embedded hosts can call this
        # repeatedly without exiting the same context manager twice.
        _MCP_WRITER_LOCK_CM = None
        _MCP_WRITER_READ_ONLY = False
        lock_cm.__exit__(None, None, None)


def _acquire_mcp_writer_lock() -> tuple[bool, str]:
    """Acquire this process's per-palace MCP writer lease.

    Returns (True, "") when this process may write. Returns (False, reason)
    when another writer owns the lease or writer initialization fails.

    Self-healing: a server that came up read-only (a peer held the lease at
    startup) RE-ATTEMPTS the non-blocking flock on every subsequent call.
    ``_mcp_peer_writer_refusal`` invokes this on each mutating tool, so once
    the original holder exits — the OS releases its flock on process death —
    the next mutating call transparently promotes this server to writer, with
    no restart. The flock is arbitrated by the kernel (LOCK_NB), so two servers
    can never both win the retry. ``_MCP_WRITER_READ_ONLY`` and
    ``_MCP_WRITER_LOCK_FAILED`` are now only status flags for the last attempt;
    neither short-circuits a later retry. Peer ownership and transient setup
    failures can both be corrected without restarting the MCP host.
    """

    global _MCP_WRITER_LOCK_CM, _MCP_WRITER_READ_ONLY, _MCP_WRITER_LOCK_FAILED
    global _MCP_WRITER_LOCK_ERROR, _MCP_WRITER_ATEXIT_REGISTERED

    if _MCP_WRITER_LOCK_CM is not None:
        return True, ""

    # Deliberately no sticky failure short-circuit here. A peer can exit, a
    # backend mismatch can be corrected, and lock-directory permissions can be
    # repaired while this long-lived stdio host remains alive. Each mutating
    # request therefore gets a fresh ownership attempt.

    _MCP_WRITER_READ_ONLY = False
    _MCP_WRITER_LOCK_FAILED = False
    _MCP_WRITER_LOCK_ERROR = ""

    try:
        from ..palace import (
            MineAlreadyRunning,
            backend_requires_single_writer,
            mine_palace_lock,
            resolve_backend_name,
        )

        backend_name = resolve_backend_name(_config.palace_path)
        if _truthy_env(_MCP_ALLOW_PEER_WRITER_ENV):
            if not backend_requires_single_writer(backend_name):
                return True, ""
            logger.warning(
                "%s cannot bypass the single-writer requirement for local backend %r",
                _MCP_ALLOW_PEER_WRITER_ENV,
                backend_name,
            )

        lock_cm = mine_palace_lock(_config.palace_path)
        lock_cm.__enter__()
    except MineAlreadyRunning as exc:
        _MCP_WRITER_READ_ONLY = True
        _MCP_WRITER_LOCK_ERROR = (
            "another mempalace writer already holds the palace lock for "
            f"{_config.palace_path!r}: {exc}"
        )
        return False, _MCP_WRITER_LOCK_ERROR
    except Exception as exc:
        _MCP_WRITER_LOCK_FAILED = True
        _MCP_WRITER_LOCK_ERROR = (
            "could not acquire MCP peer-writer lock for "
            f"{_config.palace_path!r}: {exc!r}; refusing this mutating tool "
            "because peer-writer protection could not be established; a later "
            "mutating request will retry ownership"
        )
        logger.error(_MCP_WRITER_LOCK_ERROR)
        return False, _MCP_WRITER_LOCK_ERROR

    _MCP_WRITER_LOCK_CM = lock_cm
    import atexit

    if not _MCP_WRITER_ATEXIT_REGISTERED:
        atexit.register(_release_mcp_writer_lock)
        _MCP_WRITER_ATEXIT_REGISTERED = True
    # Reads performed before promotion may have cached a query-only SQLite
    # collection. Drop it while ownership is held so the pending mutating
    # request reopens a writable handle rather than failing on query_only.
    _discard_mcp_storage_handles()
    _MCP_WRITER_READ_ONLY = False
    _MCP_WRITER_LOCK_FAILED = False
    _MCP_WRITER_LOCK_ERROR = ""
    return True, ""


def _mcp_peer_writer_refusal(req_id, tool_name: str):
    if tool_name not in _MUTATING_TOOLS or tool_name in _PEER_WRITER_EXEMPT_TOOLS:
        return None

    ok, reason = _acquire_mcp_writer_lock()
    if ok:
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32001,
            "message": (
                "MCP writer initialization failed; this server is read-only for mutating tools"
                if _MCP_WRITER_LOCK_FAILED
                else "Peer MCP writer active; this server is read-only for mutating tools"
            ),
            "data": {
                "tool": tool_name,
                "palace": _config.palace_path,
                "reason": reason,
                "failure_kind": (
                    "initialization_failed" if _MCP_WRITER_LOCK_FAILED else "peer_contention"
                ),
            },
        },
    }


def _startup_integrity_size_limit_bytes() -> int:
    """Byte size above which the startup SQLite quick_check is skipped.

    Returns 0 when the gate is disabled (``MEMPALACE_STARTUP_INTEGRITY_MAX_MB``
    set to 0, a non-positive number, or an unparseable value), meaning the
    startup probe always runs.
    """

    raw = os.environ.get(_STARTUP_INTEGRITY_MAX_MB_ENV, "").strip()
    if not raw:
        mb = _STARTUP_INTEGRITY_MAX_MB_DEFAULT
    else:
        try:
            mb = float(raw)
        except ValueError:
            logger.warning(
                "Invalid %s=%r; using default %.0f MB",
                _STARTUP_INTEGRITY_MAX_MB_ENV,
                raw,
                _STARTUP_INTEGRITY_MAX_MB_DEFAULT,
            )
            mb = _STARTUP_INTEGRITY_MAX_MB_DEFAULT
    if mb <= 0:
        return 0
    return int(mb * 1024 * 1024)


def _refresh_sqlite_integrity_status() -> None:
    """Refresh the MCP startup SQLite/FTS5 integrity gate.

    Uses repair.sqlite_integrity_status(), which wraps the read-only
    quick_check backing repair preflight and adds whether a verdict exists at
    all. A failure here is treated as an integrity failure so the server does
    not proceed silently after a malformed FTS5 index or other SQLite-layer
    corruption (#1818).
    """

    with _sqlite_integrity_refresh_lock:
        _refresh_sqlite_integrity_status_locked()


def _refresh_sqlite_integrity_status_locked() -> None:
    # Probe body; callers must hold _sqlite_integrity_refresh_lock.
    global _sqlite_integrity_checked
    global _sqlite_integrity_errors
    global _sqlite_integrity_check_error
    global _sqlite_integrity_no_verdict_reason

    if not _config.palace_path or not _is_chroma_backend():
        _sqlite_integrity_checked = True
        _sqlite_integrity_errors = []
        _sqlite_integrity_check_error = ""
        _sqlite_integrity_no_verdict_reason = ""
        return

    max_bytes = _startup_integrity_size_limit_bytes()
    if max_bytes > 0:
        sqlite_path = os.path.join(_config.palace_path, "chroma.sqlite3")
        try:
            db_bytes = os.path.getsize(sqlite_path)
        except OSError:
            db_bytes = 0
        if db_bytes > max_bytes:
            _sqlite_integrity_checked = True
            _sqlite_integrity_errors = []
            _sqlite_integrity_check_error = ""
            # This exit is its own kind of "no verdict" and does not yet name
            # itself (#2240). It must at least not inherit the previous
            # probe's reason: a palace that had no database when the server
            # started, and has an oversized one now, would otherwise be
            # described as having no database at all.
            _sqlite_integrity_no_verdict_reason = ""
            logger.warning(
                "SQLite startup integrity check skipped: %s is %.0f MB "
                "(> %.0f MB limit); PRAGMA quick_check would block MCP "
                "startup. Run `mempalace repair` for a full check, or set "
                "%s (MB; 0 disables the limit).",
                sqlite_path,
                db_bytes / (1024 * 1024),
                max_bytes / (1024 * 1024),
                _STARTUP_INTEGRITY_MAX_MB_ENV,
            )
            return

    try:
        from ..repair import sqlite_integrity_status

        status = sqlite_integrity_status(_config.palace_path)
    except Exception as exc:
        _sqlite_integrity_check_error = (
            f"sqlite integrity probe failed: {type(exc).__name__}: {exc}"
        )
        _sqlite_integrity_errors = [_sqlite_integrity_check_error]
        _sqlite_integrity_no_verdict_reason = ""
    else:
        fresh_errors = [str(error) for error in status.errors if str(error)]
        # _sqlite_integrity_payload reads these globals without the lock, so
        # the order within each branch is chosen to keep that branch's own
        # window on the safer side: entering "no verdict" records the reason
        # before the list it explains, and leaving it clears the reason only
        # once the fresh errors are in place.
        #
        # No write order makes the payload safe, and this one does not claim
        # to. That reader loads the error list more than once, so a refresh
        # landing between two of its loads can still publish `ok: true` for a
        # palace with no database. The way to close that is to publish the
        # verdict as one value, which is a change to what this gate stores
        # rather than to
        # the order it stores it in.
        if status.checked:
            _sqlite_integrity_errors = fresh_errors
            _sqlite_integrity_no_verdict_reason = ""
        else:
            _sqlite_integrity_no_verdict_reason = status.reason
            _sqlite_integrity_errors = fresh_errors
        _sqlite_integrity_check_error = ""

    _sqlite_integrity_checked = True

    if _sqlite_integrity_errors:
        logger.error(
            "SQLite integrity check failed for palace=%s: %s",
            _config.palace_path,
            "; ".join(_sqlite_integrity_errors[:3]),
        )


def _ensure_sqlite_integrity_status() -> None:
    if _sqlite_integrity_checked:
        return
    with _sqlite_integrity_refresh_lock:
        # Double-checked: the startup preflight thread may have finished the
        # probe while this caller waited on the lock — don't pay the
        # O(database size) quick_check twice.
        if not _sqlite_integrity_checked:
            _refresh_sqlite_integrity_status_locked()


def _sqlite_integrity_payload() -> dict:
    _ensure_sqlite_integrity_status()

    # These globals are read one at a time, without the refresh lock, as they
    # have always been, and a refresh landing between two of those reads can
    # make this function publish a verdict the gate never held. Both directions
    # produce one: reading through, as here, can answer `ok: true` for a palace
    # with no database, because the error list the branch below tests is read
    # again when the payload is built; snapshotting into locals first can answer
    # `ok: true` for a palace whose corruption the refresh had just found,
    # because separate loads stay separate moments either way. A snapshot
    # therefore trades one window for another of the same class rather than
    # closing anything, which is why it is not taken here. Holding the refresh
    # lock across this function would close it, at the cost of serialising every
    # status read behind an O(database size) probe; publishing the verdict as
    # one value closes it without that, and is the change worth making.
    #
    # The integrity gate only knows how to check chroma.sqlite3, and
    # _refresh_sqlite_integrity_status short-circuits for non-chroma backends,
    # so on a non-chroma backend no quick_check runs. Reporting checked/ok true
    # would imply a verification that never happened and reference a
    # chroma.sqlite3 the active backend does not use (#1931). Recorded errors
    # only ever come from the chroma path, so surface them regardless of the
    # backend lookup (which may itself fail); only the clean case is
    # reclassified as not-applicable.
    if not _sqlite_integrity_errors:
        try:
            backend_name = _selected_backend_name()
        except Exception:
            logger.debug("backend resolution failed for integrity payload", exc_info=True)
            backend_name = ""
        if backend_name != "chroma":
            return {
                "checked": False,
                "ok": None,
                "palace": _config.palace_path or "",
                "sqlite_path": "",
                "error_count": 0,
                "errors": [],
                "reason": (
                    "chroma.sqlite3 integrity check does not run for backend "
                    f"{backend_name or 'unknown'!r}"
                ),
            }
        # Same shape, second way this payload reports an absent verdict: the
        # backend is chroma, but there was no database to open. Reporting
        # checked/ok true here would claim a quick_check that never ran. A
        # palace whose file is unreachable rather than absent does not arrive
        # here at all: its probe recorded an error, so it falls through to the
        # verdict payload below with `ok: false`.
        if _sqlite_integrity_no_verdict_reason:
            return {
                "checked": False,
                "ok": None,
                "palace": _config.palace_path or "",
                "sqlite_path": os.path.join(_config.palace_path, "chroma.sqlite3")
                if _config.palace_path
                else "",
                "error_count": 0,
                "errors": [],
                "reason": _sqlite_integrity_no_verdict_reason,
            }

    payload = {
        "checked": _sqlite_integrity_checked,
        "ok": not _sqlite_integrity_errors,
        "palace": _config.palace_path,
        "sqlite_path": os.path.join(_config.palace_path, "chroma.sqlite3")
        if _config.palace_path
        else "",
        "error_count": len(_sqlite_integrity_errors),
        "errors": _sqlite_integrity_errors[:10],
    }

    if len(_sqlite_integrity_errors) > 10:
        payload["truncated"] = len(_sqlite_integrity_errors) - 10

    if _sqlite_integrity_check_error:
        payload["check_error"] = _sqlite_integrity_check_error

    return payload


def _mcp_sqlite_integrity_refusal(req_id, tool_name: str):
    if tool_name in _SQLITE_INTEGRITY_ALLOWED_TOOLS:
        return None

    _ensure_sqlite_integrity_status()

    if not _sqlite_integrity_errors:
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": _SQLITE_INTEGRITY_ERROR_CODE,
            "message": (
                "Palace SQLite integrity check failed; refusing tool call "
                "until the palace is repaired"
            ),
            "data": {
                "tool": tool_name,
                "palace": _config.palace_path or "",
                "sqlite_path": (
                    os.path.join(_config.palace_path, "chroma.sqlite3")
                    if _config.palace_path
                    else ""
                ),
                "errors": _sqlite_integrity_errors[:10],
                "error_count": len(_sqlite_integrity_errors),
                "hint": (
                    "Stop all MemPalace MCP clients/writers, back up the palace, "
                    "repair the SQLite/FTS5 corruption offline, then run "
                    "mempalace_reconnect or restart the MCP server."
                ),
            },
        },
    }


def _mcp_idle_timeout_secs() -> float:
    """Return the configured MCP idle timeout in seconds (0 = disabled)."""
    raw = os.environ.get(_MCP_IDLE_HOURS_ENV, "")
    if raw:
        try:
            hours = float(raw)
            return max(0.0, hours) * 3600
        except ValueError:
            return 0.0
    return _MCP_IDLE_HOURS_DEFAULT * 3600


def _resolve_kg_path() -> str:
    if _palace_flag_given:
        return os.path.join(_config.palace_path, "knowledge_graph.sqlite3")
    return DEFAULT_KG_PATH


def _canonicalize_kg_path(path: str) -> str:
    """Canonicalize a KG cache key so aliases collapse onto one entry.

    ``realpath`` resolves symlinks: two tenants pointing at the same
    SQLite file via different layouts (``/srv/A`` and
    ``/srv/link-to-A``) hit a single cached ``KnowledgeGraph`` rather
    than opening duplicate connections. ``normcase`` normalizes Windows
    drive-letter casing (``C:\\palace`` vs ``c:\\palace``) and
    path-separator style; on POSIX it returns the input unchanged.
    """
    return os.path.normcase(os.path.realpath(path))


def _get_kg(canonical_path=None) -> KnowledgeGraph:
    """Return the cached ``KnowledgeGraph`` for the resolved palace.

    When ``canonical_path`` is ``None`` (default), the path is resolved
    from module state and canonicalized. Callers like :func:`_call_kg`
    that have already captured a canonical key before entering a retry
    loop should pass it through here so the dict insertion uses the same
    key the caller will later use for eviction. Recomputing the key
    inside this function would let ``MEMPALACE_PALACE_PATH`` rotation,
    a symlink remap, or a mount remap between the captured value and
    this call drift the insert and evict keys apart, stranding a closed
    handle under one key while the lookup probes another.
    """
    path = (
        canonical_path if canonical_path is not None else _canonicalize_kg_path(_resolve_kg_path())
    )
    kg = _kg_by_path.get(path)
    if kg is not None:
        return kg
    with _kg_cache_lock:
        kg = _kg_by_path.get(path)
        if kg is None:
            kg = KnowledgeGraph(db_path=path)
            _kg_by_path[path] = kg
    return kg


def _call_kg(op):
    """Run ``op(kg)`` against the cached KG with one-shot retry on close.

    Race we're guarding against: a handler grabs ``kg = _get_kg()`` and is
    about to call ``kg.add_triple(...)`` when ``tool_reconnect`` fires on
    another thread, drains ``_kg_by_path``, and closes the underlying
    sqlite3.Connection. The handler's call then raises
    ``sqlite3.ProgrammingError: Cannot operate on a closed database`` and
    bubbles up as a -32000 to the MCP client even though the user just
    asked for a reconnect.

    Catch that single class of error, evict the stale entry from the
    cache (only if it still points at the closed instance — another
    thread may have already replaced it), and try once more with a fresh
    KG. Beyond one retry give up: a second close means we're losing a
    sustained race we won't win in this loop, and a hung loop is worse
    than a clear failure surface.

    The canonical path is captured once at the top and threaded through
    every ``_get_kg`` call plus the eviction lookup. Doing canonicalize
    only here means an ``OSError`` from ``realpath`` (transient Windows
    junction loss, broken mount) surfaces cleanly before any handler
    runs instead of masking a ``sqlite3.ProgrammingError`` mid-retry.
    Passing the captured key through to ``_get_kg`` also locks the
    insert key to the evict key even if FS or env state mutates between
    attempts, preventing a closed handle from leaking under a stale
    key the lookup no longer matches.
    """
    path = _canonicalize_kg_path(_resolve_kg_path())
    for attempt in range(2):
        kg = _get_kg(path)
        try:
            return op(kg)
        except sqlite3.ProgrammingError:
            if attempt == 0:
                with _kg_cache_lock:
                    if _kg_by_path.get(path) is kg:
                        _kg_by_path.pop(path, None)
                continue
            raise


def _resolve_logstream_path() -> str:
    """Resolve the RFC 003 logstream database path in the active palace.

    Fails clearly (ValueError) when no palace path is configured — the
    logstream must never silently write outside the palace directory.
    """
    palace_path = getattr(_config, "palace_path", None) or MempalaceConfig().palace_path
    if not palace_path:
        raise ValueError("no palace path configured; run mempalace init first")
    return os.path.join(os.path.expanduser(palace_path), LOGSTREAM_DB_FILENAME)


def _get_logstream(canonical_path=None) -> Logstream:
    """Return the cached ``Logstream`` for the resolved palace.

    Same canonical-key contract as :func:`_get_kg`: callers inside a retry
    loop pass the captured key through so insert and evict keys cannot
    drift apart under palace-path rotation.
    """
    path = (
        canonical_path
        if canonical_path is not None
        else _canonicalize_kg_path(_resolve_logstream_path())
    )
    ls = _logstream_by_path.get(path)
    if ls is not None:
        return ls
    with _logstream_cache_lock:
        ls = _logstream_by_path.get(path)
        if ls is None:
            ls = Logstream(db_path=path)
            _logstream_by_path[path] = ls
    return ls


def _call_logstream(op):
    """Run ``op(logstream)`` with one-shot retry on a closed connection.

    Mirrors :func:`_call_kg`: ``tool_reconnect`` on another thread can
    close the cached handle between lookup and use; evict the stale entry
    and retry once with a fresh instance.
    """
    path = _canonicalize_kg_path(_resolve_logstream_path())
    for attempt in range(2):
        ls = _get_logstream(path)
        try:
            return op(ls)
        except sqlite3.ProgrammingError:
            if attempt == 0:
                with _logstream_cache_lock:
                    if _logstream_by_path.get(path) is ls:
                        _logstream_by_path.pop(path, None)
                continue
            raise


_client_cache = None
_collection_cache = None
_collection_cache_backend = None
_collection_cache_palace = None
_collection_open_error = None
_palace_db_inode = 0  # inode of chroma.sqlite3 at cache time
_palace_db_mtime = 0.0  # mtime of chroma.sqlite3 at cache time


def _is_transient_index_error(result) -> bool:
    # Chroma can return "Internal error: Error finding id" during the
    # HNSW flush window after a bulk CLI mine — SQLite rows are
    # committed but the binary segment metadata isn't flushed yet.
    # Self-heals once the flush completes (~30-60s). See issue #1315.
    if not isinstance(result, dict):
        return False
    err = result.get("error", "")
    if not isinstance(err, str):
        return False
    err_l = err.lower()
    return (
        "error finding id" in err_l
        or "internal error" in err_l
        or "stale-index" in err_l
        or "stale index" in err_l
    )


def _force_chroma_cache_reset() -> None:
    # Drop both the MCP-local client cache and the shared backend's
    # per-palace cache so the next call rebuilds against the post-flush
    # state. Without clearing _DEFAULT_BACKEND._clients the retry
    # would just hit the same stale handle, since tool_search routes
    # via search_memories -> palace.get_collection -> backend cache.
    global \
        _client_cache, \
        _collection_cache, \
        _collection_cache_backend, \
        _collection_cache_palace, \
        _collection_open_error, \
        _palace_db_inode, \
        _palace_db_mtime, \
        _metadata_cache, \
        _metadata_cache_time
    cached_client = _client_cache
    _client_cache = None
    _collection_cache = None
    _collection_cache_backend = None
    _collection_cache_palace = None
    _collection_open_error = None
    _palace_db_inode = 0
    _palace_db_mtime = 0.0
    _invalidate_overview_caches()
    # This runs on the #1315 retry path, which drops caches precisely to
    # re-observe the palace after a transient index error. The capacity verdict
    # is another cached view of that same palace, so it must be dropped too, or
    # the retry could be answered from the pre-error verdict (its 10 s ceiling
    # outlasts the 2 s retry sleep).
    reset_hnsw_capacity_cache()
    try:
        from ..palace import get_backend_for_palace

        backend = get_backend_for_palace(_config.palace_path)
        backend.close_palace(PalaceRef(id=_config.palace_path, local_path=_config.palace_path))
    except Exception:
        logger.debug("Failed to close cached Chroma backend during cache reset", exc_info=True)
    if cached_client is not None:
        try:
            close = getattr(cached_client, "close", None)
            if callable(close):
                close()
        except Exception:
            logger.debug(
                "Failed to close MCP-local Chroma client during cache reset", exc_info=True
            )
    try:
        from chromadb.api.client import SharedSystemClient

        clear_system_cache = getattr(SharedSystemClient, "clear_system_cache", None)
        if callable(clear_system_cache):
            clear_system_cache()
    except Exception:
        logger.debug("Failed to clear Chroma shared system cache during cache reset", exc_info=True)


# ── Vector-search disabled flag (#1222) ──────────────────────────────────
# Set when ``hnsw_capacity_status`` reports a divergence between sqlite
# and the HNSW segment large enough that chromadb would segfault on
# segment load. While this is set, vector-shaped tools (``search``,
# ``check_duplicate``) route to the sqlite-only BM25 fallback in
# :func:`mempalace.searcher._bm25_only_via_sqlite`. Cleared after a
# successful repair via :func:`tool_reconnect` (which re-runs the probe).
_vector_disabled = False
_vector_disabled_reason = ""
# Optional[dict] (not ``dict | None``) keeps Python 3.9 import-time
# parsing happy — PEP 604 unions in annotations only became unconditional
# at module-eval time in 3.10.
_vector_capacity_status: Optional[dict] = None


def _refresh_vector_disabled_flag() -> None:
    """Re-run the HNSW capacity probe and update the module-level flag.

    Called from :func:`_get_client` whenever the client cache is rebuilt
    (first open or palace replacement). Cheap — pure sqlite + pickle
    read, no chromadb interaction. Never raises: a probe that crashes
    would defeat the point.
    """
    global _vector_disabled, _vector_disabled_reason, _vector_capacity_status
    if not _is_chroma_backend():
        _vector_disabled = False
        _vector_disabled_reason = ""
        _vector_capacity_status = None
        return
    try:
        info = hnsw_capacity_status(_config.palace_path, _config.collection_name)
    except Exception:
        logger.debug("HNSW capacity probe raised", exc_info=True)
        return
    _vector_capacity_status = info
    if info.get("diverged"):
        if not _vector_disabled:
            logger.warning(
                "HNSW capacity divergence detected (%s) — routing search to "
                "BM25-only sqlite fallback. Run `mempalace repair` to restore "
                "vector search.",
                info.get("message", "unknown"),
            )
        _vector_disabled = True
        _vector_disabled_reason = info.get("message", "")
    else:
        if _vector_disabled:
            logger.info(
                "HNSW capacity within tolerance (%s) — vector search re-enabled",
                info.get("message", ""),
            )
        _vector_disabled = False
        _vector_disabled_reason = ""
