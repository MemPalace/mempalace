"""
palace.py — Shared palace operations.

Consolidates collection access patterns used by both miners and the MCP server.
"""

import contextlib
import hashlib
import logging
import os
import re
import sys
import threading
from typing import Optional

from .backends import (
    BackendClosedError,
    BackendMismatchError,
    CollectionNotInitializedError,
    PalaceNotFoundError,
    PalaceRef,
    detect_backend_for_path,
    detect_backends_for_path,
    get_backend,
    get_backend_class,
    resolve_backend_for_palace,
)
from .backends.embedding_wrapper import EmbeddingCollection
from .entity_detector import _apply_known_systems_prepass, _get_coca_filter

logger = logging.getLogger("mempalace_mcp")

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    "coverage",
    ".mempalace",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".cache",
    ".tox",
    ".nox",
    ".idea",
    ".vscode",
    ".ipynb_checkpoints",
    ".eggs",
    "htmlcov",
    "target",
}

_DEFAULT_BACKEND = get_backend("chroma")
_EXPLICIT_BACKEND_ENV = "MEMPALACE_BACKEND_EXPLICIT"

# Schema version for drawer normalization. Bump when the normalization
# pipeline changes in a way that existing drawers should be rebuilt to pick up
# (e.g., new noise-stripping rules). `file_already_mined` treats drawers with
# a missing or stale `normalize_version` as "not mined", so the next mine pass
# silently rebuilds them — users don't need to manually erase + re-mine.
#
# v2 (2026-04): introduced strip_noise() for Claude Code JSONL; previous
#               drawers stored system tags / hook chrome verbatim.
NORMALIZE_VERSION = 2


# (palace_id, collection_name, model_name) tuples already validated this
# process, so the identity check (one metadata read) runs at most once per
# collection per run — keeps the hot get_collection path cheap.
_VALIDATED_IDENTITY: set = set()


def _enforce_embedder_identity(collection, palace_path, collection_name, *, create) -> None:
    """Check (and, for a brand-new collection, record) embedder identity (RFC 001).

    Check at open so a model swap fails fast — before any query silently
    returns degraded results. Record only when the collection is brand-new and
    empty: recording the *current* model on a legacy palace that already holds
    vectors from an unknown model would mislabel it, so populated-but-unrecorded
    collections warn instead and are resolved with
    ``mempalace palace set-embedder``.

    Bookkeeping must never break memory operations: only the deliberate
    identity/dimension mismatch propagates; every other error is swallowed.
    """
    import warnings

    from .backends.base import (
        DimensionMismatchError,
        EmbedderIdentity,
        EmbedderIdentityMismatchError,
        EmbedderIdentityUnknownWarning,
        check_embedder_identity,
    )
    from .embedding import current_model_name

    # A server_embedder backend embeds with its own model and ignores the
    # injected/core embedder, so its effective identity — not the configured
    # model — is what must be checked and recorded. Fall back to the configured
    # model name for the normal (core-embedder) case.
    current: Optional[EmbedderIdentity] = None
    try:
        effective = collection.effective_embedder_identity()
    except Exception:
        effective = None
    if effective is not None and getattr(effective, "model_name", ""):
        current = effective
    else:
        try:
            model_name = current_model_name()
        except Exception:
            return
        if not model_name:
            return  # nameless embedder — cannot enforce identity
        current = EmbedderIdentity(model_name=model_name, dimension=0)

    model_name = current.model_name
    key = (str(palace_path), str(collection_name), model_name)
    if key in _VALIDATED_IDENTITY:
        return

    try:
        stored = collection.get_stored_embedder_identity()
    except Exception:
        logger.debug("embedder-identity read failed for %s", collection_name, exc_info=True)
        return
    try:
        state = check_embedder_identity(stored, current)
    except (EmbedderIdentityMismatchError, DimensionMismatchError):
        raise  # deliberate, user-facing — the whole point of the contract
    except Exception:
        return

    if state == "unknown" and stored is None:
        try:
            count = collection.count()
        except Exception:
            count = None
        if count == 0:
            if create:
                try:
                    collection.set_embedder_identity(current)
                except Exception:
                    logger.debug("embedder-identity record failed", exc_info=True)
        elif count:
            warnings.warn(
                f"palace collection {collection_name!r} has no recorded embedder "
                f"identity; assuming the current model {model_name!r}. Run "
                "`mempalace palace set-embedder --model <name>` to record it.",
                EmbedderIdentityUnknownWarning,
                stacklevel=2,
            )

    _VALIDATED_IDENTITY.add(key)


def get_collection(
    palace_path: str,
    collection_name: Optional[str] = None,
    create: bool = True,
    backend: Optional[str] = None,
    _skip_identity_check: bool = False,
):
    """Get the palace collection through the backend layer.

    ``_skip_identity_check`` bypasses the embedder-identity enforcement so the
    ``set-embedder`` override path can open a palace whose recorded model
    differs from the current one (the very state it exists to repair).
    """
    if collection_name is None:
        from .config import get_configured_collection_name

        collection_name = get_configured_collection_name()
    backend_obj = get_backend_for_palace(palace_path, explicit=backend)
    palace_ref = PalaceRef(id=palace_path, local_path=palace_path)
    try:
        collection = backend_obj.get_collection(
            palace=palace_ref,
            collection_name=collection_name,
            create=create,
        )
    except TypeError as exc:
        if "unexpected keyword argument 'palace'" not in str(exc):
            raise
        collection = backend_obj.get_collection(
            palace_path,
            collection_name=collection_name,
            create=create,
        )
    if "requires_explicit_embeddings" in getattr(backend_obj, "capabilities", frozenset()):
        collection = EmbeddingCollection(collection)
    if not _skip_identity_check:
        _enforce_embedder_identity(collection, palace_path, collection_name, create=create)
    return collection


def set_palace_embedder_identity(
    palace_path: str,
    model: Optional[str] = None,
    *,
    force: bool = False,
    backend: Optional[str] = None,
    collection_name: Optional[str] = None,
):
    """Record (or force-override) a palace collection's embedder identity (RFC 001).

    Backs ``mempalace palace set-embedder``. Returns ``(old, new)`` identities.
    Without ``force``, refuses to overwrite an existing identity that names a
    different model (the user must confirm they know the vectors are
    compatible). Opens with the identity check skipped so a mismatched palace —
    the exact state being repaired — can be opened at all.
    """
    from .backends.base import EmbedderIdentity, EmbedderIdentityMismatchError
    from .config import MempalaceConfig
    from .embedding import get_embedder_identity

    configured = MempalaceConfig().embedding_model
    target = (model or configured or "").strip().lower()
    if not target:
        # No model given and none configured — there is nothing to record, and
        # recording a nameless identity is a silent no-op in every backend.
        raise ValueError(
            "no embedder model to record: pass --model NAME or configure MEMPALACE_EMBEDDING_MODEL"
        )
    if target == (configured or "").strip().lower():
        # Recording the in-use model — probe its dimension (already loaded).
        new = get_embedder_identity()
    else:
        # Explicit override of a non-configured model: record the name only,
        # never load a foreign model (which can be a large download) just to
        # probe a dimension. The model-name check is the actual protection.
        new = EmbedderIdentity(model_name=target, dimension=0)
    collection = get_collection(
        palace_path,
        collection_name=collection_name,
        create=True,
        backend=backend,
        _skip_identity_check=True,
    )
    try:
        old = collection.get_stored_embedder_identity()
    except Exception:
        old = None
    if old is not None and old.model_name != new.model_name and not force:
        raise EmbedderIdentityMismatchError(
            f"palace already records embedder {old.model_name!r}; pass --force to "
            f"overwrite it with {new.model_name!r} (only if the vectors are compatible)"
        )
    collection.set_embedder_identity(new)
    # Reset the per-process validation cache so a re-open re-checks against the
    # newly recorded identity rather than a stale verdict.
    _VALIDATED_IDENTITY.clear()
    return old, new


def get_closets_collection(
    palace_path: str,
    create: bool = True,
    backend: Optional[str] = None,
):
    """Get the closets collection — the searchable index layer."""
    return get_collection(
        palace_path,
        collection_name="mempalace_closets",
        create=create,
        backend=backend,
    )


def _config_backend_value(palace_path: str) -> Optional[str]:
    try:
        from .config import MempalaceConfig

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
        raise BackendMismatchError(
            f"palace at {palace_path!r} contains {detected!r} backend artifacts, "
            f"but {selected!r} was selected"
        )
    return selected


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
    if backend_name == "sqlite_exact":
        return "sqlite_exact.sqlite3"
    return "backend database"


def _open_collection_or_explain(
    palace_path: str,
    *,
    collection_name: Optional[str] = None,
    out=None,
    opener=None,
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
        return open_collection(
            palace_path,
            collection_name=collection_name,
            create=False,
            backend=backend_name,
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


CLOSET_CHAR_LIMIT = 1500  # fill closet until ~1500 chars, then start a new one
CLOSET_EXTRACT_WINDOW = 5000  # how many chars of source content to scan for entities/topics

# Common capitalized words that look like proper nouns but are usually
# sentence-starters or filler. Filtered out of entity extraction.
_ENTITY_STOPLIST = frozenset(
    {
        "The",
        "This",
        "That",
        "These",
        "Those",
        "When",
        "Where",
        "What",
        "Why",
        "Who",
        "Which",
        "How",
        "After",
        "Before",
        "Then",
        "Now",
        "Here",
        "There",
        "And",
        "But",
        "Or",
        "Yet",
        "So",
        "If",
        "Else",
        "Yes",
        "No",
        "Maybe",
        "Okay",
        "User",
        "Assistant",
        "System",
        "Tool",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    }
)


_CANDIDATE_RX_CACHE = None


def _candidate_entity_words(text: str) -> list:
    """Find entity candidate words using i18n-aware patterns.

    Uses the same candidate_patterns as entity_detector (loaded from locale
    JSON files via get_entity_patterns), so non-Latin names (Cyrillic,
    accented Latin, etc.) are detected alongside ASCII names.
    """
    global _CANDIDATE_RX_CACHE
    if _CANDIDATE_RX_CACHE is None:
        from .config import MempalaceConfig
        from .i18n import get_entity_patterns

        patterns = get_entity_patterns(MempalaceConfig().entity_languages)
        rxs = []
        for pat in patterns["candidate_patterns"]:
            try:
                rxs.append(re.compile(pat))
            except re.error:
                continue
        _CANDIDATE_RX_CACHE = rxs
    words = []
    for rx in _CANDIDATE_RX_CACHE:
        words.extend(rx.findall(text))
    return words


def build_closet_lines(source_file, drawer_ids, content, wing, room, drawer_metas=None):
    """Build compact closet pointer lines from drawer content.

    Returns a LIST of lines (not joined). Each line is one complete topic
    pointer — never split across closets.

    Legacy format (3 segments): ``topic|entities|→drawer_ids``
    Tier 6a format (4 segments): ``topic|entities|YYYY-MM-DD:Lstart-Lend|→drawer_ids``

    When ``drawer_metas`` is provided and the first meta carries both
    ``line_start``/``line_end`` plus a parseable ``filed_at``, the 4-segment
    form is emitted so retrieval can jump to the right span. Otherwise the
    legacy 3-segment form is used — backward compat for drawers filed before
    Tier 6a and for direct callers that don't have metadata handy.
    """
    import re
    from pathlib import Path

    drawer_ref = ",".join(drawer_ids[:3])
    window = content[:CLOSET_EXTRACT_WINDOW]

    # Tier 6a — date+line locator segment. Built once per call; ``None``
    # signals "fall back to legacy 3-segment format" for every emitted line.
    date_line_seg = _build_date_line_segment(drawer_metas)

    # Extract proper nouns (2+ occurrences). Uses i18n-aware patterns so
    # non-Latin names (Cyrillic, accented Latin, etc.) are also detected.
    # Tier 3 linguistics cleanup — known-systems compound pre-pass. Detects
    # multi-word product names ("Claude Code", "GitHub Copilot", …) atomically
    # and masks them out of the working window so the single-word extraction
    # below doesn't decompose them.
    working_window, compound_counts = _apply_known_systems_prepass(window)

    coca_filter = _get_coca_filter()
    words = _candidate_entity_words(working_window)
    word_freq: dict = dict(compound_counts)
    for w in words:
        if w in _ENTITY_STOPLIST:
            continue
        # Tier 2 linguistics cleanup — drop common English content words
        # ("Code", "Line", "Note", "Phase", …) so they don't appear in
        # closet pointers as fake entities.
        if w.lower() in coca_filter:
            continue
        word_freq[w] = word_freq.get(w, 0) + 1
    entities = sorted(
        [w for w, c in word_freq.items() if c >= 2],
        key=lambda w: -word_freq[w],
    )[:5]
    entity_str = ";".join(entities) if entities else ""

    # Extract key phrases — action verbs + context
    topics = []
    for pattern in [
        r"(?:built|fixed|wrote|added|pushed|tested|created|decided|migrated|reviewed|deployed|configured|removed|updated)\s+[\w\s]{3,40}",
    ]:
        topics.extend(re.findall(pattern, window, re.IGNORECASE))
    # Also grab section headers if present
    for header in re.findall(r"^#{1,3}\s+(.{5,60})$", window, re.MULTILINE):
        topics.append(header.strip())
    # Dedupe preserving order
    topics = list(dict.fromkeys(t.strip().lower() for t in topics))[:12]

    # Extract quotes
    quotes = re.findall(r'"([^"]{15,150})"', window)

    # Build pointer lines — each one is atomic, never split. When the
    # Tier 6a date+line segment is available, splice it in as the 3rd
    # pipe-separated field; otherwise emit the legacy 3-segment form.
    def _pointer(prefix: str) -> str:
        if date_line_seg is not None:
            return f"{prefix}|{entity_str}|{date_line_seg}|→{drawer_ref}"
        return f"{prefix}|{entity_str}|→{drawer_ref}"

    lines = []
    for topic in topics:
        lines.append(_pointer(topic))
    for quote in quotes[:3]:
        lines.append(_pointer(f'"{quote}"'))

    # Always have at least one line
    if not lines:
        name = Path(source_file).stem[:40]
        lines.append(_pointer(f"{wing}/{room}/{name}"))

    return lines


def _build_date_line_segment(drawer_metas):
    """Tier 6a — produce ``YYYY-MM-DD:Lstart-Lend`` from a drawer-meta list.

    Reads the first meta's ``filed_at`` (date prefix only — never the raw
    ISO timestamp; closet pointers stay compact and grep-friendly) plus its
    ``line_start`` / ``line_end``. Returns ``None`` when any of the three
    fields is missing or unparseable — caller then falls back to the legacy
    3-segment closet pointer format. The choice to read only the first meta
    matches ``drawer_ids[:3]`` truncation in ``build_closet_lines``: pointers
    are approximate locators, not exhaustive indexes.
    """
    if not drawer_metas:
        return None
    meta = drawer_metas[0]
    if not isinstance(meta, dict):
        return None
    line_start = meta.get("line_start")
    line_end = meta.get("line_end")
    if line_start is None or line_end is None:
        return None

    # Tier 6a date hierarchy: prefer ``content_date`` (extracted from file
    # content, frontmatter, filename, or mtime — see
    # mempalace.miner._extract_content_date) when present. Fall back to
    # ``filed_at`` (ingestion timestamp) only when no content-aware date
    # was extractable. ``content_date`` is already an ISO ``YYYY-MM-DD``;
    # ``filed_at`` may be a full ISO timestamp like
    # ``2026-05-21T22:30:00.123456+00:00`` and gets truncated at ``T``.
    content_date = meta.get("content_date")
    if content_date:
        date_part = str(content_date)
    else:
        filed_at = meta.get("filed_at")
        if not filed_at:
            return None
        date_part = str(filed_at).split("T", 1)[0]
    if not date_part:
        return None
    return f"{date_part}:L{line_start}-L{line_end}"


def purge_file_closets(closets_col, source_file: str) -> None:
    """Delete every closet associated with ``source_file``.

    Call this before ``upsert_closet_lines`` on a re-mine so stale topics
    from a prior schema/version don't survive in the closet collection.
    Mirrors the drawer-purge step in process_file().
    """
    try:
        closets_col.delete(where={"source_file": source_file})
    except Exception:
        logger.debug("Closet purge failed for %s", source_file, exc_info=True)


def upsert_closet_lines(closets_col, closet_id_base, lines, metadata):
    """Write topic lines to closets, packed greedily without splitting a line.

    Closets are deterministically numbered (``..._01``, ``..._02``, …) and
    each ``upsert`` fully overwrites the prior content at that ID. Callers
    are expected to ``purge_file_closets`` first when re-mining a source
    file so stale-numbered closets from larger prior runs don't leak.

    Returns the number of closets written.
    """
    closet_num = 1
    current_lines: list = []
    current_chars = 0
    closets_written = 0

    def _flush():
        nonlocal closets_written
        if not current_lines:
            return
        closet_id = f"{closet_id_base}_{closet_num:02d}"
        text = "\n".join(current_lines)
        closets_col.upsert(documents=[text], ids=[closet_id], metadatas=[metadata])
        closets_written += 1

    for line in lines:
        line_len = len(line)
        # Would this line fit whole in the current closet?
        if current_chars > 0 and current_chars + line_len + 1 > CLOSET_CHAR_LIMIT:
            _flush()
            closet_num += 1
            current_lines = []
            current_chars = 0

        current_lines.append(line)
        current_chars += line_len + 1  # +1 for newline

    _flush()
    return closets_written


@contextlib.contextmanager
def mine_lock(source_file: str):
    """Cross-platform file lock for mine operations.

    Prevents multiple agents from mining the same file simultaneously,
    which causes duplicate drawers when the delete+insert cycle interleaves.
    """
    lock_path = _mine_lock_path(source_file)
    lf = _acquire_mine_lock_file(lock_path)
    try:
        yield
    finally:
        try:
            _unlock_mine_lock_file(lf)
        except Exception:
            logger.debug("Mine-lock release failed", exc_info=True)
        try:
            lf.close()
        except Exception:
            logger.debug("Mine-lock close failed", exc_info=True)
        _cleanup_mine_lock_file(lock_path)


def _mine_lock_path(source_file: str) -> str:
    lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    return os.path.join(lock_dir, hashlib.sha256(source_file.encode()).hexdigest()[:16] + ".lock")


def _open_mine_lock_file(lock_path: str, *, create: bool):
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    fd = os.open(lock_path, flags, 0o600)
    return os.fdopen(fd, "r+b")


def _lock_mine_lock_file(lock_file, *, blocking: bool) -> bool:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(lock_file.fileno(), mode, 1)
        except OSError:
            if not blocking:
                return False
            raise
        return True

    import fcntl

    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(lock_file, flags)
    except BlockingIOError:
        if not blocking:
            return False
        raise
    return True


def _unlock_mine_lock_file(lock_file) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file, fcntl.LOCK_UN)


def _mine_lock_file_is_current(lock_file, lock_path: str) -> bool:
    """Return whether ``lock_file`` is still the inode reached by ``lock_path``.

    POSIX advisory locks attach to the opened inode, not the pathname. If a
    lock file is unlinked while a contender is waiting, that contender can later
    acquire a lock on an inode no new process will use. We reject that stale
    handle and retry on the current pathname.
    """
    if os.name == "nt":
        return True
    try:
        path_stat = os.stat(lock_path)
        file_stat = os.fstat(lock_file.fileno())
    except OSError:
        return False
    return (path_stat.st_dev, path_stat.st_ino) == (file_stat.st_dev, file_stat.st_ino)


def _acquire_open_mine_lock_file(lock_file, lock_path: str) -> bool:
    """Acquire ``lock_file`` and return False if cleanup made it stale."""
    _lock_mine_lock_file(lock_file, blocking=True)
    if _mine_lock_file_is_current(lock_file, lock_path):
        return True
    try:
        _unlock_mine_lock_file(lock_file)
    except Exception:
        logger.debug("Mine-lock stale-handle release failed", exc_info=True)
    return False


def _acquire_mine_lock_file(lock_path: str):
    while True:
        lf = _open_mine_lock_file(lock_path, create=True)
        try:
            if _acquire_open_mine_lock_file(lf, lock_path):
                return lf
        except Exception:
            lf.close()
            raise
        lf.close()


def _cleanup_mine_lock_file(lock_path: str) -> None:
    """Best-effort removal that preserves flock rendezvous semantics.

    A plain ``os.remove(lock_path)`` after closing the critical-section lock is
    unsafe on POSIX: a waiter may already be blocked on the old inode while a
    later process creates and locks a new inode at the same pathname. Instead,
    cleanup briefly re-acquires the current file nonblocking. If it wins, it can
    unlink that inode as cleanup-only work; waiters on the old inode will detect
    the stale handle after waking and retry on the current path.
    """
    try:
        lf = _open_mine_lock_file(lock_path, create=False)
    except FileNotFoundError:
        return
    except OSError:
        logger.debug("Mine-lock cleanup open failed for %s", lock_path, exc_info=True)
        return

    acquired = False
    closed = False
    try:
        try:
            acquired = _lock_mine_lock_file(lf, blocking=False)
        except OSError:
            logger.debug("Mine-lock cleanup acquire failed for %s", lock_path, exc_info=True)
            return
        if not acquired:
            return
        if not _mine_lock_file_is_current(lf, lock_path):
            return

        if os.name == "nt":
            # Windows generally cannot unlink an open locked file. Release and
            # close first; if another process opens the file in the gap,
            # os.remove should fail and we leave the rendezvous file in place.
            try:
                _unlock_mine_lock_file(lf)
            except Exception:
                logger.debug("Mine-lock cleanup release failed", exc_info=True)
                acquired = False
                return
            acquired = False
            lf.close()
            closed = True
            try:
                os.remove(lock_path)
            except OSError:
                pass
            return

        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("Mine-lock cleanup remove failed for %s", lock_path, exc_info=True)
    finally:
        if not closed:
            if acquired:
                try:
                    _unlock_mine_lock_file(lf)
                except Exception:
                    logger.debug("Mine-lock cleanup release failed", exc_info=True)
            lf.close()


class MineAlreadyRunning(RuntimeError):
    """Raised when another `mempalace mine` already holds the per-palace lock."""


class MineValidationError(RuntimeError):
    """Raised at end of mine when PRAGMA quick_check on the palace reports errors."""

    def __init__(self, palace_path: str, errors: list[str]) -> None:
        if not errors:
            raise ValueError("MineValidationError requires at least one error string")
        if not palace_path:
            raise ValueError("MineValidationError requires a non-empty palace_path")
        super().__init__(f"FTS5/SQLite quick_check failed: {len(errors)} issue(s)")
        self.palace_path = palace_path
        # Freeze the forensic snapshot so handlers cannot mutate it.
        self.errors: tuple[str, ...] = tuple(errors)


def _validate_palace_fts5_after_mine(palace_path: str) -> None:
    """Raise MineValidationError if PRAGMA quick_check reports any error after a mine.

    Reuses the same primitive that `cmd_repair` already runs as preflight so the
    operator sees the same recovery banner regardless of which command surfaces
    the bug.
    """
    if resolve_backend_name(palace_path) != "chroma":
        return

    # Defer-import: keeps the repair module graph out of mine's hot import path.
    from .repair import _close_chroma_handles, sqlite_integrity_errors

    # Pass the live singleton so the writer's cached PersistentClient actually
    # gets closed and WAL flushes before the read-only sqlite3 re-open.
    # A transient ChromaBackend (the default) would only clear its own empty
    # `_clients` dict and leave _DEFAULT_BACKEND's live handle in place,
    # which on Windows keeps the sqlite file mmap'd.
    _close_chroma_handles(palace_path, backend=_DEFAULT_BACKEND)

    errors = sqlite_integrity_errors(palace_path)
    if errors:
        raise MineValidationError(palace_path, errors)


# Per-thread record of palaces this thread already holds the lock for. Used by
# `mine_palace_lock` to short-circuit re-entrant acquisition from the same
# thread (e.g. miner.mine() acquires the outer lock then calls
# ChromaCollection.upsert which now also tries to acquire). Without this guard
# the inner call would block on its own outer flock (Linux fcntl locks are per
# open file description, so a same-thread second open of the lock file is a
# distinct lock and self-deadlocks).
#
# The holder set is tagged with ``pid`` so that a forked child does NOT
# inherit re-entrant credit from its parent: the OS-level flock IS NOT
# inherited as a "we hold it" semantically — the child must reacquire — but
# Python's ``threading.local`` IS inherited across fork. The pid check
# clears stale state so a forked child correctly hits the fcntl path.
_palace_lock_holders = threading.local()


def _holder_state():
    """Return the per-thread (pid, keys) record, refreshing after fork."""
    keys = getattr(_palace_lock_holders, "keys", None)
    pid = getattr(_palace_lock_holders, "pid", None)
    current_pid = os.getpid()
    if keys is None or pid != current_pid:
        keys = set()
        _palace_lock_holders.keys = keys
        _palace_lock_holders.pid = current_pid
    return keys


def _held_by_this_thread(lock_key: str) -> bool:
    """Return True if this thread already holds ``mine_palace_lock`` for ``lock_key``."""
    return lock_key in _holder_state()


def _mark_held(lock_key: str) -> None:
    _holder_state().add(lock_key)


def _mark_released(lock_key: str) -> None:
    _holder_state().discard(lock_key)


def _format_lock_holder(content: str) -> str:
    """Render a lock-file body as 'PID N (cmdline)' for diagnostic messages."""
    parts = content.split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        return "another writer (identity not recorded)"
    pid = parts[0]
    if len(parts) > 1 and parts[1].strip():
        return f"PID {pid} ({parts[1].strip()})"
    return f"PID {pid}"


# Byte 0 of the lock file is reserved as the OS lock sentinel.
# Holder identity is written from byte 1 onward so contenders can read
# the identity without colliding with byte 0 (Windows msvcrt.locking
# blocks both reads and writes on the locked byte).
_LOCK_SENTINEL_BYTES = 1


def _read_lock_holder(lock_file) -> str:
    """Read the prior holder's identity from the lock-file body, best-effort."""
    try:
        lock_file.seek(_LOCK_SENTINEL_BYTES)
        content = lock_file.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        content = content.strip()
    except OSError:
        return "another writer (identity not recorded)"
    if not content:
        return "another writer (identity not recorded)"
    return _format_lock_holder(content)


def _write_lock_holder(lock_file) -> None:
    """Record this process's identity in the lock-file body. Best-effort.

    Writes from byte 1 onward; byte 0 is the lock sentinel and must not
    be touched after acquire (truncating it on Windows can interact
    badly with the active byte-range lock).
    """
    try:
        ident = f"{os.getpid()} {' '.join(sys.argv[:3])}".strip()
        ident_bytes = ident.encode("utf-8")
        lock_file.seek(_LOCK_SENTINEL_BYTES)
        lock_file.truncate(_LOCK_SENTINEL_BYTES + len(ident_bytes))
        lock_file.write(ident_bytes)
        lock_file.flush()
    except (OSError, UnicodeError):
        pass


@contextlib.contextmanager
def mine_palace_lock(palace_path: str):
    """Per-palace non-blocking lock around the full `mine` pipeline.

    The per-file `mine_lock` only protects delete+insert interleave for a
    single source; it does not prevent N copies of `mempalace mine <dir>`
    from being spawned concurrently by hooks. When that happens, each copy
    drives ChromaDB HNSW inserts in parallel against the same palace,
    which (combined with chromadb's multi-threaded ParallelFor) can
    corrupt the HNSW graph and produce sparse link_lists.bin blowups.

    The lock file is keyed by sha256(palace_path) so mines against
    *different* palaces can still run in parallel — we only serialize
    writes into the same palace, which is the correctness boundary.

    The key is derived from a fully normalized form of the path:
    `realpath` resolves symlinks and `..` segments, and `normcase` folds
    case on Windows (which has a case-insensitive filesystem). Without
    normcase, `C:\\Palace` and `c:\\palace` would hash to different keys
    on Windows and let two concurrent mines touch the same on-disk palace.

    Non-blocking: if another `mine` is already writing to this palace,
    raise MineAlreadyRunning so the caller can exit cleanly instead of
    piling up as a waiting worker.

    Re-entrant: if the current thread already holds the lock for the same
    palace, the context manager passes through without re-acquiring. This
    lets ChromaCollection write methods (which acquire the lock themselves
    to protect MCP/direct callers) compose with miner.mine() (which holds
    the outer lock for the entire mine pipeline) without self-deadlock.
    """
    lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    resolved = os.path.realpath(os.path.expanduser(palace_path))
    lock_key_source = os.path.normcase(resolved)
    palace_key = hashlib.sha256(lock_key_source.encode()).hexdigest()[:16]
    lock_path = os.path.join(lock_dir, f"mine_palace_{palace_key}.lock")

    if _held_by_this_thread(palace_key):
        # Same thread already holds the lock for this palace — pass through.
        yield
        return

    # Ensure the file exists, then open r+ so we can both read the prior
    # holder's identity (for failure diagnostics) and write our own. "w"
    # truncates and erases the prior holder. "a+" puts the position at EOF,
    # which on Windows breaks ``msvcrt.locking`` (it locks 1 byte at the
    # *current* position, so two contenders end up locking different bytes
    # and silently both acquire — observed as Windows-CI lock test
    # failures during #1264 development).
    if not os.path.exists(lock_path):
        # Touch atomically: O_CREAT|O_EXCL would fail if a concurrent
        # contender just created it, which is fine — we proceed to open.
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    lf = open(lock_path, "r+b")
    acquired = False
    try:
        # Lock byte 0 explicitly. msvcrt.locking is byte-position dependent;
        # fcntl.flock is whole-file but the seek is harmless there.
        lf.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError as exc:
                holder = _read_lock_holder(lf)
                raise MineAlreadyRunning(
                    f"palace {resolved} is held by {holder}; "
                    "wait for it to finish or stop the holder before retrying"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError as exc:
                holder = _read_lock_holder(lf)
                raise MineAlreadyRunning(
                    f"palace {resolved} is held by {holder}; "
                    "wait for it to finish or stop the holder before retrying"
                ) from exc
        # Record our own identity for any later contender's diagnostic message.
        _write_lock_holder(lf)
        _mark_held(palace_key)
        try:
            yield
        finally:
            _mark_released(palace_key)
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    # Match the lock region: byte 0.
                    lf.seek(0)
                    msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lf, fcntl.LOCK_UN)
            except Exception:
                pass
        lf.close()


# Backward-compatible alias (previous patch iteration used a single global
# lock). Kept so third-party callers that imported it continue to work; new
# code should use `mine_palace_lock(palace_path)` for per-palace scoping.
mine_global_lock = mine_palace_lock


def _metadata_matches_extract_mode(meta: dict, extract_mode: Optional[str]) -> bool:
    if extract_mode is None:
        return True
    stored_mode = meta.get("extract_mode")
    return stored_mode == extract_mode or (extract_mode == "exchange" and stored_mode is None)


def file_already_mined(
    collection,
    source_file: str,
    check_mtime: bool = False,
    extract_mode: Optional[str] = None,
) -> bool:
    """Check if a file has already been filed in the palace.

    Returns False (so the file gets re-mined) when:
      - no drawers exist for this source_file
      - the stored `normalize_version` is missing or older than the current
        schema (triggers silent rebuild after a normalization upgrade)
      - `check_mtime=True` and the file's mtime differs from the stored one

    When check_mtime=True (used by project miner), also re-mines on content
    change. When check_mtime=False (used by convo miner), transcripts are
    assumed immutable, so only the version gate triggers a rebuild.

    When extract_mode is set (used by convo miner), idempotency is scoped to
    that extraction mode so exchange-mode and general-mode drawers can coexist
    for the same source transcript. Legacy drawers without extract_mode are
    treated as exchange-mode drawers.
    """
    try:
        # Under the additive-mining model, a single ``source_file`` can have
        # multiple ``parent_drawer_id`` groups in the palace — one per
        # mining pass — each with its own stored ``source_mtime`` and
        # ``normalize_version``. The function must return True if ANY stored
        # group is current (matching version + matching mtime when checked),
        # because ChromaDB's ``get(..., limit=1)`` has undefined ordering
        # across multiple matching rows: a ``limit=1`` shortcut picks
        # whichever row ChromaDB orders first and only checks that one,
        # causing spurious re-mines whenever the stale group is returned.
        # Iterating via the same paginated pattern used in the
        # extract_mode-is-set branch lets the function short-circuit on the
        # first matching group regardless of ordering.
        current_mtime = os.path.getmtime(source_file) if check_mtime else None
        offset = 0
        while True:
            results = collection.get(
                where={"source_file": source_file},
                limit=1000,
                offset=offset,
                include=["metadatas"],
            )
            ids = results.get("ids") or []
            metadatas = results.get("metadatas") or []
            for meta in metadatas:
                meta = meta or {}
                # extract_mode scoping (was the existing ``else`` branch):
                if extract_mode is not None and not _metadata_matches_extract_mode(
                    meta, extract_mode
                ):
                    continue
                # Pre-v2 drawers have no version field — treat them as stale.
                stored_version = meta.get("normalize_version", 1)
                if stored_version < NORMALIZE_VERSION:
                    continue
                if not check_mtime:
                    return True
                stored_mtime = meta.get("source_mtime")
                if stored_mtime is None:
                    continue
                if abs(float(stored_mtime) - current_mtime) < 0.001:
                    return True
            if not ids:
                break
            offset += len(ids)
        return False
    except Exception:
        return False


def bulk_check_mined(collection) -> dict[str, float]:
    """Pre-fetch source_file/source_mtime pairs for all documents in the collection.

    Returns a dict mapping source_file -> source_mtime (as float) for every
    document that has both fields.  Callers can check membership and compare
    mtimes locally instead of issuing one ChromaDB query per file.

    Fetches the full collection in paginated batches (like palace_graph.py)
    since a WHERE-IN filter on thousands of paths is not supported by ChromaDB.
    """
    mined: dict[str, float] = {}
    try:
        total = collection.count()
        offset = 0
        while offset < total:
            batch = collection.get(limit=1000, offset=offset, include=["metadatas"])
            for meta in batch["metadatas"]:
                src = meta.get("source_file")
                mtime = meta.get("source_mtime")
                if src and mtime is not None:
                    mined[src] = float(mtime)
            if not batch["ids"]:
                break
            offset += len(batch["ids"])
    except Exception:
        logger.warning("bulk_check_mined: partial fetch, %d files loaded", len(mined))
    return mined


def prefetch_mined_set(collection, extract_mode: Optional[str] = None) -> set[str]:
    """Pre-fetch the set of source_files already mined at the current NORMALIZE_VERSION.

    Mirrors file_already_mined()'s version-gate semantics (check_mtime=False
    branch) but in one bulk pass instead of one ChromaDB query per file.
    Returns a set of source_file paths whose stored drawers are at or above
    NORMALIZE_VERSION; callers do `if path in result_set: skip`.

    When extract_mode is set, mirrors file_already_mined(..., extract_mode=...)
    so conversation mines skip per extraction mode rather than per source file.

    The convo miner walks thousands of transcript files; per-file
    `collection.get(where={"source_file": X})` costs ~2s on a 150k-drawer
    palace, making a 2000-file sweep take >1h of pure skip-checking. This
    helper drops that to a single paginated scan plus O(1) lookups.
    """
    mined: set[str] = set()
    try:
        total = collection.count()
        offset = 0
        while offset < total:
            batch = collection.get(limit=1000, offset=offset, include=["metadatas"])
            for meta in batch["metadatas"]:
                meta = meta or {}
                src = meta.get("source_file")
                if not src:
                    continue
                if not _metadata_matches_extract_mode(meta, extract_mode):
                    continue
                # Same default as file_already_mined: missing version == 1
                version = meta.get("normalize_version", 1)
                if version >= NORMALIZE_VERSION:
                    mined.add(src)
            if not batch["ids"]:
                break
            offset += len(batch["ids"])
    except Exception:
        logger.warning("prefetch_mined_set: partial fetch, %d files loaded", len(mined))
    return mined
