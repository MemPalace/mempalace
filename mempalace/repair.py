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

from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator, Optional

from chromadb.errors import NotFoundError as ChromaNotFoundError

from .backends.chroma import ChromaBackend, hnsw_capacity_status
from .palace import PalaceWriteAlreadyRunning, palace_write_lock


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
DEFAULT_FROM_SQLITE_BATCH_SIZE = 5000

_SQLITE_EXTRACT_ROWS_SQL = """
SELECT e.embedding_id, em.key, em.string_value, em.int_value,
       em.float_value, em.bool_value
FROM embeddings e
JOIN embedding_metadata em ON em.id = e.id
WHERE e.segment_id = ?
ORDER BY e.segment_id, e.embedding_id
"""


def _sqlite_metadata_segment_id(conn: sqlite3.Connection, collection_name: str) -> Optional[str]:
    row = conn.execute(
        """
        SELECT s.id FROM segments s
        JOIN collections c ON s.collection = c.id
        WHERE c.name = ? AND s.scope = 'METADATA'
        """,
        (collection_name,),
    ).fetchone()
    return row[0] if row else None


def _sqlite_collection_id(conn: sqlite3.Connection, collection_name: str) -> Optional[str]:
    row = conn.execute(
        "SELECT id FROM collections WHERE name = ?",
        (collection_name,),
    ).fetchone()
    return row[0] if row else None


def _sqlite_collection_topic(conn: sqlite3.Connection, collection_name: str) -> Optional[str]:
    collection_id = _sqlite_collection_id(conn, collection_name)
    if collection_id is None:
        return None

    topic = f"persistent://default/default/{collection_id}"
    row = conn.execute(
        "SELECT 1 FROM embeddings_queue WHERE topic = ? LIMIT 1",
        (topic,),
    ).fetchone()
    if row:
        return topic

    row = conn.execute(
        "SELECT topic FROM embeddings_queue WHERE topic LIKE ? LIMIT 1",
        (f"%/{collection_id}",),
    ).fetchone()
    return row[0] if row else topic


def _load_sqlite_vectors(palace_path: str, collection_name: str) -> dict[str, object]:
    """Load latest stored vectors for a collection from Chroma's queue WAL.

    ChromaDB stores vector payloads in ``embeddings_queue`` as FLOAT32 blobs.
    Passing those vectors back to ``upsert`` avoids re-embedding every document
    during SQLite recovery. The table has no useful topic/id index, so this
    intentionally does one sequential scan for the collection topic and keeps
    the newest non-null vector per embedding ID.
    """
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(sqlite_path):
        return {}

    import numpy as np

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        topic = _sqlite_collection_topic(conn, collection_name)
        if topic is None:
            return {}

        vectors = {}
        for emb_id, vector, encoding in conn.execute(
            """
            SELECT id, vector, encoding
            FROM embeddings_queue
            WHERE topic = ? AND vector IS NOT NULL
            ORDER BY seq_id
            """,
            (topic,),
        ):
            if encoding and encoding.upper() != "FLOAT32":
                continue
            vectors[emb_id] = np.frombuffer(vector, dtype="<f4")
        return vectors
    finally:
        conn.close()


def _sqlite_embedding_ids(palace_path: str, collection_name: str) -> list[str]:
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(sqlite_path):
        return []

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        segment_id = _sqlite_metadata_segment_id(conn, collection_name)
        if segment_id is None:
            return []
        rows = conn.execute(
            "SELECT embedding_id FROM embeddings WHERE segment_id = ? ORDER BY embedding_id",
            (segment_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


def _recoverable_collection_names(palace_path: str, primary_collection: str) -> tuple[str, ...]:
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    fallback = (
        (primary_collection, "mempalace_closets")
        if primary_collection != "mempalace_closets"
        else (primary_collection,)
    )
    if not os.path.isfile(sqlite_path):
        return fallback

    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """
                SELECT c.name
                FROM collections c
                JOIN segments s ON s.collection = c.id AND s.scope = 'METADATA'
                JOIN embeddings e ON e.segment_id = s.id
                GROUP BY c.name
                HAVING COUNT(e.embedding_id) > 0
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return fallback

    names = {str(row[0]) for row in rows if not _is_repair_internal_collection(str(row[0]))}
    ordered = [primary_collection]
    if "mempalace_closets" in names and primary_collection != "mempalace_closets":
        ordered.append("mempalace_closets")
    ordered.extend(sorted(names - set(ordered)))
    return tuple(name for name in ordered if name in names or name == primary_collection)


def _is_repair_internal_collection(collection_name: str) -> bool:
    """Return whether ``collection_name`` is an internal repair staging collection."""
    return bool(
        re.search(
            r"__repair_tmp__\d{20}__\d+__[0-9a-f]{8}$",
            collection_name,
        )
    )


def _unique_temp_collection_name(collection_name: str) -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return f"{collection_name}__repair_tmp__{ts}__{os.getpid()}__{secrets.token_hex(4)}"


@dataclass(frozen=True)
class CollectionInventory:
    name: str
    count: int
    ids: frozenset[str]
    missing_documents: frozenset[str]


def _sqlite_collection_inventory(palace_path: str, collection_name: str) -> CollectionInventory:
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(sqlite_path):
        raise RuntimeError(f"source palace has no chroma.sqlite3 at {sqlite_path}")

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        segment_id = _sqlite_metadata_segment_id(conn, collection_name)
        if segment_id is None:
            return CollectionInventory(collection_name, 0, frozenset(), frozenset())

        id_rows = conn.execute(
            "SELECT embedding_id FROM embeddings WHERE segment_id = ?",
            (segment_id,),
        ).fetchall()
        ids = frozenset(str(row[0]) for row in id_rows)
        doc_rows = conn.execute(
            """
            SELECT e.embedding_id
            FROM embeddings e
            LEFT JOIN embedding_metadata em
              ON em.id = e.id AND em.key = 'chroma:document'
            WHERE e.segment_id = ? AND em.id IS NULL
            """,
            (segment_id,),
        ).fetchall()
        missing = frozenset(str(row[0]) for row in doc_rows)
        return CollectionInventory(collection_name, len(ids), ids, missing)
    finally:
        conn.close()


def _sqlite_inventories(
    palace_path: str, collection_names: tuple[str, ...]
) -> dict[str, CollectionInventory]:
    return {
        name: _sqlite_collection_inventory(palace_path, name)
        for name in collection_names
        if not _is_repair_internal_collection(name)
    }


def _validate_no_missing_documents(inventories: dict[str, CollectionInventory]) -> None:
    offenders = {
        name: inv.missing_documents for name, inv in inventories.items() if inv.missing_documents
    }
    if not offenders:
        return
    parts = []
    for name, missing in offenders.items():
        sample = ", ".join(sorted(missing)[:5])
        parts.append(f"{name}: {len(missing)} row(s) missing chroma:document ({sample})")
    raise RuntimeError("source SQLite metadata corruption: " + "; ".join(parts))


def _validate_destination_inventory(
    expected: CollectionInventory, actual: CollectionInventory
) -> None:
    if actual.count != expected.count:
        raise RuntimeError(
            f"{expected.name} count mismatch: expected {expected.count}, got {actual.count}; "
            "run again with --audit-content after resolving the inventory mismatch"
        )
    if actual.ids != expected.ids:
        missing = expected.ids - actual.ids
        extra = actual.ids - expected.ids
        detail = []
        if missing:
            detail.append(f"missing IDs: {', '.join(sorted(missing)[:5])}")
        if extra:
            detail.append(f"extra IDs: {', '.join(sorted(extra)[:5])}")
        raise RuntimeError(
            f"{expected.name} ID set mismatch ({'; '.join(detail)}); "
            "run again with --audit-content after resolving the inventory mismatch"
        )
    if actual.missing_documents:
        sample = ", ".join(sorted(actual.missing_documents)[:5])
        raise RuntimeError(
            f"{expected.name} destination has {len(actual.missing_documents)} row(s) "
            f"missing chroma:document ({sample})"
        )


def _sqlite_documents_by_id(palace_path: str, collection_name: str) -> dict[str, str]:
    return {emb_id: doc for emb_id, doc, _meta in extract_via_sqlite(palace_path, collection_name)}


def _audit_collection_content(source_palace: str, dest_palace: str, collection_name: str) -> None:
    source_docs = _sqlite_documents_by_id(source_palace, collection_name)
    dest_docs = _sqlite_documents_by_id(dest_palace, collection_name)
    if source_docs == dest_docs:
        return
    missing = set(source_docs) - set(dest_docs)
    extra = set(dest_docs) - set(source_docs)
    changed = {
        emb_id
        for emb_id in source_docs.keys() & dest_docs.keys()
        if source_docs[emb_id] != dest_docs[emb_id]
    }
    detail = []
    if missing:
        detail.append(f"missing docs: {', '.join(sorted(missing)[:5])}")
    if extra:
        detail.append(f"extra docs: {', '.join(sorted(extra)[:5])}")
    if changed:
        detail.append(f"changed docs: {', '.join(sorted(changed)[:5])}")
    raise RuntimeError(f"{collection_name} content audit failed ({'; '.join(detail)})")


def _get_palace_path():
    """Resolve palace path from config."""
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().palace_path
    except Exception:
        default = os.path.join(os.path.expanduser("~"), ".mempalace", "palace")
        return default


def _get_collection_name() -> str:
    """Resolve drawers collection name from config."""
    try:
        from .config import get_configured_collection_name

        return get_configured_collection_name()
    except Exception:
        return COLLECTION_NAME


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


def _sanitize_metadatas(metadatas):
    # ChromaDB 1.5.x rejects None / {} metadata during upsert; preserve
    # rebuild progress by replacing sparse historical rows with a sentinel.
    return [
        m if (isinstance(m, dict) and len(m) > 0) else {"_repaired_empty_meta": True}
        for m in metadatas
    ]


def _extract_drawers(col, total: int, batch_size: int, include_embeddings: bool = False):
    all_ids = []
    all_docs = []
    all_metas = []
    all_embeddings = []
    offset = 0
    include = ["documents", "metadatas"]
    if include_embeddings:
        include.append("embeddings")
    while offset < total:
        batch = col.get(
            limit=batch_size,
            offset=offset,
            include=include,
        )
        if not batch["ids"]:
            break
        all_ids.extend(batch["ids"])
        all_docs.extend(batch["documents"])
        all_metas.extend(_sanitize_metadatas(batch["metadatas"]))
        if include_embeddings:
            embeddings = batch.get("embeddings")
            if embeddings is not None:
                all_embeddings.extend(embeddings)
        offset += len(batch["ids"])
    if len(all_embeddings) != len(all_ids):
        all_embeddings = []
    return all_ids, all_docs, all_metas, all_embeddings or None


def _stage_collection_from_source(
    backend,
    palace_path: str,
    source_col,
    total: int,
    batch_size: int,
    collection_name: str,
    *,
    include_embeddings: bool,
    temp_name: Optional[str] = None,
    progress=print,
):
    temp_name = temp_name or _unique_temp_collection_name(collection_name)
    _delete_collection_if_exists(backend, palace_path, temp_name)

    progress(f"  Building temporary collection: {temp_name}")
    temp_col = backend.create_collection(palace_path, temp_name)
    include = ["documents", "metadatas"]
    if include_embeddings:
        include.append("embeddings")

    staged = 0
    offset = 0
    try:
        while offset < total:
            batch = source_col.get(limit=batch_size, offset=offset, include=include)
            batch_ids = batch.get("ids") or []
            if not batch_ids:
                break
            kwargs = {
                "ids": batch_ids,
                "documents": batch["documents"],
                "metadatas": _sanitize_metadatas(batch["metadatas"]),
            }
            embeddings = batch.get("embeddings") if include_embeddings else None
            if include_embeddings:
                if embeddings is None or len(embeddings) != len(batch_ids):
                    progress(
                        "  Stored embeddings were not returned for a full batch; "
                        "restarting temp rebuild with fresh embeddings for every row."
                    )
                    _delete_collection_if_exists(backend, palace_path, temp_name)
                    return _stage_collection_from_source(
                        backend,
                        palace_path,
                        source_col,
                        total,
                        batch_size,
                        collection_name,
                        include_embeddings=False,
                        temp_name=temp_name,
                        progress=progress,
                    )
                kwargs["embeddings"] = embeddings
            temp_col.upsert(**kwargs)
            staged += len(batch_ids)
            offset += len(batch_ids)
            progress(f"  Staged {staged}/{total} drawers...")

        _verify_collection_count(temp_col, staged, "temporary rebuild")
    except Exception:
        _delete_collection_if_exists(backend, palace_path, temp_name)
        raise
    return temp_col, temp_name, staged


def _swap_temp_collection_into_live(
    backend,
    palace_path: str,
    temp_col,
    temp_name: str,
    collection_name: str,
    expected: int,
    *,
    progress=print,
) -> int:
    progress("  Swapping temporary collection into live name...")
    try:
        backend.delete_collection(palace_path, collection_name)
    except Exception as exc:
        try:
            _delete_collection_if_exists(backend, palace_path, temp_name)
        except Exception:
            pass
        raise RebuildCollectionError(str(exc), live_replaced=False) from exc

    try:
        _rename_collection(temp_col, collection_name)
        live_col = backend.get_collection(palace_path, collection_name)
        _verify_collection_count(live_col, expected, "rebuilt live collection")
    except Exception as exc:
        raise RebuildCollectionError(str(exc), live_replaced=True) from exc

    try:
        _delete_collection_if_exists(backend, palace_path, temp_name)
    except Exception:
        pass
    return expected


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


def _rename_collection(collection, new_name: str) -> None:
    collection.modify(name=new_name)


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
    all_embeddings=None,
) -> int:
    expected = len(all_ids)
    collection_name = collection_name or _drawers_collection_name()
    temp_name = _unique_temp_collection_name(collection_name)

    try:
        _delete_collection_if_exists(backend, palace_path, temp_name)

        progress(f"  Building temporary collection: {temp_name}")
        temp_col = backend.create_collection(palace_path, temp_name)
        staged = 0
        for i in range(0, expected, batch_size):
            batch_ids = all_ids[i : i + batch_size]
            batch_docs = all_docs[i : i + batch_size]
            batch_metas = all_metas[i : i + batch_size]
            kwargs = {"documents": batch_docs, "ids": batch_ids, "metadatas": batch_metas}
            if all_embeddings is not None:
                kwargs["embeddings"] = all_embeddings[i : i + batch_size]
            temp_col.upsert(**kwargs)
            staged += len(batch_ids)
            progress(f"  Staged {staged}/{expected} drawers...")
        _verify_collection_count(temp_col, expected, "temporary rebuild")

        return _swap_temp_collection_into_live(
            backend,
            palace_path,
            temp_col,
            temp_name,
            collection_name,
            expected,
            progress=progress,
        )
    except RebuildCollectionError as exc:
        try:
            _delete_collection_if_exists(backend, palace_path, temp_name)
        except Exception:
            pass
        raise exc
    except Exception as exc:
        try:
            _delete_collection_if_exists(backend, palace_path, temp_name)
        except Exception:
            pass
        raise RebuildCollectionError(str(exc), live_replaced=False) from exc


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
    collection_name = collection_name or _get_collection_name()
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            segment_id = _sqlite_metadata_segment_id(conn, collection_name)
            if segment_id is None:
                return 0
            row = conn.execute(
                "SELECT COUNT(*) FROM embeddings WHERE segment_id = ?",
                (segment_id,),
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


def rebuild_index(
    palace_path=None,
    confirm_truncation_ok: bool = False,
    collection_name: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
    reembed: bool = False,
):
    """Rebuild the HNSW index through a verified temporary collection.

    1. Back up ONLY chroma.sqlite3 (not the bloated HNSW files)
    2. Stream drawers from ChromaDB into a temporary collection
    3. Cross-check the staged count against SQLite ground truth (#1208 guard)
    4. Swap the temporary collection into the live collection name
    5. Restore the SQLite backup if the live swap fails

    ``confirm_truncation_ok`` overrides the safety guard from step 3.
    Set to ``True`` only when you have independently verified that the
    palace genuinely contains exactly the extracted number of drawers
    (typically only a concern for palaces sized at exactly 10 000 rows).

    ``progress`` is the callable used for status output. Defaults to
    :class:`_DefaultProgress` which prints with elapsed/rate/ETA
    annotations on ``Staged N/M`` and ``Re-filed N/M`` lines. Pass a
    custom callable (e.g. a daemon-side capture for HTTP status, or a
    silent ``lambda *_: None`` for tests) to override.

    By default, stored embeddings are reused batch-by-batch while staging.
    ``reembed=True`` omits embeddings from the staged upserts so ChromaDB
    regenerates every vector under the configured embedding function.
    """
    if progress is None:
        progress = _DefaultProgress()
    palace_path = palace_path or _get_palace_path()
    collection_name = collection_name or _drawers_collection_name()
    try:
        with palace_write_lock(palace_path, operation="repair"):
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

            # Back up BEFORE creating the temp collection. The snapshot must represent
            # the pre-repair palace, and backup failures must not leave temp HNSW files.
            sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
            backup_path = sqlite_path + ".backup"
            if os.path.exists(sqlite_path):
                size_mb = os.path.getsize(sqlite_path) / 1e6
                progress(f"  Backing up chroma.sqlite3 ({size_mb:.0f} MB)...")
                shutil.copy2(sqlite_path, backup_path)
                progress(f"  Backup: {backup_path}")

            # Stage drawers in batches. Keep batches bounded instead of materializing
            # the full embedding matrix in Python memory.
            progress("\n  Extracting drawers...")
            batch_size = 5000
            temp_col = None
            temp_name = None
            try:
                temp_col, temp_name, extracted = _stage_collection_from_source(
                    backend,
                    palace_path,
                    col,
                    total,
                    batch_size,
                    collection_name,
                    include_embeddings=not reembed,
                    progress=progress,
                )
                progress(f"  Extracted {extracted} drawers")

                # ── #1208 guard ──────────────────────────────────────────────
                # Refuse to swap when extraction looks short of SQLite ground truth.
                check_extraction_safety(
                    palace_path,
                    extracted,
                    confirm_truncation_ok,
                    collection_name=collection_name,
                )
            except TruncationDetected as e:
                progress(e.message)
                if temp_name is not None:
                    _delete_collection_if_exists(backend, palace_path, temp_name)
                return
            except Exception as exc:
                if temp_name is not None:
                    _delete_collection_if_exists(backend, palace_path, temp_name)
                e = RebuildCollectionError(str(exc), live_replaced=False)
                progress(f"\n  ERROR during rebuild: {e}")
                progress("  Rebuild aborted before completion.")
                progress("  Live collection was not replaced; leaving the original palace untouched.")
                raise e

            progress("  Rebuilding collection with hnsw:space=cosine...")
            try:
                filed = _swap_temp_collection_into_live(
                    backend,
                    palace_path,
                    temp_col,
                    temp_name,
                    collection_name,
                    extracted,
                    progress=progress,
                )
            except Exception as exc:
                e = (
                    exc
                    if isinstance(exc, RebuildCollectionError)
                    else RebuildCollectionError(str(exc), live_replaced=False)
                )
                progress(f"\n  ERROR during rebuild: {e}")
                progress("  Rebuild aborted before completion.")
                if e.live_replaced and os.path.exists(backup_path):
                    progress(f"  Restoring from backup: {backup_path}")
                    try:
                        _close_chroma_handles(palace_path, backend=backend)
                        _delete_collection_if_exists(backend, palace_path, temp_name)
                        _delete_collection_if_exists(backend, palace_path, collection_name)
                        shutil.copy2(backup_path, sqlite_path)
                        progress("  Backup restored. Palace is back to pre-repair state.")
                    except Exception as restore_error:
                        progress(f"  Backup restore failed: {restore_error}")
                        progress(f"  Manual restore required from: {backup_path}")
                elif not e.live_replaced:
                    progress(
                        "  Live collection was not replaced; leaving the original palace untouched."
                    )
                else:
                    progress("  No backup available. Re-mine from source files to recover.")
                raise e

            progress(f"\n  Repair complete. {filed} drawers rebuilt.")
            progress("  HNSW index is now clean with cosine distance metric.")
            progress(f"\n{'=' * 55}\n")
    except PalaceWriteAlreadyRunning as exc:
        progress(f"\n  ABORT: {exc}")
        return


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
    reembed: bool = False,
) -> int:
    """Stream rows for one collection from SQLite and upsert into a
    freshly-created collection at ``dest_palace``. Returns rows
    upserted. Raises :class:`RebuildPartialError` (with the underlying
    chromadb exception as ``__cause__``) on any upsert failure so the
    caller can stop the loop and print recovery instructions instead of
    silently shipping a partial palace.
    """
    vectors_by_id = {}
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    upserted = 0
    failure_stage = "preparing rebuild"
    col = None

    def _flush() -> int:
        nonlocal upserted
        if not ids:
            return upserted

        if vectors_by_id:
            col.upsert(
                ids=list(ids),
                documents=list(docs),
                metadatas=list(metas),
                embeddings=[vectors_by_id[emb_id] for emb_id in ids],
            )
        else:
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
        failure_stage = "loading source IDs"
        source_ids = _sqlite_embedding_ids(source_palace, collection_name)
        failure_stage = "loading stored embeddings"
        vectors_by_id = {} if reembed else _load_sqlite_vectors(source_palace, collection_name)
        if vectors_by_id:
            source_id_set = set(source_ids)
            if not source_id_set or not source_id_set.issubset(vectors_by_id):
                print("    stored embeddings incomplete; re-embedding this collection")
                vectors_by_id = {}
        if vectors_by_id:
            print(f"    loaded {len(vectors_by_id)} stored embeddings")
        failure_stage = "creating destination collection"
        col = backend.create_collection(dest_palace, collection_name)
        failure_stage = "extracting and upserting rows"
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
        if source_ids and upserted != len(source_ids):
            failure_stage = "validating extracted source row count"
            raise RuntimeError(
                f"source/extracted count mismatch for {collection_name!r}: "
                f"source has {len(source_ids)} rows, extracted {upserted}"
            )
        failure_stage = "validating destination count"
        _verify_collection_count(col, upserted, f"rebuilt {collection_name}")
    except Exception as exc:  # noqa: BLE001 — chromadb raises many shapes
        partial = dict(counts_so_far)
        partial[collection_name] = upserted
        msg_parts = [
            f"Rebuild failed in collection {collection_name!r} during {failure_stage} "
            f"after {upserted} rows: {exc!r}",
            f"Partial palace left at: {dest_palace}",
        ]
        if archive_path is not None:
            msg_parts.append(f"Original palace archived at: {archive_path}")
            msg_parts.append(
                "  Recover by removing the partial dest and re-running with "
                f"mempalace --palace {shlex.quote(dest_palace)} repair "
                f"--mode from-sqlite --source {shlex.quote(archive_path)} --yes"
            )
        else:
            msg_parts.append("  Source palace is unchanged. Remove the partial dest and re-run.")
        message = "\n  ".join(msg_parts)
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
        segment_id = _sqlite_metadata_segment_id(conn, collection_name)
        if segment_id is None:
            return

        current_id: Optional[str] = None
        current_meta: dict = {}

        def _value_from_columns(sv, iv, fv, bv):
            if sv is not None:
                return sv
            if iv is not None:
                return iv
            if fv is not None:
                return fv
            if bv is not None:
                return bool(bv)
            return None

        def _emit_current():
            if current_id is None:
                return None
            kv = dict(current_meta)
            if "chroma:document" not in kv:
                return None
            doc = kv.pop("chroma:document")
            return current_id, doc, kv

        for emb_id, key, sv, iv, fv, bv in conn.execute(_SQLITE_EXTRACT_ROWS_SQL, (segment_id,)):
            if current_id is None:
                current_id = emb_id
            elif emb_id != current_id:
                emitted = _emit_current()
                if emitted is not None:
                    yield emitted
                current_id = emb_id
                current_meta = {}

            value = _value_from_columns(sv, iv, fv, bv)
            if value is not None:
                current_meta[key] = value

        emitted = _emit_current()
        if emitted is not None:
            yield emitted
    finally:
        conn.close()


def _restore_in_place_rebuild_archive(
    *,
    archive_path: str,
    dest_palace: str,
    collection_name: str,
    source_inventories: dict[str, CollectionInventory],
    backend: ChromaBackend | None,
) -> int:
    print("  In-place rebuild failed before completion; restoring archived palace...")
    _close_chroma_handles(dest_palace, backend=backend)
    if os.path.exists(dest_palace):
        shutil.rmtree(dest_palace)
    shutil.move(archive_path, dest_palace)
    restored = _sqlite_collection_inventory(dest_palace, collection_name)
    _validate_destination_inventory(source_inventories[collection_name], restored)
    print(f"  Restore verified: {restored.count} rows in {collection_name}")
    return restored.count


def rebuild_from_sqlite(
    source_palace: str,
    dest_palace: str,
    *,
    archive_existing_dest: bool = False,
    batch_size: int = DEFAULT_FROM_SQLITE_BATCH_SIZE,
    collection_name: Optional[str] = None,
    reembed: bool = False,
    audit_content: bool = False,
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

    Reuses stored vectors from Chroma's SQLite queue by default so recovery
    does not call the embedding model for every row. Pass ``reembed=True`` to
    regenerate vectors instead.

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
    stdout. Returns ``{}`` on validation failures (missing source,
    refusing to overwrite). Raises :class:`RebuildPartialError` if a
    chromadb upsert fails partway through. Cross-palace partial rebuilds
    are left in place so the user can inspect what landed. In-place
    partial rebuilds restore the archived palace before returning the
    nonzero failure to the caller.

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
    source_palace = os.path.realpath(os.path.abspath(os.path.expanduser(source_palace)))
    dest_palace = os.path.realpath(os.path.abspath(os.path.expanduser(dest_palace)))
    collection_name = collection_name or _drawers_collection_name()

    src_db = os.path.join(source_palace, "chroma.sqlite3")

    in_place = os.path.normcase(source_palace) == os.path.normcase(dest_palace)
    repair_locks = []

    def _release_repair_locks() -> None:
        while repair_locks:
            repair_locks.pop().__exit__(None, None, None)

    try:
        repair_lock = palace_write_lock(dest_palace, operation="repair")
        repair_lock.__enter__()
        repair_locks.append(repair_lock)
        if not in_place:
            source_lock = palace_write_lock(source_palace, operation="repair source")
            source_lock.__enter__()
            repair_locks.append(source_lock)
    except PalaceWriteAlreadyRunning as exc:
        _release_repair_locks()
        print(f"\n  ABORT: {exc}")
        return {}

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
    if not _validate_rebuild_from_sqlite_inputs(
        source_palace,
        dest_palace,
        source_db_path=src_db,
        collection_name=collection_name,
        in_place=in_place,
        archive_existing_dest=archive_existing_dest,
    ):
        _release_repair_locks()
        return {}

    try:
        recoverable_collections = _recoverable_collection_names(source_palace, collection_name)
        source_inventories = _sqlite_inventories(source_palace, recoverable_collections)
        _validate_no_missing_documents(source_inventories)
    except Exception:
        _release_repair_locks()
        raise

    archive_path: Optional[str] = None
    if in_place:
        try:
            archive_path = _prepare_in_place_rebuild_source(dest_palace)
            source_palace = archive_path
            src_db = os.path.join(source_palace, "chroma.sqlite3")
        except Exception:
            _release_repair_locks()
            raise

    backend: ChromaBackend | None = None
    counts: dict[str, int] = {}
    try:
        os.makedirs(dest_palace, exist_ok=True)
        backend = ChromaBackend()
        for cname in recoverable_collections:
            print(f"\n  [{cname}]")
            upserted = _rebuild_one_collection(
                backend=backend,
                source_palace=source_palace,
                dest_palace=dest_palace,
                collection_name=cname,
                batch_size=batch_size,
                archive_path=archive_path,
                counts_so_far=counts,
                reembed=reembed,
            )
            counts[cname] = upserted
            if upserted == 0:
                print(f"    no rows found for {cname} in source palace")
            else:
                print(f"    done: {upserted} rows in {cname}")
            actual = _sqlite_collection_inventory(dest_palace, cname)
            _validate_destination_inventory(source_inventories[cname], actual)
            if audit_content:
                _audit_collection_content(source_palace, dest_palace, cname)
    except RebuildPartialError as exc:
        try:
            if archive_path is not None:
                restored_count = _restore_in_place_rebuild_archive(
                    archive_path=archive_path,
                    dest_palace=dest_palace,
                    collection_name=collection_name,
                    source_inventories=source_inventories,
                    backend=backend,
                )
                restored_message = "\n  ".join(
                    [
                        exc.message.split("\n", 1)[0],
                        f"Original palace restored at: {dest_palace}",
                        f"Restore verified: {restored_count} rows in {collection_name}",
                        "Failed partial rebuild was removed; the source palace is the restored live path.",
                    ]
                )
                print(f"\n  ERROR: {restored_message}")
                raise RebuildPartialError(
                    restored_message,
                    partial_counts=exc.partial_counts,
                    failed_collection=exc.failed_collection,
                    dest_palace=dest_palace,
                    archive_path=None,
                ) from exc
            print(f"\n  ERROR: {exc.message}")
            raise
        finally:
            if backend is not None:
                backend.close()
            _release_repair_locks()
    except Exception:
        try:
            if archive_path is not None:
                _restore_in_place_rebuild_archive(
                    archive_path=archive_path,
                    dest_palace=dest_palace,
                    collection_name=collection_name,
                    source_inventories=source_inventories,
                    backend=backend,
                )
            raise
        finally:
            if backend is not None:
                backend.close()
            _release_repair_locks()

    print(f"\n  Rebuild complete. {sum(counts.values())} total rows.")
    if archive_path is not None:
        print(f"  Original palace archived at: {archive_path}")
    print(f"{'=' * 55}\n")
    if backend is not None:
        backend.close()
    _release_repair_locks()
    return counts


def _validate_rebuild_from_sqlite_inputs(
    source_palace: str,
    dest_palace: str,
    *,
    source_db_path: str,
    collection_name: str,
    in_place: bool,
    archive_existing_dest: bool,
) -> bool:
    """Return True when the from-SQLite rebuild preflight passes."""

    def _source_primary_count() -> "int | None":
        count = sqlite_drawer_count(source_palace, collection_name)
        if count == 0:
            print(f"\n  Source collection {collection_name!r} has no rows; refusing rebuild.")
        return count

    if not in_place and os.path.exists(dest_palace):
        print(
            f"\n  Refusing to rebuild into existing path: {dest_palace}\n"
            "  Move it aside, pass a different dest, or set "
            "archive_existing_dest=True if rebuilding in place "
            "(source_palace == dest_palace)."
        )
        return False

    if in_place and not archive_existing_dest:
        print(
            "\n  Source and dest are the same path. Pass "
            "archive_existing_dest=True (CLI: --archive-existing) to move "
            "the existing palace aside, or pass a different source_palace= "
            "(CLI: --source)."
        )
        return False

    if not os.path.isfile(source_db_path):
        print(f"\n  Source palace has no chroma.sqlite3 at {source_db_path}")
        return False

    return _source_primary_count() != 0


def _prepare_in_place_rebuild_source(dest_palace: str) -> str:
    """Archive ``dest_palace`` and clear the cached Chroma system state."""
    archive_path = _unique_archive_path(dest_palace)
    print(f"  Archiving {dest_palace} → {archive_path}")
    shutil.move(dest_palace, archive_path)

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

    return archive_path


def _unique_archive_path(dest_palace: str) -> str:
    """Return a non-existing in-place rebuild archive path for ``dest_palace``."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    base = f"{dest_palace}.pre-rebuild-{ts}"
    if not os.path.exists(base):
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError(f"could not allocate unique archive path for {dest_palace}")


def _repair_internal_collection_counts(palace_path: str) -> dict[str, int]:
    """Return stale internal repair collection names and metadata row counts."""
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(sqlite_path):
        return {}

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT c.name, COUNT(e.id)
            FROM collections c
            JOIN segments s ON s.collection = c.id
            LEFT JOIN embeddings e ON e.segment_id = s.id
            WHERE s.scope = 'METADATA'
              AND instr(c.name, '__repair_tmp__') > 0
            GROUP BY c.name
            ORDER BY c.name
            """
        ).fetchall()
        return {
            str(name): int(count)
            for name, count in rows
            if _is_repair_internal_collection(str(name))
        }
    finally:
        conn.close()


def _cleanup_repair_internal_collections(
    palace_path: str, artifacts: dict[str, int]
) -> dict[str, str]:
    """Delete internal repair staging collections via Chroma and report outcomes."""
    if not artifacts:
        return {}
    outcomes: dict[str, str] = {}
    try:
        with palace_write_lock(palace_path, operation="repair cleanup"):
            backend = ChromaBackend()
            try:
                for name in artifacts:
                    try:
                        backend.delete_collection(palace_path, name)
                        outcomes[name] = "deleted"
                    except Exception as exc:  # noqa: BLE001 - Chroma raises several shapes
                        outcomes[name] = f"failed: {exc}"
            finally:
                backend.close()
    except PalaceWriteAlreadyRunning as exc:
        for name in artifacts:
            outcomes[name] = f"refused: {exc}"
    return outcomes


def status(
    palace_path=None,
    collection_name: Optional[str] = None,
    *,
    cleanup_temp: bool = False,
    assume_yes: bool = False,
) -> dict:
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
    Passing ``cleanup_temp=True`` is the explicit exception: it takes the
    palace write lock and deletes stale internal collections matching
    MemPalace's generated repair-temp naming scheme after reporting them.

    Returns a wrapper dict with per-collection capacity checks
    (``drawers`` and ``closets``), detected ``repair_artifacts``, and
    ``cleanup`` outcomes. Missing palace paths return the same wrapper
    shape with unknown per-collection statuses.
    """
    palace_path = palace_path or _get_palace_path()
    collection_name = collection_name or _drawers_collection_name()
    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — Status")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")

    if not os.path.isdir(palace_path):
        print("  No palace found.\n")
        unknown = {
            "status": "unknown",
            "message": "no palace at path",
            "sqlite_count": None,
            "hnsw_count": None,
            "divergence": None,
            "diverged": False,
        }
        return {
            "status": "unknown",
            "message": "no palace at path",
            "drawers": unknown,
            "closets": unknown,
            "repair_artifacts": {},
            "cleanup": {},
        }

    artifacts = _repair_internal_collection_counts(palace_path)
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
    cleanup_results: dict[str, str] = {}
    print("\n  [repair artifacts]")
    if not artifacts:
        print("    none found")
    else:
        for name, count in artifacts.items():
            print(f"    {name}: {count:,} rows")
        print("    status:         stale internal temp collection(s)")
        print(
            "    note:           live collections are present; temp collections are ignored by recovery"
        )
        print(
            "    cleanup:        mempalace "
            f"--palace {shlex.quote(palace_path)} repair-status --cleanup-temp --yes"
        )
        if cleanup_temp:
            if not assume_yes:
                print(
                    "    cleanup skipped: pass --yes with --cleanup-temp to delete temp collections"
                )
            else:
                cleanup_results = _cleanup_repair_internal_collections(palace_path, artifacts)
                for name, outcome in cleanup_results.items():
                    print(f"    cleanup {name}: {outcome}")
                artifacts = {
                    name: count
                    for name, count in artifacts.items()
                    if cleanup_results.get(name) != "deleted"
                }
    print()
    if drawers["diverged"] or closets["diverged"]:
        status_value = "needs_repair"
    elif artifacts:
        status_value = "artifacts"
    else:
        status_value = "ok"
    return {
        "status": status_value,
        "message": "",
        "drawers": drawers,
        "closets": closets,
        "repair_artifacts": artifacts,
        "cleanup": cleanup_results,
    }


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
