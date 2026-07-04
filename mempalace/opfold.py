"""
opfold.py — The fold consumer: remote memory ops → local store (RFC 004 2a)

Op sync (opsync.py) moves ops between op-logs; this module applies them
to the local stores — the palace collection for drawer ops, the
knowledge graph for kg ops — advancing a durable per-consumer cursor in
the op-log, so a crash mid-fold resumes exactly where it stopped and
every apply is idempotent.

Rules, per the RFC 004 merge table and the dual-write shadow posture:

- OWN-ORIGIN ops are skipped: the local dual-write already applied them
  to the store before emitting the op.
- drawer.add: insert-if-absent. Folded copies are stamped with
  ``replica_origin`` (so step-1 authored-only snapshots never re-serve
  them — no echo) and ``op_hlc`` (the LWW head for later revisions).
- drawer.revise / drawer.tombstone: LWW-by-HLC against the stored
  ``op_hlc``. SHADOW GUARD: while Chroma is still the system of record,
  a remote op never mutates an UNSTAMPED (locally-authored) drawer —
  the conflict is counted and logged for the verifier instead of
  applied. The cutover lifts this guard; in tonight's fleet topology
  the case should be ~nonexistent, and if it isn't, we want to see it
  before ops become authoritative, not feel it.
- Tombstones physically delete during shadow (matching today's store
  semantics); the op itself preserves the verbatim content forever.
- kg.assert = G-set by triple id; kg.close = min-valid_to-wins;
  kg.entity.upsert = LWW register. All applied via KnowledgeGraph's
  replication appliers, which never re-emit ops.

The caller owns write serialization: the hub folds under its request
lock; the CLI path requires the hub stopped (single-writer lease), same
rule as ``replica pull``.
"""

import hashlib
import logging

from .oplog import OpLog

logger = logging.getLogger("mempalace.opfold")

FOLD_CONSUMER = "store"

# Payload sha mismatches and shadow-guard conflicts are reported with
# samples capped, counts exact — same convention as the verifier.
SAMPLE_CAP = 20

_DRAWER_KINDS = frozenset({"drawer.add", "drawer.revise", "drawer.tombstone"})
_KG_KINDS = frozenset({"kg.assert", "kg.close", "kg.entity.upsert"})

REPLICA_ORIGIN_KEY = "replica_origin"
OP_HLC_KEY = "op_hlc"


def _result_lists(result):
    getter = getattr(result, "get", None)
    if getter is None:
        return [], [], []
    return (
        list(getter("ids") or []),
        list(getter("documents") or []),
        list(getter("metadatas") or []),
    )


# Chunk ids are contiguous from 0, so an unknown-length chunked drawer's
# physical rows are reconstructed by indexed get-by-ids in blocks — never a
# full-collection scan. One block covers any realistically-sized drawer.
_CHUNK_PROBE = 256


def _chunk_id(drawer_id: str, index: int) -> str:
    return f"{drawer_id}_chunk_{index:06d}"


def _logical_state(collection, drawer_id: str):
    """Presence + provenance of a logical drawer: whether it is a single row
    or chunked, its replica_origin stamp, and the last folded op_hlc.

    A drawer is stored EITHER as one row under its bare id (content at or under
    the chunk limit) OR as contiguous ``_chunk_000000..`` rows. Both cases are
    resolved by a single indexed get of the bare id and chunk 0 — never a
    full-collection where-scan over ``parent_drawer_id`` (which decoded every
    row's metadata per fold op and drove fold drain to hours on large palaces).
    The full physical id set is resolved lazily by ``_physical_ids`` only when
    an op actually mutates the store, keeping the hot add-skip path to one get.
    """
    ids, _docs, metas = _result_lists(
        collection.get(ids=[drawer_id, _chunk_id(drawer_id, 0)], include=["metadatas"])
    )
    if not ids:
        return None
    single = drawer_id in ids
    # Provenance is identical across a drawer's rows; read it off whichever row
    # came back (the bare id for a single-row drawer, else chunk 0).
    meta = (metas[ids.index(drawer_id)] if single else metas[0]) or {}
    return {
        "single": single,
        "replica_origin": meta.get(REPLICA_ORIGIN_KEY) or None,
        "op_hlc": meta.get(OP_HLC_KEY) or None,
    }


def _physical_ids(collection, drawer_id: str, state: dict) -> list[str]:
    """The physical row ids of a present logical drawer, resolved through the
    (collection, id) index. Called only at mutation sites (tombstone delete,
    revise stale-cleanup), so the add-skip path never pays for it."""
    if state["single"]:
        return [drawer_id]
    found: list[str] = []
    start = 0
    while True:
        block = [_chunk_id(drawer_id, i) for i in range(start, start + _CHUNK_PROBE)]
        got, _docs, _metas = _result_lists(collection.get(ids=block, include=["metadatas"]))
        gotset = set(got)
        run = [cid for cid in block if cid in gotset]
        found.extend(run)
        if len(run) < _CHUNK_PROBE:  # a gap ends the contiguous chunk run
            break
        start += _CHUNK_PROBE
    return found


def _chunk_rows(drawer_id: str, content: str, base_meta: dict, chunk_size: int):
    """Chunk exactly like tool_add_drawer: single row at or under the
    limit, ``_chunk_NNNNNN`` rows with parent linkage above it."""
    if len(content) <= chunk_size:
        return [drawer_id], [content], [{**base_meta, "chunk_index": 0}]
    ids, docs, metas = [], [], []
    for start in range(0, len(content), chunk_size):
        index = start // chunk_size
        ids.append(f"{drawer_id}_chunk_{index:06d}")
        docs.append(content[start : start + chunk_size])
        metas.append({**base_meta, "chunk_index": index, "parent_drawer_id": drawer_id})
    return ids, docs, metas


def _apply_drawer_op(op: dict, collection, chunk_size: int, stats: dict) -> None:
    payload = op["payload"]
    drawer_id = payload.get("drawer_id")
    if not drawer_id:
        raise ValueError(f"{op['op_id']}: drawer op has no drawer_id")
    state = _logical_state(collection, drawer_id)
    kind = op["kind"]

    if kind == "drawer.add" and state is not None:
        stats["skipped_existing"] += 1
        return

    if kind in ("drawer.revise", "drawer.tombstone") and state is not None:
        if state["replica_origin"] is None:
            # Shadow guard: never mutate a locally-authored drawer from a
            # remote op while Chroma is authoritative.
            stats["conflicts"] += 1
            if len(stats["conflict_sample"]) < SAMPLE_CAP:
                stats["conflict_sample"].append({"op": op["op_id"], "drawer_id": drawer_id})
            logger.warning(
                "op fold shadow-guard: remote %s for locally-authored drawer %s held",
                kind,
                drawer_id,
            )
            return
        if state["op_hlc"] is not None and op["hlc"] <= state["op_hlc"]:
            stats["skipped_stale"] += 1
            return

    if kind == "drawer.tombstone":
        if state is None:
            stats["skipped_existing"] += 1  # already absent — idempotent
            return
        collection.delete(ids=_physical_ids(collection, drawer_id, state))
        stats["applied_tombstones"] += 1
        return

    content = payload.get("content")
    if not isinstance(content, str) or not content:
        raise ValueError(f"{op['op_id']}: {kind} payload has no content")
    claimed = payload.get("content_sha256")
    if claimed and hashlib.sha256(content.encode("utf-8")).hexdigest() != claimed:
        raise ValueError(f"{op['op_id']}: content fails its own sha256 — refusing to fold")

    base_meta = {
        "wing": payload.get("wing", ""),
        "room": payload.get("room", ""),
        "source_file": payload.get("source_file", ""),
        "added_by": op["author_agent"],
        "filed_at": payload.get("filed_at", op["authored_at"]),
        "id_recipe": payload.get("id_recipe", ""),
        REPLICA_ORIGIN_KEY: op["origin_replica"],
        OP_HLC_KEY: op["hlc"],
    }
    ids, docs, metas = _chunk_rows(drawer_id, content, base_meta, chunk_size)
    collection.upsert(ids=ids, documents=docs, metadatas=metas)
    if state is not None:
        new_ids = set(ids)
        stale = [
            row_id
            for row_id in _physical_ids(collection, drawer_id, state)
            if row_id not in new_ids
        ]
        if stale:
            collection.delete(ids=stale)
        stats["applied_revises"] += 1
    else:
        stats["applied_adds"] += 1


def _apply_kg_op(op: dict, kg, stats: dict) -> None:
    kind, payload = op["kind"], op["payload"]
    if kind == "kg.assert":
        if kg.apply_assert_op(payload):
            stats["applied_kg"] += 1
        else:
            stats["skipped_existing"] += 1
    elif kind == "kg.close":
        stats["applied_kg"] += kg.apply_close_op(payload)
    elif kind == "kg.entity.upsert":
        kg.apply_entity_upsert_op(payload)
        stats["applied_kg"] += 1


def fold_ops(
    oplog: OpLog,
    collection,
    kg,
    chunk_size: int = 800,
    batch: int = 500,
    max_ops: int = None,
) -> dict:
    """Fold unapplied ops into the local store, advancing the cursor.

    The cursor advances past every DECIDED op — applied, skipped, or
    shadow-guard-held (the held conflict is preserved verbatim in the
    op-log itself; the verifier and the cutover revisit it). An op that
    ERRORS stops the fold at its position so the next round retries it:
    convergence may wait, but no op is ever silently passed over.
    """
    stats = {
        "applied_adds": 0,
        "applied_revises": 0,
        "applied_tombstones": 0,
        "applied_kg": 0,
        "skipped_own": 0,
        "skipped_existing": 0,
        "skipped_stale": 0,
        "skipped_other_kind": 0,
        "conflicts": 0,
        "conflict_sample": [],
        "errors": 0,
        "error_sample": [],
        "cursor": oplog.fold_cursor(FOLD_CONSUMER),
    }
    processed = 0
    while True:
        ops = oplog.list_all(after_seq=stats["cursor"], limit=batch)
        if not ops:
            break
        for op in ops:
            if max_ops is not None and processed >= max_ops:
                return stats
            processed += 1
            try:
                if op["origin_replica"] == oplog.replica_id:
                    stats["skipped_own"] += 1
                elif op["kind"] in _DRAWER_KINDS:
                    _apply_drawer_op(op, collection, chunk_size, stats)
                elif op["kind"] in _KG_KINDS:
                    _apply_kg_op(op, kg, stats)
                else:
                    stats["skipped_other_kind"] += 1
            except Exception as exc:
                stats["errors"] += 1
                if len(stats["error_sample"]) < SAMPLE_CAP:
                    stats["error_sample"].append({"op": op["op_id"], "error": str(exc)})
                logger.error("op fold failed at %s; will retry next round", op["op_id"])
                oplog.set_fold_cursor(stats["cursor"], FOLD_CONSUMER)
                return stats
            stats["cursor"] = op["seq"]
            oplog.set_fold_cursor(stats["cursor"], FOLD_CONSUMER)
    return stats
