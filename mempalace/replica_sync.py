"""
replica_sync.py — Memory read-replica pull engine (RFC 004 step 1)

Pulls the origin palace's *facts* — verbatim drawer documents + metadata and
knowledge-graph rows — over the /snapshot/* endpoints and folds them into
this machine's local palace. The local vector index is derived here, by this
machine's own embedder, exactly per RFC 004 layer 3: sync the facts, derive
the senses. `chroma.sqlite3` is never copied.

Semantics:

- Drawer folds are upserts by drawer id; documents and metadata are stored
  verbatim, plus ONE additive provenance key: ``replica_origin`` = the
  origin's replica id. That key is what makes delete reconciliation safe —
  only drawers this replica *copied from that origin* are ever deleted when
  they disappear upstream; locally-authored drawers (a local agent's diary)
  are untouchable by reconciliation.
- KG folds are INSERT OR REPLACE by row id: invalidations (valid_to edits)
  converge on re-pull; replication never deletes KG rows.
- Every pass is idempotent. Freshness = re-run the pull; the step-2 op-log
  replaces polling with precise tails later.

Writes on a step-1 replica remain the origin's job — this engine makes
recall local and durable, not capture. Multi-writer memory is RFC 004
step 3.
"""

import logging

from .logsync import SyncPeerError, _peer_get, load_peers

logger = logging.getLogger("mempalace.replica_sync")

_PAGE = 500
REPLICA_ORIGIN_KEY = "replica_origin"


def _pull_drawers(col, url: str, token: str, origin_id: str, vectors_model: str = None) -> dict:
    """Page and fold remote drawers. With ``vectors_model``, request served
    vectors and upsert them explicitly (insert-only fold, RFC 004
    distributed derivation); items the peer has no cached vector for fall
    back to local embedding. Returns fold stats."""
    from .vector_cache import b64_to_vector

    upserted = 0
    vectors_used = 0
    locally_embedded = 0
    offset = 0
    while True:
        params = {"offset": offset, "limit": _PAGE}
        if vectors_model:
            params["vectors"] = vectors_model
        page = _peer_get(url, token, "/snapshot/drawers", params)
        items = page.get("items") or []
        if items:
            with_vec = [item for item in items if item.get("vector_b64")]
            without_vec = [item for item in items if not item.get("vector_b64")]
            for group, explicit in ((with_vec, True), (without_vec, False)):
                if not group:
                    continue
                ids = [item["id"] for item in group]
                documents = [item["document"] for item in group]
                metadatas = [
                    {**(item.get("metadata") or {}), REPLICA_ORIGIN_KEY: origin_id}
                    for item in group
                ]
                if explicit:
                    embeddings = [b64_to_vector(item["vector_b64"]) for item in group]
                    col.upsert(
                        ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings
                    )
                    vectors_used += len(group)
                else:
                    col.upsert(ids=ids, documents=documents, metadatas=metadatas)
                    locally_embedded += len(group)
            upserted += len(items)
        # Authored-only serving can return sparse pages: advance by the
        # server's raw scan cursor, not by the filtered item count.
        next_offset = page.get("next_offset")
        if next_offset is None:
            break
        offset = next_offset
    return {
        "upserted": upserted,
        "vectors_used": vectors_used,
        "locally_embedded": locally_embedded,
    }


def _pull_remote_ids(url: str, token: str) -> set:
    remote_ids = set()
    offset = 0
    while True:
        page = _peer_get(url, token, "/snapshot/ids", {"offset": offset, "limit": 1000})
        remote_ids.update(page.get("ids") or [])
        next_offset = page.get("next_offset")
        if next_offset is None:
            break
        offset = next_offset
    return remote_ids


def _reconcile_deletes(col, remote_ids: set, origin_id: str) -> int:
    """Delete local copies whose upstream original is gone.

    Scoped strictly to drawers stamped ``replica_origin == origin_id`` —
    reconciliation can never touch locally-authored content.
    """
    to_delete = []
    offset = 0
    while True:
        batch = col.get(limit=1000, offset=offset, include=["metadatas"])
        ids = batch.get("ids") or []
        if not ids:
            break
        metas = batch.get("metadatas") or []
        for drawer_id, meta in zip(ids, metas):
            if (meta or {}).get(REPLICA_ORIGIN_KEY) == origin_id and drawer_id not in remote_ids:
                to_delete.append(drawer_id)
        offset += len(ids)
    for start in range(0, len(to_delete), 500):
        col.delete(ids=to_delete[start : start + 500])
    return len(to_delete)


def _pull_kg(kg, url: str, token: str) -> dict:
    counts = {}
    for table in ("entities", "triples"):
        applied = 0
        after = 0
        while True:
            page = _peer_get(
                url, token, "/snapshot/kg", {"table": table, "after": after, "limit": _PAGE}
            )
            rows = page.get("rows") or []
            if not rows:
                break
            for row in rows:
                kg.apply_row(table, row)
                after = max(after, row.get("_rowid") or 0)
                applied += 1
        counts[table] = applied
    return counts


def pull_memory(
    palace_path: str,
    url: str,
    token: str = "",
    reconcile_deletes: bool = True,
    pull_kg: bool = True,
    with_vectors: bool = False,
) -> dict:
    """One full read-replica pass against one origin. Returns fold stats.

    Opens the LOCAL palace's collection (created on first pull) and KG;
    embedding of pulled documents happens here, on this machine's embedder.
    """
    from .knowledge_graph import KnowledgeGraph
    from .palace import get_collection

    manifest = _peer_get(url, token, "/snapshot/manifest")
    origin_id = manifest.get("replica_id") or "unknown-origin"

    vectors_model = None
    if with_vectors:
        # Served vectors are only usable under the LOCAL embedder identity —
        # a vector from a different model poisons the index silently, so the
        # identity gate is loud and mandatory (RFC 001 meets RFC 004).
        from .embedding import get_embedder_identity

        identity = get_embedder_identity()
        vectors_model = identity.model_name
        peer_cache = (manifest.get("vector_cache") or {}).get(vectors_model, 0)
        if not peer_cache:
            raise SyncPeerError(
                f"peer has no vector cache for local embedder identity "
                f"{vectors_model!r} — run 'mempalace replica embed-cache' on the "
                "origin first, or pull without --with-vectors"
            )

    col = get_collection(palace_path, create=True)
    if col is None:
        raise SyncPeerError(f"could not open local collection in {palace_path!r}")

    fold = _pull_drawers(col, url, token, origin_id, vectors_model=vectors_model)
    upserted = fold["upserted"]
    deleted = 0
    if reconcile_deletes:
        deleted = _reconcile_deletes(col, _pull_remote_ids(url, token), origin_id)

    import os

    # KG rows carry no origin stamp, so a reverse pull could overwrite a
    # newer local row with the peer's stale replicated copy (INSERT OR
    # REPLACE has no freshness check). Until step 2a gives KG facts real
    # op provenance, disable KG on pulls from peers that merely replicate
    # your KG back (pull_kg=False / CLI --no-kg).
    kg_counts = {"entities": 0, "triples": 0}
    if pull_kg:
        kg = KnowledgeGraph(
            db_path=os.path.join(os.path.expanduser(palace_path), "knowledge_graph.sqlite3")
        )
        try:
            kg_counts = _pull_kg(kg, url, token)
        finally:
            kg.close()

    return {
        "origin_url": url,
        "origin_replica": origin_id,
        "manifest": manifest,
        "drawers_upserted": upserted,
        "drawers_deleted": deleted,
        "vectors_used": fold["vectors_used"],
        "locally_embedded": fold["locally_embedded"],
        "kg_entities": kg_counts["entities"],
        "kg_triples": kg_counts["triples"],
    }


def pull_from_peers(palace_path: str, pull_kg: bool = True, with_vectors: bool = False) -> list:
    """Run a pull against every peers.json entry; per-peer errors reported,
    never raised (a dead origin must not block the others)."""
    results = []
    for peer in load_peers(palace_path):
        name = peer.get("name") or peer["url"]
        try:
            stats = pull_memory(
                palace_path,
                peer["url"],
                peer.get("token", ""),
                pull_kg=pull_kg,
                with_vectors=with_vectors,
            )
            stats["peer_name"] = name
            results.append(stats)
        except (SyncPeerError, ValueError) as exc:
            results.append({"peer_name": name, "origin_url": peer["url"], "error": str(exc)})
    return results
