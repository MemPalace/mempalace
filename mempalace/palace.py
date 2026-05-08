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

from .backends.chroma import ChromaBackend

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
    collection_name: Optional[str] = None,
    create: bool = True,
):
    """Get the palace collection through the backend layer."""
    if collection_name is None:
        from .config import get_configured_collection_name

        collection_name = get_configured_collection_name()
    return _DEFAULT_BACKEND.get_collection(
        palace_path,
        collection_name=collection_name,
        create=create,
    )


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
            logger.debug("Mine-lock release failed", exc_info=True)
        lf.close()


class MineAlreadyRunning(RuntimeError):
    """Raised when another `mempalace mine` already holds the per-palace lock."""


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
        content = lock_file.read().strip()
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
        lock_file.seek(_LOCK_SENTINEL_BYTES)
        lock_file.truncate(_LOCK_SENTINEL_BYTES + len(ident.encode("utf-8")))
        lock_file.write(ident)
        lock_file.flush()
    except OSError:
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
    lf = open(lock_path, "r+")
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
