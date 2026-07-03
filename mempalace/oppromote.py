"""
oppromote.py — The local-capture promotion pass (RFC 004 step 2a)

The op-log only knows about writes that happened after the dual-write
shadow shipped. Everything filed before it — mined sets, pre-shadow
captures, rows written while a hub's op-log was failing — exists only in
the vector store: real memory with no op to carry it. Promotion closes
that gap once per replica: every locally-authored logical drawer that has
no drawer op yet is re-emitted as a ``drawer.add`` op under this
replica's identity, with ``promoted: true`` marking the backfill.

After a clean promotion, the op-log covers the replica's entire authored
history — the precondition for the step-3 write-flip, and the mechanism
that lets FUTURE replicas receive that history as ops instead of
snapshot pulls.

Scope rules:

- Rows stamped ``replica_origin`` are remote-owned copies (folded or
  snapshot-pulled); their ORIGIN promotes them. Skipped here.
- Logical drawers that already have any drawer op (dual-write era, or a
  prior promotion run) are skipped — re-running is free and emits
  nothing, which is the idempotency contract.
- ``_reg_`` registry sentinels (miner bookkeeping, room ``_registry``)
  are not memory content and are never promoted.
- Chunked drawers are reassembled exactly the way the verifier reads
  them: the direct row if one exists, else the chunk group joined in
  ``chunk_index`` order — so ``oplog verify`` replays a promoted op to
  the same sha256 the store already holds.

Promotion reads the store and writes only the op-log, so it is safe to
run alongside a live hub (the same posture as ``replica embed-cache``);
the hub's own fold skips own-origin ops by rule. Each drawer is
independent: an error is counted and the pass continues, unlike the
fold's stop-and-retry, because nothing downstream depends on emission
order.
"""

import hashlib
import logging
import re
from datetime import datetime

from .oplog import OpLog

logger = logging.getLogger("mempalace.oppromote")

PROMOTE_AUTHOR_FALLBACK = "promotion"

_DRAWER_KINDS = ("drawer.add", "drawer.revise", "drawer.tombstone")

# Only tool_add_drawer's oversized path mints ``_chunk_NNNNNN`` row ids,
# and it always writes ``parent_drawer_id`` alongside — the id parse is a
# fallback for rows that lost their metadata, not a primary signal.
_CHUNK_ID_RE = re.compile(r"^(?P<parent>.+)_chunk_(?P<index>\d{6})$")

SAMPLE_CAP = 20

REPLICA_ORIGIN_KEY = "replica_origin"


def _result_lists(result):
    getter = getattr(result, "get", None)
    if getter is None:
        return [], [], []
    return (
        list(getter("ids") or []),
        list(getter("documents") or []),
        list(getter("metadatas") or []),
    )


def _iter_store_rows(collection, batch: int):
    """Page (row_id, metadata) over the whole collection."""
    offset = 0
    while True:
        ids, _docs, metas = _result_lists(
            collection.get(limit=batch, offset=offset, include=["metadatas"])
        )
        if not ids:
            return
        yield from zip(ids, metas)
        if len(ids) < batch:
            return
        offset += len(ids)


def scan_logical_drawers(collection, batch: int = 2000) -> dict:
    """Group store rows into logical drawers with provenance flags.

    Returns ``{"drawers": {logical_id: info}, "rows_scanned": n,
    "registry_skipped": n}`` where info carries the member rows in chunk
    order, the direct row id if one exists, the representative metadata
    (first chunk's), and whether any member row is remote-stamped.
    """
    drawers: dict = {}
    rows_scanned = 0
    registry_skipped = 0
    for row_id, meta in _iter_store_rows(collection, batch):
        rows_scanned += 1
        meta = meta or {}
        if row_id.startswith("_reg_") or meta.get("ingest_mode") == "registry":
            registry_skipped += 1
            continue
        parent = meta.get("parent_drawer_id")
        if parent:
            logical_id, index, direct = parent, meta.get("chunk_index"), False
            if not isinstance(index, int):
                match = _CHUNK_ID_RE.match(row_id)
                index = int(match.group("index")) if match else 0
        else:
            logical_id, index, direct = row_id, 0, True
        info = drawers.setdefault(
            logical_id, {"rows": [], "direct_row": None, "meta": None, "stamped": False}
        )
        info["rows"].append((index, row_id))
        if direct:
            info["direct_row"] = row_id
        if meta.get(REPLICA_ORIGIN_KEY):
            info["stamped"] = True
        if info["meta"] is None or index == 0:
            info["meta"] = meta
    return {
        "drawers": drawers,
        "rows_scanned": rows_scanned,
        "registry_skipped": registry_skipped,
    }


def _read_content(collection, info: dict) -> str:
    """Reassemble a logical drawer's content exactly as the verifier
    reads it: direct row wins; otherwise chunks joined in index order."""
    if info["direct_row"] is not None:
        row_ids = [info["direct_row"]]
    else:
        row_ids = [row_id for _index, row_id in sorted(info["rows"])]
    ids, docs, _metas = _result_lists(collection.get(ids=row_ids, include=["documents"]))
    by_id = dict(zip(ids, docs))
    return "".join(by_id.get(row_id) or "" for row_id in row_ids)


def promote_local_rows(
    oplog: OpLog,
    collection,
    dry_run: bool = False,
    limit: int = None,
    batch: int = 2000,
) -> dict:
    """Emit a ``drawer.add`` op for every op-less locally-authored drawer.

    Idempotent: drawers whose id already appears as a drawer-op subject
    are skipped, so a rerun after a partial pass (crash, ``--limit``)
    promotes only the remainder.
    """
    scan = scan_logical_drawers(collection, batch=batch)
    already_promoted = oplog.distinct_subjects(kinds=_DRAWER_KINDS)
    stats = {
        "replica_id": oplog.replica_id,
        "dry_run": dry_run,
        "rows_scanned": scan["rows_scanned"],
        "registry_skipped": scan["registry_skipped"],
        "logical_drawers": len(scan["drawers"]),
        "remote_skipped": 0,
        "already_op_carried": 0,
        "promoted": 0,
        "skipped_empty": 0,
        "empty_sample": [],
        "errors": 0,
        "error_sample": [],
        "remaining": 0,
    }
    for logical_id in sorted(scan["drawers"]):
        info = scan["drawers"][logical_id]
        if info["stamped"]:
            stats["remote_skipped"] += 1
            continue
        if logical_id in already_promoted:
            stats["already_op_carried"] += 1
            continue
        if limit is not None and stats["promoted"] >= limit:
            stats["remaining"] += 1
            continue
        if dry_run:
            # A dry run only needs the candidate COUNT, so it must not read
            # content: one get-by-id per drawer turned a 156k-drawer dry run
            # into a 40-minute grind. Empties aren't excluded here (they're
            # rare and only detectable by reading content) — the count is an
            # upper bound on what a real run promotes.
            stats["promoted"] += 1
            continue
        try:
            content = _read_content(collection, info)
            if not content:
                stats["skipped_empty"] += 1
                if len(stats["empty_sample"]) < SAMPLE_CAP:
                    stats["empty_sample"].append(logical_id)
                continue
            meta = info["meta"] or {}
            payload = {
                "drawer_id": logical_id,
                "wing": meta.get("wing", ""),
                "room": meta.get("room", ""),
                "content": content,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "source_file": meta.get("source_file", ""),
                "filed_at": meta.get("filed_at") or datetime.now().isoformat(),
                "id_recipe": meta.get("id_recipe", ""),
                "chunks": 1 if info["direct_row"] is not None else len(info["rows"]),
                "promoted": True,
            }
            author = meta.get("added_by")
            author = author.strip() if isinstance(author, str) and author.strip() else None
            oplog.append("drawer.add", payload, author_agent=author or PROMOTE_AUTHOR_FALLBACK)
            stats["promoted"] += 1
        except Exception as exc:
            stats["errors"] += 1
            if len(stats["error_sample"]) < SAMPLE_CAP:
                stats["error_sample"].append({"drawer_id": logical_id, "error": str(exc)})
            logger.error("promotion failed for drawer %s", logical_id, exc_info=True)
    return stats
