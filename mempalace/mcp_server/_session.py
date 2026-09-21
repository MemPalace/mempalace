# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


def _get_client():
    """Return a ChromaDB PersistentClient, reconnecting if the database changed on disk.

    Detects palace rebuilds (repair/nuke/purge) by checking the inode of
    chroma.sqlite3.  A full rebuild replaces the file, changing the inode.
    Also detects external writes (scripts, CLI) via mtime changes — the
    inode check alone misses in-place modifications that invalidate the
    in-memory HNSW index.

    Note: FAT/exFAT may return 0 for st_ino — the ``current_inode != 0``
    guard skips reconnect detection on those filesystems (safe fallback).
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
    if not _is_chroma_backend():
        raise RuntimeError("_get_client is only available for the Chroma backend")
    db_path = os.path.join(_config.palace_path, "chroma.sqlite3")
    try:
        st = os.stat(db_path)
        current_inode = st.st_ino
        current_mtime = st.st_mtime
    except OSError:
        current_inode = 0
        current_mtime = 0.0

    # If the DB file disappeared (e.g. during rebuild) but we have a cached
    # collection, invalidate so we don't serve stale data.  Without this,
    # both stored and current values are 0 on the first call after deletion,
    # making inode_changed and mtime_changed both False.
    if not os.path.isfile(db_path) and _collection_cache is not None:
        _client_cache = None
        _collection_cache = None
        _collection_cache_backend = None
        _collection_cache_palace = None
        _collection_open_error = None
        _palace_db_inode = 0
        _palace_db_mtime = 0.0
        # Fall through to normal reconnect which will handle missing DB

    inode_changed = current_inode != 0 and current_inode != _palace_db_inode
    mtime_changed = current_mtime != 0.0 and abs(current_mtime - _palace_db_mtime) > 0.01

    if _client_cache is None or inode_changed or mtime_changed:
        # Run the HNSW capacity probe BEFORE chromadb opens the segment --
        # if the index is severely undersized, segment load can segfault
        # the whole MCP server (#1222). The probe is pure sqlite +
        # metadata read; never touches the HNSW binary files.
        _refresh_vector_disabled_flag()
        if inode_changed or mtime_changed:
            ChromaBackend._quarantined_paths.discard(_config.palace_path)
            # #2002: a peer process changed chroma.sqlite3 on disk. chromadb
            # caches its System (and the live HNSW segment) keyed by path, so
            # make_client() below would hand back the STALE segment, which then
            # persists its outdated index over the peer's writes, driving the
            # persisted count backwards. Drop chromadb's shared cache first so
            # make_client() rebuilds the segment from the on-disk state.
            _force_chroma_cache_reset()
        _client_cache = ChromaBackend.make_client(_config.palace_path)
        _collection_cache = None
        _collection_cache_backend = None
        _collection_cache_palace = None
        _collection_open_error = None
        _invalidate_overview_caches()
        _palace_db_inode = current_inode
        _palace_db_mtime = current_mtime
    return _client_cache


def _get_collection(create=False):
    """Return the configured backend collection, caching handles between calls.

    On failure, log the exception and retry once after clearing the client
    and collection caches. Tools were silently returning ``None`` when a
    cached client/collection went stale — typically after the chromadb
    rust bindings invalidated a handle following an out-of-band write —
    leaving the LLM with no diagnostic and no recovery path. The retry
    forces ``_get_client()`` to rebuild the chromadb client from
    scratch, so the second attempt heals the common stale-handle case
    automatically.
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
    # Operator read-only mode must never bootstrap a collection. In
    # particular, sqlite_exact's normal create/open path initializes WAL,
    # schema, FTS metadata, and commits before the first read.
    if _READ_ONLY:
        create = False
    try:
        backend_name = _selected_backend_name()
    except (BackendMismatchError, KeyError) as exc:
        logger.warning("backend resolution failed for %s: %s", _config.palace_path, exc)
        _collection_open_error = {
            "error": "Backend mismatch"
            if isinstance(exc, BackendMismatchError)
            else "Unknown backend",
            "details": str(exc),
            "hint": "Select the matching backend or use a fresh palace directory.",
        }
        _collection_cache = None
        _collection_cache_backend = None
        _collection_cache_palace = None
        return None

    if backend_name != "chroma":
        # Normal stdio MCP remains capable of promotion to writer, but until
        # it actually owns the palace it must not open sqlite_exact through
        # the schema-initializing read/write path. This lets recall coexist
        # with a daemon/HTTP writer. _acquire_mcp_writer_lock() discards this
        # cached read-only collection before a promoted mutation is handled.
        collection_read_only = _READ_ONLY or (
            backend_name in {"sqlite_exact", "rust_exact"}
            and getattr(_args, "transport", "stdio") == "stdio"
            and _MCP_WRITER_LOCK_CM is None
        )
        if collection_read_only:
            create = False
        for attempt in range(2):
            try:
                if (
                    _collection_cache is not None
                    and _collection_cache_backend == backend_name
                    and _collection_cache_palace == _config.palace_path
                ):
                    _collection_open_error = None
                    return _collection_cache
                _collection_cache = None
                _collection_cache_backend = None
                _collection_cache_palace = None
                if _collection_cache is None:
                    from ..palace import get_collection as palace_get_collection

                    _collection_cache = palace_get_collection(
                        _config.palace_path,
                        collection_name=_config.collection_name,
                        create=create,
                        backend=backend_name,
                        read_only=collection_read_only,
                    )
                    _collection_cache_backend = backend_name
                    _collection_cache_palace = _config.palace_path
                    _collection_open_error = None
                    _invalidate_overview_caches()
                return _collection_cache
            except (BackendMismatchError, KeyError) as exc:
                logger.warning("backend open failed for %s: %s", _config.palace_path, exc)
                _collection_open_error = {
                    "error": "Backend mismatch"
                    if isinstance(exc, BackendMismatchError)
                    else "Unknown backend",
                    "details": str(exc),
                    "hint": "Select the matching backend or use a fresh palace directory.",
                }
                _collection_cache = None
                _collection_cache_backend = None
                _collection_cache_palace = None
                _invalidate_overview_caches()
                return None
            except Exception:
                logger.exception(
                    "_get_collection generic attempt %d/2 failed (palace=%s, create=%s)",
                    attempt + 1,
                    _config.palace_path,
                    create,
                )
                _collection_cache = None
                _collection_cache_backend = None
                _collection_cache_palace = None
                _invalidate_overview_caches()
                _collection_open_error = {
                    "error": "Backend open failed",
                    "details": "Could not open the selected backend collection.",
                    "hint": "Run: mempalace status or mempalace repair-status for diagnostics.",
                }
        return None

    db_path = os.path.join(_config.palace_path, "chroma.sqlite3")
    if not create and not os.path.isfile(db_path):
        _force_chroma_cache_reset()
        _collection_open_error = {
            "error": "Chroma database missing",
            "details": f"Could not open missing database at {db_path}.",
            "hint": "Run: mempalace status or mempalace repair-status for diagnostics.",
        }
        return None

    for attempt in range(2):
        try:
            if _collection_cache is not None and (
                _collection_cache_backend not in (None, "chroma")
                or _collection_cache_palace not in (None, _config.palace_path)
            ):
                _collection_cache = None
                _collection_cache_backend = None
                _collection_cache_palace = None
            client = _get_client()
            # ChromaDB 1.x persists the EF *identity* (its ``name()``) with the
            # collection but not the EF *instance/configuration*. So a reader or
            # writer that omits ``embedding_function=`` silently gets chromadb's
            # built-in ``DefaultEmbeddingFunction`` — its ``name()`` matches the
            # one we spoof in ``mempalace.embedding`` (both report ``"default"``,
            # the identity check passes), but the *provider list* is chromadb's
            # default rather than the user's resolved device. On bleeding-edge
            # interpreters (#1299: python 3.14 + chromadb 1.5.x on Apple Silicon)
            # that default provider selection can SIGSEGV the host process on
            # first ``col.add()``. The miner / Stop hook ingest path avoids this
            # because it routes through ``ChromaBackend.get_collection``, which
            # resolves the EF via ``ChromaBackend._resolve_embedding_function``;
            # the MCP server bypassed that abstraction. Resolve the EF inside the
            # branches that actually open a collection so warm-cache reads stay
            # zero-cost. Reuse the backend helper so the two call sites can't
            # drift on logging or fallback semantics.
            if create:
                ef = ChromaBackend._resolve_embedding_function()
                ef_kwargs = {"embedding_function": ef} if ef is not None else {}
                # hnsw:num_threads=1 disables ChromaDB's multi-threaded ParallelFor
                # HNSW insert path, which has a race in repairConnectionsForUpdate /
                # addPoint (see issues #974, #965). Set via metadata on fresh
                # collections and re-applied via _pin_hnsw_threads() for legacy
                # palaces whose collections were created before this fix (the
                # runtime config does not persist cross-process in chromadb 1.5.x,
                # so the retrofit runs every time _get_collection opens a cache).
                #
                # ChromaDB 1.5.x's Rust binding SIGSEGVs when get_or_create_collection
                # is called with metadata that differs from what's stored. The split
                # below skips the metadata-comparison codepath for existing
                # collections, mirroring the backend-layer fix from #1262.
                try:
                    raw = client.get_collection(_config.collection_name, **ef_kwargs)
                except _ChromaNotFoundError:
                    raw = client.create_collection(
                        _config.collection_name,
                        metadata={
                            "hnsw:space": "cosine",
                            "hnsw:num_threads": 1,
                            **_HNSW_WRITE_DEFAULTS,
                        },
                        **ef_kwargs,
                    )
                _pin_hnsw_threads(raw)
                _collection_cache = ChromaCollection(raw, palace_path=_config.palace_path)
                _collection_cache_backend = "chroma"
                _collection_cache_palace = _config.palace_path
                _collection_open_error = None
                _invalidate_overview_caches()
            elif _collection_cache is None:
                ef = ChromaBackend._resolve_embedding_function()
                ef_kwargs = {"embedding_function": ef} if ef is not None else {}
                raw = client.get_collection(_config.collection_name, **ef_kwargs)
                _pin_hnsw_threads(raw)
                _collection_cache = ChromaCollection(raw, palace_path=_config.palace_path)
                _collection_cache_backend = "chroma"
                _collection_cache_palace = _config.palace_path
                _collection_open_error = None
                _invalidate_overview_caches()
            return _collection_cache
        except (BackendMismatchError, KeyError) as exc:
            _collection_open_error = {
                "error": "Backend mismatch"
                if isinstance(exc, BackendMismatchError)
                else "Unknown backend",
                "details": str(exc),
                "hint": "Select the matching backend or use a fresh palace directory.",
            }
            _client_cache = None
            _collection_cache = None
            _collection_cache_backend = None
            _collection_cache_palace = None
            _palace_db_inode = 0
            _palace_db_mtime = 0.0
            _invalidate_overview_caches()
            return None
        except Exception:
            logger.exception(
                "_get_collection attempt %d/2 failed (palace=%s, create=%s)",
                attempt + 1,
                _config.palace_path,
                create,
            )
            if attempt == 0:
                # Reset all caches so the next attempt forces _get_client()
                # to rebuild the chromadb client from scratch, reopening
                # the collection cleanly and healing the common
                # stale-handle case.
                _client_cache = None
                _collection_cache = None
                _collection_cache_backend = None
                _collection_cache_palace = None
                _palace_db_inode = 0
                _palace_db_mtime = 0.0
                _invalidate_overview_caches()
                _collection_open_error = {
                    "error": "Backend open failed",
                    "details": "Could not open the Chroma collection.",
                    "hint": "Run: mempalace repair-status for diagnostics.",
                }
    _client_cache = None
    _collection_cache = None
    _collection_cache_backend = None
    _collection_cache_palace = None
    _palace_db_inode = 0
    _palace_db_mtime = 0.0
    _invalidate_overview_caches()
    _collection_open_error = _collection_open_error or {
        "error": "Backend open failed",
        "details": "Could not open the selected backend collection.",
        "hint": "Run: mempalace status or mempalace repair-status for diagnostics.",
    }
    return None


def _no_palace():
    return {
        "error": "No palace found",
        "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
    }


def _collection_error_or_no_palace():
    if not _collection_open_error:
        return _no_palace()
    result = dict(_collection_open_error)
    try:
        result["backend"] = _selected_backend_name()
    except Exception:
        pass
    return result


def _selected_backend_name() -> str:
    from ..palace import resolve_backend_name

    return resolve_backend_name(
        _config.palace_path,
        explicit=os.environ.get("MEMPALACE_BACKEND_EXPLICIT"),
    )


def _is_chroma_backend() -> bool:
    try:
        return _selected_backend_name() == "chroma"
    except Exception:
        logger.debug("backend resolution failed", exc_info=True)
        return False


def _backend_db_exists() -> bool:
    try:
        return detect_backend_for_path(_config.palace_path) is not None
    except Exception:
        logger.debug("backend artifact detection failed", exc_info=True)
        return False


# ==================== HELPERS ====================


def _safe_meta(meta):
    """Coerce a Chroma metadata value to a dict.

    ChromaDB's ``col.get()`` / ``col.query()`` can return ``None`` for the
    metadata cell of a partially-flushed row (or any row written without
    metadata in older formats). Indexing the result then yields ``None``,
    and downstream ``.get(...)`` calls raise::

        AttributeError: 'NoneType' object has no attribute 'get'

    This bug bricked the embeddings_queue cleanup path in issue #1426 —
    the handler crashed before reaching the ``DELETE FROM embeddings_queue``
    step, so the queue grew without bound while writes kept appearing
    successful.

    Centralizing the coercion through this helper makes the contract
    explicit and keeps the fix self-documenting at every call site:
    *metadata is always a dict by the time it leaves the boundary*.
    """
    return meta if isinstance(meta, dict) else {}


def _fetch_all_metadata(col, where=None):
    """Fetch every matching record's metadata via the backend's best strategy.

    Delegates to BaseCollection.get_all_metadata() (#1796), which Chroma
    satisfies with the same offset-paginated loop this function used to do
    inline, and which Qdrant overrides with a single _scroll_all() pass.
    Routing through one contract method means every backend gets its own
    correct strategy without this caller needing to know which backend it's
    talking to.
    """
    get_all = getattr(col, "get_all_metadata", None)
    if callable(get_all):
        return get_all(where=where)

    # Defensive fallback for any collection object that predates the
    # get_all_metadata() contract method (e.g. a third-party backend not yet
    # updated). Preserves the exact previous behavior.
    total = col.count()
    all_meta = []
    offset = 0
    while offset < total:
        kwargs = {"include": ["metadatas"], "limit": 1000, "offset": offset}
        if where:
            kwargs["where"] = where
        batch = col.get(**kwargs)
        if not batch["metadatas"]:
            break
        all_meta.extend(batch["metadatas"])
        offset += len(batch["metadatas"])
    return all_meta


def _supports_metadata_facets(col) -> bool:
    """Return True if the collection's backend implements metadata facets."""
    backend = getattr(col, "_backend", None)
    if backend is None:
        return False
    capabilities = getattr(backend, "capabilities", None)
    return isinstance(capabilities, (set, frozenset)) and "supports_metadata_facets" in capabilities


_metadata_cache = None
_metadata_cache_time = 0
_METADATA_CACHE_TTL = 5.0  # seconds
_taxonomy_cache = None
_taxonomy_cache_time = 0.0
_TAXONOMY_CACHE_TTL = 5.0  # seconds — same idea as the palace-graph cache
_MAX_RESULTS = 100  # upper bound for search/list limit params
_DIARY_READ_PAGE_SIZE = 1000


def _invalidate_overview_caches():
    """Drop status/list_wings taxonomy and metadata page caches after writes."""
    global _metadata_cache, _metadata_cache_time, _taxonomy_cache, _taxonomy_cache_time
    _metadata_cache = None
    _metadata_cache_time = 0
    _taxonomy_cache = None
    _taxonomy_cache_time = 0.0


def _get_cached_metadata(col, where=None):
    """Return cached metadata if fresh, else fetch and cache."""
    global _metadata_cache, _metadata_cache_time
    now = time.time()
    if (
        where is None
        and _metadata_cache is not None
        and (now - _metadata_cache_time) < _METADATA_CACHE_TTL
    ):
        return _metadata_cache
    result = _fetch_all_metadata(col, where=where)
    if where is None:
        _metadata_cache = result
        _metadata_cache_time = now
    return result


def _sanitize_optional_name(value: str = None, field_name: str = "name") -> str:
    """Validate optional wing/room-style filters."""
    if value is None or not value.strip():
        return None
    return sanitize_name(value, field_name)


# Bounds the whole stored source_file string (often an absolute path), so it is
# Linux PATH_MAX rather than the 128-char wing/room NAME limit.
_MAX_SOURCE_FILE_LENGTH = 4096


def _sanitize_optional_source_file(value: str = None) -> str:
    """Validate an optional source_file search filter (#1815).

    Unlike wing/room, a source_file is a path: ``/``, ``\\`` and ``.`` are
    legal, so it is NOT run through ``sanitize_name`` (which rejects path
    characters as traversal attempts). The value is matched verbatim as a
    ChromaDB metadata-equality / parameterized-SQL value — never used as a
    filesystem path — so there is no traversal risk to guard against. A null
    byte or a pathological length can still upset the backend (chromadb
    add/upsert chokes on null bytes / lone surrogates, #1235), so guard those
    for parity with ``sanitize_name``. Blank / whitespace-only is "no filter".
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("source_file must be a string")
    value = value.strip()
    if not value:
        return None
    if "\x00" in value:
        raise ValueError("source_file contains null bytes")
    if value != strip_lone_surrogates(value):
        raise ValueError("source_file contains invalid surrogate characters")
    if len(value) > _MAX_SOURCE_FILE_LENGTH:
        raise ValueError(
            f"source_file exceeds maximum length of {_MAX_SOURCE_FILE_LENGTH} characters"
        )
    return value


# The #1128 date-filter helpers moved to ``mempalace.date_window`` so the
# search-side window (#463) can share them without importing this module
# (whose import installs MCP stdio protection). Aliased under their
# historical private names — every call site and test keeps working.
_parse_date_filter = parse_date_bound
_filed_at_in_window = filed_at_in_window
