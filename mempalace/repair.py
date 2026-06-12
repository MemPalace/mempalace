"""
repair.py — Scan, prune corrupt entries, and rebuild HNSW index
================================================================

When ChromaDB's HNSW index accumulates duplicate entries (from repeated
add() calls with the same ID), link_lists.bin can grow unbounded —
terabytes on large palaces — eventually causing segfaults.

This module provides four operations:

  status  — compare sqlite vs HNSW element counts (read-only health check)
  scan    — find every corrupt/unfetchable ID in the palace
  prune   — delete only the corrupt IDs (surgical)
  rebuild — extract all drawers, delete the collection, recreate with
            correct HNSW settings, and upsert everything back

The rebuild backs up ONLY chroma.sqlite3 (the source of truth), not the
full palace directory — so it works even when link_lists.bin is bloated.

Usage (standalone):
    python -m mempalace.repair status
    python -m mempalace.repair scan [--wing X]
    python -m mempalace.repair prune --confirm
    python -m mempalace.repair rebuild

Usage (from CLI):
    mempalace repair
    mempalace repair-scan [--wing X]
    mempalace repair-prune --confirm
"""

import argparse
import contextlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from typing import Callable, Iterator, Optional

from chromadb.errors import NotFoundError as ChromaNotFoundError

from .backends.chroma import ChromaBackend, hnsw_capacity_status


COLLECTION_NAME = "mempalace_drawers"
REPAIR_TEMP_COLLECTION = f"{COLLECTION_NAME}__repair_tmp"

# The closets collection (AAAK index layer) is intentionally fixed —
# closets reference drawer IDs by string and live alongside drawers in the
# same palace; renaming the closets collection per-deployment would break
# cross-palace AAAK lookups. Drawer collection name comes from config
# (see ``_recoverable_collections``).
CLOSETS_COLLECTION_NAME = "mempalace_closets"


def _drawers_collection_name() -> str:
    """Resolve the drawers collection name from user config, falling back
    to the module default ``COLLECTION_NAME`` if config is unreadable.

    Recovery flows must honor ``MempalaceConfig().collection_name`` so a
    user with a non-default drawer collection (e.g. multi-palace setups)
    rebuilds the right rows. Closets remain fixed — see
    ``CLOSETS_COLLECTION_NAME``.
    """
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().collection_name or COLLECTION_NAME
    except Exception:
        return COLLECTION_NAME


def _sqlite_lock_holders(sqlite_path: str) -> list[str]:
    """Return best-effort process summaries currently holding sqlite_path open."""
    if not os.path.isfile(sqlite_path):
        return []
    if shutil.which("lsof") is None:
        return []
    try:
        completed = subprocess.run(
            ["lsof", sqlite_path],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) <= 1:
        return []
    holders: list[str] = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        holders.append(f"{parts[0]} pid={parts[1]} user={parts[2]}")
    return holders


def _recoverable_collections() -> tuple[str, ...]:
    """Collections rebuilt by ``rebuild_from_sqlite``, in upsert order.

    Drawers first (bulk data), then closets (AAAK index layer that
    references drawer IDs by string in their documents — no
    foreign-key validation, so ordering is informational, not
    load-bearing).
    """
    return (_drawers_collection_name(), CLOSETS_COLLECTION_NAME)


# Back-compat alias for callers that imported the constant. New code
# should call ``_recoverable_collections()`` so config changes are picked
# up at call time.
RECOVERABLE_COLLECTIONS = (COLLECTION_NAME, CLOSETS_COLLECTION_NAME)


def _get_palace_path():
    """Resolve palace path from config."""
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().palace_path
    except Exception:
        default = os.path.join(os.path.expanduser("~"), ".mempalace", "palace")
        return default


@contextlib.contextmanager
def _rebuild_write_locks(source_palace: str, dest_palace: str):
    """Acquire per-palace write locks for the full from-sqlite rebuild.

    Rebuild mutates ``dest_palace`` and, for in-place mode, renames the
    original palace before reading from the archive. Without taking the same
    ``mine_palace_lock`` used by miners/MCP writes, concurrent writers can
    interleave with repair and leave the destination immediately unhealthy.
    """
    from .palace import mine_palace_lock

    paths = sorted(
        {
            os.path.abspath(os.path.expanduser(source_palace)),
            os.path.abspath(os.path.expanduser(dest_palace)),
        }
    )
    with contextlib.ExitStack() as stack:
        for path in paths:
            stack.enter_context(mine_palace_lock(path))
        yield


def _paginate_ids(col, where=None):
    """Pull all IDs in a collection using pagination."""
    ids = []
    page = 1000
    offset = 0
    while True:
        try:
            r = col.get(where=where, include=[], limit=page, offset=offset)
        except Exception:
            try:
                r = col.get(where=where, include=[], limit=page)
                new_ids = [i for i in r["ids"] if i not in set(ids)]
                if not new_ids:
                    break
                ids.extend(new_ids)
                offset += len(new_ids)
                continue
            except Exception:
                break
        n = len(r["ids"]) if r["ids"] else 0
        if n == 0:
            break
        ids.extend(r["ids"])
        offset += n
        if n < page:
            break
    return ids


def _extract_drawers(col, total: int, batch_size: int):
    all_ids = []
    all_docs = []
    all_metas = []
    offset = 0
    while offset < total:
        batch = col.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        if not batch["ids"]:
            break
        all_ids.extend(batch["ids"])
        all_docs.extend(batch["documents"])
        # chromadb 1.5.x's upsert validates that every metadatas[i] is a
        # non-empty dict (chromadb/api/types.py:validate_metadata). Drawers
        # extracted from sqlite ground truth can come back with None or {}
        # for sparse historical writes — coerce those to a sentinel so the
        # rebuild upsert can complete instead of raising ValueError ~80%
        # through a multi-hour run. See #1458 for full context.
        sanitized_metas = [
            m if (isinstance(m, dict) and len(m) > 0) else {"_repaired_empty_meta": True}
            for m in batch["metadatas"]
        ]
        all_metas.extend(sanitized_metas)
        offset += len(batch["ids"])
    return all_ids, all_docs, all_metas


def _verify_collection_count(col, expected: int, label: str) -> None:
    actual = col.count()
    if actual != expected:
        raise RuntimeError(f"{label} count mismatch: expected {expected}, got {actual}")


def _is_missing_collection_value_error(exc: ValueError) -> bool:
    message = str(exc).lower()
    return "does not exist" in message or "not found" in message


def _delete_collection_if_exists(backend, palace_path: str, collection_name: str) -> None:
    try:
        backend.delete_collection(palace_path, collection_name)
    except ValueError as exc:
        if _is_missing_collection_value_error(exc):
            return
        raise
    except (FileNotFoundError, ChromaNotFoundError):
        return


class RebuildCollectionError(RuntimeError):
    """Raised when temp rebuild fails, carrying whether the live swap happened."""

    def __init__(self, message: str, *, live_replaced: bool):
        super().__init__(message)
        self.live_replaced = live_replaced


def _rebuild_collection_via_temp(
    backend,
    palace_path: str,
    all_ids,
    all_docs,
    all_metas,
    batch_size: int,
    collection_name: Optional[str] = None,
    progress=print,
) -> int:
    expected = len(all_ids)
    collection_name = collection_name or _drawers_collection_name()
    temp_name = f"{collection_name}__repair_tmp"
    live_replaced = False

    try:
        _delete_collection_if_exists(backend, palace_path, temp_name)

        progress(f"  Building temporary collection: {temp_name}")
        temp_col = backend.create_collection(palace_path, temp_name)
        staged = 0
        for i in range(0, expected, batch_size):
            batch_ids = all_ids[i : i + batch_size]
            batch_docs = all_docs[i : i + batch_size]
            batch_metas = all_metas[i : i + batch_size]
            temp_col.upsert(documents=batch_docs, ids=batch_ids, metadatas=batch_metas)
            staged += len(batch_ids)
            progress(f"  Staged {staged}/{expected} drawers...")
        _verify_collection_count(temp_col, expected, "temporary rebuild")

        progress("  Rebuilding live collection...")
        backend.delete_collection(palace_path, collection_name)
        live_replaced = True
        new_col = backend.create_collection(palace_path, collection_name)

        rebuilt = 0
        for i in range(0, expected, batch_size):
            batch_ids = all_ids[i : i + batch_size]
            batch_docs = all_docs[i : i + batch_size]
            batch_metas = all_metas[i : i + batch_size]
            new_col.upsert(documents=batch_docs, ids=batch_ids, metadatas=batch_metas)
            rebuilt += len(batch_ids)
            progress(f"  Re-filed {rebuilt}/{expected} drawers...")
        _verify_collection_count(new_col, expected, "rebuilt live collection")

        try:
            _delete_collection_if_exists(backend, palace_path, temp_name)
        except Exception:
            pass
        return rebuilt
    except Exception as exc:
        try:
            _delete_collection_if_exists(backend, palace_path, temp_name)
        except Exception:
            pass
        raise RebuildCollectionError(str(exc), live_replaced=live_replaced) from exc


def scan_palace(palace_path=None, only_wing=None, collection_name: Optional[str] = None):
    """Scan the palace for corrupt/unfetchable IDs.

    Probes in batches of 100, falls back to per-ID on failure.
    Writes corrupt_ids.txt to the palace directory for the prune step.

    Returns (good_set, bad_set).
    """
    palace_path = palace_path or _get_palace_path()
    collection_name = collection_name or _drawers_collection_name()
    print(f"\n  Palace: {palace_path}")
    print("  Loading...")

    col = ChromaBackend().get_collection(palace_path, collection_name)

    where = {"wing": only_wing} if only_wing else None
    total = col.count()
    print(f"  Collection: {collection_name}, total: {total:,}")
    if only_wing:
        print(f"  Scanning wing: {only_wing}")

    print("\n  Step 1: listing all IDs...")
    t0 = time.time()
    all_ids = _paginate_ids(col, where=where)
    print(f"  Found {len(all_ids):,} IDs in {time.time() - t0:.1f}s\n")

    if not all_ids:
        print("  Nothing to scan.")
        return set(), set()

    print("  Step 2: probing each ID (batches of 100)...")
    t0 = time.time()
    good_set = set()
    bad_set = set()
    batch = 100

    for i in range(0, len(all_ids), batch):
        chunk = all_ids[i : i + batch]
        try:
            r = col.get(ids=chunk, include=["documents"])
            for got in r["ids"]:
                good_set.add(got)
            for mid in chunk:
                if mid not in good_set:
                    bad_set.add(mid)
        except Exception:
            for sid in chunk:
                try:
                    r = col.get(ids=[sid], include=["documents"])
                    if r["ids"]:
                        good_set.add(sid)
                    else:
                        bad_set.add(sid)
                except Exception:
                    bad_set.add(sid)

        if (i // batch) % 50 == 0:
            elapsed = time.time() - t0
            rate = (i + batch) / max(elapsed, 0.01)
            eta = (len(all_ids) - i - batch) / max(rate, 0.01)
            print(
                f"    {i + batch:>6}/{len(all_ids):>6}  "
                f"good={len(good_set):>6}  bad={len(bad_set):>6}  "
                f"eta={eta:.0f}s"
            )

    print(f"\n  Scan complete in {time.time() - t0:.1f}s")
    print(f"  GOOD: {len(good_set):,}")
    print(f"  BAD:  {len(bad_set):,}  ({len(bad_set) / max(len(all_ids), 1) * 100:.1f}%)")

    bad_file = os.path.join(palace_path, "corrupt_ids.txt")
    with open(bad_file, "w") as f:
        for bid in sorted(bad_set):
            f.write(bid + "\n")
    print(f"\n  Bad IDs written to: {bad_file}")
    return good_set, bad_set


def prune_corrupt(palace_path=None, confirm=False, collection_name: Optional[str] = None):
    """Delete corrupt IDs listed in corrupt_ids.txt."""
    palace_path = palace_path or _get_palace_path()
    collection_name = collection_name or _drawers_collection_name()
    bad_file = os.path.join(palace_path, "corrupt_ids.txt")

    if not os.path.exists(bad_file):
        print("  No corrupt_ids.txt found — run scan first.")
        return

    with open(bad_file) as f:
        bad_ids = [line.strip() for line in f if line.strip()]
    print(f"  {len(bad_ids):,} corrupt IDs queued for deletion")

    if not confirm:
        print("\n  DRY RUN — no deletions performed.")
        print("  Re-run with --confirm to actually delete.")
        return

    col = ChromaBackend().get_collection(palace_path, collection_name)
    before = col.count()
    print(f"  Collection size before: {before:,}")

    batch = 100
    deleted = 0
    failed = 0
    for i in range(0, len(bad_ids), batch):
        chunk = bad_ids[i : i + batch]
        try:
            col.delete(ids=chunk)
            deleted += len(chunk)
        except Exception:
            for sid in chunk:
                try:
                    col.delete(ids=[sid])
                    deleted += 1
                except Exception:
                    failed += 1
        if (i // batch) % 20 == 0:
            print(f"    deleted {deleted}/{len(bad_ids)}  (failed: {failed})")

    after = col.count()
    print(f"\n  Deleted: {deleted:,}")
    print(f"  Failed:  {failed:,}")
    print(f"  Collection size: {before:,} → {after:,}")


# ChromaDB's ``collection.get()`` enforces an internal default ``limit``
# of 10 000 rows when the caller does not pass one. We pass an explicit
# ``limit=batch_size`` below, but the underlying segment also caps reads
# during stale/quarantined-HNSW recovery flows: extraction silently stops
# at exactly 10 000 even on palaces with many more rows. Refusing to
# overwrite when this exact value comes back is the simplest signal we
# can detect without depending on chromadb internals.
CHROMADB_DEFAULT_GET_LIMIT = 10_000


class TruncationDetected(Exception):
    """Raised by :func:`check_extraction_safety` when extraction looks short.

    Carries the human-readable abort message so callers (CLI ``cmd_repair``,
    ``rebuild_index``) can print and exit consistently without re-deriving
    the wording.
    """

    def __init__(self, message: str, sqlite_count: "int | None", extracted: int):
        super().__init__(message)
        self.message = message
        self.sqlite_count = sqlite_count
        self.extracted = extracted


def check_extraction_safety(
    palace_path: str,
    extracted: int,
    confirm_truncation_ok: bool = False,
    collection_name: Optional[str] = None,
) -> None:
    """Cross-check that ``extracted`` matches the SQLite ground truth.

    Two signals trip the guard:

    1. **Strong** — ``chroma.sqlite3`` reports more drawers than were
       extracted. This is the user-reported #1208 case: 67 580 on disk,
       10 000 came back through the chromadb collection layer, repair
       would have destroyed the difference.
    2. **Weak** — extracted count equals exactly ``CHROMADB_DEFAULT_GET_LIMIT``
       AND the SQLite check couldn't run (schema drift, locked file).
       Hitting the chromadb default ``get()`` cap exactly is suspicious
       enough to refuse without explicit acknowledgement.

    Raises :class:`TruncationDetected` with a printable message when the
    guard fires. Does nothing on safe extractions or when
    ``confirm_truncation_ok`` is set.
    """
    if confirm_truncation_ok:
        return

    collection_name = collection_name or _drawers_collection_name()
    sqlite_count = sqlite_drawer_count(palace_path, collection_name)
    cap_signal = extracted == CHROMADB_DEFAULT_GET_LIMIT

    if sqlite_count is not None and sqlite_count > extracted:
        loss = sqlite_count - extracted
        pct = 100 * loss / sqlite_count
        message = (
            f"\n  ABORT: chroma.sqlite3 reports {sqlite_count:,} drawers but only {extracted:,}\n"
            "  came back through the chromadb collection layer. The segment metadata is\n"
            "  stale (often after manual HNSW quarantine) — proceeding would silently\n"
            f"  destroy {loss:,} drawers (~{pct:.0f}%).\n"
            "\n"
            "  Recovery options:\n"
            "    1. Restore from your most recent palace backup, then re-mine.\n"
            "    2. Direct-extract from chroma.sqlite3 (rows are still on disk) and\n"
            "       rebuild the palace from source files.\n"
            "    3. If you have independently confirmed the palace really contains only\n"
            f"       {extracted:,} drawers, re-run with --confirm-truncation-ok.\n"
        )
        raise TruncationDetected(message, sqlite_count, extracted)

    if cap_signal and sqlite_count is None:
        message = (
            f"\n  ABORT: extracted exactly {CHROMADB_DEFAULT_GET_LIMIT:,} drawers, which matches\n"
            "  ChromaDB's internal default get() limit. The on-disk SQLite count couldn't\n"
            "  be cross-checked from this Python context, so we can't tell whether the\n"
            f"  palace genuinely holds {CHROMADB_DEFAULT_GET_LIMIT:,} rows or whether extraction was\n"
            "  silently capped. Refusing to overwrite the palace.\n"
            "\n"
            "  If you have independently confirmed (e.g. via direct sqlite3 query) that\n"
            f"  the palace really contains exactly {CHROMADB_DEFAULT_GET_LIMIT:,} drawers, re-run with\n"
            "  --confirm-truncation-ok.\n"
        )
        raise TruncationDetected(message, sqlite_count, extracted)


def sqlite_drawer_count(palace_path: str, collection_name: Optional[str] = None) -> "int | None":
    """Count rows in ``chroma.sqlite3.embeddings`` for the drawers collection.

    Used as an independent ground-truth check against the chromadb
    collection-layer ``count()`` / ``get()``: when the on-disk SQLite
    row count exceeds the extraction count, the segment metadata is
    stale and repair would destroy the difference.

    Returns ``None`` when the schema isn't readable (chromadb version
    drift, missing tables, locked file). Callers treat ``None`` as
    "unknown" and fall back to the cap-detection check.
    """
    collection_name = collection_name or _drawers_collection_name()
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.exists(sqlite_path):
        return None
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM embeddings e
                JOIN segments s ON e.segment_id = s.id
                JOIN collections c ON s.collection = c.id
                WHERE c.name = ?
                """,
                (collection_name,),
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()
    except Exception:
        # chromadb schema differs by version (segments / collections column
        # names occasionally rename). Silent fallback is correct here —
        # the cap-detection check still catches the user-reported case.
        return None


def sqlite_integrity_errors(palace_path: str) -> list[str]:
    """Return SQLite quick_check errors for chroma.sqlite3.

    The repair rebuild path eventually calls Chroma's delete_collection().
    If the SQLite layer has corrupt secondary indexes or FTS5 shadow pages,
    Chroma can raise an opaque SQLITE_CORRUPT_INDEX / code 779 error before
    repair reaches the HNSW rebuild.

    Run a direct SQLite quick_check first so repair can fail with a clear,
    actionable message before invoking Chroma's destructive collection-delete
    path.
    """

    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.exists(sqlite_path):
        return []

    try:
        with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as conn:
            rows = conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as e:
        return [f"PRAGMA quick_check failed: {e}"]

    errors: list[str] = []
    for row in rows:
        if not row:
            continue
        message = str(row[0])
        if message.lower() != "ok":
            errors.append(message)

    return errors


_FTS_MALFORMED_INDEX_ERROR = (
    "malformed inverted index for fts5 table main.embedding_fulltext_search"
)


def _is_only_fts_malformed_index(errors: list[str]) -> bool:
    if not errors:
        return False
    return all(_FTS_MALFORMED_INDEX_ERROR in str(e).lower() for e in errors)


def _is_btree_corruption(errors: list[str]) -> bool:
    """Return ``True`` when *errors* contains non-FTS B-tree-level corruption.

    B-tree errors include messages such as "2nd reference to page N", "wrong #
    of entries in index", "database disk image is malformed", and "row N
    missing from index". These cannot be fixed by an FTS rebuild or an HNSW
    rebuild — the only recovery path is
    ``mempalace repair --mode from-sqlite --archive-existing``.

    Returns ``False`` for an empty list or when every error is the known
    FTS-only malformed-index message (which *can* be fixed with a targeted FTS
    rebuild without involving from-sqlite).
    """
    if not errors:
        return False
    return not _is_only_fts_malformed_index(errors)


def validate_palace_sqlite(
    palace_path: str,
    *,
    set_sentinel: bool = True,
) -> list[str]:
    """Run ``PRAGMA quick_check`` and classify the result.

    This is the *expensive* validation path — suitable only for
    latency-tolerant entry points (CLI ``search``, ``status``, MCP server
    startup).  Do NOT call from the stop-hook or any path where < 500 ms
    latency is required.

    Behaviour by error class
    ~~~~~~~~~~~~~~~~~~~~~~~~
    * **Empty / "ok"** — palace is healthy.  Clears any stale corruption
      sentinel so a previously-flagged palace that has been restored does not
      remain locked.  Returns ``[]``.

    * **FTS-only malformed-index** — returns the errors without raising.
      The existing :func:`maybe_rebuild_fts_index_when_only_fts_corrupt`
      self-heal path handles this class; we should not block the caller.

    * **B-tree corruption (non-FTS errors)** — if *set_sentinel* is ``True``,
      atomically writes the ``.sqlite_corrupt`` sentinel file before raising so
      future cheap reads (stop-hook, MCP ``_get_client``) see the flag
      immediately.  Always raises :class:`PalaceSqliteCorruptError`.

    Parameters
    ----------
    palace_path:
        Absolute path to the palace directory.
    set_sentinel:
        Write the ``.sqlite_corrupt`` sentinel when B-tree corruption is
        found.  Set to ``False`` only in tests that need to inspect the
        raise without touching the filesystem.

    Returns
    -------
    list[str]
        Empty on a healthy palace or when only FTS errors are present.

    Raises
    ------
    PalaceSqliteCorruptError
        When ``PRAGMA quick_check`` reports B-tree corruption.
    """
    from .corruption_sentinel import clear_corruption_sentinel, write_corruption_sentinel

    errors = sqlite_integrity_errors(palace_path)

    if not errors:
        # Clean — clear any stale sentinel so a restored palace isn't locked.
        clear_corruption_sentinel(palace_path)
        return []

    if _is_only_fts_malformed_index(errors):
        # FTS-only: leave for the existing self-heal path.
        return errors

    # B-tree corruption.
    if set_sentinel:
        write_corruption_sentinel(palace_path, errors)
    raise PalaceSqliteCorruptError(palace_path, errors)


_probe_logger = logging.getLogger(__name__)


def probe_palace_loadable(palace_path: str, *, timeout: float = 120.0) -> list[str]:
    """Run the HNSW loadability probe in a subprocess and classify the result.

    Spawns ``python -m mempalace._loadprobe <palace_path>`` in a fresh
    interpreter so that a native SIGSEGV in the chromadb Rust bindings cannot
    kill the calling process.

    Parameters
    ----------
    palace_path:
        Absolute path to the palace directory to probe.
    timeout:
        Seconds to wait before giving up. Defaults to 120 s (generous, but
        ``collection.count()`` on a large palace can take tens of seconds).

    Returns
    -------
    list[str]
        Empty list when the palace loads cleanly.  A single-element list with
        a human-readable error string when the probe detects corruption,
        times out, or fails to spawn.  (Never raises.)
    """
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isdir(palace_path) or not os.path.isfile(sqlite_path):
        # Nothing to probe — palace absent or not yet initialised.
        return []

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "mempalace._loadprobe", palace_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [f"HNSW loadability probe timed out after {timeout}s"]
    except (FileNotFoundError, OSError) as exc:
        _probe_logger.warning("HNSW loadability probe could not be spawned: %s", exc)
        return []

    if completed.returncode == 0:
        return []

    if completed.returncode in (-11, 139):
        return [
            "HNSW segment unloadable: native crash (SIGSEGV) loading the vector segment"
            " — torn/corrupt binary segment not visible to PRAGMA quick_check"
        ]

    combined = (completed.stdout or "") + (completed.stderr or "")
    if "error loading hnsw index" in combined.lower():
        # Find the first line that mentions the error for a concise message.
        for line in combined.splitlines():
            if "error loading hnsw index" in line.lower():
                return [f"HNSW segment unloadable: {line.strip()}"]
        return ["HNSW segment unloadable: Error loading hnsw index (no detail)"]

    # Some other non-zero exit — surface the last stderr line.
    stderr_lines = [ln for ln in (completed.stderr or "").splitlines() if ln.strip()]
    last = stderr_lines[-1] if stderr_lines else "unknown"
    return [f"HNSW loadability probe failed (rc={completed.returncode}): {last}"]


def validate_palace_health(
    palace_path: str,
    *,
    set_sentinel: bool = True,
) -> list[str]:
    """Full latency-tolerant health check: SQLite integrity + HNSW loadability.

    This is the preferred entry point for latency-tolerant paths (CLI
    ``search``, ``status``, MCP server startup).  It supersedes the narrower
    :func:`validate_palace_sqlite` by also running the subprocess-isolated
    HNSW loadability probe when SQLite is clean.

    Do **not** call from the stop-hook or any path where < 500 ms latency is
    required — both the PRAGMA quick_check and the subprocess probe can take
    several seconds on large palaces.

    Behaviour
    ~~~~~~~~~
    1. Run ``PRAGMA quick_check`` via :func:`sqlite_integrity_errors`.

       * B-tree corruption → write sentinel + raise
         :class:`PalaceSqliteCorruptError` (probe is NOT called).
       * FTS-only errors → fall through to probe (FTS does not affect HNSW).
       * Clean → fall through to probe.

    2. Run :func:`probe_palace_loadable` in a subprocess.

       * Errors returned → write sentinel + raise
         :class:`PalaceSegmentUnloadableError`.
       * Clean → clear any stale sentinel, return ``[]``.

    Parameters
    ----------
    palace_path:
        Absolute path to the palace directory.
    set_sentinel:
        Write / clear the ``.sqlite_corrupt`` sentinel.  Set to ``False``
        only in tests that must not touch the filesystem.

    Returns
    -------
    list[str]
        Empty when the palace is healthy.

    Raises
    ------
    PalaceSqliteCorruptError
        When SQLite B-tree corruption is detected.
    PalaceSegmentUnloadableError
        When the HNSW binary segment fails to load.
    """
    from .corruption_sentinel import clear_corruption_sentinel, write_corruption_sentinel

    # ── Step 1: SQLite integrity ──────────────────────────────────────────────
    errors = sqlite_integrity_errors(palace_path)

    if errors and _is_btree_corruption(errors):
        # B-tree: stop here, do not run the probe.
        if set_sentinel:
            write_corruption_sentinel(palace_path, errors)
        raise PalaceSqliteCorruptError(palace_path, errors)

    # errors may contain FTS-only entries — those do not affect HNSW loadability.

    # ── Step 2: HNSW loadability probe ───────────────────────────────────────
    probe_errors = probe_palace_loadable(palace_path)
    if probe_errors:
        if set_sentinel:
            write_corruption_sentinel(palace_path, probe_errors)
        raise PalaceSegmentUnloadableError(palace_path, probe_errors)

    # Both checks passed — clear any stale sentinel.
    if set_sentinel:
        clear_corruption_sentinel(palace_path)

    return []


def maybe_rebuild_fts_index_when_only_fts_corrupt(
    palace_path: str,
    errors: list[str],
) -> list[str]:
    """Attempt targeted FTS5 rebuild when quick_check reports only FTS corruption.

    Returns the post-rebuild quick_check errors (empty on success), or the
    original ``errors`` when this path is not applicable or rebuild fails.
    """
    if not _is_only_fts_malformed_index(errors):
        # B-tree corruption: in-place repair cannot help.  Raise so
        # legacy/HNSW callers route the user to from-sqlite recovery
        # instead of dead-ending with an opaque Chroma panic.
        if _is_btree_corruption(errors):
            raise PalaceSqliteCorruptError(palace_path, errors)
        return errors

    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(sqlite_path):
        return errors

    from .palace import MineAlreadyRunning, mine_palace_lock

    print("\n  Detected isolated FTS5 malformed-index errors from quick_check.")
    print("  Attempting targeted FTS rebuild under palace lock...")

    try:
        with mine_palace_lock(palace_path):
            with sqlite3.connect(sqlite_path) as conn:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute(
                    "INSERT INTO embedding_fulltext_search(embedding_fulltext_search) "
                    "VALUES('rebuild')"
                )
                rows = conn.execute("PRAGMA quick_check").fetchall()
    except MineAlreadyRunning as exc:
        print(f"  FTS rebuild skipped: {exc}")
        return errors
    except sqlite3.Error as exc:
        print(f"  FTS rebuild failed: {exc}")
        return errors

    post_errors: list[str] = []
    for row in rows:
        if not row:
            continue
        message = str(row[0])
        if message.lower() != "ok":
            post_errors.append(message)

    if post_errors:
        print("  FTS rebuild completed but quick_check still reports issues.")
        return post_errors

    print("  FTS rebuild succeeded; quick_check is now healthy.")
    return []


def print_sqlite_integrity_abort(palace_path: str, errors: list[str]) -> None:
    """Print a clear repair abort message for SQLite-layer corruption."""

    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    preview = errors[:5]

    print("\n  ABORT: SQLite-layer corruption detected before repair rebuild.")
    print("  `mempalace repair` will not call Chroma delete_collection() because")
    print("  the SQLite database failed `PRAGMA quick_check`.")
    print()
    print(f"  Database: {sqlite_path}")
    print()
    print("  quick_check output:")
    for message in preview:
        print(f"    - {message}")
    if len(errors) > len(preview):
        print(f"    ... and {len(errors) - len(preview)} more issue(s)")
    print()
    print("  This often means derived SQLite structures, such as secondary indexes")
    print("  or FTS5 shadow tables, are corrupt while the underlying rows may still")
    print("  be recoverable.")
    print()
    print("  Suggested recovery:")
    print("    1. Stop all MemPalace writers / MCP clients.")
    print("    2. Back up the entire palace directory.")
    print("    3. Recover chroma.sqlite3 offline with sqlite3 `.recover` or `.dump`.")
    print("    4. Recreate the FTS5 virtual table from intact embedding_metadata rows.")
    print("    5. Verify `PRAGMA integrity_check` returns `ok`.")
    print("    6. Re-run `mempalace repair --yes`.")


def maybe_repair_poisoned_max_seq_id_before_rebuild(
    palace_path: str,
    *,
    backup: bool = True,
    dry_run: bool = False,
    assume_yes: bool = False,
) -> "dict | None":
    """Run non-destructive max_seq_id repair before a rebuild if needed.

    A poisoned ``max_seq_id`` row can make Chroma believe it has already
    consumed every row in ``embeddings_queue``. Writes then report success
    because they land in the queue, but they never become visible in
    ``embeddings``.

    If this precise corruption is present, do the narrow bookmark repair and
    stop instead of continuing into the legacy rebuild path. The rebuild path
    extracts only already-visible embeddings and can discard queued writes.
    """

    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return None

    try:
        poisoned = _detect_poisoned_max_seq_ids(db_path)
    except Exception:
        return None

    if not poisoned:
        return None

    print("\n  Detected poisoned max_seq_id rows before repair rebuild.")
    print(
        "  This can make writes report success while embeddings_queue grows "
        "and embeddings stay static."
    )
    print("  Running the non-destructive max_seq_id repair instead of rebuilding the collection.")
    print(
        "  Queued writes remain in chroma.sqlite3 for Chroma to drain after "
        "the bookmark is unpoisoned."
    )

    return repair_max_seq_id(
        palace_path,
        backup=backup,
        dry_run=dry_run,
        assume_yes=assume_yes,
    )


_PROGRESS_RE_STAGED = re.compile(r"Staged\s+(\d+)/(\d+)")
_PROGRESS_RE_REFILED = re.compile(r"Re-filed\s+(\d+)/(\d+)")


def _format_eta(seconds: float) -> str:
    """Pretty-print an ETA in the smallest reasonable unit."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


class _DefaultProgress:
    """Default ``progress`` callable for :func:`rebuild_index`.

    Behaves like ``print`` for non-progress lines. For ``"Staged N/M"`` /
    ``"Re-filed N/M"`` lines it appends elapsed/rate/ETA to give the
    operator a sense of how long the rebuild has left:

        Staged 5000/182953 drawers... (elapsed 7m, rate 11.3/s, ETA 4h)

    The clock resets at the stage→refile transition so the rate is
    accurate within each phase (refile re-embeds from scratch and runs
    at potentially different throughput than stage).
    """

    def __init__(self):
        self._start: Optional[float] = None
        self._phase: Optional[str] = None
        self._initial_completed: int = 0

    def __call__(self, msg) -> None:
        msg = str(msg)
        decorated = self._maybe_decorate(msg)
        print(decorated)

    def _maybe_decorate(self, msg: str) -> str:
        for pattern, phase in (
            (_PROGRESS_RE_STAGED, "stage"),
            (_PROGRESS_RE_REFILED, "refile"),
        ):
            m = pattern.search(msg)
            if m is None:
                continue
            completed = int(m.group(1))
            expected = int(m.group(2))
            return msg + self._eta_suffix(phase, completed, expected)
        return msg

    def _eta_suffix(self, phase: str, completed: int, expected: int) -> str:
        now = time.monotonic()
        # Reset clock + baseline at first call OR at phase transition,
        # so refile-phase rate isn't muddied by the slower stage phase.
        if self._phase != phase:
            self._phase = phase
            self._start = now
            self._initial_completed = completed
        elapsed = now - (self._start or now)
        done_this_phase = completed - self._initial_completed
        rate = done_this_phase / elapsed if elapsed > 0 and done_this_phase > 0 else 0.0
        remaining = max(0, expected - completed)
        if rate <= 0:
            return f" (elapsed {_format_eta(elapsed)})"
        eta = remaining / rate
        return f" (elapsed {_format_eta(elapsed)}, rate {rate:.1f}/s, ETA {_format_eta(eta)})"


def _vacuum_and_rebuild_fts5(palace_path: str, progress=print) -> None:
    """VACUUM the palace SQLite file and rebuild the FTS5 index if present.

    Repeated ``repair --yes`` runs delete and recreate the drawers collection,
    leaving freed SQLite pages unreclaimable without an explicit VACUUM.  The
    FTS5 virtual table (``embedding_fulltext_search``) can also become
    internally inconsistent after multiple collection deletes; the rebuild
    command fixes it atomically without touching any row data.

    Failures are non-fatal: a warning is printed and the caller continues.
    The repair itself succeeded at this point — VACUUM/FTS5 are best-effort
    cleanup, not correctness requirements.
    """
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.exists(sqlite_path):
        return
    try:
        with closing(sqlite3.connect(sqlite_path, isolation_level=None)) as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "embedding_fulltext_search" in tables:
                conn.execute(
                    "INSERT INTO embedding_fulltext_search"
                    "(embedding_fulltext_search) VALUES('rebuild')"
                )
                conn.commit()
                progress("  FTS5 index rebuilt.")
            conn.execute("VACUUM")
            progress("  SQLite VACUUM complete.")
    except Exception as exc:
        progress(f"  Warning: post-repair cleanup failed (non-fatal): {exc}")


def rebuild_index(
    palace_path=None,
    confirm_truncation_ok: bool = False,
    collection_name: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
):
    """Rebuild the HNSW index from scratch.

    1. Extract all drawers via ChromaDB get()
    2. Cross-check against the SQLite ground truth (#1208 guard)
    3. Back up ONLY chroma.sqlite3 (not the bloated HNSW files)
    4. Delete and recreate the collection with hnsw:space=cosine
    5. Upsert all drawers back

    ``confirm_truncation_ok`` overrides the safety guard from step 2.
    Set to ``True`` only when you have independently verified that the
    palace genuinely contains exactly the extracted number of drawers
    (typically only a concern for palaces sized at exactly 10 000 rows).

    ``progress`` is the callable used for status output. Defaults to
    :class:`_DefaultProgress` which prints with elapsed/rate/ETA
    annotations on ``Staged N/M`` and ``Re-filed N/M`` lines. Pass a
    custom callable (e.g. a daemon-side capture for HTTP status, or a
    silent ``lambda *_: None`` for tests) to override.
    """
    if progress is None:
        progress = _DefaultProgress()
    palace_path = palace_path or _get_palace_path()
    collection_name = collection_name or _drawers_collection_name()

    if not os.path.isdir(palace_path):
        progress(f"\n  No palace found at {palace_path}")
        return

    progress(f"\n{'=' * 55}")
    progress("  MemPalace Repair — Index Rebuild")
    progress(f"{'=' * 55}\n")
    progress(f" Palace: {palace_path}")

    # Run the SQLite integrity preflight before any chromadb client open.
    # ChromaDB's rust binding raises pyo3_runtime.PanicException (which is
    # not a regular Exception subclass) on a malformed page, propagating
    # past the try/except around get_collection below. Catching the
    # corruption here lets us surface the clear recovery instructions and
    # exit cleanly before chromadb's compactor touches the disk.
    sqlite_errors = sqlite_integrity_errors(palace_path)
    try:
        sqlite_errors = maybe_rebuild_fts_index_when_only_fts_corrupt(palace_path, sqlite_errors)
    except PalaceSqliteCorruptError as exc:
        print_sqlite_integrity_abort(palace_path, exc.errors)
        return
    if sqlite_errors:
        print_sqlite_integrity_abort(palace_path, sqlite_errors)
        return

    preflight = maybe_repair_poisoned_max_seq_id_before_rebuild(
        palace_path,
        assume_yes=True,
    )
    if preflight is not None:
        return

    backend = ChromaBackend()
    try:
        col = backend.get_collection(palace_path, collection_name)
        total = col.count()
    except Exception as e:
        progress(f"  Error reading palace: {e}")
        progress("  Palace may need to be re-mined from source files.")
        return

    progress(f"  Drawers found: {total}")

    if total == 0:
        progress("  Nothing to repair.")
        return

    # Extract all drawers in batches
    progress("\n  Extracting drawers...")
    batch_size = 5000
    all_ids, all_docs, all_metas = _extract_drawers(col, total, batch_size)
    progress(f"  Extracted {len(all_ids)} drawers")

    # ── #1208 guard ──────────────────────────────────────────────────
    # Refuse to ``delete_collection`` + rebuild when extraction looks
    # short of the SQLite ground truth (or when extraction == chromadb
    # default get() cap and the SQLite check couldn't run).
    try:
        check_extraction_safety(
            palace_path,
            len(all_ids),
            confirm_truncation_ok,
            collection_name=collection_name,
        )
    except TruncationDetected as e:
        progress(e.message)
        return

    # Back up ONLY the SQLite database, not the bloated HNSW files
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    backup_path = sqlite_path + ".backup"
    if os.path.exists(sqlite_path):
        progress(f"  Backing up chroma.sqlite3 ({os.path.getsize(sqlite_path) / 1e6:.0f} MB)...")
        shutil.copy2(sqlite_path, backup_path)
        progress(f"  Backup: {backup_path}")

    # Rebuild with correct HNSW settings
    progress("  Rebuilding collection with hnsw:space=cosine...")
    try:
        filed = _rebuild_collection_via_temp(
            backend,
            palace_path,
            all_ids,
            all_docs,
            all_metas,
            batch_size,
            collection_name=collection_name,
            progress=progress,
        )
    except RebuildCollectionError as e:
        progress(f"\n  ERROR during rebuild: {e}")
        progress("  Rebuild aborted before completion.")
        if e.live_replaced and os.path.exists(backup_path):
            progress(f"  Restoring from backup: {backup_path}")
            try:
                _close_chroma_handles(palace_path, backend=backend)
                _delete_collection_if_exists(backend, palace_path, collection_name)
                shutil.copy2(backup_path, sqlite_path)
                progress("  Backup restored. Palace is back to pre-repair state.")
            except Exception as restore_error:
                progress(f"  Backup restore failed: {restore_error}")
                progress(f"  Manual restore required from: {backup_path}")
        elif e.live_replaced:
            progress("  No backup available. Re-mine from source files to recover.")
        else:
            print("  Live collection was not replaced; leaving the original palace untouched.")
        raise

    _close_chroma_handles(palace_path, backend=backend)
    _vacuum_and_rebuild_fts5(palace_path, progress=progress)

    print(f"\n  Repair complete. {filed} drawers rebuilt.")
    print("  HNSW index is now clean with cosine distance metric.")
    print(f"\n{'=' * 55}\n")


class RebuildPartialError(Exception):
    """Raised when ``rebuild_from_sqlite`` fails partway through upserts.

    Carries enough state for the user (or CLI) to recover: the
    per-collection counts that succeeded, the collection that failed,
    the dest path holding the partial palace, and the archive path
    (when an in-place rebuild had moved the original aside). Re-raises
    the underlying chromadb error as ``__cause__``.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_counts: dict[str, int],
        failed_collection: str,
        dest_palace: str,
        archive_path: Optional[str],
    ):
        super().__init__(message)
        self.message = message
        self.partial_counts = partial_counts
        self.failed_collection = failed_collection
        self.dest_palace = dest_palace
        self.archive_path = archive_path


class PalaceCorruptError(Exception):
    """Base class for all palace-corruption errors.

    Catch this base to handle both SQLite B-tree corruption and torn HNSW
    binary segment errors uniformly (e.g. in CLI entry points and the MCP
    server open-guard).  The subclass carries the details.
    """


class PalaceSqliteCorruptError(PalaceCorruptError):
    """Raised when ``PRAGMA quick_check`` reveals B-tree-level corruption.

    In-place repair (HNSW rebuild, FTS rebuild) cannot fix B-tree corruption
    because the corruption lives in SQLite's on-disk page structures, not in
    derived indexes.  The only fully automated recovery path is to rebuild the
    palace from the verbatim documents still stored in the SQLite rows:

        mempalace repair --mode from-sqlite --archive-existing

    That command reads rows directly via SQL — bypassing HNSW and FTS — and
    writes a fresh, structurally clean palace.

    Attributes
    ----------
    palace_path:
        Absolute path to the corrupt palace directory.
    errors:
        Non-empty list of ``quick_check`` output lines describing the
        corruption.
    """

    def __init__(self, palace_path: str, errors: list[str]):
        self.palace_path = palace_path
        self.errors = errors
        super().__init__(str(self))

    def __str__(self) -> str:
        preview = "; ".join(self.errors[:3])
        return (
            f"SQLite B-tree corruption detected in palace at '{self.palace_path}': "
            f"{preview}. "
            "In-place repair cannot fix this. "
            "Run: mempalace repair --mode from-sqlite --archive-existing"
        )


class PalaceSegmentUnloadableError(PalaceCorruptError):
    """Raised when the HNSW binary segment cannot be loaded by ChromaDB.

    SQLite ``PRAGMA quick_check`` passes for this failure mode — the binary
    segment (``data_level0.bin``, ``link_lists.bin``, or
    ``index_metadata.pickle``) is torn or corrupt but that is invisible to
    SQLite.  The subprocess-isolated loadability probe detects it when
    ``collection.count()`` raises ``InternalError: Error loading hnsw index``
    or causes a native SIGSEGV in the chromadb Rust bindings.

    Recovery path (same as B-tree corruption — SQLite rows remain intact):

        mempalace repair --mode from-sqlite --archive-existing

    Attributes
    ----------
    palace_path:
        Absolute path to the palace with the unloadable segment.
    errors:
        List of error strings from the loadability probe.
    """

    def __init__(self, palace_path: str, errors: list[str]):
        self.palace_path = palace_path
        self.errors = errors
        super().__init__(str(self))

    def __str__(self) -> str:
        preview = "; ".join(self.errors[:3])
        return (
            f"HNSW segment unloadable in palace at '{self.palace_path}': "
            f"{preview}. "
            "SQLite is intact but the binary HNSW segment is torn/corrupt. "
            "Run: mempalace repair --mode from-sqlite --archive-existing"
        )


def _rebuild_one_collection(
    *,
    backend: ChromaBackend,
    source_palace: str,
    dest_palace: str,
    collection_name: str,
    batch_size: int,
    archive_path: Optional[str],
    counts_so_far: dict[str, int],
) -> int:
    """Stream rows for one collection from SQLite and upsert into a
    freshly-created collection at ``dest_palace``. Returns rows
    upserted. Raises :class:`RebuildPartialError` (with the underlying
    chromadb exception as ``__cause__``) on any upsert failure so the
    caller can stop the loop and print recovery instructions instead of
    silently shipping a partial palace.
    """
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    upserted = 0
    col = None

    def _flush() -> int:
        nonlocal upserted
        if not ids:
            return upserted
        col.upsert(ids=list(ids), documents=list(docs), metadatas=list(metas))
        upserted += len(ids)
        print(f"    upserted {upserted}")
        ids.clear()
        docs.clear()
        metas.clear()
        return upserted

    try:
        # ``create_collection`` lives inside the try so a Chroma-side
        # "Collection already exists" failure (which can happen when the
        # process-wide System cache still holds a pre-archive schema) is
        # reported as a structured ``RebuildPartialError`` carrying
        # ``archive_path`` — instead of an unstructured exception that
        # strands the user without recovery instructions.
        col = backend.create_collection(dest_palace, collection_name)

        for emb_id, doc, meta in extract_via_sqlite(source_palace, collection_name):
            ids.append(emb_id)
            docs.append(doc or "")
            # chromadb 1.5.x rejects both None and empty-dict entries in
            # the metadatas list (ValueError: Expected metadata to be a
            # non-empty dict). Mempalace drawers always carry at least
            # wing/room, so this branch is defensive — corruption in
            # embedding_metadata could yield an emb_id with no rows.
            # Coerce to a sentinel that satisfies validation and is
            # discoverable later via `where={"_repaired_empty_meta": True}`.
            metas.append(meta if (meta and len(meta) > 0) else {"_repaired_empty_meta": True})
            if len(ids) >= batch_size:
                _flush()
        _flush()
    except Exception as exc:  # noqa: BLE001 — chromadb raises many shapes
        partial = dict(counts_so_far)
        partial[collection_name] = upserted
        msg_parts = [
            f"Upsert failed in collection {collection_name!r} after {upserted} rows: {exc!r}",
            f"Partial palace left at: {dest_palace}",
        ]
        if archive_path is not None:
            msg_parts.append(f"Original palace archived at: {archive_path}")
            msg_parts.append(
                "  Recover by removing the partial dest and re-running with "
                f"--source {archive_path}"
            )
        else:
            msg_parts.append("  Source palace is unchanged. Remove the partial dest and re-run.")
        message = "\n  ".join(msg_parts)
        print(f"\n  ERROR: {message}")
        raise RebuildPartialError(
            message,
            partial_counts=partial,
            failed_collection=collection_name,
            dest_palace=dest_palace,
            archive_path=archive_path,
        ) from exc

    return upserted


def extract_via_sqlite(palace_path: str, collection_name: str) -> Iterator[tuple[str, str, dict]]:
    """Yield ``(embedding_id, document, metadata)`` for every row in
    ``collection_name``'s metadata segment by reading ``chroma.sqlite3``
    directly.

    Bypasses the chromadb client entirely — never opens a
    ``PersistentClient``, never imports hnswlib, never invokes the
    HNSW segment writer. This is the recovery path for palaces where
    ``Collection.count()`` / ``Collection.get()`` raise ``InternalError``
    because the compactor cannot apply WAL logs to the HNSW segment
    (#1308). The drawer rows are still on disk in
    ``embeddings`` + ``embedding_metadata``; the corruption lives in the
    on-disk index files, not the SQLite tables.

    Resolution rule for chromadb's typed metadata columns: each
    ``embedding_metadata`` row stores its value in exactly one of
    ``string_value`` / ``int_value`` / ``float_value`` / ``bool_value``;
    we pick the first non-NULL column in that order. Rows where every
    typed column is NULL are dropped (chromadb never writes that shape).
    The ``chroma:document`` key is removed from the metadata dict and
    returned as the document; this matches how chromadb itself stores
    ``add(documents=...)``.

    Silent on missing palace, missing ``chroma.sqlite3``, or unknown
    collection name — yields nothing. Callers that need to distinguish
    "empty collection" from "collection not present" should query
    :func:`sqlite_drawer_count` first.
    """
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(sqlite_path):
        return

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        seg_row = conn.execute(
            """
            SELECT s.id FROM segments s
            JOIN collections c ON s.collection = c.id
            WHERE c.name = ? AND s.scope = 'METADATA'
            """,
            (collection_name,),
        ).fetchone()
        if not seg_row:
            return
        segment_id = seg_row[0]

        per_id: dict[str, dict] = defaultdict(dict)
        order: list[str] = []
        for emb_id, key, sv, iv, fv, bv in conn.execute(
            """
            SELECT e.embedding_id, em.key, em.string_value, em.int_value,
                   em.float_value, em.bool_value
            FROM embedding_metadata em
            JOIN embeddings e ON em.id = e.id
            WHERE e.segment_id = ?
            ORDER BY em.id
            """,
            (segment_id,),
        ):
            if emb_id not in per_id:
                order.append(emb_id)
            if sv is not None:
                per_id[emb_id][key] = sv
            elif iv is not None:
                per_id[emb_id][key] = iv
            elif fv is not None:
                per_id[emb_id][key] = fv
            elif bv is not None:
                per_id[emb_id][key] = bool(bv)

        for emb_id in order:
            kv = per_id[emb_id]
            doc = kv.pop("chroma:document", "")
            yield emb_id, doc, kv
    finally:
        conn.close()


def rebuild_from_sqlite(
    source_palace: str,
    dest_palace: str,
    *,
    archive_existing_dest: bool = False,
    batch_size: int = 1000,
) -> dict[str, int]:
    """Rebuild a palace by reading drawers from ``source_palace``'s
    ``chroma.sqlite3`` and upserting them into a fresh palace at
    ``dest_palace``.

    Recovery path for the #1308 failure mode: the chromadb client raises
    ``InternalError: Failed to apply logs to the hnsw segment writer``
    on every operation that touches the index (``count``, ``get``,
    ``query``), but the underlying SQLite tables are intact. Both the
    legacy ``rebuild_index`` and the inline ``cli.cmd_repair`` path call
    ``Collection.count()`` as their first read — exactly the call that
    fails — so neither can recover this class of corruption. This
    function bypasses the chromadb read path entirely via
    :func:`extract_via_sqlite`.

    Re-embeds documents at upsert time using the configured embedding
    function; the original HNSW vectors are not preserved (they live in
    the corrupt ``data_level0.bin`` / ``link_lists.bin``, not in
    SQLite). Acceptable for a corruption-recovery flow because the
    embedding model is deterministic — same model + same document text
    yields semantically equivalent search results.

    ``archive_existing_dest`` controls behavior when ``dest_palace``
    already exists:

    * ``False`` (default) — refuse with a clear message. Callers must
      manually move the existing palace aside first.
    * ``True`` — rename ``dest_palace`` to
      ``<dest_palace>.pre-rebuild-<timestamp>`` and read from there
      instead. Used by the in-place CLI flow where ``--source`` defaults
      to the same path as ``--palace``.

    Returns a ``{collection_name: row_count}`` dict so callers (CLI,
    tests) can verify the per-collection rebuild count without parsing
    stdout. A successful rebuild always returns a dict with one key per
    recoverable collection (values may be ``0`` when a collection is
    legitimately empty in the source). The empty dict ``{}`` is reserved
    for validation refusals (missing source DB, refusing to overwrite an
    existing dest, in-place mode without ``archive_existing_dest``); CLI
    callers should treat ``{}`` as an error and exit non-zero so CI and
    scripts can distinguish "invalid inputs" from "successful recovery
    that found zero rows." Raises :class:`RebuildPartialError` if a
    chromadb upsert fails partway through; the dest palace is left in
    place so the user can inspect what landed, and the in-place archive
    (when applicable) is reported in the error so the user can re-run
    against it.

    .. warning::

       In-place mode (``source_palace == dest_palace`` with
       ``archive_existing_dest=True``) calls
       ``chromadb.api.client.SharedSystemClient.clear_system_cache()`` to
       drop chromadb's process-wide System registry — required because
       an existing cached System built against the original palace will
       refuse ``create_collection`` after the dir is renamed (chromadb
       still thinks the collections exist). This invalidates any
       PersistentClient instances held elsewhere in the same process for
       *any* palace, not just this one. Do not call this function from
       inside a long-running mempalace process (MCP server, daemon)
       while other callers hold live ``PersistentClient`` references —
       use the CLI in a separate process instead. Cross-palace use
       (``source != dest``) does not touch the cache.

    Note on metadata fidelity: the resolution rule
    (``string_value`` → ``int_value`` → ``float_value`` → ``bool_value``)
    matches the precedent in :mod:`mempalace.migrate`. ChromaDB 0.4.x
    occasionally wrote booleans as ``int_value=0/1``; those will
    round-trip as ``int`` rather than ``bool`` after this rebuild. This
    is a known divergence and matches the existing migrate-path
    behavior.
    """
    source_palace = os.path.abspath(os.path.expanduser(source_palace))
    dest_palace = os.path.abspath(os.path.expanduser(dest_palace))

    src_db = os.path.join(source_palace, "chroma.sqlite3")

    in_place = source_palace == dest_palace

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — Rebuild from SQLite")
    print(f"{'=' * 55}\n")
    print(f"  Source: {source_palace}")
    print(f"  Dest:   {dest_palace}")

    # Validate source BEFORE any destructive moves. An earlier draft
    # archived the dest first and surfaced the missing-chroma.sqlite3
    # error after — leaving the user with a renamed dir to manually undo
    # when the archive itself was empty. Validate first so a user error
    # (--source pointing at a non-palace dir) bails cleanly.
    if in_place:
        if not archive_existing_dest:
            print(
                "\n  Source and dest are the same path. Pass "
                "archive_existing_dest=True (CLI: --archive-existing) to move "
                "the existing palace aside, or pass a different source_palace= "
                "(CLI: --source)."
            )
            return {}
        if not os.path.isfile(src_db):
            print(f"\n  Source palace has no chroma.sqlite3 at {src_db}")
            return {}
    else:
        if not os.path.isfile(src_db):
            print(f"\n  Source palace has no chroma.sqlite3 at {src_db}")
            return {}
        if os.path.exists(dest_palace):
            print(
                f"\n  Refusing to rebuild into existing path: {dest_palace}\n"
                "  Move it aside, pass a different dest, or set "
                "archive_existing_dest=True if rebuilding in place "
                "(source_palace == dest_palace)."
            )
            return {}

    archive_path: Optional[str] = None
    with _rebuild_write_locks(source_palace, dest_palace):
        if in_place:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            archive_path = f"{dest_palace}.pre-rebuild-{ts}"
            print(f"  Archiving {dest_palace} → {archive_path}")
            shutil.move(dest_palace, archive_path)
            source_palace = archive_path
            src_db = os.path.join(source_palace, "chroma.sqlite3")

            # In-place only: drop chromadb's process-wide System registry so
            # the new client at dest_palace builds a fresh System. Without
            # this, ``create_collection`` raises "Collection already exists"
            # because the cached System still holds the pre-rename schema.
            # Cross-palace mode does not need this and would needlessly
            # invalidate other callers' clients (see docstring warning).
            try:
                from chromadb.api.client import SharedSystemClient

                SharedSystemClient.clear_system_cache()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"  Warning: could not clear chromadb system cache ({exc!r}); "
                    "in-place rebuild may fail with 'Collection already exists'."
                )

        os.makedirs(dest_palace, exist_ok=True)

        # Backend lifetime is wrapped in try/finally so the dest palace's
        # PersistentClient handle (opened lazily inside ``create_collection``
        # / ``get_collection``) is released on every exit path: success,
        # ``RebuildPartialError``, or any unexpected exception. Without this,
        # a long-running process that calls ``rebuild_from_sqlite`` would
        # leak SQLite/HNSW file handles into Chroma's ``SharedSystemClient``
        # cache, surfacing later as "Collection already exists" on the next
        # in-place rebuild or as a Windows file-lock failure on cleanup
        # (cf. #1285's lifecycle hardening for the legacy rebuild path).
        backend = ChromaBackend()
        counts: dict[str, int] = {}
        try:
            for cname in _recoverable_collections():
                print(f"\n  [{cname}]")
                upserted = _rebuild_one_collection(
                    backend=backend,
                    source_palace=source_palace,
                    dest_palace=dest_palace,
                    collection_name=cname,
                    batch_size=batch_size,
                    archive_path=archive_path,
                    counts_so_far=counts,
                )
                counts[cname] = upserted
                if upserted == 0:
                    print(f"    no rows found for {cname} in source palace")
                else:
                    print(f"    done: {upserted} rows in {cname}")

            print(f"\n  Rebuild complete. {sum(counts.values())} total rows.")
            if archive_path is not None:
                print(f"  Original palace archived at: {archive_path}")
            print(f"{'=' * 55}\n")
            # Clear any corruption sentinel on the destination (live) palace so
            # future open-guards and hook checks see a clean state.
            try:
                from .corruption_sentinel import clear_corruption_sentinel

                clear_corruption_sentinel(dest_palace)
            except Exception:  # noqa: BLE001
                pass
            return counts
        finally:
            backend.close()


def status(palace_path=None, collection_name: Optional[str] = None) -> dict:
    """Read-only health check: compare sqlite vs HNSW element counts.

    Catches the #1222 failure mode where chromadb's HNSW segment freezes
    at a stale ``max_elements`` while sqlite keeps accumulating rows.
    Once the divergence is large enough, every tool call segfaults when
    chromadb tries to load the undersized HNSW. Running ``mempalace
    repair-status`` *before* opening the segment lets the operator
    discover the problem without crashing the MCP server.

    The check itself never opens a chromadb client and never imports
    hnswlib — it reads ``chroma.sqlite3`` and ``index_metadata.pickle``
    directly via :func:`mempalace.backends.chroma.hnsw_capacity_status`.

    Returns the capacity-status dict (also printed). Returns a dict with
    ``status="unknown"`` when no palace exists at the given path.
    """
    palace_path = palace_path or _get_palace_path()
    collection_name = collection_name or _drawers_collection_name()
    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — Status")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")

    if not os.path.isdir(palace_path):
        print("  No palace found.\n")
        return {"status": "unknown", "message": "no palace at path"}
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    lock_holders = _sqlite_lock_holders(sqlite_path)
    if lock_holders:
        print("\n  [sqlite lock holders]")
        for holder in lock_holders[:10]:
            print(f"    {holder}")

    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        print(f"  Palace dir at {palace_path} exists but has no chroma.sqlite3 yet.\n")
        return {"status": "uninitialized", "message": "palace has no chroma.sqlite3 yet"}

    # Cheap collection-existence check via sqlite. By design this function
    # never opens a chromadb client (see the docstring); sqlite_drawer_count
    # reads chroma.sqlite3 directly and returns None on any schema/lock error
    # (chromadb version drift, missing tables, locked file). None means fall
    # through and let hnsw_capacity_status report "unknown".
    drawer_row_count = sqlite_drawer_count(palace_path, collection_name)
    if isinstance(drawer_row_count, int) and drawer_row_count == 0:
        print("  Palace is initialized but empty (no drawers yet).\n")
        return {"status": "empty", "message": "palace has no drawers yet"}

    drawers = hnsw_capacity_status(palace_path, collection_name)
    closets = hnsw_capacity_status(palace_path, CLOSETS_COLLECTION_NAME)

    for label, info in (("drawers", drawers), ("closets", closets)):
        print(f"\n  [{label}]")
        if info["sqlite_count"] is None:
            print("    sqlite count:   (unreadable)")
        else:
            print(f"    sqlite count:   {info['sqlite_count']:,}")
        if info["hnsw_count"] is None:
            print("    hnsw count:     (no flushed metadata yet)")
        else:
            print(f"    hnsw count:     {info['hnsw_count']:,}")
        if info["divergence"] is not None:
            print(f"    divergence:     {info['divergence']:,}")
        marker = "DIVERGED" if info["diverged"] else info["status"].upper()
        print(f"    status:         {marker}")
        if info["message"]:
            print(f"    note:           {info['message']}")

    if drawers["diverged"] or closets["diverged"]:
        print("\n  Recommended: run `mempalace repair` to rebuild the index.")
    print()
    return {"drawers": drawers, "closets": closets, "lock_holders": lock_holders}


# ---------------------------------------------------------------------------
# max-seq-id mode: un-poison max_seq_id rows corrupted by the old shim
# ---------------------------------------------------------------------------


def _close_chroma_handles(palace_path: str, backend: "ChromaBackend | None" = None) -> None:
    """Drop ChromaBackend + chromadb singleton caches so OS mmap handles release.

    When ``backend`` is provided, close the live instance so rollback/restore
    releases the handles it was already using. Otherwise fall back to a
    transient backend instance for the max-seq-id repair path.
    """
    import gc

    try:
        closer = backend if backend is not None else ChromaBackend()
        closer.close_palace(palace_path)
    except Exception:
        pass
    try:
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    except Exception:
        pass
    gc.collect()


class MaxSeqIdVerificationError(RuntimeError):
    """Raised when post-repair detection still sees poisoned rows."""


#: Any ``max_seq_id.seq_id`` above this is unreachable by a real palace.
#: Clean values are bounded by the embeddings_queue's monotonic counter (<1e10
#: in practice), and 2**53 is the float64 exact-integer ceiling. Poisoned
#: values from the 0.6.x shim misinterpreting chromadb 1.5.x's
#: ``b'\x11\x11' + 6 ASCII digits`` format start at ~1.23e18, so anything
#: above the threshold is confidently a shim-poisoning artefact.
MAX_SEQ_ID_SANITY_THRESHOLD = 1 << 53


def _detect_poisoned_max_seq_ids(
    db_path: str,
    *,
    segment: Optional[str] = None,
    threshold: int = MAX_SEQ_ID_SANITY_THRESHOLD,
) -> list[tuple[str, int]]:
    """Return ``[(segment_id, poisoned_seq_id), ...]`` for rows above threshold.

    If ``segment`` is given, the detection is restricted to that segment id
    (still only returning it if it actually exceeds the threshold).
    """
    with sqlite3.connect(db_path) as conn:
        if segment is not None:
            rows = conn.execute(
                "SELECT segment_id, seq_id FROM max_seq_id WHERE segment_id = ? AND seq_id > ?",
                (segment, threshold),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT segment_id, seq_id FROM max_seq_id WHERE seq_id > ?",
                (threshold,),
            ).fetchall()
    return [(str(sid), int(val)) for sid, val in rows]


def _compute_heuristic_seq_id(cur: sqlite3.Cursor, segment_id: str) -> int:
    """Return ``MAX(embeddings.seq_id)`` over the collection owning ``segment_id``.

    Matches the METADATA segment's pre-poison value exactly (its max equals
    the collection-wide embeddings max). For the sibling VECTOR segment the
    value is a few seq_ids ahead of its own pre-poison max; the queue
    treats that as "already consumed", skipping a small window of
    already-indexed embeddings on next subscribe. That is an acceptable
    loss vs. resetting to 0 (which would re-process the entire queue and
    risk HNSW bloat from issue #1046).

    ``embeddings.seq_id`` rows can be BLOB-typed on palaces where
    chromadb 1.5.x has been writing seq_ids natively (8-byte big-endian
    uint64). When SQLite's ``MAX`` returns such a row, decode it back to
    an integer rather than crashing on ``int(bytes)``.
    """
    row = cur.execute(
        """
        SELECT MAX(e.seq_id)
        FROM embeddings e
        JOIN segments s ON e.segment_id = s.id
        WHERE s.collection = (
            SELECT collection FROM segments WHERE id = ?
        )
        """,
        (segment_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    val = row[0]
    if isinstance(val, (bytes, bytearray)):
        return int.from_bytes(val, "big")
    return int(val)


def _read_sidecar_seq_ids(sidecar_path: str) -> dict[str, int]:
    """Load ``{segment_id: seq_id}`` from a sidecar DB's ``max_seq_id`` table.

    Rejects sidecar files whose ``max_seq_id.seq_id`` is itself BLOB-typed
    — a sidecar that old predates chromadb's type normalisation and is not
    a trustworthy restoration source.
    """
    if not os.path.isfile(sidecar_path):
        raise FileNotFoundError(f"Sidecar database not found: {sidecar_path}")
    out: dict[str, int] = {}
    with sqlite3.connect(sidecar_path) as conn:
        rows = conn.execute("SELECT segment_id, seq_id, typeof(seq_id) FROM max_seq_id").fetchall()
    for segment_id, seq_id, kind in rows:
        if kind == "blob":
            raise ValueError(
                f"Sidecar has BLOB-typed seq_id for {segment_id}; refusing to use it. "
                "Pass a sidecar that was already migrated to INTEGER rows."
            )
        out[str(segment_id)] = int(seq_id)
    return out


def repair_max_seq_id(
    palace_path: str,
    *,
    segment: Optional[str] = None,
    from_sidecar: Optional[str] = None,
    threshold: int = MAX_SEQ_ID_SANITY_THRESHOLD,
    backup: bool = True,
    dry_run: bool = False,
    assume_yes: bool = False,
) -> dict:
    """Un-poison ``max_seq_id`` rows corrupted by ``_fix_blob_seq_ids`` misfire.

    The old shim ran ``int.from_bytes(blob, 'big')`` across every BLOB
    ``max_seq_id.seq_id`` row, including chromadb 1.5.x's native
    ``b'\\x11\\x11' + ASCII digits`` format. That conversion yields a
    ~1.23e18 integer that silently suppresses every subsequent
    ``embeddings_queue`` write for the affected segment. This command
    restores clean values either from a pre-corruption sidecar DB
    (exact) or heuristically (``MAX(embeddings.seq_id)`` over the owning
    collection).
    """
    from .migrate import confirm_destructive_action, contains_palace_database

    palace_path = os.path.abspath(os.path.expanduser(palace_path))
    db_path = os.path.join(palace_path, "chroma.sqlite3")

    result: dict = {
        "palace_path": palace_path,
        "dry_run": dry_run,
        "aborted": False,
        "segment_repaired": [],
        "before": {},
        "after": {},
        "backup": None,
    }

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — max_seq_id Un-poison")
    print(f"{'=' * 55}\n")
    print(f"  Palace:  {palace_path}")
    if segment:
        print(f"  Segment: {segment}")
    if from_sidecar:
        print(f"  Sidecar: {from_sidecar}")

    if not os.path.isdir(palace_path):
        print(f"  No palace found at {palace_path}")
        result["aborted"] = True
        result["reason"] = "palace-missing"
        return result
    if not contains_palace_database(palace_path):
        print(f"  No palace database at {palace_path}")
        result["aborted"] = True
        result["reason"] = "db-missing"
        return result

    poisoned = _detect_poisoned_max_seq_ids(db_path, segment=segment, threshold=threshold)
    if not poisoned:
        print("  No poisoned max_seq_id rows detected. Nothing to do.")
        print(f"\n{'=' * 55}\n")
        return result

    sidecar_map: dict[str, int] = {}
    if from_sidecar:
        sidecar_map = _read_sidecar_seq_ids(from_sidecar)

    plan: list[tuple[str, int, int]] = []
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        for seg_id, old_val in poisoned:
            if from_sidecar:
                if seg_id not in sidecar_map:
                    print(f"  Skipped segment {seg_id}: no sidecar entry")
                    continue
                new_val = sidecar_map[seg_id]
            else:
                new_val = _compute_heuristic_seq_id(cur, seg_id)
            plan.append((seg_id, old_val, new_val))
            result["before"][seg_id] = old_val
            result["after"][seg_id] = new_val

    print()
    print("  Report")
    print(f"    poisoned rows        {len(poisoned):>6}")
    print(f"    planned repairs      {len(plan):>6}")
    source = "sidecar" if from_sidecar else "heuristic (collection MAX)"
    print(f"    clean-value source   {source}")
    for seg_id, old_val, new_val in plan:
        print(f"    {seg_id}  {old_val}  →  {new_val}")

    if dry_run:
        print("\n  DRY RUN — no rows modified.\n" + "=" * 55 + "\n")
        return result

    if not plan:
        print("  No actionable repairs.")
        print(f"\n{'=' * 55}\n")
        return result

    if not confirm_destructive_action("Repair max_seq_id", palace_path, assume_yes=assume_yes):
        result["aborted"] = True
        result["reason"] = "user-aborted"
        return result

    if backup:
        import glob

        from .backups import prune_backups
        from .config import MempalaceConfig

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(palace_path, f"chroma.sqlite3.max-seq-id-backup-{timestamp}")
        shutil.copy2(db_path, backup_path)
        result["backup"] = backup_path
        print(f"  Backup:  {backup_path}")

        # Retain only the most recent N backups (the copy just written is the
        # newest and is kept). Without this, every max-seq-id repair leaves a
        # full chroma.sqlite3 copy behind that is never cleaned up.
        prune_backups(
            os.path.join(glob.escape(palace_path), "chroma.sqlite3.max-seq-id-backup-*"),
            MempalaceConfig().max_backups,
            log=print,
        )

    _close_chroma_handles(palace_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("BEGIN")
        try:
            conn.executemany(
                "UPDATE max_seq_id SET seq_id = ? WHERE segment_id = ?",
                [(new_val, seg_id) for seg_id, _old, new_val in plan],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    remaining = _detect_poisoned_max_seq_ids(db_path, segment=segment, threshold=threshold)
    if remaining:
        raise MaxSeqIdVerificationError(
            f"Post-repair detection still found {len(remaining)} poisoned row(s): "
            f"{[sid for sid, _ in remaining]}. Backup at {result['backup']}."
        )

    result["segment_repaired"] = [seg_id for seg_id, _old, _new in plan]
    print(f"\n  Repair complete. {len(plan)} row(s) restored.")
    print(f"  Backup:  {result['backup'] or '(skipped)'}")
    print(f"\n{'=' * 55}\n")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="MemPalace repair tools")
    p.add_argument("command", choices=["status", "scan", "prune", "rebuild"])
    p.add_argument("--palace", default=None, help="Palace directory path")
    p.add_argument("--wing", default=None, help="Scan only this wing")
    p.add_argument("--confirm", action="store_true", help="Actually delete corrupt IDs")
    args = p.parse_args()

    path = os.path.expanduser(args.palace) if args.palace else None

    if args.command == "status":
        status(palace_path=path)
    elif args.command == "scan":
        scan_palace(palace_path=path, only_wing=args.wing)
    elif args.command == "prune":
        prune_corrupt(palace_path=path, confirm=args.confirm)
    elif args.command == "rebuild":
        rebuild_index(palace_path=path)
