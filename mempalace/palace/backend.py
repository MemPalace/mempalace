# Loaded into mempalace.palace via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.palace":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.palace")


def _config_backend_value(palace_path: str) -> Optional[str]:
    try:
        from ..config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg_palace = os.path.abspath(os.path.expanduser(cfg.palace_path))
        target_palace = os.path.abspath(os.path.expanduser(palace_path))
        if cfg_palace != target_palace:
            return None
        value = cfg._file_config.get("backend")
        return str(value).strip().lower() if value else None
    except Exception:
        return None


def _env_backend_value() -> Optional[str]:
    value = os.environ.get("MEMPALACE_BACKEND")
    return value.strip().lower() if value else None


def resolve_backend_name(palace_path: str, explicit: Optional[str] = None) -> str:
    """Resolve and validate the selected backend for ``palace_path``.

    Public resolution order:

    1. Explicit CLI/MCP flag or direct ``get_collection(..., backend=...)``.
    2. ``backend`` in ``~/.mempalace/config.json``.
    3. ``MEMPALACE_BACKEND``.
    4. Detected existing palace artifacts.
    5. ``chroma``.

    If artifacts for a different backend are already present, raise
    ``BackendMismatchError`` so normal write paths cannot silently mix storage
    formats in one palace directory.
    """
    explicit = explicit or os.environ.get(_EXPLICIT_BACKEND_ENV)
    selected = resolve_backend_for_palace(
        explicit=explicit.strip().lower() if explicit else None,
        config_value=_config_backend_value(palace_path),
        env_value=_env_backend_value(),
        palace_path=palace_path,
        default="chroma",
    )
    get_backend_class(selected)
    detected_backends = detect_backends_for_path(palace_path)
    if len(detected_backends) > 1:
        raise BackendMismatchError(
            f"palace at {palace_path!r} contains multiple backend artifacts: "
            f"{', '.join(detected_backends)}"
        )
    detected = detected_backends[0] if detected_backends else None
    if detected and detected != selected:
        exact_family = {"sqlite_exact", "rust_exact"}
        if not (detected in exact_family and selected in exact_family):
            raise BackendMismatchError(
                f"palace at {palace_path!r} contains {detected!r} backend artifacts, "
                f"but {selected!r} was selected"
            )
    return selected


_MULTI_PROCESS_WRITER_BACKENDS = frozenset({"pgvector", "qdrant"})


def backend_requires_single_writer(backend_name: str) -> bool:
    """Return whether a backend needs one process-lifetime writer owner.

    Local file-backed backends cannot safely coordinate independent long-lived
    clients by serializing only individual calls: each process may retain
    SQLite/WAL, FTS, or vector-index state across operations. Unknown and
    plugin backends are treated conservatively. Only backends whose storage
    service is explicitly responsible for cross-process concurrency opt out.
    """
    normalized = backend_name.strip().lower()
    if normalized == "milvus":
        # Only embedded Milvus Lite is local single-writer storage. A remote
        # Milvus server or Zilliz Cloud coordinates concurrent clients itself.
        from ..backends.milvus import milvus_uri_is_server
        from ..config import MempalaceConfig

        return not milvus_uri_is_server(MempalaceConfig().milvus_uri)
    return normalized not in _MULTI_PROCESS_WRITER_BACKENDS


def get_backend_for_palace(palace_path: str, explicit: Optional[str] = None):
    """Return the resolved backend instance for ``palace_path``."""
    return get_backend(resolve_backend_name(palace_path, explicit=explicit))


def _backend_artifact_label(backend_name: Optional[str]) -> str:
    if backend_name == "chroma":
        return "chroma.sqlite3"
    if backend_name == "qdrant":
        return "qdrant_backend.json"
    if backend_name == "pgvector":
        return "pgvector_backend.json"
    if backend_name in {"sqlite_exact", "rust_exact"}:
        return "sqlite_exact.sqlite3"
    return "backend database"


def _open_collection_or_explain(
    palace_path: str,
    *,
    collection_name: Optional[str] = None,
    out=None,
    opener=None,
    read_only: bool = False,
):
    """Open the palace collection or print a state-specific message and return ``None``.

    For CLI and repair commands that want consistent, actionable user-facing
    messages distinguishing four "not-healthy" states from one another. MCP
    and library callers should catch
    :class:`mempalace.backends.PalaceNotFoundError` /
    :class:`mempalace.backends.CollectionNotInitializedError` directly.

    The MCP server (``mcp_server.tool_status``) deliberately does NOT use
    this helper: it uses ``_get_collection(create=db_exists)`` so a valid
    palace whose collection was never bootstrapped lazily gets one on the
    first status call, and a corruption-detection sqlite-only probe fires
    first when the vector path is disabled (see PR #831 / issue #830).

    State A: palace dir is absent.
    State B: dir is present but no backend database artifact is present.
        The helper short-circuits to a message before reaching the backend,
        because some backends lazily create their DB file on first open —
        calling the backend on this state would silently mutate the filesystem
        for what should be a read-only inspection.
    State C: DB is present but the ``mempalace_drawers`` collection has
        never been bootstrapped (``init`` ran, ``mine`` has not).
    State D: healthy — returns the opened collection.
    State E: an unexpected error opens the backend — message points the
        user at ``repair-status`` for further diagnosis.

    ``out`` is the message sink; defaults to the builtin ``print``. Pass a
    callable (e.g. a repair progress emitter) to route messages through it.
    """
    emit = out if out is not None else print
    open_collection = opener or get_collection

    if not os.path.isdir(palace_path):
        emit(f"\n  No palace found at {palace_path}")
        emit("  Run: mempalace init <dir> then mempalace mine <dir>")
        return None
    try:
        backend_name = resolve_backend_name(palace_path)
    except BackendMismatchError as e:
        emit(f"\n  Backend mismatch at {palace_path}: {e}")
        emit("  Select the matching backend or use a fresh palace directory.")
        return None
    except KeyError as e:
        # Unknown backend name (e.g. a typo in MEMPALACE_BACKEND/--backend):
        # resolve_backend_name -> get_backend_class raises KeyError carrying the
        # available-backend list. Surface it as a CLI state message rather than
        # letting it escape as a stack trace.
        emit(f"\n  Unknown backend selected for {palace_path}: {e.args[0] if e.args else e}")
        emit("  Set --backend or MEMPALACE_BACKEND to a registered backend.")
        return None
    detected = detect_backend_for_path(palace_path)
    if detected is None:
        emit(
            f"\n  Palace dir at {palace_path} exists but has no "
            f"{_backend_artifact_label(backend_name)} yet."
        )
        emit("  Run: mempalace mine <dir>")
        return None
    try:
        options = {"read_only": True} if read_only else {}
        return open_collection(
            palace_path,
            collection_name=collection_name,
            create=False,
            backend=backend_name,
            **options,
        )
    except CollectionNotInitializedError:
        emit(f"\n  Palace at {palace_path} is initialized but empty (no drawers yet).")
        emit("  Run: mempalace mine <dir>")
        return None
    except PalaceNotFoundError:
        emit(f"\n  No palace found at {palace_path}")
        emit("  Run: mempalace init <dir> then mempalace mine <dir>")
        return None
    except BackendMismatchError as e:
        emit(f"\n  Backend mismatch at {palace_path}: {e}")
        emit("  Select the matching backend or use a fresh palace directory.")
        return None
    except BackendClosedError:
        # Surface this as a programmer error, not a palace-state UX message:
        # a closed backend means the caller violated the backend lifecycle,
        # not that the palace on disk is in a recoverable state.
        raise
    except Exception as e:  # noqa: BLE001 — backend exceptions vary (chromadb, OSError, lock errors)
        emit(f"\n  Error opening palace at {palace_path}: {e!r}")
        emit("  Try: mempalace repair-status --palace <path>")
        return None
