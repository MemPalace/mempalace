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

- The applier (separate, copy-first, vectors copied not re-derived) consumes a
  validated plan. It is intentionally not in this first cut: you do not rewrite
  a six-figure palace before seeing the plan's collision count and blast radius
  on the real data.
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
