"""
op_emit.py — shared op-emission for the ingest + bulk-delete write paths (RFC 004 2a)

The MCP drawer tools (add/update/delete) emit their own ops inline. The
remaining store-mutation paths — the file/convo miners and the bulk-delete
tools — went straight to the vector store with no op, so their writes never
reached the op-log and could not replicate. This module gives those paths one
place to emit from, in the same dual-write shadow posture: emission never
raises (``shadow_append``), so a failed op can never break a mine or a delete.

A miner upsert of one source file is modeled as:

- ``drawer.revise`` for every chunk it writes. Revise is the upsert semantic
  in the RFC 004 merge table — a peer that lacks the drawer folds it as an
  add, a peer that already holds an older copy takes the new content by
  last-writer-wins. So the same op is correct whether the file is freshly
  mined or re-mined with changed content, without the miner having to know
  which.
- ``drawer.tombstone`` for any chunk id that existed before the re-mine but
  not after — a file that now produces fewer chunks. The tombstone carries the
  dropped chunk's verbatim content so nothing is lost to history.

Each miner chunk is its own content-addressed drawer id (one row = one logical
drawer), so no chunk-group reassembly is needed here — unlike the bulk-delete
path, which groups physical rows back into logical drawers before tombstoning.
"""

import hashlib
import logging
from typing import Optional

from .oplog import OpLog, shadow_append

logger = logging.getLogger("mempalace.op_emit")


OP_HLC_KEY = "op_hlc"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stamp_op_hlc(collection, row_ids, op) -> None:
    """Stamp locally-authored drawer rows with ``op_hlc`` — the LWW head — from
    the op the write just emitted (RFC 004 write-flip).

    This makes a local drawer a first-class last-writer-wins participant: a
    later cross-replica ``drawer.revise``/``tombstone`` resolves by HLC in the
    fold instead of being held as unversioned. Metadata-only update, so no
    re-embed. ``replica_origin`` is deliberately left ABSENT — the "no
    replica_origin stamp = locally authored" convention is load-bearing in
    vector_cache, oppromote, and replica_sync; only ``op_hlc`` is added.
    Best-effort: a failed stamp just leaves the drawer unversioned, which the
    fold's safety net holds rather than clobbers.
    """
    if not op or not row_ids:
        return
    hlc = op.get("hlc") if hasattr(op, "get") else None
    if not hlc:
        return
    try:
        ids = list(row_ids)
        collection.update(ids=ids, metadatas=[{OP_HLC_KEY: hlc}] * len(ids))
    except Exception:
        logger.warning("op_hlc stamp failed for %d row(s)", len(row_ids), exc_info=True)


def emit_miner_writes(
    oplog: Optional[OpLog],
    source_file: str,
    prior: dict,
    new_drawers: list,
    author_agent: str = "mempalace",
) -> list:
    """Emit ops for a miner's (re-)ingest of one source file.

    ``prior`` maps ``drawer_id -> (content, meta)`` for the file's drawers as
    they were BEFORE the re-mine purge (empty for a first mine). ``new_drawers``
    is the list of ``(drawer_id, content, meta)`` just upserted. Revise every
    written chunk; tombstone any prior chunk id no longer present.

    Returns the ``[(drawer_id, op), ...]`` of the revise ops that appended, so
    the caller can stamp each drawer's ``op_hlc`` (:func:`stamp_op_hlc`) — the
    write-flip's LWW head for locally-authored drawers.
    """
    if oplog is None:
        return []
    new_ids = set()
    stamped: list = []
    for drawer_id, content, meta in new_drawers:
        if not drawer_id or not isinstance(content, str) or not content:
            continue
        new_ids.add(drawer_id)
        meta = meta or {}
        op = shadow_append(
            oplog,
            "drawer.revise",
            {
                "drawer_id": drawer_id,
                "wing": meta.get("wing", ""),
                "room": meta.get("room", ""),
                "content": content,
                "content_sha256": _sha(content),
                "source_file": source_file,
                "filed_at": meta.get("filed_at") or "",
                "id_recipe": meta.get("id_recipe", ""),
                "chunks": 1,
            },
            author_agent=author_agent,
        )
        if op is not None:
            stamped.append((drawer_id, op))
    for drawer_id in set(prior) - new_ids:
        content, meta = prior[drawer_id]
        content = content or ""
        meta = meta or {}
        shadow_append(
            oplog,
            "drawer.tombstone",
            {
                "drawer_id": drawer_id,
                "content": content,
                "content_sha256": _sha(content),
                "wing": meta.get("wing", ""),
                "room": meta.get("room", ""),
                "reason": "remine_dropped",
            },
            author_agent=author_agent,
        )
    return stamped


def emit_bulk_tombstones(
    oplog: Optional[OpLog],
    drawers,
    author_agent: str = "mcp",
    reason: str = "bulk_delete",
) -> int:
    """Emit a ``drawer.tombstone`` per logical drawer being bulk-deleted.

    ``drawers`` is an iterable of ``(drawer_id, content, meta)`` — one entry per
    LOGICAL drawer (the caller reassembles chunk groups first). Returns the
    number of tombstones emitted. Shadow posture: never raises.
    """
    if oplog is None:
        return 0
    emitted = 0
    for drawer_id, content, meta in drawers:
        if not drawer_id:
            continue
        content = content or ""
        meta = meta or {}
        shadow_append(
            oplog,
            "drawer.tombstone",
            {
                "drawer_id": drawer_id,
                "content": content,
                "content_sha256": _sha(content),
                "wing": meta.get("wing", ""),
                "room": meta.get("room", ""),
                "reason": reason,
            },
            author_agent=author_agent,
        )
        emitted += 1
    return emitted


def read_logical_drawers_where(collection, where: dict, batch: int = 1000) -> list:
    """Reassemble the logical drawers matching a where-filter into
    ``(drawer_id, content, meta)`` tuples — the input to
    :func:`emit_bulk_tombstones` for the bulk-delete paths.

    Physical rows are grouped by ``parent_drawer_id`` (chunked drawers) or their
    own id (single-row drawers); chunk content is rejoined in ``chunk_index``
    order. Paginated so it survives large sources.
    """
    groups: dict = {}
    offset = 0
    while True:
        res = collection.get(
            where=where, include=["documents", "metadatas"], limit=batch, offset=offset
        )
        ids = list((res.get("ids") if hasattr(res, "get") else None) or [])
        docs = list((res.get("documents") if hasattr(res, "get") else None) or [])
        metas = list((res.get("metadatas") if hasattr(res, "get") else None) or [])
        if not ids:
            break
        for row_id, doc, meta in zip(ids, docs, metas):
            meta = meta or {}
            parent = meta.get("parent_drawer_id")
            logical_id = parent or row_id
            index = meta.get("chunk_index", 0) if isinstance(meta.get("chunk_index"), int) else 0
            g = groups.setdefault(logical_id, {"chunks": [], "meta": None})
            g["chunks"].append((index, doc or ""))
            if g["meta"] is None or index == 0:
                g["meta"] = meta
        if len(ids) < batch:
            break
        offset += len(ids)
    out = []
    for logical_id, g in groups.items():
        content = "".join(doc for _index, doc in sorted(g["chunks"]))
        out.append((logical_id, content, g["meta"] or {}))
    return out
