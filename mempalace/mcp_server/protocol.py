# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


def _internal_tool_error(req_id, tool_name: str, exc: BaseException = None) -> dict:
    logger.exception(f"Tool error in {tool_name}")
    error: dict = {"code": -32000, "message": "Internal tool error"}
    if exc is not None:
        error["data"] = {
            "error_class": type(exc).__name__,
            "message": str(exc),
        }
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": error,
    }


def _mcp_read_only_refusal(req_id, tool_name: str):
    """Refuse state-changing tools when the server runs in read-only mode (#1877).

    Read-only is an operator-set server mode (``--read-only`` /
    ``MEMPALACE_MCP_READ_ONLY``), distinct from the dynamic peer-writer lock:
    it is an unconditional gate so a shared team server can expose recall
    without write access. Enforced at dispatch, not merely hidden from
    tools/list, so a client that calls a mutating tool by name is still refused.

    Gates on ``_READ_ONLY_REFUSED_TOOLS``, not ``_MUTATING_TOOLS``: a tool can
    write outside the palace database, which the peer-writer lease has no reason
    to arbitrate but read-only still has to refuse.
    """
    if not _READ_ONLY or tool_name not in _READ_ONLY_REFUSED_TOOLS:
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32003,
            "message": "Server is in read-only mode; this tool is disabled",
            "data": {"tool": tool_name},
        },
    }


def _mcp_diverged_index_refusal(req_id, tool_name: str):
    """Refuse vector writes while the HNSW segment is known to be diverged.

    The capacity probe (#1222) already routes *reads* around a diverged index:
    ``search`` and ``check_duplicate`` fall back to BM25-only sqlite. Writes had
    no such gate — they went straight to chromadb, where an upsert into that same
    index can never come back. Observed on a 3.7.0 palace whose flushed segment
    held 803 of 820 embeddings: three of five freshly generated vectors blocked
    forever (25+ minutes, then killed), the other two committed in 0.03 s, and
    the same vector reproduced the same verdict on every retry — so a write's
    fate depended on where its embedding landed in the damaged graph. After
    ``mempalace repair rebuild-index`` all five committed.

    Refusing is also the honest answer for the write that does not hang: chromadb
    acknowledges it into sqlite and the metadata segment, then leaves it out of
    the HNSW segment. That drawer is filed and reported as success while being
    invisible to vector search — 16 such drawers came out of a single checkpoint
    that returned "completed successfully in 2s".

    The probe behind this is pure sqlite + pickle, so the gate costs no chromadb
    interaction on the path it is protecting.
    """
    if tool_name not in _VECTOR_WRITE_TOOLS:
        return None

    _refresh_vector_disabled_flag()

    if not _vector_disabled:
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": _DIVERGED_INDEX_ERROR_CODE,
            "message": ("Palace vector index is diverged; refusing the write until it is rebuilt"),
            "data": {
                "tool": tool_name,
                "palace": _config.palace_path or "",
                "vector_disabled_reason": _vector_disabled_reason,
                "hint": (
                    "Stop the MemPalace MCP servers, run `mempalace repair rebuild-index`, "
                    "then mempalace_reconnect. Recall keeps working meanwhile through the "
                    "BM25 fallback; writes stay refused so they can neither hang inside "
                    "chromadb nor land outside the index."
                ),
            },
        },
    }


def _excluded_working_directory() -> str:
    """The working directory to keep out of the metadata search, resolved once.

    Read at import and never again. The value has to be identical for the
    startup baseline and every later reading, or the set of watched directories
    would shift under a server that never moved: a rename or a redeploy of the
    checkout changes what ``os.getcwd()`` answers, and a directory that was
    excluded at startup would quietly start counting. Resolving symlinks keeps
    the comparison honest against a path spelled differently on either side.
    """
    try:
        return os.path.realpath(os.getcwd())
    except OSError:
        # The working directory can be gone underneath a long-lived process.
        # Nothing to exclude then, and no reason to stop answering.
        return ""


_DIST_PATH_EXCLUDED_CWD = _excluded_working_directory()


def _dist_search_path() -> list[str]:
    """``sys.path`` entries this server will trust to answer "what is installed".

    The working directory is excluded. Under the documented launch
    ``python -m mempalace.mcp_server`` (website/guide/mcp-integration.md) the
    interpreter puts the MCP host's own working directory first on sys.path, so
    a plain ``importlib.metadata.version()`` would resolve against whatever
    project the user happens to have open. Any repository carrying a top-level
    ``mempalace.egg-info/`` would then dictate this server's write policy — and
    it would win again on every restart, so the gate's own "restart the server"
    remedy could not clear it. Distribution metadata is an installation fact,
    not a property of the directory the host was started in.

    This is narrower than what the import system itself would resolve, and
    deliberately so, but it introduces no asymmetry: the startup baseline and
    every later reading come from this same path, so a directory left out here
    is simply not watched and can never produce a mismatch on its own. Running
    from a source tree is the case that narrowing costs, and it is already
    outside what installed metadata can describe.
    """
    cwd = _DIST_PATH_EXCLUDED_CWD
    search: list[str] = []
    for entry in sys.path:
        if not entry:
            continue
        if "\x00" in entry:
            # No filesystem accepts this; the platforms disagree only about
            # where it is refused. POSIX raises in the realpath below and the
            # entry drops out there, while Windows resolves it and leaves every
            # later call to fail on it instead — os.stat and os.listdir reject
            # it in the argument conversion, which raises ValueError rather
            # than the OSError those callers hold, so a single junk entry would
            # take the whole reading down and switch the gate off. Dropped here
            # so both platforms go on to search the same list.
            continue
        try:
            resolved = os.path.realpath(entry)
        except (OSError, ValueError):
            continue
        if cwd and resolved == cwd:
            continue
        search.append(entry)
    return search


def _stat_fingerprint(path: str) -> tuple:
    try:
        stat_result = os.stat(path)
        return (path, stat_result.st_mtime_ns, stat_result.st_ino, stat_result.st_size)
    except OSError:
        # Missing is a state too: a dist-info that disappears must read as a
        # change, not as "same as last time".
        return (path, None, None, None)


def _watched_metadata_files(root: str) -> list[str]:
    """Metadata files under ``root`` belonging to the watched distributions.

    Installers name these directories after the normalized distribution name,
    and both watched names are already lowercase single words, so matching the
    prefix is enough here without pulling in full PEP 503 normalization.
    """
    try:
        entries = os.listdir(root)
    except OSError:
        return []

    found = []
    for dist in _STALE_LIBRARY_WATCHED_DISTS:
        prefix = f"{dist}-"
        for entry in entries:
            lowered = entry.lower()
            if lowered == f"{dist}.egg-info":
                found.append(os.path.join(root, entry, "PKG-INFO"))
                continue
            if not lowered.startswith(prefix):
                continue
            # `name-version.dist-info`, `name-version.egg-info` and
            # `name-version-pyX.Y.egg-info` are all layouts importlib.metadata
            # resolves, so all three have to be watched or an upgrade in the
            # unwatched one moves nothing this fingerprint can see. A
            # normalized PEP 440 version always starts with a digit; requiring
            # one is defence in depth rather than load-bearing, since PEP 427
            # escaping already spells a sibling like `mempalace-remote` as
            # `mempalace_remote-...` and that fails the prefix outright.
            remainder = lowered[len(prefix) :]
            if not remainder[:1].isdigit():
                continue
            if lowered.endswith(".dist-info"):
                found.append(os.path.join(root, entry, "METADATA"))
            elif lowered.endswith(".egg-info"):
                found.append(os.path.join(root, entry, "PKG-INFO"))
    return sorted(found)


def _dist_search_signature(search_path: list[str]) -> tuple:
    """Stat-only fingerprint used to decide whether the metadata must be reread.

    Two things are watched, because an upgrade can show up as either. A normal
    ``pip``/``uv`` upgrade removes one ``*.dist-info`` directory and creates
    another, which moves the containing directory's mtime; but a metadata file
    rewritten in place leaves that mtime untouched, so the metadata files
    themselves are fingerprinted too. Watching only the directories left the
    cache blind to the in-place case, and a cache that cannot see a change is
    the same silent failure this gate exists to prevent.
    """
    signature = []
    for entry in search_path:
        signature.append(_stat_fingerprint(entry))
        for metadata_file in _watched_metadata_files(entry):
            signature.append(_stat_fingerprint(metadata_file))
    return tuple(signature)


def _unlistable_search_entries(search_path: list[str]) -> list[str]:
    """Search roots that are there but refuse to be listed.

    ``importlib.metadata`` scans a root with ``with suppress(Exception):
    os.listdir(...)`` and falls through to an empty listing, so a permission
    error or a file-descriptor exhaustion on site-packages arrives as "there
    are no distributions here" — the same answer a genuine uninstall gives.
    Read literally, that would make this gate refuse every write on a wholly
    healthy install, and ``EMFILE`` is an ordinary peak-load condition for a
    threaded server rather than a hypothetical one. Probing the roots directly
    is the only way to tell the two apart, because the fault is swallowed
    before any of it reaches us.
    """
    blocked = []
    for entry in search_path:
        try:
            os.listdir(entry)
        except (FileNotFoundError, NotADirectoryError):
            # A sys.path entry that does not exist, or a zip/file rather than a
            # directory, is ordinary; importlib reads those its own way.
            continue
        except OSError:
            blocked.append(entry)
    return blocked


def _log_stale_library_errors(errors: dict[str, str]) -> None:
    """Report metadata faults when they appear or change, not on every call.

    A failed reading is deliberately never memoized, so this path runs again on
    every mutating call for as long as the fault lasts. Logging it each time
    would turn one persistent permission problem into a flood into the MCP
    host's stderr, and file-descriptor exhaustion reaches this same branch, so
    the flood would peak exactly when the server can least afford it.
    """
    global _stale_library_reported_errors

    with _stale_library_log_lock:
        if errors == _stale_library_reported_errors:
            return
        _stale_library_reported_errors = dict(errors)

    for dist, reason in sorted(errors.items()):
        logger.warning("stale-library gate inactive for %s: %s", dist, reason)


def _log_stale_library_drift(drift: list, described: str) -> None:
    """Announce a refusal when the drift appears or changes, not per call.

    A client that retries a rejected write — an agent will — would otherwise get
    one line per attempt for a condition that only clears on restart, which is
    the same flood ``_log_stale_library_errors`` exists to avoid. The neighbours
    (``_mcp_peer_writer_refusal``, ``_mcp_sqlite_integrity_refusal``) log nothing
    at all on refusal; this logs once, because unlike theirs this condition has
    no other place an operator would notice it.
    """
    global _stale_library_reported_drift

    with _stale_library_log_lock:
        if drift == _stale_library_reported_drift:
            return
        _stale_library_reported_drift = [dict(entry) for entry in drift]

    logger.warning(
        "Refusing writes: this server is running code that is no longer installed (%s)", described
    )


def _read_installed_dist_versions(search_path: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Read the watched distributions' versions from ``search_path``.

    Returns ``(versions, errors)``. Three states, not two, and the caller has to
    keep them apart: a distribution that is simply not installed is absent from
    both maps, which reads as uninstalled and is the strongest form of drift
    there is; one whose metadata could not be read is recorded in ``errors`` and
    left uncompared. Collapsing the second into the first would refuse every
    write over a filesystem fault, on an install where nothing is stale.

    ``importlib.metadata`` gives us no help telling them apart: it suppresses
    the failure at both levels it reads, the file (``read_text``) and the
    directory (``FastPath.children``), so a fault arrives as an empty version or
    as no distribution at all. Both are recovered here rather than trusted.
    """
    from importlib.metadata import DistributionFinder, MetadataPathFinder

    versions: dict[str, str] = {}
    errors: dict[str, str] = {}
    finder = MetadataPathFinder()
    # importlib.metadata memoizes each search root's directory listing against
    # that root's mtime (``FastPath.search`` -> ``self.lookup(self.mtime)``), and
    # that mtime is read in whole-ish seconds rather than the nanoseconds the
    # fingerprint above compares. A removal and a creation that both land inside
    # one timestamp tick therefore leave the memo answering from the listing
    # taken before them, naming the dist-info the upgrade has already deleted.
    # Its version then reads as empty, which this function records as unreadable
    # and the caller leaves uncompared — the gate switching itself off for that
    # distribution, permanently, since nothing here writes to the root to move
    # its mtime again. That is the exact upgrade this gate exists to catch, so
    # the memo is dropped instead of trusted. It costs one relisting per real
    # change: this function is only reached when the fingerprint has already
    # moved.
    #
    # Called on the instance rather than the class: before 3.11 it is not a
    # classmethod, so ``MetadataPathFinder.invalidate_caches()`` is a TypeError
    # on the 3.9 in CI.
    try:
        finder.invalidate_caches()
    except Exception:
        # Best effort by construction. Dropping a cache sharpens the reading; it
        # is never what the reading depends on. A finder that cannot do it is
        # still asked for the versions below, and still allowed to fail there,
        # where the three states are told apart.
        logger.debug("stale-library metadata cache could not be invalidated", exc_info=True)
    # Probed only if something turns up missing: it costs a listing of every
    # search root, and on a healthy installation nothing reaches that branch.
    blocked: "list[str] | None" = None

    for dist in _STALE_LIBRARY_WATCHED_DISTS:
        try:
            context = DistributionFinder.Context(name=dist, path=list(search_path))
            found = next(iter(finder.find_distributions(context)), None)
            raw = "" if found is None else str(found.version or "")
        except Exception:
            # Fail open — an unreadable metadata directory must not take the
            # server down — but never silently: with the version unknown this
            # gate cannot protect that distribution at all, and an operator has
            # to be able to see that from mempalace_status. The reason is kept
            # generic because it is quoted back to the client, and an OSError
            # carries the full path it failed on; the detail goes to the log.
            errors[dist] = "installed metadata could not be read"
            logger.debug("stale-library metadata read failed for %s", dist, exc_info=True)
            continue

        if found is None:
            if blocked is None:
                blocked = _unlistable_search_entries(search_path)
            if blocked:
                # Nothing was found, but a search root would not open, and an
                # unopenable root looks exactly like an empty one from here.
                # Reporting this as uninstalled is what would refuse writes on
                # a healthy install, so it is left uncompared instead.
                errors[dist] = "distribution search path unreadable"
                continue
            # Genuinely absent from a path we could read end to end.
            continue
        if not raw:
            # Present, but its version could not be read. importlib.metadata
            # swallows PermissionError, FileNotFoundError, IsADirectoryError,
            # NotADirectoryError and KeyError inside read_text, so a metadata
            # file that is unreadable, missing or truncated arrives here as an
            # empty version rather than as an exception.
            errors[dist] = "version unreadable in installed metadata"
            continue
        if not _VERSION_TEXT_RE.match(raw):
            errors[dist] = "malformed version metadata"
            continue
        versions[dist] = raw

    return versions, errors


def _installed_dist_state() -> tuple[dict[str, str], dict[str, str]]:
    """``(versions, errors)`` for the watched distributions, read together.

    One reader call, one cache generation. Reading the versions and the errors
    separately let a caller pair a version map from one generation with an error
    map from another, and both mixtures are wrong: one invents drift on an
    installation where nothing changed, the other hides real drift behind an
    error recorded a moment later.

    A reading that produced errors is never memoized. The fingerprint is built
    from stat data, and a permission change moves none of it, so caching a
    failed reading would keep the gate answering from that failure long after
    the cause was repaired. The cost of that is a full reread per call for as
    long as a fault lasts, which is why the logging of it is deduplicated.
    """
    search_path = _dist_search_path()
    signature = _dist_search_signature(search_path)

    with _stale_library_cache_lock:
        if _stale_library_cache["signature"] == signature:
            return dict(_stale_library_cache["versions"]), dict(_stale_library_cache["errors"])

    versions, errors = _read_installed_dist_versions(search_path)
    _log_stale_library_errors(errors)

    with _stale_library_cache_lock:
        if errors:
            # Left empty rather than filled with this reading: a slot keyed on a
            # signature of None is never matched again, so storing the maps here
            # would be a write nothing can read.
            _stale_library_cache.update(signature=None, versions={}, errors={})
        else:
            _stale_library_cache.update(signature=signature, versions=versions, errors=errors)
    return dict(versions), dict(errors)


# Baseline: what was installed at the moment this module was imported, which is
# the moment the code being served was loaded. Every watched distribution is
# already in sys.modules by now — mempalace by definition, chromadb through the
# unconditional `from chromadb.errors import NotFoundError as _ChromaNotFoundError`
# above — so this snapshot describes the code actually running.
#
# Both sides of the comparison are therefore read the same way, from the same
# metadata, and that is what keeps the gate honest. Comparing a live
# `module.__version__` against installed metadata would instead drift apart on
# its own: an editable checkout (`uv sync --extra dev`, the setup CONTRIBUTING
# documents) moves version.py on every `git pull` while the recorded metadata
# stays put, and chromadb hardcodes its own `__version__` literal, which a
# repackaged build (conda, distro, `1.5.7+corp1`) can spell differently from
# its metadata. Either would refuse every write on an installation where
# nothing whatsoever is stale.
#
# Preserved across importlib.reload via globals(), like _logging_configured
# above: a reload re-executes this module body but leaves sys.modules alone, so
# the chromadb this server is serving is still the one loaded at startup.
# Recomputing the baseline there would adopt the newly-installed version as
# "what we are serving" and disarm the gate for the library the reload did not
# actually replace.
def _initial_dist_state() -> tuple[dict[str, str], dict[str, str]]:
    """The baseline reading, which must never stop this module from importing.

    Every other call into the gate happens inside a request and fails open
    there. This one happens at import: an exception escaping it would abort the
    import and the server would not start at all, which is a far worse outcome
    than a gate that stays switched off for the life of the process.
    """
    try:
        return _installed_dist_state()
    except Exception:
        logger.warning(
            "stale-library baseline could not be read; the gate is inactive", exc_info=True
        )
        return {}, {}


_STARTUP_DIST_STATE: tuple = globals().get("_STARTUP_DIST_STATE") or _initial_dist_state()
_STARTUP_DIST_VERSIONS: dict[str, str] = _STARTUP_DIST_STATE[0]
# Watched distributions whose metadata could not be read at import have no
# baseline and can never be compared. That is the gate silently off for them,
# so it is kept and reported rather than discarded.
_STARTUP_DIST_ERRORS: dict[str, str] = _STARTUP_DIST_STATE[1]


def _stale_library_report() -> tuple[list[dict], dict[str, str]]:
    """``(drift, unreadable)``: what the gate found, from one metadata reading.

    ``drift`` lists the watched distributions whose installed version moved
    since startup; ``unreadable`` those whose metadata could not be read and
    which are therefore not being compared at all. Both come out of a single
    ``_installed_dist_state()`` call because they are two halves of one answer:
    reading them separately would let a refusal be decided against one cache
    generation and explained by another, which is how a status report ends up
    naming a package as both drifted and uncompared.

    Nothing is latched: rolling an install back to the version this process
    started with leaves nothing stale, and a metadata read that lands
    mid-upgrade (files half replaced) corrects itself on the next call instead
    of wedging the server.

    Never raises. This runs in request preflight, ahead of the dispatcher's own
    error handling, and an exception here would leave the client waiting on a
    reply that is never written.
    """
    try:
        installed, unreadable = _installed_dist_state()
        drift = []
        for dist, startup_version in sorted(_STARTUP_DIST_VERSIONS.items()):
            if dist in unreadable:
                # The version could not be read at all, which is not evidence
                # that the distribution went away. Refusing writes on a
                # transient metadata failure would turn a filesystem hiccup
                # into an outage, so this stays open and reports the gap
                # through mempalace_status instead.
                continue
            # Absent now, present at startup, is the strongest form of this:
            # the code being served is not merely a different version, it is a
            # version that is no longer installed at all. `pipx install
            # --force` and `uv tool upgrade` rebuild the environment rather
            # than rewriting metadata in place, so this is the shape the common
            # upgrade paths actually take. A distribution that was already
            # absent at startup never enters this loop and stays uncompared.
            current = installed.get(dist, _UNINSTALLED)
            if current != startup_version:
                drift.append({"package": dist, "serving": startup_version, "installed": current})
        return drift, unreadable
    except Exception:
        # Fail open rather than refuse every write on a bug in the gate itself.
        logger.warning("stale-library check failed; allowing the call", exc_info=True)
        return [], {}


def _stale_library_payload() -> dict:
    """``mempalace_status`` view of the gate, so the state is diagnosable."""
    drift, errors = _stale_library_report()
    payload = {
        "stale": bool(drift),
        "serving": dict(_STARTUP_DIST_VERSIONS),
        "packages": drift,
    }
    # Everything the gate is NOT covering, in one place. A distribution with no
    # baseline is the quietest case of all: it was never resolvable when this
    # process started, so nothing about it is compared and nothing about it
    # would otherwise appear here — leaving "stale: false" to be read as
    # "checked and fine" when it means "not checked at all".
    inactive = dict(errors)
    for dist in _STALE_LIBRARY_WATCHED_DISTS:
        if dist not in _STARTUP_DIST_VERSIONS:
            inactive.setdefault(
                dist,
                _STARTUP_DIST_ERRORS.get(
                    dist, "no baseline: not resolved when this server started"
                ),
            )
    if inactive:
        payload["unreadable"] = inactive
    if drift and _truthy_env(_MCP_ALLOW_STALE_LIBRARY_ENV):
        payload["gate_disabled_by"] = _MCP_ALLOW_STALE_LIBRARY_ENV
    return payload


def _mcp_stale_library_refusal(req_id, tool_name: str):
    """Refuse mutating tools once the served code is no longer what is installed (#899).

    Reads stay available on purpose: the palace itself is intact at this point,
    and a user who has just been told to restart still needs ``status`` and
    ``search`` to see what state their memory is in. Only the writes are
    stopped, because those are what a superseded library would persist in a
    format the newly-installed one may not read back.

    Restarting the server is the only remedy — ``mempalace_reconnect`` reopens
    the database but cannot reload Python modules — so the hint says so.
    """
    if tool_name not in _MUTATING_TOOLS:
        return None

    if _truthy_env(_MCP_ALLOW_STALE_LIBRARY_ENV):
        return None

    drift, _unreadable = _stale_library_report()
    if not drift:
        return None

    described = ", ".join(
        f"{entry['package']} {entry['serving']} -> {entry['installed']}" for entry in drift
    )
    _log_stale_library_drift(drift, described)

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": _STALE_LIBRARY_ERROR_CODE,
            # The remedy rides in the message itself, not only in data.hint:
            # several MCP clients surface only the top-level message of an
            # error, so a hint-only remedy never reaches the model that has
            # to act on it.
            "message": (
                "Server is running a library version that is no longer installed "
                f"({described}); refusing writes until it is restarted. Restart the "
                "MCP server (or the host application that spawned it) to pick up the "
                "installed version — mempalace_reconnect cannot clear this"
            ),
            "data": {
                "tool": tool_name,
                "packages": drift,
                "action_required": "restart_mcp_server",
                # Named as a field, not only in the prose below, so a client can
                # find it without parsing English. The peer-writer gate spells
                # the same idea the same way.
                "override_env": _MCP_ALLOW_STALE_LIBRARY_ENV,
                "hint": (
                    "The package was upgraded after this server started, so it is "
                    "still serving the previous code. Restart the MCP server (or the "
                    "host application that spawned it) to pick up the installed "
                    "version. mempalace_reconnect reopens the palace but cannot "
                    "reload Python modules, so it will not clear this. An operator "
                    "who wants writes to continue across upgrades can set "
                    f"{_MCP_ALLOW_STALE_LIBRARY_ENV}=1 in the server's environment "
                    "before it starts."
                ),
            },
        },
    }


def _mcp_tool_preflight_refusal(req_id, tool_name: str, *, check_writer: bool = True):
    """Run MCP request preflight gates outside handle_request complexity."""

    read_only_error = _mcp_read_only_refusal(req_id, tool_name)
    if read_only_error is not None:
        return read_only_error

    # Corruption outranks staleness: a malformed palace is the more severe and
    # more actionable condition, and reporting the stale library first would
    # replace the -32002 message that tells the user to repair it.
    sqlite_integrity_error = _mcp_sqlite_integrity_refusal(req_id, tool_name)
    if sqlite_integrity_error is not None:
        return sqlite_integrity_error

    # Staleness outranks a diverged index: the diverged gate's remedy is to run
    # `mempalace repair rebuild-index`, which would execute the INSTALLED code
    # against a palace this server is still writing with the superseded one.
    # Restarting has to come first, and it also un-gates the index check for
    # free — the probe re-runs per call. Ordering this way also skips the
    # diverged gate's _refresh_vector_disabled_flag() read on a call that is
    # refused either way.
    stale_library_error = _mcp_stale_library_refusal(req_id, tool_name)
    if stale_library_error is not None:
        return stale_library_error

    diverged_index_error = _mcp_diverged_index_refusal(req_id, tool_name)
    if diverged_index_error is not None:
        return diverged_index_error

    return _mcp_peer_writer_refusal(req_id, tool_name) if check_writer else None


def _decorate_mcp_tool_result(tool_name: str, result):
    """Attach MCP transport-only diagnostics outside handle_request complexity."""

    if tool_name == "mempalace_status" and isinstance(result, dict):
        from ..update_awareness import cached_update_status, schedule_update_check

        result.setdefault("sqlite_integrity", _sqlite_integrity_payload())
        result.setdefault("library_versions", _stale_library_payload())
        result.setdefault("updates", {"server": cached_update_status()})
        schedule_update_check()

    return result


def handle_request(request):
    global _last_request_time
    if not isinstance(request, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    _last_request_time = time.monotonic()
    method = request.get("method") or ""
    params = request.get("params") or {}
    req_id = request.get("id")

    if method == "initialize":
        client_version = params.get("protocolVersion", SUPPORTED_PROTOCOL_VERSIONS[-1])
        negotiated = (
            client_version
            if client_version in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mempalace", "version": __version__},
            },
        }
    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    elif method.startswith("notifications/"):
        # Notifications (no id) never get a response per JSON-RPC spec
        return None
    elif method == "tools/list":
        # In read-only mode, hide the refused tools so clients don't advertise
        # write capabilities they can't use (dispatch also refuses them, #1877).
        # Same set on both sides, or a tool would be listed and then rejected.
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {"name": n, "description": t["description"], "inputSchema": t["input_schema"]}
                    for n, t in TOOLS.items()
                    if not (_READ_ONLY and n in _READ_ONLY_REFUSED_TOOLS)
                ]
            },
        }
    elif method == "tools/call":
        if not isinstance(params, dict) or "name" not in params:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": "Invalid params: 'name' is required for tools/call",
                },
            }
        tool_name = params.get("name")
        tool_args = params.get("arguments") or {}
        if tool_name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }
        # Whitelist arguments to declared schema properties only.
        # Prevents callers from spoofing internal params like added_by/source_file.
        # Skip filtering if handler explicitly accepts **kwargs (pass-through).
        # Default to filtering on inspect failure (safe fallback).
        import inspect

        schema_props = TOOLS[tool_name]["input_schema"].get("properties", {})
        try:
            handler = TOOLS[tool_name]["handler"]
            sig = inspect.signature(handler)
            accepts_var_keyword = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
        except (ValueError, TypeError):
            accepts_var_keyword = False
        if not accepts_var_keyword:
            # An unknown kwarg here is almost always a wrong parameter *name*
            # (e.g. text= instead of content=). Silently dropping it makes the
            # cause surface only indirectly as a later "Missing required 'X'",
            # so name it explicitly — symmetric with the missing-required path
            # below. wait_for_previous is an internal transport kwarg in no
            # tool schema; it is popped before dispatch further down, so it
            # must not be reported as unknown here.
            unknown = [k for k in tool_args if k not in schema_props and k != "wait_for_previous"]
            if unknown:
                quoted = ", ".join(f"'{k}'" for k in unknown)
                word = "parameter" if len(unknown) == 1 else "parameters"
                logger.debug("Tool %s: unknown %s %s", tool_name, word, quoted)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32602,
                        "message": f"Unknown {word} {quoted} for tool {tool_name}",
                    },
                }
            tool_args = {k: v for k, v in tool_args.items() if k in schema_props}
        # Coerce argument types based on input_schema.
        # MCP JSON transport may deliver integers as floats or strings;
        # ChromaDB and Python slicing require native int.
        for key, value in list(tool_args.items()):
            prop_schema = schema_props.get(key, {})
            declared_type = prop_schema.get("type")
            try:
                if declared_type == "integer" and not isinstance(value, int):
                    tool_args[key] = int(value)
                elif declared_type == "number" and not isinstance(value, (int, float)):
                    tool_args[key] = float(value)
            except (ValueError, TypeError):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": f"Invalid value for parameter '{key}'"},
                }
        tool_args.pop("wait_for_previous", None)
        preflight_error = _mcp_tool_preflight_refusal(req_id, tool_name)
        if preflight_error is not None:
            return preflight_error

        # 'content' is an accepted alias for diary_write's 'entry' (callers often
        # reuse add_drawer's 'content' name). Map it in here, before dispatch, so a
        # content-only call still satisfies the required 'entry' param while the
        # signature-based missing-parameter diagnostic (-32602) keeps working.
        # 'entry' wins if both are supplied.
        if tool_name == "mempalace_diary_write" and "content" in tool_args:
            content_val = tool_args.pop("content")
            # Only fill from the alias when the caller did not supply 'entry' at
            # all (or passed it as null). An explicit entry — even "" — wins.
            if "entry" not in tool_args or tool_args["entry"] is None:
                tool_args["entry"] = content_val
        try:
            with _write_stall_watch(tool_name):
                result = _decorate_mcp_tool_result(
                    tool_name, TOOLS[tool_name]["handler"](**tool_args)
                )

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}
                    ]
                },
            }
        except TypeError as e:
            # Qualname match prevents leaking internal helper/param names raised
            # inside the handler body — see test_handler_internal_signature_shape_stays_generic.
            msg = str(e)
            handler = TOOLS[tool_name]["handler"]
            handler_qn = getattr(handler, "__qualname__", None) or getattr(handler, "__name__", "")
            # Qualname can include "<locals>" for nested defs and "<lambda>"
            # for lambdas — accept Python's TypeError emit verbatim.
            m_missing = re.match(
                r"^([\w\.<>]+)\(\) missing \d+ required "
                r"(?:positional |keyword-only )?arguments?: (.+)$",
                msg,
            )
            if m_missing and m_missing.group(1) == handler_qn:
                names = re.findall(r"'(\w+)'", m_missing.group(2))
                if names:
                    quoted = ", ".join(f"'{n}'" for n in names)
                    word = "parameter" if len(names) == 1 else "parameters"
                    logger.debug("Tool %s: missing required %s %s", tool_name, word, quoted)
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32602,
                            "message": f"Missing required {word} {quoted} for tool {tool_name}",
                        },
                    }
            return _internal_tool_error(req_id, tool_name, e)
        except Exception as exc:
            return _internal_tool_error(req_id, tool_name, exc)

    # Notifications (missing id) must never get a response
    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def _restore_stdout():
    """Restore real stdout for MCP JSON-RPC output (see issue #225)."""
    global _REAL_STDOUT, _REAL_STDOUT_FD
    if _REAL_STDOUT_FD is not None:
        try:
            os.dup2(_REAL_STDOUT_FD, 1)
            os.close(_REAL_STDOUT_FD)
        except OSError:
            pass
        _REAL_STDOUT_FD = None
    sys.stdout = _REAL_STDOUT


_WARMUP_TRUTHY = {"1", "true", "yes", "on"}
_WARMUP_FALSY = {"", "0", "false", "no", "off"}
# Sentinel text for the warmup query. Distinctive so it cannot semantically
# match real drawer content (e.g. a palace containing notes about "warmup"
# routines) and is greppable in chromadb debug logs if the team ever adds
# request instrumentation. Single non-empty string is enough to trigger
# ChromaDB's ONNXMiniLM_L6_V2.__call__ → _download_model_if_not_exists +
# InferenceSession.
_WARMUP_PROBE_TEXT = "__mempalace_warmup_probe__"


def _describe_device_safe() -> str:
    """Return ``embedding.describe_device()`` value or ``"unknown"`` on failure.

    Used only inside warmup-failure log lines; the import is deferred so
    that an embedding-stack import error cannot itself crash the warmup
    diagnostic path.
    """
    try:
        from ..embedding import describe_device

        return describe_device()
    except Exception:  # fail-soft: see docstring — log-message helper must not crash
        return "unknown"


def _maybe_eager_warmup_embedder() -> None:
    """Pre-load embedder + HNSW segment at startup when ``MEMPALACE_EAGER_WARMUP`` is truthy.

    The first MCP tool call that touches chromadb (``diary_write``,
    ``add_drawer``, ``search``) otherwise pays two compounding cold-load
    costs that together can exceed the MCP client timeout and surface as
    ``-32000`` "Internal tool error" with no recoverable trace on the
    agent side (#1495):

    1. ONNX/CoreML embedder init in :func:`mempalace.embedding.get_embedding_function`
       (5–30s on first inference; ChromaDB's ``ONNXMiniLM_L6_V2.__call__``
       triggers ``_download_model_if_not_exists`` + ``InferenceSession``).
    2. HNSW segment cold-load (reading ``data_level0.bin`` into RAM on
       first collection operation; seconds on palaces of 50k+ drawers).

    Warming via :func:`_get_collection`'s collection-then-query path
    covers BOTH in a single startup-phase call — mirroring the reporter's
    proposal in #1495 — so users with large existing palaces see the
    same benefit as users on the embedder-only cost path.

    Truthy parsing accepts ``1/true/yes/on`` (case-insensitive); falsy
    set ``0/false/no/off`` and empty/whitespace are silently off; any
    other value logs a warning and stays off so typos like ``tru`` do
    not silently disable the feature.

    Fresh-install guard (pre-check, NOT a catch): ``_get_collection``'s
    retry layer absorbs ``_ChromaNotFoundError`` and returns ``None`` while
    also materialising ``chroma.sqlite3`` on disk via the chromadb client
    constructor. To preserve the documented "no palace yet → nothing to
    warm" contract WITHOUT writing palace scaffolding before
    ``mempalace init`` (which would violate CLAUDE.md "Incremental only"),
    we test for ``chroma.sqlite3`` ourselves before touching the chromadb
    client. Operators who set ``MEMPALACE_EAGER_WARMUP=1`` in their MCP
    config and launch the server before running ``mempalace init`` get a
    single INFO line and no on-disk side effect.

    Fail-soft beyond the fresh-install pre-check:

    * **Backend open failure** (palace path misconfigured, file locked,
      corrupted HNSW that ``quarantine_stale_hnsw`` cannot recover) →
      log exception with device + palace context and return. The next
      embedding-requiring call sees the same fail mode it would have
      without warmup.
    * **`_get_collection` retried and returned None** → palace exists
      but chromadb cannot open the collection (rare; usually a stale
      sqlite + segment-files mismatch surfaced by `_get_client` rebuild).
      A warning suffices because the retry layer already wrote two
      tracebacks with the underlying chromadb error class.
    * **Query failure** (network failure during ONNX model download,
      provider init crash, runtime decoder error) → log exception with
      device + palace context and return. Same fail-mode preservation.

    Note: on an existing palace with an empty collection (created via
    ``mempalace init`` but never written to), ``col.query`` succeeds but
    returns ``{'ids': [[]]}`` without reading any HNSW segment — the
    embedder warms but there is no HNSW segment to load. The success log
    still says ``embedder + HNSW ready`` because the no-HNSW-segment case
    has zero cold-load cost; nothing was skipped that the first real tool
    call would have paid.
    """
    raw = os.environ.get("MEMPALACE_EAGER_WARMUP", "").strip().lower()
    if raw in _WARMUP_FALSY:
        return
    if raw not in _WARMUP_TRUTHY:
        logger.warning(
            "MEMPALACE_EAGER_WARMUP=%r is not recognized (use one of %s); warmup disabled",
            raw,
            sorted(_WARMUP_TRUTHY | (_WARMUP_FALSY - {""})),
        )
        return
    palace_path = _config.palace_path
    try:
        backend_name = _selected_backend_name()
    except Exception as exc:  # fail-soft per docstring
        logger.warning(
            "MEMPALACE_EAGER_WARMUP=%s: backend resolution failed for %s (%s)",
            raw,
            palace_path,
            exc,
        )
        return
    if not _backend_db_exists():
        # Pre-check (NOT a try/except on _ChromaNotFoundError, which never
        # propagates out of _get_collection — see docstring). No palace
        # file means nothing to warm AND avoids the chromadb-client
        # side effect of materialising the palace dir.
        logger.info(
            "MEMPALACE_EAGER_WARMUP=%s: no palace at %s — nothing to warm",
            raw,
            palace_path,
        )
        return
    # Cache device once: _describe_device_safe re-imports embedding stack
    # each call, which is wasteful inside a function that already paid
    # that cost via the warmup query below.
    device = _describe_device_safe()
    try:
        col = _get_collection(create=False)
    except Exception as exc:  # fail-soft per docstring — broad on purpose
        logger.exception(
            "MEMPALACE_EAGER_WARMUP=%s: collection open failed (palace=%s, device=%s, error=%s)",
            raw,
            palace_path,
            device,
            type(exc).__name__,
        )
        return
    if col is None:
        logger.warning(
            "MEMPALACE_EAGER_WARMUP=%s: _get_collection returned None for palace=%s — see prior log lines",
            raw,
            palace_path,
        )
        return
    try:
        col.query(query_texts=[_WARMUP_PROBE_TEXT], n_results=1)
    except Exception as exc:  # fail-soft per docstring — broad on purpose
        logger.exception(
            "MEMPALACE_EAGER_WARMUP=%s: warmup query failed (palace=%s, device=%s, error=%s)",
            raw,
            palace_path,
            device,
            type(exc).__name__,
        )
    else:
        warmed = "embedder + HNSW ready" if backend_name == "chroma" else "embedder + backend ready"
        logger.info(
            "MEMPALACE_EAGER_WARMUP=%s: %s (palace=%s, device=%s)",
            raw,
            warmed,
            palace_path,
            device,
        )


_WRITE_STALL_WARN_ENV = "MEMPALACE_MCP_WRITE_STALL_WARN_SECS"
_WRITE_STALL_WARN_DEFAULT = 60.0
_WRITE_STALL_EXIT_ENV = "MEMPALACE_MCP_WRITE_STALL_EXIT_SECS"
_WRITE_STALL_EXIT_DEFAULT = 0.0
# EX_TEMPFAIL: the palace is fine, this process is not. A client that restarts
# the server gets a working one; a zero exit would read as an orderly shutdown.
_WRITE_STALL_EXIT_CODE = 75

_write_stall_lock = threading.Lock()
# Optional[dict]: {"tool": str, "since": float(monotonic), "warned": bool}
_write_stall_inflight: Optional[dict] = None


def _write_stall_secs(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name, "")
    if not raw.strip():
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("%s=%r is not a number; using %.0fs", env_name, raw, default)
        return default


def _write_stall_action(elapsed: float, warn_secs: float, exit_secs: float, warned: bool):
    """Decide what an in-flight vector write has earned: ``None``/warn/exit.

    Pure so the thresholds can be tested without a stalled write; ``exit`` is
    checked first so a single tick can escalate straight past an unsent warning.
    """
    if exit_secs > 0 and elapsed >= exit_secs:
        return "exit"
    if warn_secs > 0 and elapsed >= warn_secs and not warned:
        return "warn"
    return None


@contextlib.contextmanager
def _write_stall_watch(tool_name: str):
    """Register a vector write as in flight for the stall watchdog."""
    global _write_stall_inflight

    if tool_name not in _VECTOR_WRITE_TOOLS:
        yield
        return

    with _write_stall_lock:
        _write_stall_inflight = {
            "tool": tool_name,
            "since": time.monotonic(),
            "warned": False,
        }
    try:
        yield
    finally:
        with _write_stall_lock:
            _write_stall_inflight = None


def _start_write_stall_watchdog() -> None:
    """Start a daemon thread that reports a vector write that stopped returning.

    A chromadb write has no timeout of its own. When one blocks, this process
    holds the dispatch lock, the palace mine lock and the writer lease, so it
    answers nothing else and every peer session drops to read-only — and no log
    on the server side says why. The only trace of a 25-minute outage was the
    client's own "tool still running" ticks; the handshake stayed healthy, and
    ``mempalace_status`` could not answer because the dispatch lock was held by
    the stuck call. So the report has to come from a thread that is not waiting
    on that lock, and it has to reach stderr, where the MCP host records it.

    ``MEMPALACE_MCP_WRITE_STALL_WARN_SECS`` (default 60, 0 disables) sets when to
    warn. ``MEMPALACE_MCP_WRITE_STALL_EXIT_SECS`` (default 0 = never) lets an
    operator turn the wedge into a restartable failure: a server stuck inside
    chromadb will not recover, and exiting is what releases the locks its peers
    are queued behind.
    """
    warn_secs = _write_stall_secs(_WRITE_STALL_WARN_ENV, _WRITE_STALL_WARN_DEFAULT)
    exit_secs = _write_stall_secs(_WRITE_STALL_EXIT_ENV, _WRITE_STALL_EXIT_DEFAULT)
    if warn_secs <= 0 and exit_secs <= 0:
        return

    thresholds = [t for t in (warn_secs, exit_secs) if t > 0]
    interval = max(1.0, min(15.0, min(thresholds) / 4))

    def _watchdog() -> None:
        while True:
            time.sleep(interval)
            with _write_stall_lock:
                inflight = _write_stall_inflight
                if inflight is None:
                    continue
                elapsed = time.monotonic() - inflight["since"]
                action = _write_stall_action(elapsed, warn_secs, exit_secs, inflight["warned"])
                tool = inflight["tool"]
                if action == "warn":
                    inflight["warned"] = True
            if action == "warn":
                logger.warning(
                    "%s has been inside the palace write path for %.0fs and has not "
                    "returned. chromadb writes have no timeout: this server now answers "
                    "nothing else and holds the writer lease, so peer sessions are "
                    "read-only. Check `mempalace repair --dry-run` for HNSW divergence; "
                    "restarting this MCP server releases the locks.",
                    tool,
                    elapsed,
                )
            elif action == "exit":
                logger.error(
                    "%s stalled in the palace write path for %.0fs (limit %s=%.0fs); "
                    "exiting so the palace locks are released and the client can "
                    "reconnect. The stalled write is lost — rebuild the index before "
                    "retrying it.",
                    tool,
                    elapsed,
                    _WRITE_STALL_EXIT_ENV,
                    exit_secs,
                )
                os._exit(_WRITE_STALL_EXIT_CODE)

    t = threading.Thread(target=_watchdog, name="mcp-write-stall-watchdog", daemon=True)
    t.start()


def _start_idle_exit_watchdog() -> None:
    """Start a daemon thread that exits the process after an idle period.

    When no request has been handled for ``MEMPALACE_MCP_IDLE_HOURS``
    (default 8 h), the thread terminates the process so that stale MCP
    servers from ended Claude Code sessions do not accumulate ChromaDB /
    HNSW file handles on Windows (#1552).

    Set ``MEMPALACE_MCP_IDLE_HOURS=0`` to disable the watchdog.
    """
    timeout = _mcp_idle_timeout_secs()
    if timeout <= 0:
        return
    check_interval = min(60.0, timeout / 4)

    def _watchdog() -> None:
        while True:
            time.sleep(check_interval)
            idle = time.monotonic() - _last_request_time
            if idle >= timeout:
                logger.info(
                    "MCP server idle for %.1f h (limit %.1f h); exiting to release file handles.",
                    idle / 3600,
                    timeout / 3600,
                )
                os._exit(0)

    t = threading.Thread(target=_watchdog, name="mcp-idle-watchdog", daemon=True)
    t.start()


def _json_rpc_parse_error(req_id=None):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32700, "message": "Parse error"},
    }
