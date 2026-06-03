"""
repair.py — Scan, prune corrupt entries, and rebuild HNSW index
================================================================

When ChromaDB's HNSW index accumulates duplicate entries (from repeated
add() calls with the same ID), link_lists.bin can grow unbounded —
terabytes on large palaces — eventually causing segfaults.

This module provides several operations:

  status       — compare sqlite vs HNSW element counts (read-only health check)
  scan         — find every corrupt/unfetchable ID in the palace
  prune        — delete only the corrupt IDs (surgical)
  rebuild      — extract all drawers, delete the collection, recreate with
                 correct HNSW settings, and upsert everything back
  hnsw-rebuild — segment-level HNSW rebuild from data_level0.bin +
                 index_metadata.pickle; avoids re-embedding, bounded memory,
                 atomic swap-aside with rollback. Productionises the
                 recovery path from the 2026-04-19 incident. Issue #1046.
  max-seq-id   — un-poison ``max_seq_id`` rows corrupted by the legacy 0.6.x
                 BLOB shim misreading chromadb 1.5.x's native format.

The rebuild backs up ONLY chroma.sqlite3 (the source of truth), not the
full palace directory — so it works even when link_lists.bin is bloated.

Usage (standalone):
    python -m mempalace.repair status
    python -m mempalace.repair scan [--wing X]
    python -m mempalace.repair prune --confirm
    python -m mempalace.repair rebuild
    python -m mempalace.repair hnsw --segment <uuid> [--dry-run] [--purge-queue] ...
    python -m mempalace.repair max-seq-id [--segment <uuid>] [--from-sidecar <path>]

Usage (from CLI):
    mempalace repair
    mempalace repair --mode hnsw --segment <uuid>
    mempalace repair --mode max-seq-id [--segment <uuid>] [--from-sidecar <path>]

The hnsw-rebuild path imports numpy and hnswlib lazily — they are not
core mempalace dependencies (per CONTRIBUTING.md). Install only when
running this rescue command.
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import pickle
import shutil
import sqlite3
import struct
import tempfile
import time
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Callable, Iterator, Optional

from chromadb.errors import NotFoundError as ChromaNotFoundError

from .backends.chroma import ChromaBackend, hnsw_capacity_status

logger = logging.getLogger(__name__)


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

_FLOAT32_SIZE = 4


def _get_palace_path():
    """Resolve palace path from config."""
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().palace_path
    except Exception:
        default = os.path.join(os.path.expanduser("~"), ".mempalace", "palace")
        return default


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
        if info.get("segment_id"):
            print(f"    segment id:     {info['segment_id']}")
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

    diverged_segments = [
        (label, info["segment_id"])
        for label, info in (("drawers", drawers), ("closets", closets))
        if info["diverged"] and info.get("segment_id")
    ]
    if drawers["diverged"] or closets["diverged"]:
        print("\n  Recommended next steps:")
        if diverged_segments:
            print(
                "    - Targeted segment rebuild (faster, no re-embed):"
                " `mempalace repair --mode hnsw --segment <uuid>`"
            )
            for label, seg_id in diverged_segments:
                print(f"        {label}: {seg_id}")
        print("    - Full-palace rebuild (re-embeds, slower): `mempalace repair`")
    print()
    return {"drawers": drawers, "closets": closets}


# ---------------------------------------------------------------------------
# hnsw-mode: segment-level rebuild (issue #1046)
# ---------------------------------------------------------------------------


class RebuildVerificationError(RuntimeError):
    """Raised when the rebuilt index fails its self-query sanity check."""


@dataclass
class _HnswHeader:
    """Subset of the chromadb-wrapped hnswlib header we rely on.

    Byte layout (little-endian) of the first 100 bytes:
      off  0:  u32  format_version
      off  4:  u64  offset_level0
      off 12:  u64  max_elements
      off 20:  u64  cur_count
      off 28:  u64  size_per_element
      off 36:  u64  label_offset
      off 44:  u64  offset_data
      off 52:  i32  maxlevel
      off 56:  u32  enterpoint_node
      off 60:  u64  maxM
      off 68:  u64  maxM0
      off 76:  u64  M
      off 84:  f64  mult
      off 92:  u64  ef_construction
    """

    format_version: int
    max_elements: int
    cur_count: int
    size_per_element: int
    label_offset: int
    offset_data: int
    M: int
    ef_construction: int
    dim: int


def _parse_hnsw_header(data: bytes) -> _HnswHeader:
    """Parse the 100-byte hnswlib header.

    Derives ``dim`` from ``size_per_element - offset_data - 8`` (trailing
    8 bytes are the u64 label), divided by 4 (float32).
    """
    if len(data) < 100:
        raise ValueError(f"HNSW header too short: {len(data)} bytes")

    format_version = struct.unpack_from("<I", data, 0)[0]
    max_elements = struct.unpack_from("<Q", data, 12)[0]
    cur_count = struct.unpack_from("<Q", data, 20)[0]
    size_per_element = struct.unpack_from("<Q", data, 28)[0]
    label_offset = struct.unpack_from("<Q", data, 36)[0]
    offset_data = struct.unpack_from("<Q", data, 44)[0]
    M = struct.unpack_from("<Q", data, 76)[0]
    ef_construction = struct.unpack_from("<Q", data, 92)[0]

    vector_bytes = size_per_element - offset_data - 8
    if vector_bytes <= 0 or vector_bytes % _FLOAT32_SIZE != 0:
        raise ValueError(
            f"Inferred vector width is invalid: "
            f"size_per_element={size_per_element}, offset_data={offset_data}"
        )
    dim = vector_bytes // _FLOAT32_SIZE

    return _HnswHeader(
        format_version=format_version,
        max_elements=max_elements,
        cur_count=cur_count,
        size_per_element=size_per_element,
        label_offset=label_offset,
        offset_data=offset_data,
        M=M,
        ef_construction=ef_construction,
        dim=dim,
    )


def _extract_vectors(data: bytes, hdr: _HnswHeader):
    """Return ``(labels, vectors)`` as numpy arrays of length ``cur_count``."""
    import numpy as np

    n = hdr.cur_count
    stride = hdr.size_per_element
    expected = n * stride
    if len(data) < expected:
        raise ValueError(f"data_level0.bin is {len(data)} bytes, expected >= {expected}")

    labels = np.empty(n, dtype=np.uint64)
    vectors = np.empty((n, hdr.dim), dtype=np.float32)
    vec_end = hdr.offset_data + hdr.dim * _FLOAT32_SIZE
    for i in range(n):
        slot = i * stride
        vectors[i] = np.frombuffer(data[slot + hdr.offset_data : slot + vec_end], dtype=np.float32)
        labels[i] = struct.unpack_from("<Q", data, slot + hdr.label_offset)[0]
    return labels, vectors


def _sanitize_vectors(labels, vectors):
    """Drop ``label == 0`` and deduplicate, keeping the last occurrence.

    hnswlib rejects duplicate labels; drift sometimes leaves zero-labelled
    slots. Reverse-unique preserves the most-recent write for each label,
    which is the copy that matches the pickle mapping.
    """
    import numpy as np

    if len(labels) == 0:
        return labels, vectors

    n = len(labels)
    rev_idx = np.arange(n)[::-1]
    _, first_rev = np.unique(labels[::-1], return_index=True)
    keep = np.sort(rev_idx[first_rev])
    labels = labels[keep]
    vectors = vectors[keep]

    zero_mask = labels == 0
    if zero_mask.any():
        labels = labels[~zero_mask]
        vectors = vectors[~zero_mask]
    return labels, vectors


def _detect_space(palace_path: str, segment: str) -> str:
    """Look up ``hnsw:space`` for the collection that owns ``segment``.

    Falls back to ``"l2"`` (hnswlib's default) with a warning if absent —
    matches ChromaDB's default when no metadata is recorded.
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        logger.warning("No chroma.sqlite3 at %s — defaulting space to l2", palace_path)
        return "l2"

    row = None
    with sqlite3.connect(db_path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(collection_metadata)").fetchall()]
        value_col = "str_value" if "str_value" in cols else "string_value"
        try:
            row = conn.execute(
                f"""
                SELECT cm.{value_col}
                FROM segments s
                JOIN collection_metadata cm ON cm.collection_id = s.collection
                WHERE s.id = ? AND cm.key = 'hnsw:space'
                """,
                (segment,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None

    if row and row[0]:
        return row[0]
    logger.warning("No hnsw:space for segment %s — defaulting to l2 (ChromaDB default)", segment)
    return "l2"


def _meta_get(meta, key):
    """Read a field from index_metadata (0.6.x attr-object or 1.5.x dict)."""
    return meta[key] if isinstance(meta, dict) else getattr(meta, key)


def _meta_set(meta, key, value):
    """Write a field to index_metadata (0.6.x attr-object or 1.5.x dict)."""
    if isinstance(meta, dict):
        meta[key] = value
    else:
        setattr(meta, key, value)


def _reconcile_with_pickle(labels, pickle_path: str):
    """Intersect HNSW labels with the pickle's ``label_to_id`` mapping.

    Returns ``(keep_mask, orphan_hnsw_labels, stale_pickle_ids, meta)``
    where ``meta`` has had its three mapping tables pruned to the healthy
    set (caller persists it afterwards).
    """
    import numpy as np

    with open(pickle_path, "rb") as f:
        meta = pickle.load(f)

    label_to_id = _meta_get(meta, "label_to_id")
    id_to_label = _meta_get(meta, "id_to_label")
    id_to_seq_id = _meta_get(meta, "id_to_seq_id")

    mapped_labels = set(label_to_id.keys())
    hnsw_labels = set(int(x) for x in labels)
    healthy = hnsw_labels & mapped_labels
    orphan_hnsw = hnsw_labels - mapped_labels
    stale_uids = [uid for lbl, uid in label_to_id.items() if lbl not in healthy]
    dropped_uid_set = set(stale_uids)

    _meta_set(
        meta,
        "label_to_id",
        {lbl: uid for lbl, uid in label_to_id.items() if lbl in healthy},
    )
    _meta_set(
        meta,
        "id_to_label",
        {uid: lbl for uid, lbl in id_to_label.items() if uid not in dropped_uid_set},
    )
    _meta_set(
        meta,
        "id_to_seq_id",
        {uid: sid for uid, sid in id_to_seq_id.items() if uid not in dropped_uid_set},
    )

    keep_mask = np.fromiter((int(x) in healthy for x in labels), dtype=bool, count=len(labels))
    return keep_mask, sorted(orphan_hnsw), stale_uids, meta


def _compute_max_elements(count: int, override: Optional[int]) -> int:
    """Pick the ``max_elements`` value for the new index.

    Default ``max(count * 1.3, 200_000)`` leaves headroom so the next
    flush does not auto-resize (the very bug #2594 we are fixing).
    """
    if override is not None:
        if override < count:
            raise ValueError(
                f"--max-elements={override} is smaller than healthy vector count {count}"
            )
        return int(override)
    return max(int(count * 1.3), 200_000)


def _build_persistent_index(
    vectors,
    labels,
    *,
    space: str,
    dim: int,
    max_elements: int,
    persistence_location: str,
    M: int = 16,
    ef_construction: int = 100,
):
    """Build a persistent hnswlib index and write it to ``persistence_location``."""
    import hnswlib

    idx = hnswlib.Index(space=space, dim=dim)
    idx.init_index(
        max_elements=max_elements,
        ef_construction=ef_construction,
        M=M,
        is_persistent_index=True,
        persistence_location=persistence_location,
    )
    idx.set_num_threads(1)

    n = len(labels)
    chunk = 10_000
    for i in range(0, n, chunk):
        j = min(i + chunk, n)
        idx.add_items(vectors[i:j], labels[i:j], num_threads=1)
    idx.persist_dirty()
    return idx


def _self_query_verify(index, sample_vectors, sample_labels, k: int = 10) -> None:
    """Verify the rebuilt index returns each sample within its own top-k neighbors.

    Looser than top-1 to tolerate near-duplicates — mined corpora regularly contain
    drawers with byte-identical vectors (e.g. the same code snippet appearing in
    multiple transcripts), so an exact top-1 check false-positives on corpora that
    are actually indexed correctly.

    Bumps ``ef`` before querying: hnswlib's default (≈10) is too tight a search
    beam for a ~500k-element index with ``M=16`` and can miss even a byte-identical
    self-match because its neighborhood in the HNSW graph is sparse. ChromaDB sets
    its own ``ef`` at query time, so this only affects the verify step.
    """
    if len(sample_labels) == 0:
        return
    index.set_ef(max(200, k * 4))
    labels, _dists = index.knn_query(sample_vectors, k=k)
    expected = [int(x) for x in sample_labels]
    misses = []
    for i, exp in enumerate(expected):
        row = [int(x) for x in labels[i]]
        if exp not in row:
            misses.append((exp, row))
    if misses:
        raise RebuildVerificationError(
            f"Self-query mismatch (top-{k}): {len(misses)}/{len(expected)} samples "
            f"did not include their own label — first miss: expected {misses[0][0]}, "
            f"got {misses[0][1]}"
        )


def _atomic_swap_segment(tmpdir: str, segment_dir: str) -> None:
    """Rename-aside swap: move live out of the way, drop new in, rollback on failure."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stale = f"{segment_dir}.old-{stamp}"
    os.rename(segment_dir, stale)
    try:
        os.replace(tmpdir, segment_dir)
    except OSError:
        try:
            os.rename(stale, segment_dir)
        except OSError:
            logger.exception("Swap failed AND rollback failed. Live segment left at %s", stale)
        raise
    shutil.rmtree(stale, ignore_errors=True)


def _backup_segment(palace_path: str, segment: str, timestamp: str) -> str:
    """Copy chroma.sqlite3 plus the small HNSW files (skip bloated link_lists.bin)."""
    seg_dir = os.path.join(palace_path, segment)
    backup_dir = os.path.join(palace_path, f"{segment}.hnsw-backup-{timestamp}")
    os.makedirs(backup_dir, exist_ok=True)

    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if os.path.isfile(sqlite_path):
        shutil.copy2(sqlite_path, os.path.join(backup_dir, "chroma.sqlite3"))

    for fname in ("header.bin", "data_level0.bin", "index_metadata.pickle", "length.bin"):
        src = os.path.join(seg_dir, fname)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(backup_dir, fname))
    return backup_dir


def _purge_segment_queue(palace_path: str, segment: str) -> int:
    """Delete ``embeddings_queue`` rows for the collection that owns ``segment``.

    ``topic`` is ``persistent://default/default/<COLLECTION_UUID>`` — we look
    up the collection UUID via ``segments.collection`` and match by pattern.
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT collection FROM segments WHERE id = ?", (segment,)).fetchone()
        if not row:
            return 0
        collection_uuid = row[0]
        cur = conn.execute(
            "DELETE FROM embeddings_queue WHERE topic LIKE ?",
            (f"%{collection_uuid}%",),
        )
        deleted = cur.rowcount
        conn.commit()
    return int(deleted or 0)


def _quarantine_orphans(palace_path: str, stale_ids, orphan_labels) -> str:
    """Append dropped UUIDs + orphan HNSW labels to a sidecar JSON file."""
    sidecar = os.path.join(palace_path, "quarantined_orphans.json")
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "stale_pickle_ids": list(stale_ids),
        "orphan_hnsw_labels": [int(x) for x in orphan_labels],
    }
    history: list = []
    if os.path.isfile(sidecar):
        try:
            with open(sidecar) as f:
                history = json.load(f)
            if not isinstance(history, list):
                history = [history]
        except Exception:
            history = []
    history.append(entry)
    with open(sidecar, "w") as f:
        json.dump(history, f, indent=2)
    return sidecar


# ---------------------------------------------------------------------------
# max-seq-id mode: un-poison max_seq_id rows corrupted by the old shim
# ---------------------------------------------------------------------------


def _close_chroma_handles(palace_path: str, backend: "ChromaBackend | None" = None) -> None:
    """Drop ChromaBackend + chromadb singleton caches so OS mmap handles release.

    When ``backend`` is provided, close the live instance so rollback/restore
    releases the handles it was already using. Otherwise fall back to a
    transient backend instance for the max-seq-id repair path.
    """

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
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(palace_path, f"chroma.sqlite3.max-seq-id-backup-{timestamp}")
        shutil.copy2(db_path, backup_path)
        result["backup"] = backup_path
        print(f"  Backup:  {backup_path}")

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


# ---------------------------------------------------------------------------
# hnsw-mode driver: rebuild a single segment from data_level0.bin (issue #1046)
# ---------------------------------------------------------------------------


def _peak_memory_mb() -> float:
    """Return the process peak RSS in MB (mac returns bytes, linux kilobytes)."""
    try:
        import resource
        import sys

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return peak / (1024 * 1024)
        return peak / 1024
    except Exception:
        return 0.0


def rebuild_hnsw_segment(
    palace_path: str,
    *,
    segment: str,
    max_elements: Optional[int] = None,
    backup: bool = True,
    purge_queue: bool = False,
    quarantine_orphans: bool = False,
    dry_run: bool = False,
    assume_yes: bool = False,
) -> dict:
    """Rebuild a single HNSW segment from on-disk ``data_level0.bin`` + pickle.

    Avoids re-embedding by reading vectors straight out of the persistent
    HNSW data file. Atomic swap-aside with rollback keeps the live palace
    untouched on any failure. Issue #1046.

    On successful completion the palace is healthy on disk; if the running
    MCP server still has ``_vector_disabled`` set from a prior #1222 capacity
    probe, calling ``mempalace_reconnect`` will refresh the probe and clear
    the flag — the runtime check is the authoritative source of truth and
    re-runs at every reconnect.
    """
    from .migrate import confirm_destructive_action, contains_palace_database

    palace_path = os.path.abspath(os.path.expanduser(palace_path))
    seg_dir = os.path.join(palace_path, segment)
    header_path = os.path.join(seg_dir, "header.bin")
    data_path = os.path.join(seg_dir, "data_level0.bin")
    pickle_path = os.path.join(seg_dir, "index_metadata.pickle")

    result: dict = {
        "palace_path": palace_path,
        "segment": segment,
        "dry_run": dry_run,
        "aborted": False,
    }

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — HNSW Segment Rebuild")
    print(f"{'=' * 55}\n")
    print(f"  Palace:  {palace_path}")
    print(f"  Segment: {segment}")

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
    if not os.path.isdir(seg_dir):
        print(f"  Segment directory not found: {seg_dir}")
        result["aborted"] = True
        result["reason"] = "segment-missing"
        return result
    if not os.path.isfile(data_path):
        print(f"  data_level0.bin not found in {seg_dir}")
        result["aborted"] = True
        result["reason"] = "data-missing"
        return result

    try:
        import numpy  # noqa: F401
        import hnswlib  # noqa: F401
    except ImportError as e:
        print(f"  Required dependency missing: {e}")
        print("  Install with: pip install numpy chroma-hnswlib")
        result["aborted"] = True
        result["reason"] = "deps-missing"
        return result

    header_src = header_path if os.path.isfile(header_path) else data_path
    with open(header_src, "rb") as f:
        header_bytes = f.read(100)
    hdr = _parse_hnsw_header(header_bytes)
    print(
        f"  Header:  dim={hdr.dim}, cur_count={hdr.cur_count:,}, "
        f"max_elements={hdr.max_elements:,}, size_per_element={hdr.size_per_element}"
    )

    space = _detect_space(palace_path, segment)
    print(f"  Space:   {space}")

    with open(data_path, "rb") as f:
        data_bytes = f.read()
    labels, vectors = _extract_vectors(data_bytes, hdr)
    del data_bytes
    raw_n = len(labels)
    labels, vectors = _sanitize_vectors(labels, vectors)
    sanitized_n = len(labels)

    orphan_hnsw: list = []
    stale_uids: list = []
    meta = None
    if os.path.isfile(pickle_path):
        keep_mask, orphan_hnsw, stale_uids, meta = _reconcile_with_pickle(labels, pickle_path)
        labels = labels[keep_mask]
        vectors = vectors[keep_mask]
    else:
        logger.warning("No index_metadata.pickle for segment %s — skipping reconcile", segment)

    healthy_n = len(labels)
    new_max = _compute_max_elements(healthy_n, max_elements)

    data_bytes_size = os.path.getsize(data_path)
    link_lists_path = os.path.join(seg_dir, "link_lists.bin")
    link_lists_size = os.path.getsize(link_lists_path) if os.path.isfile(link_lists_path) else 0

    print()
    print("  Report")
    print(f"    raw labels          {raw_n:>10,}")
    print(f"    after dedup/zeros   {sanitized_n:>10,}")
    print(f"    healthy (in pickle) {healthy_n:>10,}")
    print(f"    orphan HNSW labels  {len(orphan_hnsw):>10,}")
    print(f"    stale pickle ids    {len(stale_uids):>10,}")
    print(f"    new max_elements    {new_max:>10,}")
    print(f"    data_level0.bin     {data_bytes_size:>10,} bytes")
    print(f"    link_lists.bin      {link_lists_size:>10,} bytes (will be rebuilt)")

    result.update(
        {
            "raw_labels": raw_n,
            "sanitized_labels": sanitized_n,
            "healthy_labels": healthy_n,
            "orphan_hnsw_labels": len(orphan_hnsw),
            "stale_pickle_ids": len(stale_uids),
            "max_elements": new_max,
            "data_bytes": data_bytes_size,
            "link_lists_bytes": link_lists_size,
            "space": space,
            "dim": hdr.dim,
        }
    )

    if dry_run:
        print("\n  DRY RUN — no files modified.\n" + "=" * 55 + "\n")
        return result

    if healthy_n == 0:
        print("  No healthy labels to rebuild — aborting.")
        result["aborted"] = True
        result["reason"] = "no-healthy-labels"
        return result

    if not confirm_destructive_action("Rebuild HNSW segment", palace_path, assume_yes=assume_yes):
        result["aborted"] = True
        result["reason"] = "user-aborted"
        return result

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    backup_dir: Optional[str] = None
    if backup:
        backup_dir = _backup_segment(palace_path, segment, timestamp)
        print(f"  Backup:  {backup_dir}")

    _close_chroma_handles(palace_path)

    tmpdir = tempfile.mkdtemp(prefix="mempalace_hnsw_", dir=palace_path)
    t0 = time.time()
    try:
        idx = _build_persistent_index(
            vectors,
            labels,
            space=space,
            dim=hdr.dim,
            max_elements=new_max,
            persistence_location=tmpdir,
            M=hdr.M or 16,
            ef_construction=hdr.ef_construction or 100,
        )
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    build_seconds = time.time() - t0
    print(f"  Built:   {healthy_n:,} vectors in {build_seconds:.1f}s")

    try:
        sample_n = min(3, healthy_n)
        _self_query_verify(idx, vectors[:sample_n], labels[:sample_n], k=min(10, healthy_n))
    except RebuildVerificationError:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    finally:
        del idx
        gc.collect()

    if meta is not None:
        _meta_set(meta, "total_elements_added", healthy_n)
        with open(os.path.join(tmpdir, "index_metadata.pickle"), "wb") as f:
            pickle.dump(meta, f, protocol=pickle.HIGHEST_PROTOCOL)

    _atomic_swap_segment(tmpdir, seg_dir)

    if purge_queue:
        deleted = _purge_segment_queue(palace_path, segment)
        print(f"  Queue:   purged {deleted:,} embeddings_queue rows")
        result["queue_rows_purged"] = deleted
    if quarantine_orphans and (stale_uids or orphan_hnsw):
        sidecar = _quarantine_orphans(palace_path, stale_uids, orphan_hnsw)
        print(f"  Orphans: appended to {sidecar}")
        result["orphan_sidecar"] = sidecar

    peak_mb = _peak_memory_mb()
    print(f"\n  Rebuild complete in {build_seconds:.1f}s (peak RSS ≈ {peak_mb:.0f} MB)")
    print(f"  Backup:  {backup_dir or '(skipped)'}")
    print("\n  If the MCP server is currently running with vector_disabled set")
    print("  (e.g. after a #1222 capacity-divergence detection), call the")
    print("  `mempalace_reconnect` tool to refresh the capacity probe and")
    print("  restore vector search.")
    print(f"\n{'=' * 55}\n")

    result.update({"build_seconds": build_seconds, "peak_rss_mb": peak_mb, "backup": backup_dir})
    return result


# ---------------------------------------------------------------------------
# reconcile mode: re-embed SQL-only rows that never landed an HNSW label
# ---------------------------------------------------------------------------


def _resolve_metadata_segment(db_path: str, vector_segment: str) -> Optional[str]:
    """Find the METADATA segment that shares a collection with ``vector_segment``."""
    if not os.path.isfile(db_path):
        return None
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT collection FROM segments WHERE id = ?", (vector_segment,)
        ).fetchone()
        if not row:
            return None
        rows = conn.execute(
            "SELECT id FROM segments WHERE collection = ? AND scope = 'METADATA'",
            (row[0],),
        ).fetchall()
    if len(rows) != 1:
        return None
    return str(rows[0][0])


def _fetch_sql_only_docs(db_path: str, metadata_segment: str, hnsw_uuids: set) -> list:
    """Return ``[(embedding_id, document), ...]`` for SQL rows missing from HNSW.

    Stable order by ``embedding_id`` so reconciles are reproducible.
    """
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            """
            SELECT e.embedding_id, em.string_value
            FROM embeddings e
            JOIN embedding_metadata em ON e.id = em.id
            WHERE e.segment_id = ? AND em.key = 'chroma:document'
            ORDER BY e.embedding_id
            """,
            (metadata_segment,),
        ).fetchall()
    return [(uid, doc) for uid, doc in rows if uid not in hnsw_uuids]


def _embed_in_batches(ef, docs, *, dim: int, batch: int = 64):
    """Encode ``docs`` with ``ef`` in fixed-size batches; return ``np.ndarray``."""
    import numpy as np

    out = np.empty((len(docs), dim), dtype=np.float32)
    for i in range(0, len(docs), batch):
        j = min(i + batch, len(docs))
        out[i:j] = np.asarray(ef(docs[i:j]), dtype=np.float32)
    return out


def reconcile_orphan_sql_rows(
    palace_path: str,
    *,
    segment: str,
    metadata_segment: Optional[str] = None,
    max_elements: Optional[int] = None,
    backup: bool = True,
    dry_run: bool = False,
    assume_yes: bool = False,
    embedding_function=None,
) -> dict:
    """Re-embed SQL-only embeddings into the HNSW vector ``segment``.

    Some chromadb crash modes (e.g. issue #6979) commit ``embeddings``/
    ``embedding_metadata`` rows transactionally to SQL but lose the
    corresponding HNSW additions, leaving a subset of drawers visible to
    metadata queries but unreachable to vector search. This mode finds
    those orphans, embeds their ``chroma:document`` payloads with the
    palace's configured embedding function, and writes a fresh persistent
    index containing both the existing labels (extracted from
    ``data_level0.bin``) and freshly-allocated labels for the SQL-only
    rows. Atomic swap with rollback on failure — same safety profile as
    ``--mode hnsw``.

    ``metadata_segment`` is auto-detected from the sibling METADATA
    segment in the ``segments`` table when omitted. ``embedding_function``
    is injectable for tests; production callers should leave it as
    ``None`` so the palace's configured EF is resolved.
    """
    from .migrate import confirm_destructive_action, contains_palace_database

    palace_path = os.path.abspath(os.path.expanduser(palace_path))
    seg_dir = os.path.join(palace_path, segment)
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    pickle_path = os.path.join(seg_dir, "index_metadata.pickle")
    data_path = os.path.join(seg_dir, "data_level0.bin")
    header_path = os.path.join(seg_dir, "header.bin")

    result: dict = {
        "palace_path": palace_path,
        "segment": segment,
        "dry_run": dry_run,
        "aborted": False,
    }

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — SQL/HNSW Reconcile")
    print(f"{'=' * 55}\n")
    print(f"  Palace:  {palace_path}")
    print(f"  Segment: {segment}")

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
    if not os.path.isdir(seg_dir):
        print(f"  Segment directory not found: {seg_dir}")
        result["aborted"] = True
        result["reason"] = "segment-missing"
        return result
    if not os.path.isfile(pickle_path):
        print(f"  index_metadata.pickle not found in {seg_dir}")
        result["aborted"] = True
        result["reason"] = "pickle-missing"
        return result

    try:
        import hnswlib  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as e:
        print(f"  Required dependency missing: {e}")
        result["aborted"] = True
        result["reason"] = "deps-missing"
        return result

    if metadata_segment is None:
        metadata_segment = _resolve_metadata_segment(db_path, segment)
    if not metadata_segment:
        print("  Could not resolve sibling METADATA segment — pass --metadata-segment")
        result["aborted"] = True
        result["reason"] = "metadata-segment-unresolved"
        return result
    print(f"  Metadata segment: {metadata_segment}")

    with open(pickle_path, "rb") as f:
        meta = pickle.load(f)
    id_to_label = dict(_meta_get(meta, "id_to_label") or {})
    label_to_id = dict(_meta_get(meta, "label_to_id") or {})
    pickle_total = int(_meta_get(meta, "total_elements_added") or 0)
    if len(id_to_label) != pickle_total:
        print(
            f"  Pickle inconsistent: id_to_label={len(id_to_label)} "
            f"vs total_elements_added={pickle_total}. Run --mode hnsw first."
        )
        result["aborted"] = True
        result["reason"] = "pickle-inconsistent"
        return result

    src = header_path if os.path.isfile(header_path) else data_path
    with open(src, "rb") as f:
        hdr = _parse_hnsw_header(f.read(100))
    space = _detect_space(palace_path, segment)

    missing = _fetch_sql_only_docs(db_path, metadata_segment, set(id_to_label.keys()))
    print()
    print("  Report")
    print(f"    existing labels      {len(id_to_label):>10,}")
    print(f"    sql-only orphans     {len(missing):>10,}")
    print(f"    space                {space}")
    print(f"    dim                  {hdr.dim}")

    result.update(
        {
            "existing_labels": len(id_to_label),
            "sql_only_orphans": len(missing),
            "space": space,
            "dim": hdr.dim,
        }
    )

    if not missing:
        print("  Nothing to reconcile.")
        print(f"\n{'=' * 55}\n")
        return result

    if dry_run:
        print("\n  DRY RUN — no embedding, no HNSW write, no swap.\n" + "=" * 55 + "\n")
        return result

    if not confirm_destructive_action("Reconcile HNSW segment", palace_path, assume_yes=assume_yes):
        result["aborted"] = True
        result["reason"] = "user-aborted"
        return result

    if embedding_function is None:
        from .embedding import get_embedding_function

        embedding_function = get_embedding_function()

    import numpy as np

    with open(data_path, "rb") as f:
        data_bytes = f.read()
    raw_labels, raw_vectors = _extract_vectors(data_bytes, hdr)
    del data_bytes
    keep_set = set(id_to_label.values())
    keep_mask = np.array([int(x) in keep_set for x in raw_labels], dtype=bool)
    existing_labels = raw_labels[keep_mask]
    existing_vectors = raw_vectors[keep_mask]
    if len(existing_labels) != len(id_to_label):
        print(f"  WARN: extracted healthy ({len(existing_labels)}) != pickle ({len(id_to_label)}).")

    docs = [d for _uid, d in missing]
    uuids = [u for u, _d in missing]
    new_vectors = _embed_in_batches(embedding_function, docs, dim=hdr.dim)
    max_label = max(id_to_label.values()) if id_to_label else 0
    new_labels = np.arange(max_label + 1, max_label + 1 + len(missing), dtype=np.int64)
    new_total = len(id_to_label) + len(missing)
    new_max = _compute_max_elements(new_total, max_elements)
    if int(new_labels[-1]) >= new_max:
        new_max = max(new_max, int(new_labels[-1]) + 1)

    all_vectors = np.concatenate([existing_vectors, new_vectors])
    all_labels = np.concatenate([existing_labels.astype(np.int64), new_labels])

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir: Optional[str] = None
    if backup:
        backup_dir = _backup_segment(palace_path, segment, timestamp)
        print(f"  Backup:  {backup_dir}")

    _close_chroma_handles(palace_path)

    tmpdir = tempfile.mkdtemp(prefix="mempalace_reconcile_", dir=palace_path)
    t0 = time.time()
    try:
        idx = _build_persistent_index(
            all_vectors,
            all_labels,
            space=space,
            dim=hdr.dim,
            max_elements=new_max,
            persistence_location=tmpdir,
            M=hdr.M or 16,
            ef_construction=hdr.ef_construction or 100,
        )
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    build_seconds = time.time() - t0

    try:
        sample_n = min(3, len(missing))
        existing_n = min(3, len(existing_vectors))
        sv = np.concatenate([existing_vectors[:existing_n], new_vectors[:sample_n]])
        sl = np.concatenate(
            [
                existing_labels[:existing_n].astype(np.int64),
                new_labels[:sample_n],
            ]
        )
        _self_query_verify(idx, sv, sl, k=min(10, len(all_labels)))
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    finally:
        del idx
        gc.collect()

    new_id_to_label = dict(id_to_label)
    new_label_to_id = dict(label_to_id)
    for uid, lbl in zip(uuids, new_labels):
        new_id_to_label[uid] = int(lbl)
        new_label_to_id[int(lbl)] = uid
    _meta_set(meta, "id_to_label", new_id_to_label)
    _meta_set(meta, "label_to_id", new_label_to_id)
    _meta_set(meta, "total_elements_added", new_total)
    with open(os.path.join(tmpdir, "index_metadata.pickle"), "wb") as f:
        pickle.dump(meta, f, protocol=pickle.HIGHEST_PROTOCOL)

    _atomic_swap_segment(tmpdir, seg_dir)

    peak_mb = _peak_memory_mb()
    print(
        f"\n  Reconcile complete: {len(missing):,} new labels appended in "
        f"{build_seconds:.1f}s (peak RSS ≈ {peak_mb:.0f} MB)"
    )
    print(f"  Backup:  {backup_dir or '(skipped)'}")
    print(f"\n{'=' * 55}\n")

    result.update(
        {
            "new_labels": len(missing),
            "total_elements_added": new_total,
            "build_seconds": build_seconds,
            "peak_rss_mb": peak_mb,
            "backup": backup_dir,
        }
    )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="MemPalace repair tools")
    p.add_argument("--palace", default=None, help="Palace directory path")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Read-only sqlite-vs-HNSW capacity health check")
    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--wing", default=None)
    p_prune = sub.add_parser("prune")
    p_prune.add_argument("--confirm", action="store_true")
    sub.add_parser("rebuild")
    p_hnsw = sub.add_parser("hnsw", help="Single-segment HNSW rebuild (issue #1046)")
    p_hnsw.add_argument("--segment", required=True)
    p_hnsw.add_argument("--max-elements", type=int, default=None)
    p_hnsw.add_argument("--backup", action=argparse.BooleanOptionalAction, default=True)
    p_hnsw.add_argument("--purge-queue", action="store_true")
    p_hnsw.add_argument("--quarantine-orphans", action="store_true")
    p_hnsw.add_argument("--dry-run", action="store_true")
    p_hnsw.add_argument("--yes", action="store_true")
    p_msi = sub.add_parser(
        "max-seq-id", help="Un-poison max_seq_id rows (legacy 0.6.x shim damage)"
    )
    p_msi.add_argument("--segment", default=None)
    p_msi.add_argument("--from-sidecar", default=None)
    p_msi.add_argument("--backup", action=argparse.BooleanOptionalAction, default=True)
    p_msi.add_argument("--dry-run", action="store_true")
    p_msi.add_argument("--yes", action="store_true")

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
    elif args.command == "hnsw":
        rebuild_hnsw_segment(
            path or _get_palace_path(),
            segment=args.segment,
            max_elements=args.max_elements,
            backup=args.backup,
            purge_queue=args.purge_queue,
            quarantine_orphans=args.quarantine_orphans,
            dry_run=args.dry_run,
            assume_yes=args.yes,
        )
    elif args.command == "max-seq-id":
        repair_max_seq_id(
            path or _get_palace_path(),
            segment=args.segment,
            from_sidecar=args.from_sidecar,
            backup=args.backup,
            dry_run=args.dry_run,
            assume_yes=args.yes,
        )
