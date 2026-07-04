"""
migrate_v4.py — the v4 content-pure id migration (the RFC 004 id-purity design)

Today's drawer ids embed organization and provenance: miner drawers hash
``(source_file, chunk_index)``, MCP drawers hash ``(wing, room, content)``. So
the same content in two places is two different drawers, and moving a drawer
between wings changes its identity — which is why organization can't be
represented as an op (it lives *inside* identity). The v4 recipe makes identity
the verbatim content alone (``drawer_<hash(content)>``); wing/room and
source_file become plain metadata, and the same content anywhere in the mesh is
the same drawer.

This is an audited, irreversible rewrite, so it runs in two stages the RFC
mandates — **plan first, on a copy**:

- :func:`plan_v4_migration` is READ-ONLY. It reassembles every logical drawer,
  computes its v4 id, and reports the alias map (old id → v4 id), the content
  COLLISION groups (distinct old drawers whose content is identical — they
  merge into one v4 drawer, the cross-machine dedup), how many drawers are
  already v4 (idempotency), and how many inbound references (knowledge-graph
  ``source_drawer_id``, tunnel endpoints) point at drawers that will be
  renamed. Nothing is written.

- :func:`apply_v4_migration` (copy-first, vectors copied not re-derived) runs
  once a plan is validated. It is two-pass and streaming — content+metadata to
  decide winners, then a second pass that carries vectors to the target a write
  batch at a time — so a six-figure palace rewrites in bounded memory rather
  than materializing every drawer and vector at once.
"""

import logging

from .ids import make_drawer_id_content_pure

logger = logging.getLogger("mempalace.migrate_v4")

SAMPLE_CAP = 20


def _result_lists(result):
    getter = getattr(result, "get", None)
    if getter is None:
        return [], [], []
    return (
        list(getter("ids") or []),
        list(getter("documents") or []),
        list(getter("metadatas") or []),
    )


def _scan_logical_drawers(collection, batch):
    """Group physical rows into logical drawers with their full content.

    Returns ``{logical_id: {"content": str, "meta": dict, "rows": [row_id]}}``.
    Registry sentinels are excluded (they are miner bookkeeping, not content).
    """
    groups: dict = {}
    rows_scanned = 0
    registry_skipped = 0
    offset = 0
    while True:
        ids, docs, metas = _result_lists(
            collection.get(limit=batch, offset=offset, include=["documents", "metadatas"])
        )
        if not ids:
            break
        for row_id, doc, meta in zip(ids, docs, metas):
            rows_scanned += 1
            meta = meta or {}
            if row_id.startswith("_reg_") or meta.get("ingest_mode") == "registry":
                registry_skipped += 1
                continue
            parent = meta.get("parent_drawer_id")
            logical_id = parent or row_id
            index = meta.get("chunk_index")
            index = index if isinstance(index, int) else 0
            g = groups.setdefault(logical_id, {"chunks": [], "meta": None, "rows": []})
            g["chunks"].append((index, doc or ""))
            g["rows"].append(row_id)
            if g["meta"] is None or index == 0:
                g["meta"] = meta
        if len(ids) < batch:
            break
        offset += len(ids)
    out = {}
    for logical_id, g in groups.items():
        content = "".join(doc for _index, doc in sorted(g["chunks"]))
        out[logical_id] = {"content": content, "meta": g["meta"] or {}, "rows": g["rows"]}
    return out, rows_scanned, registry_skipped


def plan_v4_migration(collection, kg_source_ids=None, tunnel_drawer_ids=None, batch=2000) -> dict:
    """Produce the v4 migration plan for a palace collection. Read-only.

    ``kg_source_ids`` / ``tunnel_drawer_ids`` are the sets of drawer ids
    referenced by the knowledge graph (``source_drawer_id``) and by tunnels; the
    caller reads them from those stores so this stays store-agnostic. The plan
    reports how many of those references point at a drawer whose id changes.
    """
    drawers, rows_scanned, registry_skipped = _scan_logical_drawers(collection, batch)

    alias: dict = {}
    v4_to_old: dict = {}
    already_v4 = 0
    empty = []
    for logical_id, info in drawers.items():
        content = info["content"]
        if not content:
            empty.append(logical_id)
            continue
        new_id = make_drawer_id_content_pure(content)
        alias[logical_id] = new_id
        v4_to_old.setdefault(new_id, []).append(logical_id)
        if logical_id == new_id or (info["meta"] or {}).get("id_recipe") == "v4":
            already_v4 += 1

    # Only ids that actually change need a rewrite + alias entry.
    changing = {old: new for old, new in alias.items() if old != new}
    collisions = {new: sorted(olds) for new, olds in v4_to_old.items() if len(olds) > 1}
    collision_drawers = sum(len(olds) for olds in collisions.values())

    kg_source_ids = set(kg_source_ids or [])
    tunnel_drawer_ids = set(tunnel_drawer_ids or [])
    kg_refs_remapped = sorted(kg_source_ids & set(changing))
    tunnel_refs_remapped = sorted(tunnel_drawer_ids & set(changing))
    kg_refs_dangling = sorted(kg_source_ids - set(alias) - {""})
    tunnel_refs_dangling = sorted(tunnel_drawer_ids - set(alias) - {""})

    return {
        "rows_scanned": rows_scanned,
        "registry_skipped": registry_skipped,
        "logical_drawers": len(drawers),
        "already_v4": already_v4,
        "changing": len(changing),
        "alias_sample": dict(list(changing.items())[:SAMPLE_CAP]),
        "collision_groups": len(collisions),
        "collision_drawers": collision_drawers,
        "collision_sample": {k: collisions[k] for k in list(collisions)[:SAMPLE_CAP]},
        "empty_drawers": len(empty),
        "empty_sample": empty[:SAMPLE_CAP],
        "kg_refs_remapped": len(kg_refs_remapped),
        "kg_refs_remapped_sample": kg_refs_remapped[:SAMPLE_CAP],
        "kg_refs_dangling": len(kg_refs_dangling),
        "tunnel_refs_remapped": len(tunnel_refs_remapped),
        "tunnel_refs_dangling": len(tunnel_refs_dangling),
        # The full alias/collision maps drive the applier; kept out of the
        # summary above so a CLI --json stays readable, exposed for the applier.
        "_alias": changing,
        "_collisions": collisions,
    }


def _placement_key(meta: dict):
    """Winner-selection key for a content-collision group. Latest filed_at wins
    (mirroring the org.file last-writer-wins placement register), preferring a
    drawer that actually carries a wing+room; ties broken deterministically by
    the caller's stable id sort."""
    meta = meta or {}
    has_place = 1 if (meta.get("wing") and meta.get("room")) else 0
    return (has_place, str(meta.get("filed_at") or ""))


def _plan_winners(collection, batch):
    """Pass 1 (no embeddings): reassemble logical drawers, bucket by v4 id, and
    pick each content-collision group's winner.

    Returns ``(winners, alias, counts)`` where ``winners`` maps the WINNING
    logical id -> v4 id (the drawers whose rows get written) and ``alias`` maps
    EVERY changing/merging logical id -> v4 id (so inbound references can be
    repointed). Vectors are never read here — the palace's ~GB of embeddings
    stay on disk until the write pass streams them a batch at a time.
    """
    drawers, _rows_scanned, _reg = _scan_logical_drawers(collection, batch)
    by_v4: dict = {}
    alias: dict = {}
    for logical_id, info in drawers.items():
        content = info["content"]
        if not content:
            continue
        v4_id = make_drawer_id_content_pure(content)
        alias[logical_id] = v4_id
        by_v4.setdefault(v4_id, []).append((logical_id, info["meta"] or {}))
    winners: dict = {}
    written = merged_away = 0
    for v4_id, members in by_v4.items():
        # Deterministic winner: best placement, then stable id sort.
        winner_id, _meta = sorted(
            members, key=lambda m: (_placement_key(m[1]), m[0]), reverse=True
        )[0]
        winners[winner_id] = v4_id
        written += 1
        merged_away += len(members) - 1
    return (
        winners,
        alias,
        {"logical_drawers": len(alias), "written": written, "merged_away": merged_away},
    )


def _get_lists(res, key):
    raw = res.get(key) if hasattr(res, "get") else None
    return list(raw) if raw is not None else None


def apply_v4_migration(
    source_col, target_col, batch: int = 2000, write_batch: int = 1000, dry_run: bool = False
) -> dict:
    """Rewrite ``source_col`` into ``target_col`` under content-pure v4 ids.

    Copy-first and non-destructive: the source is only read; the operator gives
    a fresh target and swaps after validating. Vectors are COPIED, never
    re-derived. Content-identical drawers merge into one v4 drawer (the winner
    chosen by :func:`_placement_key`); every merged-away id still maps to the v4
    id in the returned alias so inbound references can be repointed.

    Two-pass and streaming so a six-figure palace migrates in bounded memory:
    pass 1 (:func:`_plan_winners`) reads content + metadata only to decide the
    winners and alias; pass 2 re-streams the source WITH embeddings and upserts
    the winners' rows to the target in blocks of ``write_batch``. Peak memory is
    one write batch of vectors, not the whole source — the earlier all-in-RAM
    materialization peaked at multiple GB on the real palace. Batched upserts
    also cut the per-call + index-build overhead that dominated the write phase.

    Returns ``{"logical_drawers", "written", "merged_away", "rows_written",
    "alias": {old_id: v4_id}}``.
    """
    winners, alias, counts = _plan_winners(source_col, batch)
    stats = {
        "logical_drawers": counts["logical_drawers"],
        "written": counts["written"],
        "merged_away": counts["merged_away"],
        "rows_written": 0,
        "alias": alias,
    }
    if dry_run:
        return stats

    # Rows carrying a copied vector and rows without are upserted separately: a
    # single upsert can't mix present and absent embeddings, and a row with no
    # vector is left for the target to embed on write.
    emb_ids, emb_docs, emb_metas, emb_vecs = [], [], [], []
    plain_ids, plain_docs, plain_metas = [], [], []

    def flush():
        if emb_ids:
            target_col.upsert(
                ids=emb_ids, documents=emb_docs, metadatas=emb_metas, embeddings=emb_vecs
            )
            stats["rows_written"] += len(emb_ids)
            emb_ids.clear(), emb_docs.clear(), emb_metas.clear(), emb_vecs.clear()
        if plain_ids:
            target_col.upsert(ids=plain_ids, documents=plain_docs, metadatas=plain_metas)
            stats["rows_written"] += len(plain_ids)
            plain_ids.clear(), plain_docs.clear(), plain_metas.clear()

    offset = 0
    while True:
        res = source_col.get(
            limit=batch, offset=offset, include=["documents", "metadatas", "embeddings"]
        )
        ids = _get_lists(res, "ids") or []
        if not ids:
            break
        docs = _get_lists(res, "documents") or [""] * len(ids)
        metas = _get_lists(res, "metadatas") or [{}] * len(ids)
        embs = _get_lists(res, "embeddings")
        embs = embs if embs is not None else [None] * len(ids)
        for row_id, doc, meta, emb in zip(ids, docs, metas, embs):
            meta = meta or {}
            if row_id.startswith("_reg_") or meta.get("ingest_mode") == "registry":
                continue
            parent = meta.get("parent_drawer_id")
            logical_id = parent or row_id
            v4_id = winners.get(logical_id)
            if v4_id is None:
                continue  # a merged-away loser, an empty drawer, or not a drawer
            new_meta = dict(meta)
            new_meta["id_recipe"] = "v4"
            if parent:
                index = meta.get("chunk_index")
                index = index if isinstance(index, int) else 0
                new_row_id = f"{v4_id}_chunk_{index:06d}"
                new_meta["parent_drawer_id"] = v4_id
                new_meta["chunk_index"] = index
            else:
                new_row_id = v4_id
            if emb is not None:
                emb_ids.append(new_row_id)
                emb_docs.append(doc or "")
                emb_metas.append(new_meta)
                emb_vecs.append(emb)
            else:
                plain_ids.append(new_row_id)
                plain_docs.append(doc or "")
                plain_metas.append(new_meta)
            if len(emb_ids) >= write_batch or len(plain_ids) >= write_batch:
                flush()
        if len(ids) < batch:
            break
        offset += len(ids)
    flush()
    return stats


def read_kg_source_ids(kg) -> set:
    """Distinct non-null ``source_drawer_id`` values in the knowledge graph."""
    try:
        conn = kg._conn() if hasattr(kg, "_conn") else kg.conn
    except Exception:
        return set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_drawer_id FROM triples WHERE source_drawer_id IS NOT NULL"
        ).fetchall()
    except Exception:
        logger.debug("could not read KG source_drawer_id set", exc_info=True)
        return set()
    return {row[0] for row in rows if row and row[0]}


def remap_kg_source_ids(kg, alias: dict) -> int:
    """Repoint knowledge-graph ``source_drawer_id`` provenance at the v4 ids.

    Every triple whose ``source_drawer_id`` is an old id becomes the v4 id, so a
    triple's "which drawer did I come from" survives the rewrite. Returns the
    number of triples updated. Deterministic and idempotent (an id already v4 is
    not in ``alias`` and is left alone).
    """
    if not alias:
        return 0
    conn = kg._conn() if hasattr(kg, "_conn") else kg.conn
    updated = 0
    with conn:
        for old_id, new_id in alias.items():
            cur = conn.execute(
                "UPDATE triples SET source_drawer_id = ? WHERE source_drawer_id = ?",
                (new_id, old_id),
            )
            updated += cur.rowcount or 0
    return updated
