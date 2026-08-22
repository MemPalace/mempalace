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
import errno
import os
import shutil
import sqlite3
import stat
import time
from collections import defaultdict
from contextlib import closing, suppress
from datetime import datetime
import re
from typing import Callable, Iterator, Optional

from chromadb.errors import NotFoundError as ChromaNotFoundError

from .backends.chroma import ChromaBackend, hnsw_capacity_status
from .config import sqlite_read_uri


COLLECTION_NAME = "mempalace_drawers"
REPAIR_TEMP_COLLECTION = f"{COLLECTION_NAME}__repair_tmp"

# The closets collection (AAAK index layer) is intentionally fixed —
# closets reference drawer IDs by string and live alongside drawers in the
# same palace; renaming the closets collection per-deployment would break
# cross-palace AAAK lookups. Drawer collection name comes from config
# (see ``_recoverable_collections``).
CLOSETS_COLLECTION_NAME = "mempalace_closets"


def _no_follow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _non_blocking_flag() -> int:
    """Return O_NONBLOCK, or 0 where the platform has no such flag (Windows).

    Without it the ``S_ISREG`` refusal in ``_open_regular_file_no_follow``
    is unreachable for a FIFO: opening one for reading blocks in the kernel
    until a writer appears, so ``repair`` would wedge instead of refusing.
    """
    return getattr(os, "O_NONBLOCK", 0)


def _open_regular_file_no_follow(path: str) -> int:
    if os.path.islink(path):
        raise RuntimeError(f"Refusing symlinked file: {path}")
    flags = os.O_RDONLY | _no_follow_flag() | _non_blocking_flag()
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        # EAGAIN here is a write-lease break, which the kernel grants on
        # regular files only, so re-check the type and open the way this
        # helper did before the flag existed. Anything else propagates.
        if exc.errno != errno.EAGAIN or not stat.S_ISREG(os.lstat(path).st_mode):
            raise
        fd = os.open(path, flags & ~_non_blocking_flag())
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"Refusing non-regular file: {path}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _write_text_replace_no_follow(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    basename = os.path.basename(path)
    tmp_path = os.path.join(
        directory,
        f".{basename}.{os.getpid()}.{int(time.time() * 1_000_000)}.tmp",
    )
    fd = os.open(
        tmp_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _copy_file_no_follow(src: str, dst: str, *, replace: bool = False) -> None:
    src_fd = _open_regular_file_no_follow(src)
    try:
        src_f = os.fdopen(src_fd, "rb")
    except Exception:
        os.close(src_fd)
        raise
    # ``src_f`` now owns ``src_fd`` and closes it on exit.
    with src_f:
        flags = os.O_WRONLY | os.O_CREAT | _no_follow_flag()
        flags |= os.O_TRUNC if replace else os.O_EXCL
        dst_fd = os.open(dst, flags, 0o600)
        try:
            dst_f = os.fdopen(dst_fd, "wb")
        except Exception:
            os.close(dst_fd)
            raise
        with dst_f:
            shutil.copyfileobj(src_f, dst_f)
    try:
        shutil.copystat(src, dst, follow_symlinks=False)
    except OSError:
        pass


def _unique_backup_path(path: str, label: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{path}.{label}.{stamp}.{os.getpid()}"


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
            except Exception as fallback_exc:
                # Both the offset request AND the no-offset fallback failed.
                # Whatever is in ``ids`` so far is a partial (or empty) prefix
                # of the collection, not a complete listing. Returning it
                # would make scan_palace/rebuild act on a truncated palace as
                # though it were whole (or, on the first page, print "Nothing
                # to scan." for a palace we never actually read). The whole
                # point of this path is to fail loudly rather than silently
                # truncate, so raise instead of breaking.
                raise RuntimeError(
                    f"_paginate_ids: both offset-based pagination and the "
                    f"no-offset fallback failed after collecting {len(ids)} "
                    f"ids. Refusing to return a silently truncated ID list -- "
                    f"investigate the collection, or use a repair mode that "
                    f"does not depend on offset paging (e.g. --mode "
                    f"from-sqlite). Underlying error: {fallback_exc}"
                ) from fallback_exc
            new_ids = [i for i in r["ids"] if i not in set(ids)]
            if not new_ids:
                # Offset is broken and the no-offset fallback always
                # re-fetches the same first `page` results, so it can never
                # advance past that boundary. Landing exactly on that
                # boundary (len(ids) >= page) is ambiguous from the fetched
                # page alone: it could mean "collection has exactly `page`
                # ids, genuinely complete" or "collection has more and we
                # are truncating" -- those two states are indistinguishable
                # without an authoritative count. Disambiguate against the
                # collection's own count() (same pattern already used by
                # _verify_collection_count in this file; shares its known
                # native-crash-surface caveat on a corrupted collection,
                # which is out of scope for this fix -- see the deferred
                # HNSW-preflight-sweep work).
                #
                # ``col.count()`` is collection-wide and takes no ``where``
                # argument, so it is only authoritative when this call is
                # UNFILTERED. With a ``where`` filter, a global count > the
                # collected rows does NOT prove the filtered set is truncated
                # (the extra rows may belong to other wings), so we cannot use
                # it as proof and treat filtered completeness as unknown
                # rather than raising a false-positive truncation error.
                # This raise is intentionally OUTSIDE the narrow try/except
                # above so it propagates instead of being swallowed as a
                # get()-failure.
                if where is None and len(ids) >= page and len(r["ids"] or []) >= page:
                    try:
                        total = col.count()
                    except Exception:
                        total = None
                    if total is None or total > len(ids):
                        raise RuntimeError(
                            f"_paginate_ids: offset-based pagination failed "
                            f"and the no-offset fallback cannot advance past "
                            f"the first {page} results ({len(ids)} collected, "
                            f"collection reports "
                            f"{total if total is not None else 'an unreadable'} "
                            f"total). Refusing to return a silently truncated "
                            f"ID list -- investigate the collection, or use a "
                            f"repair mode that does not depend on offset "
                            f"paging (e.g. --mode from-sqlite)."
                        )
                break
            ids.extend(new_ids)
            offset += len(new_ids)
            continue
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
        if not live_replaced:
            # The live collection was never touched -- the temp build is a
            # discardable in-progress copy, safe to clean up on failure.
            try:
                _delete_collection_if_exists(backend, palace_path, temp_name)
            except Exception:
                pass
            raise RebuildCollectionError(str(exc), live_replaced=live_replaced) from exc
        # The live collection was already deleted (line 294) before this
        # failure. `temp_name` is now the ONLY intact, verified copy of the
        # data left on disk -- never delete it here. Point the operator
        # at it instead of destroying the one thing that can still recover.
        raise RebuildCollectionError(
            f"{exc}. The live collection '{collection_name}' was already "
            f"replaced and the re-upload into it failed. The fully-verified "
            f"pre-swap copy survives under '{temp_name}' -- do NOT delete it. "
            f"Recover by removing the broken '{collection_name}' collection "
            f"and promoting '{temp_name}' in its place.",
            live_replaced=live_replaced,
        ) from exc


def _promote_temp_collection(
    backend,
    palace_path: str,
    temp_name: str,
    collection_name: str,
    expected: int,
    batch_size: int,
    progress=print,
) -> int:
    """Recover a failed live-swap by promoting the verified temp copy.

    `_rebuild_collection_via_temp` fully verifies `temp_name` before it ever
    touches the live collection. If the post-swap re-upload into a fresh live
    collection then fails, the honest recovery is to copy directly from that
    verified temp copy -- NOT to restore a pre-rebuild sqlite3 file backup,
    whose on-disk HNSW segment directories were already destroyed by the
    live-collection delete and would leave the palace referencing segment
    UUIDs that no longer exist on disk.
    """
    temp_col = backend.get_collection(palace_path, temp_name)
    ids, docs, metas = _extract_drawers(temp_col, expected, batch_size)
    _delete_collection_if_exists(backend, palace_path, collection_name)
    new_col = backend.create_collection(palace_path, collection_name)
    promoted = 0
    for i in range(0, len(ids), batch_size):
        new_col.upsert(
            documents=docs[i : i + batch_size],
            ids=ids[i : i + batch_size],
            metadatas=metas[i : i + batch_size],
        )
        promoted += len(ids[i : i + batch_size])
        progress(f"  Promoted {promoted}/{expected} drawers from verified temp copy...")
    _verify_collection_count(new_col, expected, "promoted temp collection")
    # Promotion has already fully succeeded and verified at this point --
    # cleaning up the now-redundant temp copy is best-effort, matching the
    # identical post-success cleanup in _rebuild_collection_via_temp above.
    # A failure here (e.g. a transient Windows file lock) must not turn a
    # successful recovery into a reported failure.
    try:
        _delete_collection_if_exists(backend, palace_path, temp_name)
    except Exception:
        pass
    return promoted


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

    # Preflight HNSW divergence before opening the collection: count() on a
    # diverged segment can hit the #1222 SIGSEGV/panic class, which a
    # try/except around count() cannot catch (a native crash takes the
    # whole process down). scan_palace is meant to find corruption, not
    # crash on the exact corruption class it should be reporting.
    capacity_info = hnsw_capacity_status(palace_path, collection_name)
    if capacity_info.get("diverged"):
        print(f"\n  HNSW index is diverged: {capacity_info.get('message', '')}")
        print(index_read_recovery_guidance())
        return set(), set()

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
    _write_text_replace_no_follow(
        bad_file,
        "".join(f"{bid}\n" for bid in sorted(bad_set)),
    )
    print(f"\n  Bad IDs written to: {bad_file}")
    return good_set, bad_set


def prune_corrupt(palace_path=None, confirm=False, collection_name: Optional[str] = None):
    """Delete corrupt IDs listed in corrupt_ids.txt."""
    palace_path = palace_path or _get_palace_path()
    collection_name = collection_name or _drawers_collection_name()
    bad_file = os.path.join(palace_path, "corrupt_ids.txt")

    if not os.path.exists(bad_file):
        print("  No corrupt_ids.txt found -- run scan first.")
        return

    with open(bad_file) as f:
        bad_ids = [line.strip() for line in f if line.strip()]
    print(f"  {len(bad_ids):,} corrupt IDs queued for deletion")

    if not confirm:
        print("\n  DRY RUN -- no deletions performed.")
        print("  Re-run with --confirm to actually delete.")
        return

    # Preflight HNSW divergence before opening the collection — see
    # scan_palace's identical guard for why this can't rely on except
    # Exception around count() alone.
    capacity_info = hnsw_capacity_status(palace_path, collection_name)
    if capacity_info.get("diverged"):
        print(f"\n  HNSW index is diverged: {capacity_info.get('message', '')}")
        print(index_read_recovery_guidance())
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
    print(f"  Collection size: {before:,} -> {after:,}")


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

        conn = sqlite3.connect(sqlite_read_uri(sqlite_path), uri=True)
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


_SQLITE_INTEGRITY_BUSY_TIMEOUT_SECONDS = 15.0


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
        # A writer holding SQLite's lock is contention, not corruption. The
        # sqlite3 module defaults to five seconds, which is shorter than
        # routine batch mines and curator writes on rollback-journal palaces.
        # Give those writers a bounded grace period before surfacing BUSY to
        # callers; genuine corruption still comes from PRAGMA quick_check.
        with sqlite3.connect(
            sqlite_read_uri(sqlite_path),
            uri=True,
            timeout=_SQLITE_INTEGRITY_BUSY_TIMEOUT_SECONDS,
        ) as conn:
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


# quick_check's wording for a corrupt FTS5 inverted index differs by SQLite
# version — both forms describe the same recoverable condition:
#   "malformed inverted index for FTS5 table main.embedding_fulltext_search"
#     (older SQLite)
#   "fts5: corruption found reading blob N from table \"embedding_fulltext_search\""
#     (SQLite >= ~3.5x, confirmed on 3.53.2 / Python 3.13.7 — the exact
#     message this repo's own test fixture produces on that build)
# Either failure says the index and the ``embedding_fulltext_search_content``
# shadow table disagree; neither names the side that is damaged. Concurrent
# killed-mid-write mines are the usual cause (#1596). A regex matching only
# the older phrasing would silently decline to auto-heal on newer SQLite —
# the exact failure this repo's own test suite caught (test_repair.py's two
# auto-heal tests failed on this machine until this pattern was widened).
_FTS5_MALFORMED_RE = re.compile(
    r"malformed inverted index for fts5 table|fts5:\s*corruption found",
    re.IGNORECASE,
)

# ``embedding_fulltext_search`` is derived data twice over: chroma writes each
# document into ``embedding_metadata`` under ``chroma:document`` and into the
# FTS5 table at ``rowid = embeddings.id``, and every read path returns the
# metadata copy (checked against chromadb 1.5.7). So the content shadow table
# has an authority to be checked against, and a rebuild that reads it is only
# as good as that check.
#
# Both queries scan the metadata table: ``(id, key)`` is its primary key, so a
# lookup by ``key`` alone cannot use it. An index for the heal alone would be
# paid on every write instead, which is the worse trade.
#
# ``typeof(m.id) = 'integer'`` is not decoration. ``id`` is nullable — a NULL
# is storable in that composite key — and feeding NULL into the content table's
# ``INTEGER PRIMARY KEY`` makes SQLite assign a fresh rowid rather than conflict,
# so such a row would never reconcile and the heal would decline on every run.
_FTS5_CONTENT_TO_RESTORE_SQL = """
    SELECT count(*)
      FROM embedding_metadata AS m
      LEFT JOIN embedding_fulltext_search_content AS c ON c.id = m.id
     WHERE m.key = 'chroma:document'
       AND typeof(m.id) = 'integer'
       AND m.string_value IS NOT NULL
       AND c.c0 IS NOT m.string_value
"""

# How much of the content table the authority can speak for. A row it cannot —
# chroma's own update path stores ``{'chroma:document': None}`` by deleting the
# metadata row while still writing the FTS row — keeps its content untouched,
# because for those rows the shadow table may be the only copy. If the authority
# can speak for none of them there is nothing to rebuild on, and a row at an id
# no ``embeddings`` row uses says the table is not keyed the way this code reads
# it: chroma's ``00003-full-text-tokenize`` migration populated the FTS table
# from ``embedding_metadata.rowid`` over every string value, not from
# ``embeddings.id`` over documents.
_FTS5_CONTENT_CENSUS_SQL = """
    SELECT coalesce(sum(CASE WHEN m.id IS NULL THEN 0 ELSE 1 END), 0),
           coalesce(sum(CASE WHEN m.id IS NULL THEN 1 ELSE 0 END), 0),
           coalesce(sum(CASE WHEN e.id IS NULL THEN 1 ELSE 0 END), 0)
      FROM embedding_fulltext_search_content AS c
      LEFT JOIN embedding_metadata AS m
        ON m.id = c.id
       AND m.key = 'chroma:document'
       AND m.string_value IS NOT NULL
      LEFT JOIN embeddings AS e ON e.id = c.id
"""

# Written to the shadow table directly, not through the virtual table: an
# INSERT or DELETE on ``embedding_fulltext_search`` goes through the inverted
# index, which is the structure quick_check has just called malformed. Writing
# the content rows and then rebuilding is the one order that does not depend on
# the damaged side. ``c0`` is FTS5's own column-naming convention for the first
# indexed column, and the SELECT needs its WHERE clause for ``ON CONFLICT`` to
# parse at all — dropping that predicate turns this into a syntax error.
_FTS5_CONTENT_RESTORE_SQL = """
    INSERT INTO embedding_fulltext_search_content(id, c0)
    SELECT m.id, m.string_value
      FROM embedding_metadata AS m
     WHERE m.key = 'chroma:document'
       AND typeof(m.id) = 'integer'
       AND m.string_value IS NOT NULL
        ON CONFLICT(id) DO UPDATE SET c0 = excluded.c0
     WHERE embedding_fulltext_search_content.c0 IS NOT excluded.c0
"""

_FTS5_REBUILD_SQL = (
    "INSERT INTO embedding_fulltext_search(embedding_fulltext_search) VALUES('rebuild')"  # noqa: E501
)


def _errors_are_isolated_fts5(errors: list[str]) -> bool:
    """True when every quick_check error is a malformed FTS5 inverted index.

    Isolation is necessary but not sufficient for an auto-heal: it rules out
    page/row corruption elsewhere in the file, and nothing more. Which of the
    two FTS5 tables is damaged is still open — see
    :func:`maybe_autoheal_fts5_index`, which settles that before writing.
    """
    return bool(errors) and all(_FTS5_MALFORMED_RE.search(e) for e in errors)


def _fts5_content_rows_to_restore(conn: sqlite3.Connection) -> int:
    """Count documents whose shadow copy is missing or says something else."""
    return int(conn.execute(_FTS5_CONTENT_TO_RESTORE_SQL).fetchone()[0])


def _fts5_content_census(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Content rows a ``chroma:document`` can speak for, rows it cannot, rows
    sitting at an id no ``embeddings`` row uses."""
    checked, unverifiable, unkeyed = conn.execute(_FTS5_CONTENT_CENSUS_SQL).fetchone()
    return int(checked), int(unverifiable), int(unkeyed)


def maybe_autoheal_fts5_index(palace_path: str, errors: list[str], *, progress=print) -> list[str]:
    """Rebuild a malformed FTS5 inverted index in place; return remaining errors.

    The repair preflight aborts when ``PRAGMA quick_check`` reports SQLite-layer
    corruption. After concurrent killed-mid-write mines (#1596) the common
    failure is an isolated ``malformed inverted index for FTS5 table``, and
    ``rebuild`` recovers it by regenerating the index from
    ``embedding_fulltext_search_content``.

    That error says the index and the content table disagree; it does not say
    which of them is wrong. So the content table is checked against
    ``embedding_metadata`` first, and any row that disagrees is restored from it
    before the rebuild reads it — otherwise a rebuild over a damaged content
    table would overwrite an index that still held the drawer's own words and
    leave quick_check clean, reporting success for a palace that lost full-text
    reach. Both writes are derived from ``embedding_metadata``; rows that
    table cannot speak for are left untouched.

    Everything happens under the palace write lock (so a live mine cannot race
    it) and in one transaction, which is why a restored row cannot outlive a
    rebuild that then fails. Returns the remaining quick_check errors — empty
    when the heal succeeded. Broader corruption, a lock held by another writer,
    a content table that cannot be checked or cannot be brought into agreement,
    a rebuild failure, or a quick_check still dirty afterwards leaves ``errors``
    unchanged so the caller still aborts with the banner.
    """
    if not _errors_are_isolated_fts5(errors):
        return errors

    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.exists(sqlite_path):
        return errors

    # Lazy import: palace.py is heavier and importing it at module load would
    # widen repair.py's import graph for callers that never hit this path.
    from .palace import MineAlreadyRunning, mine_palace_lock

    progress(
        "\n  Isolated FTS5 inverted-index corruption detected; checking the content\n"
        "  table against embedding_metadata before rebuilding the index from it."
    )
    declined = False
    to_restore = checked = unverifiable = unkeyed = 0
    try:
        with mine_palace_lock(palace_path):
            with closing(sqlite3.connect(sqlite_path, isolation_level=None)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    to_restore = _fts5_content_rows_to_restore(conn)
                    checked, unverifiable, unkeyed = _fts5_content_census(conn)
                except sqlite3.Error as exc:
                    # Suppressed: a failing ROLLBACK would replace the message
                    # that says why the heal declined. Closing the connection
                    # rolls the transaction back either way.
                    with suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                    declined = True
                    progress(
                        "  Skipped FTS5 rebuild: the content table cannot be checked against "
                        f"embedding_metadata ({exc}). The index still holds the terms it was "
                        "built from."
                    )
                if not declined and unverifiable and not checked and not to_restore:
                    with suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                    declined = True
                    progress(
                        "  Skipped FTS5 rebuild: no content row has an embedding_metadata "
                        "document to check it against. The index still holds the terms it "
                        "was built from."
                    )
                if not declined and to_restore:
                    # Present tense on purpose: this prints before COMMIT, so it
                    # is also what an operator sees on a run that then rolls back.
                    progress(
                        f"  Restoring {to_restore} content row(s) from embedding_metadata "
                        "before rebuilding."
                    )
                    conn.execute(_FTS5_CONTENT_RESTORE_SQL)
                    if _fts5_content_rows_to_restore(conn):
                        with suppress(sqlite3.Error):
                            conn.execute("ROLLBACK")
                        declined = True
                        progress(
                            "  Skipped FTS5 rebuild: the content table still disagrees with "
                            "embedding_metadata after restoring it. Nothing was written."
                        )
                    else:
                        # Re-taken: a restore can add content rows the authority
                        # has and the shadow table had lost, so the census from
                        # before it would under-report what the rebuild reads.
                        checked, unverifiable, unkeyed = _fts5_content_census(conn)
                if not declined:
                    if unverifiable:
                        progress(
                            f"  {unverifiable} content row(s) have no embedding_metadata document "
                            "to check against; the rebuild indexes them as they stand."
                        )
                    if unkeyed:
                        progress(
                            f"  {unkeyed} content row(s) sit at an id no embeddings row uses; "
                            "this table was not written by the current chromadb schema."
                        )
                    conn.execute(_FTS5_REBUILD_SQL)
                    conn.execute("COMMIT")
    except MineAlreadyRunning as exc:
        progress(
            f"  Skipped FTS5 rebuild: palace is being written by another process ({exc}). "
            "Stop it and re-run."
        )
        return errors
    except Exception as exc:
        # Deliberately broad and deliberately not naming the rebuild: this now
        # covers the lock, the transaction, both counts and the restore, and a
        # heal that raises must never be what fails a mine.
        progress(f"  FTS5 heal failed (leaving palace untouched): {exc}")
        return errors

    if declined:
        return errors

    remaining = sqlite_integrity_errors(palace_path)
    if remaining:
        progress("  FTS5 rebuild did not clear quick_check; aborting for safety.")
    else:
        progress(
            f"  FTS5 index rebuilt from content checked against embedding_metadata "
            f"({checked} row(s)); quick_check is clean."
        )
    return remaining


def index_read_recovery_guidance() -> str:
    """Recovery guidance for a failed drawer-index read in the legacy paths.

    Both ``cmd_repair`` (cli.py) and :func:`rebuild_index` read the drawers
    collection via ``Collection.count()`` as their first step. The common
    reason that read raises is the chromadb compactor failing to apply the
    WAL into the HNSW segment (``InternalError: Failed to apply logs to the
    hnsw segment writer``, issues #1308 / #1843): the on-disk HNSW index is
    corrupt while the rows stay intact in ``chroma.sqlite3``, so
    :func:`rebuild_from_sqlite` (``repair --mode from-sqlite``) recovers them
    and re-mining would needlessly drop drawers added through the MCP server
    and diary entries that have no source file.

    The other thing that strands this read is a live MemPalace server or
    mine still holding the palace open, so the guidance says to stop it and
    retry before assuming corruption. Worded conditionally because the bare
    ``except Exception`` cannot prove which case it caught. Returned as a
    pre-indented block so the ``print``-based CLI path and the
    ``progress``-callable rebuild path emit it unchanged.
    """
    return (
        "  If a MemPalace server or mine is still running against this palace,\n"
        "  stop it and retry. Otherwise the drawer index is likely corrupt\n"
        "  (for example a failed chromadb HNSW compaction) while your drawer\n"
        "  rows remain in chroma.sqlite3. Rebuild the index from SQLite rather\n"
        "  than re-mining:\n"
        "\n"
        "      mempalace repair --mode from-sqlite --archive-existing\n"
        "\n"
        "  (Re-mining from source files would drop drawers added via the MCP\n"
        "  server and diary entries, which have no source file.)"
    )


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


def _vacuum_and_rebuild_fts5(
    palace_path: str,
    progress=print,
    *,
    strict: bool = False,
) -> None:
    """VACUUM the palace SQLite file and rebuild the FTS5 index if present.

    Repeated ``repair --yes`` runs delete and recreate the drawers collection,
    leaving freed SQLite pages unreclaimable without an explicit VACUUM.  The
    FTS5 virtual table (``embedding_fulltext_search``) can also become
    internally inconsistent after multiple collection deletes; the rebuild
    command fixes it atomically without touching any row data.

    Existing repair paths use the default best-effort behavior: failures log a
    warning and return because their primary rebuild has already succeeded.
    SQLite recovery passes ``strict=True`` because its bulk upserts can leave
    this derived index malformed; that path must not report success until the
    rebuild, VACUUM, and a final quick_check all complete.
    """
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.exists(sqlite_path):
        if strict:
            raise FileNotFoundError(f"recovered palace has no SQLite database: {sqlite_path}")
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
            if strict:
                rows = conn.execute("PRAGMA quick_check").fetchall()
                errors = [str(row[0]) for row in rows if row and str(row[0]).lower() != "ok"]
                if errors:
                    raise sqlite3.DatabaseError(
                        "post-recovery quick_check failed: " + "; ".join(errors[:3])
                    )
                progress("  SQLite quick_check clean.")
    except Exception as exc:
        if strict:
            # Preserve the concrete SQLite/filesystem exception for callers
            # that need to classify the failure. The strict recovery caller
            # owns the user-facing error message, so logging here would also
            # print the same failure twice.
            raise
        progress(f"  Warning: post-repair cleanup failed (non-fatal): {exc}")


def _post_rebuild_cleanup(palace_path: str, backend: "ChromaBackend", progress=print) -> None:
    """Close cached chroma handles, then VACUUM and rebuild the FTS5 index.

    Shared epilogue for the two full-rebuild paths (``rebuild_index`` and the
    CLI legacy ``cmd_repair``), so neither can drift out of the post-run
    cleanup again (issues #1517, #1747). ChromaDB's PersistentClient keeps
    chroma.sqlite3 open and VACUUM needs exclusive access, so the handles
    are released first.
    """
    _close_chroma_handles(palace_path, backend=backend)
    _vacuum_and_rebuild_fts5(palace_path, progress=progress)


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
    progress("  MemPalace Repair -- Index Rebuild")
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
        sqlite_errors = maybe_autoheal_fts5_index(palace_path, sqlite_errors, progress=progress)
    if sqlite_errors:
        print_sqlite_integrity_abort(palace_path, sqlite_errors)
        return

    preflight = maybe_repair_poisoned_max_seq_id_before_rebuild(
        palace_path,
        assume_yes=True,
    )
    if preflight is not None:
        return

    # Preflight HNSW divergence before opening the collection: status()
    # already has this same guard via hnsw_capacity_status (docstring above
    # this module's status() references it as "the safe pattern"), but
    # rebuild_index -- the legacy rebuild the CLI's rebuild-index subcommand
    # dispatches straight to -- opens the collection and calls col.count()
    # directly, wrapped only in except Exception, which cannot catch the
    # #1222 SIGSEGV/panic class a diverged segment triggers.
    capacity_info = hnsw_capacity_status(palace_path, collection_name)
    if capacity_info.get("diverged"):
        progress(f"\n  HNSW index is diverged: {capacity_info.get('message', '')}")
        progress(index_read_recovery_guidance())
        return

    # Hold the palace writer lease for the complete snapshot -> rebuild/swap
    # -> cleanup cycle. A writer landing after the snapshot but before the
    # rebuilt collection becomes authoritative would otherwise be lost from
    # the rebuilt index and recreate SQLite/HNSW divergence.
    from .palace import mine_palace_lock

    with mine_palace_lock(palace_path):
        _rebuild_index_under_lease(
            backend=ChromaBackend(),
            palace_path=palace_path,
            collection_name=collection_name,
            confirm_truncation_ok=confirm_truncation_ok,
            progress=progress,
        )


def _rebuild_index_under_lease(
    *,
    backend,
    palace_path: str,
    collection_name: str,
    confirm_truncation_ok: bool,
    progress: Callable[[str], None],
):
    """Run rebuild_index's snapshot/rebuild body under its writer lease."""
    try:
        col = backend.get_collection(palace_path, collection_name)
        total = col.count()
    except Exception as e:
        progress(f"  Error reading palace: {e}")
        progress(index_read_recovery_guidance())
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
    backup_path = _unique_backup_path(sqlite_path, "backup")
    if os.path.exists(sqlite_path):
        progress(f"  Backing up chroma.sqlite3 ({os.path.getsize(sqlite_path) / 1e6:.0f} MB)...")
        _copy_file_no_follow(sqlite_path, backup_path)
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
        if e.live_replaced:
            # Restoring the pre-rebuild chroma.sqlite3 file here would be
            # misleading: the live collection's on-disk HNSW segment
            # directories were already destroyed by the delete that
            # preceded this failure, so a sqlite-only restore leaves the
            # palace referencing segment UUIDs that no longer exist.
            # The verified good copy is the temp collection instead --
            # promote it directly.
            temp_name = f"{collection_name}__repair_tmp"
            progress(f"  Attempting recovery: promoting verified copy from '{temp_name}'...")
            try:
                _close_chroma_handles(palace_path, backend=backend)
                _promote_temp_collection(
                    backend,
                    palace_path,
                    temp_name,
                    collection_name,
                    len(all_ids),
                    batch_size,
                    progress=progress,
                )
                progress(
                    "  Recovery succeeded: live collection restored from the verified temp copy."
                )
            except Exception as promote_error:
                progress(f"  Automatic recovery failed: {promote_error}")
                progress(
                    f"  The verified pre-swap copy still survives under '{temp_name}' -- "
                    f"do NOT delete it. Recover manually by promoting it, or re-mine "
                    f"from source files."
                )
        else:
            print("  Live collection was not replaced; leaving the original palace untouched.")
        raise

    _post_rebuild_cleanup(palace_path, backend=backend, progress=progress)

    print(f"\n  Repair complete. {filed} drawers rebuilt.")
    print("  HNSW index is now clean with cosine distance metric.")

    # rebuild_index only ever touches collection_name (drawers by default).
    # status() checks divergence for BOTH drawers and closets and recommends
    # --mode from-sqlite (which rebuilds both via _recoverable_collections()),
    # but a caller who ran this legacy rebuild instead would otherwise see
    # an unqualified "Repair complete" even when closets remains diverged
    # and just as capable of crashing reads via the same #1222 mechanism (#13).
    if collection_name == _drawers_collection_name():
        closets_info = hnsw_capacity_status(palace_path, CLOSETS_COLLECTION_NAME)
        if closets_info.get("diverged"):
            print(
                f"\n  NOTE: the closets index is still diverged "
                f"({closets_info.get('message', '')}).\n"
                "  This rebuild only covers drawers. Run "
                "`mempalace repair --mode from-sqlite --archive-existing`\n"
                "  to rebuild closets too."
            )
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


class RebuildCleanupError(Exception):
    """Raised when all recoverable rows landed but final cleanup failed.

    The destination is intentionally retained for inspection, and an in-place
    rebuild's original archive remains untouched. Callers must not treat this
    as success because the derived FTS5 index has not been verified clean.
    """

    def __init__(
        self,
        message: str,
        *,
        counts: dict[str, int],
        dest_palace: str,
        archive_path: Optional[str],
    ):
        super().__init__(message)
        self.message = message
        self.counts = counts
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

    Driven from ``embeddings`` (LEFT JOIN ``embedding_metadata``), not
    the other way around: an embedding with zero ``embedding_metadata``
    rows — a sparse historical write with no ``chroma:document`` and no
    other key, the same condition ``_extract_drawers`` already sanitizes
    for the collection-layer path, see #1458 — must still be yielded
    with an empty metadata dict, not silently excluded by the join.

    Silent on missing palace, missing ``chroma.sqlite3``, or unknown
    collection name — yields nothing. Callers that need to distinguish
    "empty collection" from "collection not present" should query
    :func:`sqlite_drawer_count` first.
    """
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(sqlite_path):
        return

    conn = sqlite3.connect(sqlite_read_uri(sqlite_path), uri=True)
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
            FROM embeddings e
            LEFT JOIN embedding_metadata em ON em.id = e.id
            WHERE e.segment_id = ?
            ORDER BY e.id
            """,
            (segment_id,),
        ):
            if emb_id not in per_id:
                order.append(emb_id)
            if key is None:
                # LEFT JOIN unmatched row: this embedding has zero
                # embedding_metadata rows. `order`/`per_id` already
                # account for it via the defaultdict below; nothing to
                # merge for this row.
                continue
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


def _preserve_knowledge_graph_sqlite(source_palace: str, dest_palace: str) -> list[str]:
    """Copy KG SQLite sidecars when rebuilding a palace from chroma.sqlite3.

    rebuild_from_sqlite reconstructs Chroma collections into a fresh
    destination directory. The knowledge graph is a separate SQLite database,
    so it must be copied explicitly or the repair succeeds while silently
    dropping KG state (#1816).
    """

    copied: list[str] = []

    for suffix in ("", "-wal", "-shm"):
        filename = f"knowledge_graph.sqlite3{suffix}"
        src = os.path.join(source_palace, filename)
        dst = os.path.join(dest_palace, filename)

        if not os.path.isfile(src):
            continue
        if os.path.abspath(src) == os.path.abspath(dst):
            continue

        os.makedirs(dest_palace, exist_ok=True)
        try:
            _copy_file_no_follow(src, dst, replace=True)
        except RuntimeError:
            continue
        copied.append(filename)

    if copied:
        print(" Preserved knowledge graph: " + ", ".join(copied))

    return copied


def rebuild_from_sqlite(
    source_palace: str,
    dest_palace: str,
    *,
    archive_existing_dest: bool = False,
    batch_size: int = 1000,
    dry_run: bool = False,
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

    ``dry_run`` (CLI: ``--dry-run``) previews the rebuild without making any
    change: source validation runs as normal, then per-collection row counts
    are read from the source SQLite and printed, and the function returns
    those would-be counts *without* archiving the existing palace, taking the
    mine-lock, creating collections, or re-embedding (#2095, #2133). Useful
    before a multi-hour rebuild on a large palace. A dry run returns a
    populated dict (one key per recoverable collection) so CLI callers treat
    it as success; a validation refusal still returns ``{}`` exactly as a real
    run would. If SQLite row counts cannot be read, the preview fails closed
    with ``{}`` rather than inventing zeros.

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
    against it. Raises :class:`RebuildCleanupError` if all rows land but the
    required FTS5 rebuild, VACUUM, or final quick_check fails; this prevents a
    structurally unverified recovery from being reported as complete.

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
    print("  MemPalace Repair -- Rebuild from SQLite")
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

    # --dry-run: validation has passed, so report what a real run would do
    # and stop before the first irreversible step (mine-lock + archive).
    # Counts come from ``sqlite_drawer_count`` — the same SQLite ground-truth
    # helper repair uses elsewhere — so the preview matches the per-collection
    # counts a real rebuild upserts. Reads the original ``source_palace``
    # (not yet archived). Must never take the mine-lock or rename anything.
    if dry_run:
        return _preview_rebuild_from_sqlite(
            source_palace=source_palace,
            dest_palace=dest_palace,
            in_place=in_place,
        )

    # Acquire the single-writer mine-lock BEFORE the archive/rename. The
    # rebuild upserts into ``dest_palace`` through the same backend write
    # path that takes ``mine_palace_lock`` per batch; if a daemon or a
    # concurrent mine already holds it, those upserts used to fail *after*
    # ``shutil.move`` had already stranded the existing palace aside
    # (renamed to ``.pre-rebuild-…`` with no rebuilt replacement, leaving a
    # partial/archived mess). Taking the lock up front makes contention
    # fail CLEAN — no archive, no partial dest, the palace left untouched —
    # and runs the whole rebuild as one writer (the inner per-batch
    # acquires pass through re-entrantly on this thread).
    from .palace import mine_palace_lock

    with mine_palace_lock(dest_palace):
        return _rebuild_from_sqlite_locked(
            source_palace=source_palace,
            dest_palace=dest_palace,
            in_place=in_place,
            batch_size=batch_size,
        )


def _print_unreadable_count_refusal(*, collection_name: str, palace_path: str) -> None:
    """Refuse to preview a collection whose SQLite row count cannot be read.

    Fail closed: inventing 0 would hide an unreadable source and make the
    operator believe a real run would upsert nothing (review note on #1654 /
    #2095). Shared by both previews so the wording cannot drift apart.
    """
    print(
        f"\n  Cannot preview [{collection_name}]: SQLite row count is unreadable "
        f"at {os.path.join(palace_path, 'chroma.sqlite3')}.\n"
        "  Fix source readability (schema, lock, permissions) and re-run "
        "--dry-run; refusing to invent zero counts."
    )


def _preview_rebuild_from_sqlite(
    *,
    source_palace: str,
    dest_palace: str,
    in_place: bool,
) -> dict[str, int]:
    """Read-only preview for :func:`rebuild_from_sqlite` (``dry_run=True``).

    Never archives, locks, or writes. Returns ``{}`` if SQLite counts are
    unreadable so a broken preview cannot look like a successful zero-row plan.
    """
    print("\n  DRY RUN -- no changes will be made.")
    if in_place:
        print(
            f"  Would archive {dest_palace} → "
            f"{dest_palace}.pre-rebuild-<timestamp>, then rebuild from the copy."
        )
    else:
        print(f"  Would rebuild into {dest_palace} from {source_palace}.")

    counts: dict[str, int] = {}
    for cname in _recoverable_collections():
        n = sqlite_drawer_count(source_palace, cname)
        if n is None:
            _print_unreadable_count_refusal(collection_name=cname, palace_path=source_palace)
            return {}
        counts[cname] = n
        print(f"  [{cname}] would re-embed and upsert {n} rows")
    print(
        f"\n  Would rebuild {sum(counts.values())} total rows. Re-run without --dry-run to execute."
    )
    print(f"{'=' * 55}\n")
    return counts


def _preview_legacy_repair(
    *,
    palace_path: str,
    collection_name: str,
    confirm_truncation_ok: bool = False,
) -> dict[str, int]:
    """Read-only preview for the default (legacy) ``repair`` path (``dry_run=True``).

    Never opens a chromadb client, takes a lock, or writes. Opening a client is
    itself a write to ``chroma.sqlite3``, so the row count comes from the
    read-only SQLite ground truth :func:`check_extraction_safety` already
    trusts. That is a different source than the real run rebuilds from (it
    re-files what the chromadb collection layer returns), so the plan below
    states the ``#1208`` contingency rather than promising the number.

    ``confirm_truncation_ok`` mirrors the real run's flag: it switches that
    contingency off, so the preview has to say the guard is disabled rather
    than promise an abort that would not happen.

    Returns ``{}`` when the count is unreadable so a broken preview cannot look
    like a valid plan (#1654, #2095, #2133).
    """
    print("\n  DRY RUN -- no changes will be made.")
    n = sqlite_drawer_count(palace_path, collection_name)
    if n is None:
        _print_unreadable_count_refusal(collection_name=collection_name, palace_path=palace_path)
        print(f"{'=' * 55}\n")
        return {}

    if n == 0:
        # The real run stops at ``total == 0`` with "Nothing to repair.", or —
        # when the collection is absent altogether — at the index-read error
        # that points to --mode from-sqlite. Neither backs up nor rebuilds, so
        # promising a backup and a VACUUM here would describe a run that does
        # not happen.
        print(
            f"  [{collection_name}] chroma.sqlite3 holds no rows. A real run would report\n"
            "  nothing to repair, or an index read error, and change nothing."
        )
        print(f"{'=' * 55}\n")
        return {collection_name: 0}

    backup_path = os.path.normpath(palace_path) + ".backup"
    if confirm_truncation_ok:
        print(
            f"  [{collection_name}] chroma.sqlite3 holds {n} rows, and --confirm-truncation-ok\n"
            "  is set, so the #1208 truncation guard is DISABLED. A real run would re-file\n"
            f"  whatever the chromadb collection layer returns, even if that is fewer than {n}\n"
            "  rows, and the difference would be destroyed. It would, in order:"
        )
    else:
        print(
            f"  [{collection_name}] chroma.sqlite3 holds {n} rows. A real run would extract them\n"
            "  through the chromadb collection layer first and abort without changes if that\n"
            f"  returns fewer than {n} (#1208 truncation guard). It would then, in order:"
        )
    if os.path.exists(backup_path):
        print(f"    1. DELETE the existing backup at {backup_path} -- or refuse outright")
        print("       if it is not a palace -- and copy the live palace in its place")
    else:
        print(f"    1. copy the palace directory to {backup_path}")
    print(f"    2. DELETE the live '{collection_name}' collection and re-file the extracted rows")
    print("       into a fresh one, staged and verified in a temp collection first")
    print("    3. rebuild the FTS5 index and VACUUM chroma.sqlite3")
    print("\n  Without --yes it would ask for confirmation before step 1.")
    print("  Re-run without --dry-run to execute.")
    print(f"{'=' * 55}\n")
    return {collection_name: n}


def resolve_repair_preflight_errors(
    palace_path: str,
    errors: list[str],
    *,
    dry_run: bool,
    progress=print,
) -> list[str]:
    """Return the quick_check errors that still block a repair.

    A real run heals an isolated malformed FTS5 inverted index in place and
    carries on (#1596). ``--dry-run`` must not perform that write, so it
    classifies the errors with the same :func:`_errors_are_isolated_fts5`
    predicate the real path gates on: an isolated FTS5 error is reported and
    cleared, anything broader still aborts. Without this a preview would print
    the ABORT banner — offline ``sqlite3 .recover``, recreate the FTS5 table —
    for a palace the tool repairs by itself.

    The prediction is deliberately the optimistic branch, and it is stated as
    an attempt rather than a promise: the real heal still returns the errors
    unchanged when another process holds the mine lock, when the content table
    cannot be checked against ``embedding_metadata`` or cannot be brought into
    agreement with it, when the rebuild raises, or when ``quick_check`` is still
    dirty afterwards. A dry run cannot tell those apart without taking the lock
    and writing, which is exactly what it must not do, so the wording names them
    instead.
    """
    if not errors:
        return errors
    if not dry_run:
        return maybe_autoheal_fts5_index(palace_path, errors, progress=progress)
    if _errors_are_isolated_fts5(errors):
        progress(
            "\n  DRY RUN — quick_check reports an isolated FTS5 inverted-index error.\n"
            "  A real run would check the content table against embedding_metadata,\n"
            "  restore any row that disagrees, rebuild the index from it and continue\n"
            "  if that succeeds; it aborts instead if another process holds the mine\n"
            "  lock, if the content table cannot be checked or restored, or if the\n"
            "  rebuild leaves quick_check dirty. This preview leaves the index untouched."
        )
        return []
    return errors


def _rebuild_from_sqlite_locked(
    *,
    source_palace: str,
    dest_palace: str,
    in_place: bool,
    batch_size: int,
) -> dict[str, int]:
    """Body of :func:`rebuild_from_sqlite`, run while holding
    ``mine_palace_lock(dest_palace)`` so the archive/rename and the
    upserts form one atomic single-writer operation.

    Split out so the lock acquired in :func:`rebuild_from_sqlite` wraps
    every destructive step (the archive ``shutil.move`` below included).
    Raising :class:`MineAlreadyRunning` from the wrapping ``with`` happens
    before this body runs, so a held lock never reaches the archive.
    """
    archive_path: Optional[str] = None
    if in_place:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_path = f"{dest_palace}.pre-rebuild-{ts}"
        print(f"  Archiving {dest_palace} -> {archive_path}")
        # os.rename, NOT shutil.move. When any file inside the palace is
        # held open by another process (MCP server, a running mine, another
        # harness), renaming the directory fails atomically UP FRONT on
        # Windows and the palace is left untouched. shutil.move's fallback
        # for a failed rename is copytree + rmtree — and that rmtree
        # deletes the live palace file-by-file until it hits the first
        # locked file, leaving the palace partially gutted next to a
        # partial archive copy. Observed live (Windows 11, 2026-07-05/06):
        # two interrupted in-place repairs; the palace survived only
        # because the locked chroma.sqlite3/*.bin themselves could not be
        # unlinked. Same lesson as the source-validation comment above:
        # fail cleanly before touching anything.
        try:
            os.rename(dest_palace, archive_path)
        except OSError as exc:
            print(
                f"\n  Cannot archive the existing palace: a file inside it "
                f"is held open by another process (MCP server, running "
                f"mine, another harness?).\n"
                f"  Close those processes and retry. The palace was left "
                f"untouched.\n"
                f"  ({exc})"
            )
            return {}
        source_palace = archive_path

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
    _preserve_knowledge_graph_sqlite(source_palace, dest_palace)

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

    finally:
        backend.close()

    # Bulk Chroma upserts can leave the derived FTS5 index internally
    # inconsistent even when all source rows landed.  Rebuild it only after
    # the backend releases its SQLite handle; otherwise VACUUM cannot obtain
    # the exclusive lock it needs on Windows.
    try:
        _vacuum_and_rebuild_fts5(dest_palace, strict=True)
    except Exception as exc:
        message_parts = [
            f"Post-recovery cleanup failed after {sum(counts.values())} rows were rebuilt: {exc}",
            f"Recovered palace retained at: {dest_palace}",
        ]
        if archive_path is not None:
            message_parts.append(f"Original palace remains archived at: {archive_path}")
        else:
            message_parts.append(f"Source palace is unchanged at: {source_palace}")
        message = "\n  ".join(message_parts)
        print(f"\n  ERROR: {message}")
        raise RebuildCleanupError(
            message,
            counts=dict(counts),
            dest_palace=dest_palace,
            archive_path=archive_path,
        ) from exc

    print(f"\n  Rebuild complete. {sum(counts.values())} total rows.")
    if archive_path is not None:
        print(f"  Original palace archived at: {archive_path}")
    print(f"{'=' * 55}\n")
    return counts


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
    print("  MemPalace Repair -- Status")
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
        print(
            "\n  Recommended: rebuild the index from SQLite rather than re-mining:\n"
            "\n      mempalace repair --mode from-sqlite --archive-existing\n"
            "\n  A diverged index usually means the HNSW segment is out of sync with\n"
            "  chroma.sqlite3 (for example a failed chromadb HNSW compaction). The\n"
            "  drawer rows are intact in SQLite, so --mode from-sqlite recovers them.\n"
            "  Do not re-mine from source files: that would drop drawers added via\n"
            "  the MCP server and diary entries, which have no source file (#1843)."
        )
    print()
    return {"drawers": drawers, "closets": closets}


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
    print("  MemPalace Repair -- max_seq_id Un-poison")
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
        print(f"    {seg_id}  {old_val}  ->  {new_val}")

    if dry_run:
        print("\n  DRY RUN -- no rows modified.\n" + "=" * 55 + "\n")
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
