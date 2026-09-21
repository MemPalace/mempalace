"""
reconcile_v3.py — drain legacy v3-keyed drawers to their content-hash v4 id in
place (RFC 004 write-flip PART B).

opfold PART A re-keys v3-keyed ops on fold, so no NEW ghosts appear. This drains
the ones already in the store: rows tagged ``id_recipe != 'v4'`` that folded in
before PART A landed (sqlite_exact upserted a v3-keyed op under its stale id; a
healed chroma would too). Each is REWRITTEN to ``drawer_<hash(content)>`` — never
deleted, because these are the SOLE copy of their (revised) content, with no v4
twin. If a v4 twin IS present (content already stored), the ghost is dropped as a
duplicate.

In place, not copy-first (unlike ``migrate_v4``): the ghost set is tiny and lives
in the live collection. Idempotent — re-running after a partial pass finishes it,
and content-identical ghosts collapse (first rewrites, the rest see the twin and
drop). Ghosts are selected by ``id_recipe != 'v4'`` metadata ONLY, never by id
shape — v4 multi-chunk drawers carry ``_chunk_NNNNNN`` suffixes and must not be
touched.
"""

import logging

from .ids import make_drawer_id_content_pure

logger = logging.getLogger("mempalace.reconcile_v3")

SAMPLE_CAP = 20


def _get(res, key):
    raw = res.get(key) if hasattr(res, "get") else None
    return list(raw) if raw is not None else None


def _is_ghost(meta: dict) -> bool:
    """A store row is a legacy ghost when its id_recipe is anything but v4."""
    return (meta or {}).get("id_recipe") != "v4"


def _scan_ghost_drawers(collection, batch: int, with_embeddings: bool) -> dict:
    """Group ghost physical rows into logical drawers.

    Returns ``{logical_id: {"rows": [(index, row_id, content, meta, emb)]}}``;
    ``emb`` is None when embeddings are not requested. Registry sentinels and
    v4 rows are excluded.
    """
    include = ["documents", "metadatas"] + (["embeddings"] if with_embeddings else [])
    groups: dict = {}
    offset = 0
    while True:
        res = collection.get(limit=batch, offset=offset, include=include)
        ids = _get(res, "ids") or []
        if not ids:
            break
        docs = _get(res, "documents") or [""] * len(ids)
        metas = _get(res, "metadatas") or [{}] * len(ids)
        embs = _get(res, "embeddings")
        embs = embs if embs is not None else [None] * len(ids)
        for row_id, doc, meta, emb in zip(ids, docs, metas, embs):
            meta = meta or {}
            if row_id.startswith("_reg_") or meta.get("ingest_mode") == "registry":
                continue
            if not _is_ghost(meta):
                continue
            parent = meta.get("parent_drawer_id")
            logical_id = parent or row_id
            index = meta.get("chunk_index")
            index = index if isinstance(index, int) else 0
            g = groups.setdefault(logical_id, {"rows": []})
            g["rows"].append((index, row_id, doc or "", meta, emb))
        if len(ids) < batch:
            break
        offset += len(ids)
    return groups


def _twin_present(collection, v4_id: str) -> bool:
    """Whether the content-hash v4 drawer already exists (bare id or chunk 0)."""
    res = collection.get(ids=[v4_id, f"{v4_id}_chunk_000000"], include=["metadatas"])
    return bool(_get(res, "ids"))


def plan_v3_reconcile(collection, batch: int = 2000) -> dict:
    """Read-only: count ghost drawers and how many would rewrite vs drop.

    Projection mirrors ``apply_v3_reconcile``'s SEQUENTIAL drain over the same
    ``groups.items()`` order: a ghost drops when its content-hash twin is ALREADY
    in the store, OR when an earlier ghost in this same set already mints that v4
    id (duplicate-content ghosts collapse — first rewrites, the rest drop). Only
    the pre-write store is queried, so intra-set duplicates are tracked here rather
    than re-queried, keeping the preview honest (e.g. 109 ghosts, 32 of them dups,
    previews 77 rewrite / 32 drop — exactly what --apply does).
    """
    groups = _scan_ghost_drawers(collection, batch, with_embeddings=False)
    rewrite = drop = 0
    sample: list = []
    projected: set = set()  # v4 ids an earlier ghost in this drain already mints
    for logical_id, g in groups.items():
        content = "".join(doc for _i, _r, doc, _m, _e in sorted(g["rows"]))
        if not content:
            continue
        v4_id = make_drawer_id_content_pure(content)
        if v4_id == logical_id:
            continue  # already content-pure — not actually a ghost to move
        if v4_id in projected or _twin_present(collection, v4_id):
            drop += 1
        else:
            rewrite += 1
            projected.add(v4_id)
        if len(sample) < SAMPLE_CAP:
            sample.append({"old": logical_id, "new": v4_id})
    return {
        "ghost_drawers": len(groups),
        "rewrite": rewrite,
        "drop": drop,
        "sample": sample,
    }


def apply_v3_reconcile(collection, batch: int = 2000, dry_run: bool = False) -> dict:
    """Rewrite ghost drawers to their content-hash v4 id (or drop dups).

    Each logical ghost's physical rows are re-keyed via ``collection.rekey_row``
    (embedding carried, so no re-derive), preserving the chunk layout and stamping
    ``id_recipe=v4``. A ghost whose content already exists as a v4 drawer is
    dropped instead. NEVER a blind delete of un-twinned content.
    """
    groups = _scan_ghost_drawers(collection, batch, with_embeddings=not dry_run)
    stats = {"ghost_drawers": len(groups), "rewritten": 0, "dropped": 0, "rows_rewritten": 0}
    for logical_id, g in groups.items():
        rows = sorted(g["rows"])
        content = "".join(doc for _i, _r, doc, _m, _e in rows)
        if not content:
            continue
        v4_id = make_drawer_id_content_pure(content)
        if v4_id == logical_id:
            continue
        chunked = len(rows) > 1 or bool(rows and rows[0][3].get("parent_drawer_id"))
        if _twin_present(collection, v4_id):
            stats["dropped"] += 1
            if not dry_run:
                collection.delete(ids=[r[1] for r in rows])
            continue
        stats["rewritten"] += 1
        if dry_run:
            continue
        for index, old_row_id, doc, meta, emb in rows:
            new_meta = dict(meta)
            new_meta["id_recipe"] = "v4"
            if chunked:
                new_row_id = f"{v4_id}_chunk_{index:06d}"
                new_meta["parent_drawer_id"] = v4_id
                new_meta["chunk_index"] = index
            else:
                new_row_id = v4_id
                new_meta.pop("parent_drawer_id", None)
            collection.rekey_row(
                old_row_id, new_row_id, content=doc, metadata=new_meta, embedding=emb
            )
            stats["rows_rewritten"] += 1
    return stats
