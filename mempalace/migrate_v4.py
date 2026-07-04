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

import json
import logging
import os
from pathlib import Path

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
    winner_row_ids: list = []
    written = merged_away = 0
    for v4_id, members in by_v4.items():
        # Deterministic winner: best placement, then stable id sort.
        winner_id, _meta = sorted(
            members, key=lambda m: (_placement_key(m[1]), m[0]), reverse=True
        )[0]
        winners[winner_id] = v4_id
        winner_row_ids.extend(drawers[winner_id]["rows"])
        written += 1
        merged_away += len(members) - 1
    return (
        winners,
        alias,
        winner_row_ids,
        {"logical_drawers": len(alias), "written": written, "merged_away": merged_away},
    )


def _get_lists(res, key):
    raw = res.get(key) if hasattr(res, "get") else None
    return list(raw) if raw is not None else None


def _read_embeddings_resilient(collection, ids, stats) -> dict:
    """``{row_id: embedding-or-None}`` for ``ids``, read through the index.

    A six-figure palace can carry localized vector-index damage — a row whose
    metadata is present but whose vector cannot be retrieved (ChromaDB raises
    ``Error finding id``). One such row must not fail the whole migration: try
    the batch, and on any read error fall back to per-row so exactly the damaged
    rows degrade. A row whose vector can't be read maps to None — the target
    re-derives it on write — and is counted in ``vectors_unreadable`` so the
    integrity gap is surfaced, never silent.
    """
    try:
        res = collection.get(ids=ids, include=["embeddings"])
        got = _get_lists(res, "ids") or []
        embs = _get_lists(res, "embeddings")
        if embs is not None:
            return {row_id: emb for row_id, emb in zip(got, embs)}
    except Exception:
        logger.warning(
            "v4 migration: batch embedding read failed for %d ids; retrying per-row",
            len(ids),
            exc_info=True,
        )
    out: dict = {}
    for row_id in ids:
        try:
            res = collection.get(ids=[row_id], include=["embeddings"])
            embs = _get_lists(res, "embeddings")
            out[row_id] = embs[0] if embs else None
        except Exception:
            out[row_id] = None
            stats["vectors_unreadable"] += 1
            logger.warning("v4 migration: vector unreadable for %s — target will re-embed", row_id)
    return out


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
    winners, the alias, and the exact set of winner row ids; pass 2 fetches those
    rows BY ID (indexed, and it sidesteps a ChromaDB failure mode where a large
    offset-paged embeddings read errors at a segment boundary) in blocks of
    ``write_batch`` and upserts them to the target. Peak memory is one write
    batch of vectors, not the whole source; merged-away losers are never fetched.

    Vectors are copied where readable; a row whose vector can't be read (a
    localized index-damage case) is written without one for the target to
    re-derive, and counted in ``vectors_unreadable`` rather than crashing.

    Returns ``{"logical_drawers", "written", "merged_away", "rows_written",
    "vectors_unreadable", "alias": {old_id: v4_id}}``.
    """
    winners, alias, winner_row_ids, counts = _plan_winners(source_col, batch)
    stats = {
        "logical_drawers": counts["logical_drawers"],
        "written": counts["written"],
        "merged_away": counts["merged_away"],
        "rows_written": 0,
        "vectors_unreadable": 0,
        "alias": alias,
    }
    if dry_run:
        return stats

    # Rows carrying a copied vector and rows without are upserted separately: a
    # single upsert can't mix present and absent embeddings, and a row with no
    # readable vector is left for the target to embed on write.
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

    for start in range(0, len(winner_row_ids), write_batch):
        chunk = winner_row_ids[start : start + write_batch]
        base = source_col.get(ids=chunk, include=["documents", "metadatas"])
        b_ids = _get_lists(base, "ids") or []
        if not b_ids:
            continue
        b_docs = _get_lists(base, "documents") or [""] * len(b_ids)
        b_metas = _get_lists(base, "metadatas") or [{}] * len(b_ids)
        emb_map = _read_embeddings_resilient(source_col, b_ids, stats)
        for row_id, doc, meta in zip(b_ids, b_docs, b_metas):
            meta = meta or {}
            parent = meta.get("parent_drawer_id")
            logical_id = parent or row_id
            v4_id = winners.get(logical_id)
            if v4_id is None:
                continue  # defensive: winner_row_ids holds only winner rows
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
            emb = emb_map.get(row_id)
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


# ── Replica sidecar state (the live-swap contract) ────────────────────────
#
# A palace directory is more than its collection: it carries the replica's
# identity and coordination state as sidecar files. For the migrated target to
# be a drop-in replacement (swap it over the live palace and the mesh sees the
# SAME replica, not a stranger), these must be carried:
#
#   replica.json              — the replica identity (``origin_replica`` of
#                               every op this seat authors). Without it the
#                               target mints a fresh id on first promote and
#                               forks the palace's provenance.
#   logstream.sqlite3 (+wal/shm) — the RFC 003 coordination log. Event ids are
#                               drawer-id-independent; history carries over
#                               verbatim.
#   peers.json                — mesh peer configuration.
#   mempalace_embedder.json   — the embedder identity, so vectors the target
#                               re-derives (and every future embed) come from
#                               the same model as the copied ones.
#
# Deliberately NOT carried:
#
#   oplog.sqlite3             — the shadow op-log is REBUILT by re-promotion
#                               after migration, per the RFC 004 cutover plan.
#                               It cannot be carried: its drawer ops reference
#                               v3 ids that no longer exist (verify would go
#                               dirty), and it cannot be rebuilt unilaterally
#                               either — op_ids are ``op_<replica>_<rowid>``
#                               and peers hold version-vector watermarks at the
#                               OLD origin_seq heights, so a lone rebuilt log
#                               would re-mint already-seen op_ids with
#                               different payloads at seqs below every peer's
#                               cursor. The rebuild is only sound as the
#                               coordinated fleet-wide step: every replica
#                               migrates and rebuilds in the same window, so
#                               all cursors reset together (shadow posture
#                               makes discarding the old log safe — it was
#                               never authoritative).
#   vector_cache.sqlite3      — derived cache; the target rebuilds it.

SIDECAR_CARRY = (
    "replica.json",
    "logstream.sqlite3",
    "logstream.sqlite3-wal",
    "logstream.sqlite3-shm",
    "peers.json",
    "mempalace_embedder.json",
)

SIDECAR_EXCLUDE = ("oplog.sqlite3", "vector_cache.sqlite3")


def copy_replica_sidecars(source_palace: str, target_palace: str) -> dict:
    """Carry the replica's sidecar state so the target is swap-ready.

    Copies each :data:`SIDECAR_CARRY` file that exists in ``source_palace``
    into ``target_palace`` (overwriting — the target is the migration's fresh
    output, its sidecars can only be accidental). Never copies the excluded
    files. Returns ``{"carried": [names], "missing": [names],
    "replica_id": str|None}`` — ``replica_id`` read back from the copied
    ``replica.json`` so callers can print/assert the preserved identity.
    """
    import shutil

    src = Path(os.path.expanduser(source_palace))
    dst = Path(os.path.expanduser(target_palace))
    dst.mkdir(parents=True, exist_ok=True)
    carried, missing = [], []
    for name in SIDECAR_CARRY:
        s = src / name
        if s.exists():
            shutil.copy2(s, dst / name)
            carried.append(name)
        else:
            missing.append(name)
    replica_id = None
    rj = dst / "replica.json"
    if rj.exists():
        try:
            replica_id = json.loads(rj.read_text(encoding="utf-8")).get("replica_id")
        except (ValueError, OSError):
            logger.warning("copied replica.json is unreadable", exc_info=True)
    if "replica.json" in missing:
        logger.warning(
            "source palace %s has no replica.json — the target will mint a NEW "
            "replica identity on first promote; a live swap would fork provenance",
            source_palace,
        )
    return {"carried": carried, "missing": missing, "replica_id": replica_id}
