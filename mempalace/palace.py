"""
palace.py — Shared palace operations.

Consolidates collection access patterns used by both miners and the MCP server.
The embedding model is stored in collection metadata and read at open time.
"""

import contextlib
import hashlib
import logging
import os
import re

from .backends.chroma import ChromaBackend

from .config import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_COLLECTION_NAME,
    EmbeddingModelMismatchError,
    MempalaceConfig,
    get_embedding_function,
    get_embedding_model_name,
    read_collection_metadata,
)

logger = logging.getLogger("mempalace")

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

_DEFAULT_BACKEND = ChromaBackend()

# Schema version for drawer normalization. Bump when the normalization
# pipeline changes in a way that existing drawers should be rebuilt to pick up
# (e.g., new noise-stripping rules). `file_already_mined` treats drawers with
# a missing or stale `normalize_version` as "not mined", so the next mine pass
# silently rebuilds them — users don't need to manually erase + re-mine.
#
# v2 (2026-04): introduced strip_noise() for Claude Code JSONL; previous
#               drawers stored system tags / hook chrome verbatim.
NORMALIZE_VERSION = 2


def get_collection(
    palace_path: str,
    collection_name: str = "mempalace_drawers",
    create: bool = True,
    force: bool = False,
    model: str = None,
    chunk_size: int = None,
    chunk_overlap: int = None,
):
    """Get the palace collection through the backend layer.

    Reads the embedding model from collection metadata (set at init time).
    On first creation, stamps the model into metadata.
    Raises EmbeddingModelMismatchError on mismatch unless force=True.

    Args:
        model: Override model for creation (used by init/re-mine).
               If None, reads from existing collection metadata.
        chunk_size: Stored in metadata on creation.
        chunk_overlap: Stored in metadata on creation.
    """
    device = MempalaceConfig.detect_device()

    # Determine the target model:
    # 1. Explicit override (init/re-mine)
    # 2. Read from existing collection metadata
    # 3. Default
    existing_meta = read_collection_metadata(palace_path, collection_name)
    if model:
        current_model = model
    else:
        current_model = existing_meta.get("embedding_model")
        if current_model is None:
            # Secondary collections (closets, compressed) are created lazily
            # after the drawers collection and without an explicit --model.
            # Inherit the palace's canonical model (stamped on drawers at init)
            # so one palace never mixes embedding models. Without this they
            # silently fall back to the ChromaDB default while drawers use the
            # configured model, degrading non-English search on the index layer.
            if collection_name != DEFAULT_COLLECTION_NAME:
                current_model = get_embedding_model_name(palace_path)
            else:
                current_model = "chromadb-default"

    # Mismatch pre-check: surface our domain error BEFORE chromadb's internal
    # EF-conflict validator does. Without this, an explicit model override
    # that differs from the stamped model triggers chromadb's generic
    # `ValueError: An embedding function already exists ...` from inside
    # backend get_collection() — masking the actionable
    # EmbeddingModelMismatchError that tells the user to `re-mine --model`
    # or pass `--force`. Cheap to check (read_collection_metadata is one
    # SQLite query) and avoids loading the new model only to discard it.
    stored_model = existing_meta.get("embedding_model")
    if model and stored_model is not None and stored_model != current_model:
        if not force:
            raise EmbeddingModelMismatchError(stored_model, current_model)
        # Forced mismatch: open the collection with the STORED EF (chromadb
        # rejects any other), then re-stamp metadata to the requested
        # model. Caller (typically `re-mine --model`) is responsible for
        # the vector re-write that must follow — the metadata flip alone
        # leaves drawers embedded under the old model.
        stored_ef = get_embedding_function(model_name=stored_model, device=device)
        col = _DEFAULT_BACKEND.get_collection(
            palace_path,
            collection_name=collection_name,
            create=False,
            embedding_function=stored_ef,
        )
        logger.warning(
            "Embedding model mismatch (forced): %s -> %s",
            stored_model,
            current_model,
        )
        existing = col.metadata or {}
        non_hnsw = {k: v for k, v in existing.items() if not k.startswith("hnsw:")}
        forced_meta = {"embedding_model": current_model}
        if chunk_size is not None:
            forced_meta["chunk_size"] = chunk_size
        if chunk_overlap is not None:
            forced_meta["chunk_overlap"] = chunk_overlap
        col.modify(metadata={**non_hnsw, **forced_meta})
        return col

    ef = get_embedding_function(model_name=current_model, device=device)

    # Build metadata for creation
    create_meta = {"embedding_model": current_model}
    if chunk_size is not None:
        create_meta["chunk_size"] = chunk_size
    if chunk_overlap is not None:
        create_meta["chunk_overlap"] = chunk_overlap

    try:
        col = _DEFAULT_BACKEND.get_collection(
            palace_path,
            collection_name=collection_name,
            create=False,
            embedding_function=ef,
        )
        stored_model = (col.metadata or {}).get("embedding_model")

        if stored_model is None:
            # Legacy palace — stamp with current model.
            # ChromaDB rejects passing hnsw:* keys back through modify()
            # ("Changing the distance function ... is not supported currently"),
            # even when the value is unchanged. Filter them out — they live
            # at collection-creation time only.
            existing = col.metadata or {}
            non_hnsw = {k: v for k, v in existing.items() if not k.startswith("hnsw:")}
            col.modify(metadata={**non_hnsw, **create_meta})
        elif stored_model != current_model:
            if force:
                logger.warning(
                    "Embedding model mismatch (forced): %s -> %s",
                    stored_model, current_model,
                )
                existing = col.metadata or {}
                non_hnsw = {k: v for k, v in existing.items() if not k.startswith("hnsw:")}
                col.modify(metadata={**non_hnsw, **create_meta})
            else:
                raise EmbeddingModelMismatchError(stored_model, current_model)

        return col
    except EmbeddingModelMismatchError:
        raise
    except Exception:
        if not create:
            raise
        # Set defaults for chunk params on creation
        create_meta.setdefault("chunk_size", chunk_size or DEFAULT_CHUNK_SIZE)
        create_meta.setdefault("chunk_overlap", chunk_overlap or DEFAULT_CHUNK_OVERLAP)
        return _DEFAULT_BACKEND.get_collection(
            palace_path,
            collection_name=collection_name,
            create=True,
            embedding_function=ef,
            metadata=create_meta,
        )


def iter_all_metadatas(collection, where=None, page_size: int = 10000):
    """Yield every metadata entry in the collection, paginating past the page cap."""
    offset = 0
    while True:
        kwargs = {"include": ["metadatas"], "limit": page_size, "offset": offset}
        if where is not None:
            kwargs["where"] = where
        page = collection.get(**kwargs)
        metas = page.get("metadatas") or []
        if not metas:
            break
        for m in metas:
            yield m
        offset += len(metas)


def get_closets_collection(palace_path: str, create: bool = True):
    """Get the closets collection — the searchable index layer."""
    return get_collection(palace_path, collection_name="mempalace_closets", create=create)


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


def build_closet_lines(source_file, drawer_ids, content, wing, room):
    """Build compact closet pointer lines from drawer content.

    Returns a LIST of lines (not joined). Each line is one complete topic
    pointer — never split across closets.

    Format: topic|entities|→drawer_ids
    """
    import re
    from pathlib import Path

    drawer_ref = ",".join(drawer_ids[:3])
    window = content[:CLOSET_EXTRACT_WINDOW]

    # Extract proper nouns (2+ occurrences). Uses i18n-aware patterns so
    # non-Latin names (Cyrillic, accented Latin, etc.) are also detected.
    words = _candidate_entity_words(window)
    word_freq = {}
    for w in words:
        if w in _ENTITY_STOPLIST:
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

    # Build pointer lines — each one is atomic, never split
    lines = []
    for topic in topics:
        lines.append(f"{topic}|{entity_str}|→{drawer_ref}")
    for quote in quotes[:3]:
        lines.append(f'"{quote}"|{entity_str}|→{drawer_ref}')

    # Always have at least one line
    if not lines:
        name = Path(source_file).stem[:40]
        lines.append(f"{wing}/{room}/{name}|{entity_str}|→{drawer_ref}")

    return lines


def purge_file_closets(closets_col, source_file: str) -> None:
    """Delete every closet associated with ``source_file``.

    Call this before ``upsert_closet_lines`` on a re-mine so stale topics
    from a prior schema/version don't survive in the closet collection.
    Mirrors the drawer-purge step in process_file().
    """
    try:
        closets_col.delete(where={"source_file": source_file})
    except Exception:
        pass


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
    lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(
        lock_dir, hashlib.sha256(source_file.encode()).hexdigest()[:16] + ".lock"
    )

    lf = open(lock_path, "w")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass
        lf.close()


class MineAlreadyRunning(RuntimeError):
    """Raised when another `mempalace mine` already holds the per-palace lock."""


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
    """
    lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    resolved = os.path.realpath(os.path.expanduser(palace_path))
    lock_key_source = os.path.normcase(resolved)
    palace_key = hashlib.sha256(lock_key_source.encode()).hexdigest()[:16]
    lock_path = os.path.join(lock_dir, f"mine_palace_{palace_key}.lock")

    lf = open(lock_path, "w")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError as exc:
                raise MineAlreadyRunning(
                    f"another `mempalace mine` is already running against {resolved}"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError as exc:
                raise MineAlreadyRunning(
                    f"another `mempalace mine` is already running against {resolved}"
                ) from exc
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

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


def file_already_mined(collection, source_file: str, check_mtime: bool = False) -> bool:
    """Check if a file has already been filed in the palace.

    Returns False (so the file gets re-mined) when:
      - no drawers exist for this source_file
      - the stored `normalize_version` is missing or older than the current
        schema (triggers silent rebuild after a normalization upgrade)
      - `check_mtime=True` and the file's mtime differs from the stored one

    When check_mtime=True (used by project miner), also re-mines on content
    change. When check_mtime=False (used by convo miner), transcripts are
    assumed immutable, so only the version gate triggers a rebuild.
    """
    try:
        results = collection.get(where={"source_file": source_file}, limit=1)
        if not results.get("ids"):
            return False
        stored_meta = results.get("metadatas", [{}])[0] or {}
        # Pre-v2 drawers have no version field — treat them as stale.
        stored_version = stored_meta.get("normalize_version", 1)
        if stored_version < NORMALIZE_VERSION:
            return False
        if check_mtime:
            stored_mtime = stored_meta.get("source_mtime")
            if stored_mtime is None:
                return False
            current_mtime = os.path.getmtime(source_file)
            return abs(float(stored_mtime) - current_mtime) < 0.001
        return True
    except Exception:
        return False
