# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


# ==================== KNOWLEDGE GRAPH ====================

# A recursive kg_query stops after this many facts and says so, so a walk
# with no predicate over a dense graph cannot return the whole graph.
_KG_QUERY_MAX_FACTS = 5000


def _temporal_bound_key(value, *, end: bool = False) -> Optional[str]:
    if not value:
        return None
    text = str(value)
    if "T" in text:
        return text
    return f"{text}T23:59:59Z" if end else f"{text}T00:00:00Z"


def _fact_interval_bucket(row: dict, now_key: str) -> str:
    start_key = _temporal_bound_key(row.get("valid_from"), end=False)
    end_key = _temporal_bound_key(row.get("valid_to"), end=True)
    if start_key and start_key > now_key:
        return "future"
    if end_key and end_key < now_key:
        return "historical"
    return "active"


def tool_kg_query(
    entity: str,
    as_of: str = None,
    direction: str = "both",
    predicate: str = None,
    recurse: bool = False,
    max_depth: int = 20,
):
    """Query the knowledge graph for an entity's relationships.

    By default this is one-hop behavior (direct facts only). When ``recurse`` is
    true, perform breadth-first traversal in the requested direction and return
    de-duplicated facts discovered up to ``max_depth`` hops.
    """
    try:
        entity = sanitize_kg_value(entity, "entity")
        as_of = sanitize_iso_temporal(as_of, "as_of")
        predicate = sanitize_name(predicate, "predicate") if predicate else None
    except ValueError as e:
        return {"error": str(e)}

    if direction not in ("outgoing", "incoming", "both"):
        return {"error": "direction must be 'outgoing', 'incoming', or 'both'"}

    recurse = bool(recurse)
    max_depth = max(1, min(int(max_depth or 20), 100))

    def _filtered_facts(node_id: str) -> list[dict]:
        facts = _call_kg(lambda kg: kg.query_entity(node_id, as_of=as_of, direction=direction))
        if not predicate:
            return [fact for fact in facts if isinstance(fact, dict)]
        return [
            fact for fact in facts if isinstance(fact, dict) and fact.get("predicate") == predicate
        ]

    if not recurse:
        results = _filtered_facts(entity)
        if as_of is None:
            now_key = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            active = []
            historical = []
            future = []
            for row in results:
                bucket = _fact_interval_bucket(row, now_key)
                if bucket == "future":
                    future.append(row)
                elif bucket == "historical":
                    historical.append(row)
                else:
                    active.append(row)
        else:
            active = results
            historical = []
            future = []

        payload = {
            "entity": entity,
            "as_of": as_of,
            "direction": direction,
            "predicate": predicate,
            "recurse": False,
            "active_facts": active,
            "historical_facts": historical,
            "future_facts": future,
            "facts": results,
            "count": len(results),
        }

        if results:
            resolved_names = {
                r.get("subject") if r.get("direction") == "outgoing" else r.get("object")
                for r in results
            }
            resolved_names.discard(None)
            if len(resolved_names) == 1:
                resolved = next(iter(resolved_names))
                if resolved != entity:
                    payload["resolved_from"] = entity
                    payload["entity"] = resolved
        else:
            candidates = _call_kg(lambda kg: kg.find_entity_candidates(entity))
            if candidates:
                payload["candidates"] = candidates

        return payload

    # One query per level of the walk, not one query_entity() per node.
    walk = _call_kg(
        lambda kg: kg.traverse(
            entity,
            direction=direction,
            predicate=predicate,
            as_of=as_of,
            max_depth=max_depth,
            max_facts=_KG_QUERY_MAX_FACTS,
        )
    )
    traversed = walk["facts"]

    payload = {
        "entity": entity,
        "as_of": as_of,
        "direction": direction,
        "predicate": predicate,
        "recurse": True,
        "max_depth": max_depth,
        "visited_nodes": walk["visited_nodes"],
        "facts": traversed,
        "count": len(traversed),
    }
    if walk["truncated"]:
        payload["truncated"] = True
        payload["max_facts"] = _KG_QUERY_MAX_FACTS

    if not traversed:
        candidates = _call_kg(lambda kg: kg.find_entity_candidates(entity))
        if candidates:
            payload["candidates"] = candidates

    return payload


def tool_kg_add(
    subject: str,
    predicate: str,
    object: str,
    valid_from: str = None,
    valid_to: str = None,
    source_closet: str = None,
    source_file: str = None,
    source_drawer_id: str = None,
):
    """Add a relationship to the knowledge graph.

    All temporal and provenance fields are optional. ``valid_to`` lets callers
    backfill historical facts with a known end date/time in a single call
    instead of a separate ``kg_invalidate`` call.

    Temporal values accept either ``YYYY-MM-DD`` or canonical UTC datetimes in
    the form ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    try:
        subject = sanitize_kg_value(subject, "subject")
        predicate = sanitize_name(predicate, "predicate")
        object = sanitize_kg_value(object, "object")
        valid_from = sanitize_iso_temporal(valid_from, "valid_from")
        valid_to = sanitize_iso_temporal(valid_to, "valid_to")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    _wal_log(
        "kg_add",
        {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source_closet": source_closet,
            "source_file": source_file,
            "source_drawer_id": source_drawer_id,
        },
    )

    try:
        triple_id = _call_kg(
            lambda kg: kg.add_triple(
                subject,
                predicate,
                object,
                valid_from=valid_from,
                valid_to=valid_to,
                source_closet=source_closet,
                source_file=source_file,
                source_drawer_id=source_drawer_id,
            )
        )
    except Exception as e:
        # Preserve the dispatcher-visible exception contract (tool_kg_add lets
        # KG write errors bubble through _call_kg, which the MCP dispatcher
        # turns into a -32000 response with context). Record the intent's
        # outcome in the WAL before re-raising so the audit trail shows the
        # error instead of a bare ``result: null``.
        _wal_result("kg_add", {"success": False, "error": str(e)})
        raise
    outcome = {
        "success": True,
        "triple_id": triple_id,
        "fact": f"{subject} → {predicate} → {object}",
    }
    _wal_result("kg_add", outcome)
    return outcome


def tool_kg_invalidate(subject: str, predicate: str, object: str, ended: str = None):
    """Mark a fact as no longer true.

    Returns the actual ``ended`` date/time that was stored. When the caller
    omits ``ended``, the underlying graph stamps ``date.today()`` and the
    response reflects that resolved value.

    Temporal values accept either ``YYYY-MM-DD`` or canonical UTC datetimes in
    the form ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    try:
        subject = sanitize_kg_value(subject, "subject")
        predicate = sanitize_name(predicate, "predicate")
        object = sanitize_kg_value(object, "object")
        ended = sanitize_iso_temporal(ended, "ended")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resolved_ended = ended or date.today().isoformat()

    _wal_log(
        "kg_invalidate",
        {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "ended": resolved_ended,
        },
    )

    _call_kg(lambda kg: kg.invalidate(subject, predicate, object, ended=resolved_ended))
    return {
        "success": True,
        "fact": f"{subject} → {predicate} → {object}",
        "ended": resolved_ended,
    }


def tool_kg_supersede(
    subject: str,
    predicate: str,
    old_object: str,
    new_object: str,
    at: str = None,
):
    """Atomically replace one fact with another at a single shared boundary.

    Closes ``(subject, predicate, old_object)`` and opens
    ``(subject, predicate, new_object)`` at one shared instant, so a
    point-in-time query at the boundary returns only the new value. Use this
    instead of a separate ``kg_invalidate`` + ``kg_add`` when a single-valued
    fact changes (e.g. a model, employer, or address changes).

    ``at`` accepts ``YYYY-MM-DD`` or a canonical UTC datetime
    (``YYYY-MM-DDTHH:MM:SSZ``) and defaults to the current UTC instant.
    """
    try:
        subject = sanitize_kg_value(subject, "subject")
        predicate = sanitize_name(predicate, "predicate")
        old_object = sanitize_kg_value(old_object, "old_object")
        new_object = sanitize_kg_value(new_object, "new_object")
        at = sanitize_iso_temporal(at, "at")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    _wal_log(
        "kg_supersede",
        {
            "subject": subject,
            "predicate": predicate,
            "old_object": old_object,
            "new_object": new_object,
            "at": at,
        },
    )

    # Domain ValueErrors from kg.supersede (e.g. inverted boundary) are left to
    # bubble to the dispatcher, matching tool_kg_add / tool_kg_invalidate: the
    # -32000 response carries error_class + message in error.data. Only input
    # sanitization above returns the {success: False} envelope.
    triple_id = _call_kg(lambda kg: kg.supersede(subject, predicate, old_object, new_object, at=at))
    return {
        "success": True,
        "triple_id": triple_id,
        "fact": f"{subject} → {predicate} → {new_object}",
        "superseded": old_object,
    }


def tool_kg_timeline(entity: str = None, limit: int = 100, offset: int = 0):
    """Get chronological timeline of facts, optionally for one entity.

    Paginated with ``limit``/``offset`` following the ``tool_list_drawers``
    convention; defaults match the historical behavior (first 100 facts).
    """
    limit = max(1, min(limit, _MAX_RESULTS))
    offset = max(0, offset)
    if entity is not None:
        try:
            entity = sanitize_kg_value(entity, "entity")
        except ValueError as e:
            return {"error": str(e)}

    def _query(kg):
        return {
            "timeline": kg.timeline(entity, limit=limit, offset=offset),
            "total": kg.timeline_total(entity),
        }

    result = _call_kg(_query)
    return {
        "entity": entity or "all",
        "timeline": result["timeline"],
        "count": len(result["timeline"]),
        "total": result["total"],
        "offset": offset,
        "limit": limit,
    }


def tool_kg_stats():
    """Knowledge graph overview: entities, triples, relationship types."""
    return _call_kg(lambda kg: kg.stats())


# ==================== LINEAGE AND MERGE GRAPH ====================
#
# Lineage (``synthesized-from``) and merge (``merged-into``) links are ordinary
# KG facts. These tools read every link a call needs with
# KnowledgeGraph.reachable_edges(), one query per level of the walk for all
# the nodes at once, and walk the result in memory with mempalace.lineage.
#
# Each tool opens the drawer collection once and passes it down instead of
# calling another tool that opens it again: _get_collection() can replace the
# client between calls, and a handle taken before that stops working.

_MERGED_INTO = "merged-into"
_SYNTHESIZED_FROM = "synthesized-from"
_MAX_MERGE_HOPS = 50


def _canonical_chains(nodes, max_hops: int = _MAX_MERGE_HOPS) -> dict:
    """Follow ``merged-into`` for many nodes with one walk.

    Returns ``{node: {"chain": [...]}}`` or, for a node whose chain loops,
    ``{node: {"error": ..., "chain": [...]}}``. Chains hold stored names and
    start with the node exactly as given.
    """
    from .. import lineage

    nodes = list(dict.fromkeys(nodes))
    if not nodes:
        return {}

    def _resolve(kg):
        edges, names = kg.reachable_edges(nodes, _MERGED_INTO, max_depth=max_hops)
        merged_into = lineage.adjacency(edges)
        out = {}
        for node in nodes:
            chain, cycle = lineage.canonical_chain(kg.entity_id(node), merged_into, max_hops)
            if cycle is not None:
                out[node] = {
                    "error": "merged-into cycle detected",
                    "chain": [node] + [names.get(i, i) for i in cycle[1:]],
                }
            else:
                out[node] = {"chain": [node] + [names.get(i, i) for i in chain[1:]]}
        return out

    return _call_kg(_resolve)


def _lineage_parents(kg, starts, max_depth=None):
    """``synthesized-from`` adjacency (by id) below ``starts``, and id -> name."""
    from .. import lineage

    edges, names = kg.reachable_edges(starts, _SYNTHESIZED_FROM, max_depth=max_depth)
    return lineage.adjacency(edges), names


def _ancestor_sets(nodes, max_depth: int) -> dict:
    """Each node's ``synthesized-from`` ancestors (stored names), one walk for all."""
    from .. import lineage

    nodes = list(dict.fromkeys(nodes))
    if not nodes:
        return {}

    def _walk(kg):
        parents, names = _lineage_parents(kg, nodes, max_depth=max_depth)
        return {
            node: {
                names.get(a, a) for a in lineage.ancestors(kg.entity_id(node), parents, max_depth)
            }
            for node in nodes
        }

    return _call_kg(_walk)


def tool_resolve_canonical(node_id: str, max_hops: int = 50):
    """Resolve canonical node by following active merged-into links."""
    try:
        node_id = sanitize_kg_value(node_id, "node_id")
    except ValueError as e:
        return {"error": str(e)}

    max_hops = max(1, min(int(max_hops or 50), 200))
    resolved = _canonical_chains([node_id], max_hops)[node_id]
    if "error" in resolved:
        return resolved

    chain = resolved["chain"]
    return {
        "node_id": node_id,
        "canonical_node_id": chain[-1],
        "hops": len(chain) - 1,
        "chain": chain,
    }


def tool_get_height(node_id: str):
    """Compute canonical lineage height over active ``synthesized-from`` edges.

    Height is defined as the longest outgoing path from the canonical node to a
    leaf source (no outgoing lineage edges). Returns both computed height and
    any stored metadata hint for observability.
    """
    from .. import lineage

    resolved = tool_resolve_canonical(node_id)
    if "error" in resolved:
        return resolved

    start = resolved["canonical_node_id"]

    def _height(kg):
        parents, _names = _lineage_parents(kg, [start])
        start_id = kg.entity_id(start)
        return lineage.heights(parents, [start_id])[start_id]

    computed_height = _call_kg(_height)

    stored_height = None
    col = _get_collection()
    if col:
        record = _logical_drawer_record(col, start)
        if record is not None:
            stored_height = _height_from_record(record)

    return {
        "node_id": start,
        "height": computed_height,
        "stored_height": stored_height,
        "canonical_chain": resolved.get("chain", [start]),
    }


def _closets_collection():
    from ..palace import get_closets_collection

    try:
        return get_closets_collection(_config.palace_path, create=False)
    except Exception:
        logger.debug("closet collection lookup failed", exc_info=True)
        return None


def _closet_records(closets_col, wing: str = None, room: str = None) -> list[dict]:
    """List closet records with minimal metadata for graph-validity checks."""
    if closets_col is None:
        return []

    where = {}
    if wing:
        where["wing"] = wing
    if room:
        where["room"] = room
    if len(where) > 1:
        where = {"$and": [{key: value} for key, value in where.items()]}

    get_kwargs = {"include": ["metadatas"]}
    if where:
        get_kwargs["where"] = where

    try:
        results = closets_col.get(**get_kwargs)
    except Exception:
        logger.debug("closet scan failed", exc_info=True)
        return []

    ids = _get_result_ids(results)
    metadatas = _chroma_field(results, "metadatas", []) or []
    return [
        {
            "closet_id": closet_id,
            "metadata": _safe_meta(metadatas[idx] if idx < len(metadatas) else {}),
        }
        for idx, closet_id in enumerate(ids)
    ]


def _existing_nodes(col, closets_col, node_ids, page_size: int = 500) -> set[str]:
    """The members of ``node_ids`` that are a drawer, a chunked drawer, or a closet.

    Batched: one ``get`` per page against drawers, then one per page against
    closets for what is left. Only ids still missing after both fall back to
    the per-id logical lookup, which finds a chunked drawer by its parent id.
    """
    pending = sorted({n for n in node_ids if isinstance(n, str) and n})
    found = set()

    def _probe(collection, ids):
        for start in range(0, len(ids), page_size):
            try:
                found.update(
                    _get_result_ids(collection.get(ids=ids[start : start + page_size], include=[]))
                )
            except Exception:
                logger.debug("existence probe failed", exc_info=True)

    _probe(col, pending)
    if closets_col is not None:
        _probe(closets_col, [n for n in pending if n not in found])
    for node in [n for n in pending if n not in found]:
        if _logical_drawer_record(col, node) is not None:
            found.add(node)
    return found


def tool_find_closet_lineage_issues(
    wing: str = None,
    room: str = None,
    include_merged: bool = False,
    limit: int = 20,
    offset: int = 0,
):
    """Validate closet lineage integrity from active KG edges.

    Targets closet IDs and checks lineage modeled via active
    ``synthesized-from`` facts. Flags:
    - missing or unresolvable source references,
    - stale source references that resolve to a different canonical node,
    - stored/computed height mismatches when closet metadata includes height.

    Ordinary closets with no lineage participation are skipped to avoid
    false positives; this tool focuses on lineage-aware closet records.

    The whole audit costs a fixed number of graph queries per level of
    lineage plus one existence probe per page of sources, whatever the number
    of closets.
    """
    from .. import lineage

    try:
        wing = _sanitize_optional_name(wing, "wing")
        room = _sanitize_optional_name(room, "room")
    except ValueError as e:
        return {"error": str(e)}

    limit = max(1, min(int(limit or 20), 500))
    offset = max(0, int(offset or 0))
    empty = {
        "orphans": [],
        "count": 0,
        "total": 0,
        "limit": limit,
        "offset": offset,
        "target": "closets",
    }

    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()

    closets_col = _closets_collection()
    closet_records = _closet_records(closets_col, wing=wing, room=room)
    if not closet_records:
        return empty
    meta_by_id = {record["closet_id"]: record["metadata"] for record in closet_records}

    def _lineage(kg):
        participating = kg.entities_in_triples(meta_by_id)
        members = [cid for cid in meta_by_id if cid in participating]
        parents, names = _lineage_parents(kg, members)
        sources = {
            cid: sorted({names.get(p, p) for p in parents.get(kg.entity_id(cid), ())})
            for cid in members
        }
        return members, parents, sources

    members, parents, sources = _call_kg(_lineage)
    if not members:
        return empty

    chains = _canonical_chains(members + [s for srcs in sources.values() for s in srcs])

    # Heights are measured from each closet's canonical node. The walk above
    # covered the closets themselves; a merged closet's canonical node may
    # have lineage of its own.
    canonical_of = {cid: chains[cid]["chain"][-1] for cid in members if "error" not in chains[cid]}

    def _heights(kg):
        extra = [c for c in set(canonical_of.values()) if kg.entity_id(c) not in parents]
        merged_parents = dict(parents)
        if extra:
            more, _names = _lineage_parents(kg, extra)
            for node, targets in more.items():
                merged_parents.setdefault(node, targets)
        start_ids = {c: kg.entity_id(c) for c in set(canonical_of.values())}
        computed = lineage.heights(merged_parents, start_ids.values())
        return {c: computed.get(i, 0) for c, i in start_ids.items()}

    height_of = _call_kg(_heights) if canonical_of else {}

    canonical_sources = {
        chains[s]["chain"][-1]
        for srcs in sources.values()
        for s in srcs
        if "error" not in chains.get(s, {"error": True})
    }
    existing = _existing_nodes(col, closets_col, canonical_sources)

    issues = []
    for closet_id in members:
        meta = meta_by_id[closet_id]
        resolved = chains[closet_id]
        if "error" in resolved:
            issues.append(
                {
                    "closet_id": closet_id,
                    "wing": meta.get("wing", ""),
                    "room": meta.get("room", ""),
                    "issues": ["canonical_resolution_failed"],
                    "canonical_error": resolved["error"],
                }
            )
            continue

        canonical_id = canonical_of[closet_id]
        if not include_merged and canonical_id != closet_id:
            continue

        source_ids = sources[closet_id]
        found_issues = []
        missing_sources = []
        unresolvable_sources = []
        stale_sources = []

        if not source_ids:
            found_issues.append("no_active_sources")

        for source_id in source_ids:
            resolved_source = chains[source_id]
            if "error" in resolved_source:
                unresolvable_sources.append(source_id)
                continue
            canonical_source_id = resolved_source["chain"][-1]
            if canonical_source_id != source_id:
                stale_sources.append(
                    {"source_node_id": source_id, "canonical_node_id": canonical_source_id}
                )
            if canonical_source_id not in existing:
                missing_sources.append(
                    {"source_node_id": source_id, "canonical_node_id": canonical_source_id}
                )

        if missing_sources:
            found_issues.append("missing_source_nodes")
        if unresolvable_sources:
            found_issues.append("unresolvable_sources")
        if stale_sources:
            found_issues.append("stale_source_references")

        stored_height = meta.get("height")
        computed_value = _coerce_non_negative_int(height_of.get(canonical_id), default=0)
        if stored_height is not None:
            stored_value = _coerce_non_negative_int(stored_height, default=0)
            if stored_value != computed_value:
                found_issues.append("stored_height_mismatch")
        else:
            stored_value = None

        if found_issues:
            issues.append(
                {
                    "closet_id": closet_id,
                    "canonical_node_id": canonical_id,
                    "wing": meta.get("wing", ""),
                    "room": meta.get("room", ""),
                    "issues": found_issues,
                    "active_source_ids": source_ids,
                    "missing_sources": missing_sources,
                    "unresolvable_source_ids": unresolvable_sources,
                    "stale_sources": stale_sources,
                    "stored_height": stored_value,
                    "computed_height": computed_value,
                }
            )

    total = len(issues)
    page = issues[offset : offset + limit]
    return {
        "orphans": page,
        "count": len(page),
        "total": total,
        "limit": limit,
        "offset": offset,
        "target": "closets",
    }


def _merge_scan_node_ids(col, wing: str = None, room: str = None) -> list[str]:
    """Logical drawer ids available for merge analysis, sorted.

    Reads ids and metadata only (never documents), from sqlite directly on the
    Chroma backend.
    """
    conditions = []
    if wing:
        conditions.append({"wing": wing})
    if room:
        conditions.append({"room": room})
    where = None
    if len(conditions) == 1:
        where = conditions[0]
    elif conditions:
        where = {"$and": conditions}

    listed = None
    if _is_chroma_backend() and _config.palace_path:
        from ..backends.chroma import sqlite_list_id_metadata

        listed = sqlite_list_id_metadata(_config.palace_path, _config.collection_name, where=where)
    if listed is not None:
        ids, metadatas = listed
    else:
        ids, _docs, metadatas = _fetch_drawer_rows(col, where=where, include=["metadatas"])

    logical = set()
    for idx, row_id in enumerate(ids):
        meta = _safe_meta(metadatas[idx] if idx < len(metadatas) else {})
        parent = meta.get("parent_drawer_id")
        logical.add(parent if isinstance(parent, str) and parent else row_id)
    return sorted(logical)


def _seed_records(col, seeds: list[str]) -> list[dict]:
    """``{drawer_id, embedding, content}`` for each seed that exists.

    Uses the vectors already stored for the seed instead of embedding its text
    again: one ``get`` for seeds stored as a single row, one more for the
    chunks of chunked seeds, whose vectors are averaged. Seeds that are not
    found are dropped.
    """
    import math

    seeds = list(dict.fromkeys(seeds))
    if not seeds:
        return []

    rows_by_seed = {}
    direct = col.get(ids=seeds, include=["embeddings", "documents"])
    direct_ids = _get_result_ids(direct)
    direct_embs = _chroma_field(direct, "embeddings", []) or []
    direct_docs = _chroma_field(direct, "documents", []) or []
    for idx, row_id in enumerate(direct_ids):
        emb = direct_embs[idx] if idx < len(direct_embs) else None
        doc = direct_docs[idx] if idx < len(direct_docs) else ""
        rows_by_seed[row_id] = [(0, emb, doc)]

    chunked = [s for s in seeds if s not in rows_by_seed]
    if chunked:
        rest = col.get(
            where={"parent_drawer_id": {"$in": chunked}},
            include=["embeddings", "documents", "metadatas"],
        )
        rest_ids = _get_result_ids(rest)
        rest_embs = _chroma_field(rest, "embeddings", []) or []
        rest_docs = _chroma_field(rest, "documents", []) or []
        rest_metas = _chroma_field(rest, "metadatas", []) or []
        for idx, _row_id in enumerate(rest_ids):
            meta = _safe_meta(rest_metas[idx] if idx < len(rest_metas) else {})
            parent = meta.get("parent_drawer_id")
            if parent in chunked:
                rows_by_seed.setdefault(parent, []).append(
                    (
                        _coerce_non_negative_int(meta.get("chunk_index"), default=0),
                        rest_embs[idx] if idx < len(rest_embs) else None,
                        rest_docs[idx] if idx < len(rest_docs) else "",
                    )
                )

    records = []
    for seed in seeds:
        rows = sorted(rows_by_seed.get(seed, ()), key=lambda row: row[0])
        if not rows:
            continue
        vectors = [list(row[1]) for row in rows if row[1] is not None and len(row[1])]
        embedding = None
        if vectors and len(vectors) == len(rows):
            mean = [sum(values) / len(vectors) for values in zip(*vectors)]
            norm = math.sqrt(sum(v * v for v in mean)) or 1.0
            embedding = [float(v / norm) for v in mean]
        records.append(
            {
                "drawer_id": seed,
                "embedding": embedding,
                "content": "".join(row[2] or "" for row in rows),
            }
        )
    return records


def _batch_duplicate_matches_for_records(
    col,
    seed_records: list[dict],
    threshold: float,
) -> tuple[dict[str, list[dict]], dict | None]:
    """Run one batched vector query and map thresholded matches by seed drawer ID.

    Queries with the seeds' stored vectors when every seed has one, so no seed
    text is embedded again; falls back to the text otherwise.
    """
    embeddings = [rec.get("embedding") for rec in seed_records]
    try:
        if all(emb is not None for emb in embeddings):
            batch_results = col.query(
                query_embeddings=embeddings, n_results=5, include=["distances"]
            )
        else:
            batch_results = col.query(
                query_texts=[rec["content"] for rec in seed_records],
                n_results=5,
                include=["distances"],
            )
    except Exception as e:
        return {}, {"error": f"Batch query failed: {e}"}

    metric = _metric_for_collection(col)
    ids_rows = batch_results.get("ids") or []
    dist_rows = batch_results.get("distances") or []

    matches_by_seed = {}
    for idx, seed_record in enumerate(seed_records):
        seed = seed_record["drawer_id"]
        matches = []
        if idx < len(ids_rows) and ids_rows[idx]:
            row_dists = dist_rows[idx] if idx < len(dist_rows) else []
            for i, match_id in enumerate(ids_rows[idx]):
                if i >= len(row_dists):
                    continue
                similarity = round(_distance_to_similarity(row_dists[i], metric), 3)
                if similarity >= threshold:
                    matches.append({"id": match_id, "similarity": similarity})
        matches_by_seed[seed] = matches

    return matches_by_seed, None


def _logical_drawer_ids_for_any_ids(
    col,
    drawer_ids: list[str],
    page_size: int = 500,
) -> dict[str, str]:
    """Resolve row/chunk IDs to logical drawer IDs in one or more batched reads."""
    normalized = []
    seen = set()
    for raw_id in drawer_ids or []:
        if not isinstance(raw_id, str) or not raw_id or raw_id in seen:
            continue
        seen.add(raw_id)
        normalized.append(raw_id)

    if not normalized:
        return {}

    resolved = {drawer_id: drawer_id for drawer_id in normalized}
    page_size = max(1, int(page_size or 500))

    for start in range(0, len(normalized), page_size):
        batch = normalized[start : start + page_size]
        result = col.get(ids=batch, include=["metadatas"])
        ids = _chroma_field(result, "ids", []) or []
        metadatas = _chroma_field(result, "metadatas", []) or []

        for idx, row_id in enumerate(ids):
            meta = _safe_meta(metadatas[idx] if idx < len(metadatas) else {})
            parent_id = meta.get("parent_drawer_id")
            if isinstance(parent_id, str) and parent_id:
                resolved[row_id] = parent_id

    return resolved


def _collect_match_ids(matches_by_seed: dict[str, list[dict]]) -> list[str]:
    """Collect non-empty string match IDs from batched duplicate results."""
    all_match_ids = []
    for match_rows in matches_by_seed.values():
        for match in match_rows:
            match_id = match.get("id")
            if isinstance(match_id, str) and match_id:
                all_match_ids.append(match_id)
    return all_match_ids


def tool_find_merge_candidates(
    drawer_id: str = None,
    threshold: float = 0.9,
    limit: int = 20,
    max_nodes: int = 40,
    max_depth: int = 20,
    wing: str = None,
    room: str = None,
    require_topological_distance: bool = True,
):
    """Find semantically near node pairs with optional topology filtering."""
    try:
        wing = _sanitize_optional_name(wing, "wing")
        room = _sanitize_optional_name(room, "room")
    except ValueError as e:
        return {"error": str(e)}

    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return {"error": "threshold must be a number between 0 and 1"}
    if threshold < 0 or threshold > 1:
        return {"error": "threshold must be between 0 and 1"}

    limit = max(1, min(int(limit or 20), 200))
    max_nodes = max(1, min(int(max_nodes or 40), 500))
    max_depth = max(1, min(int(max_depth or 20), 100))

    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()

    if drawer_id is not None:
        if not isinstance(drawer_id, str) or not drawer_id.strip():
            return {"error": "drawer_id must be a non-empty string when provided"}
        seed = _logical_drawer_id_for_any_id(col, strip_lone_surrogates(drawer_id.strip()))
        if _logical_drawer_record(col, seed) is None:
            return {"error": f"drawer_id not found: {drawer_id.strip()}"}
        seeds = [seed]
    else:
        seeds = _merge_scan_node_ids(col, wing=wing, room=room)[:max_nodes]

    base = {
        "candidates": [],
        "count": 0,
        "scanned_nodes": len(seeds),
        "threshold": threshold,
        "require_topological_distance": bool(require_topological_distance),
    }
    if not seeds:
        return base

    _refresh_vector_disabled_flag()
    if _vector_disabled:
        return {
            **base,
            "vector_disabled": True,
            "vector_disabled_reason": _vector_disabled_reason,
        }

    seed_records = _seed_records(col, seeds)
    if not seed_records:
        return base

    matches_by_seed, batch_error = _batch_duplicate_matches_for_records(
        col,
        seed_records,
        threshold,
    )
    if batch_error:
        return batch_error

    all_match_ids = _collect_match_ids(matches_by_seed)
    match_logical_ids = _logical_drawer_ids_for_any_ids(col, all_match_ids)

    # Every canonical id this call needs, in one walk; then every ancestor set
    # the surviving pairs need, in one more.
    seed_ids = [rec["drawer_id"] for rec in seed_records]
    chains = _canonical_chains(seed_ids + [match_logical_ids.get(m, m) for m in all_match_ids])

    def _canonical(node):
        resolved = chains.get(node)
        return None if not resolved or "error" in resolved else resolved["chain"][-1]

    seen_pairs = set()
    pairs = []
    for seed in seed_ids:
        seed_canonical = _canonical(seed)
        if not seed_canonical:
            continue
        for match in matches_by_seed.get(seed, []):
            match_id = match.get("id")
            if not isinstance(match_id, str) or not match_id:
                continue
            match_logical_id = match_logical_ids.get(match_id, match_id)
            if match_logical_id == seed:
                continue
            target_canonical = _canonical(match_logical_id)
            if not target_canonical or target_canonical == seed_canonical:
                continue
            pair_key = tuple(sorted((seed_canonical, target_canonical)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            pairs.append((seed, match_logical_id, seed_canonical, target_canonical, match))

    ancestors = _ancestor_sets([p[2] for p in pairs] + [p[3] for p in pairs], max_depth=max_depth)

    candidates = []
    for seed, target, seed_canonical, target_canonical, match in pairs:
        common_ancestors = sorted(
            ancestors.get(seed_canonical, set()) & ancestors.get(target_canonical, set())
        )
        topologically_distant = not common_ancestors
        if require_topological_distance and not topologically_distant:
            continue
        candidates.append(
            {
                "source_node_id": seed,
                "target_node_id": target,
                "source_canonical_node_id": seed_canonical,
                "target_canonical_node_id": target_canonical,
                "similarity": match.get("similarity"),
                "topologically_distant": topologically_distant,
                "common_ancestor_count": len(common_ancestors),
                "common_ancestors": common_ancestors[:10],
            }
        )

    candidates.sort(key=lambda item: float(item.get("similarity") or 0.0), reverse=True)
    capped = candidates[:limit]

    return {**base, "candidates": capped, "count": len(capped)}


def tool_apply_merge(
    source_node_id: str,
    canonical_node_id: str,
    ended: str = None,
    invalidate_source_edges: bool = True,
):
    """Apply a deterministic merge by wiring merged-into and retiring stale edges.

    The link changes are one transaction (``KnowledgeGraph.apply_merge``): a
    failure part way through leaves the graph as it was.
    """
    if not isinstance(source_node_id, str) or not source_node_id.strip():
        return {"success": False, "error": "source_node_id is required"}
    if not isinstance(canonical_node_id, str) or not canonical_node_id.strip():
        return {"success": False, "error": "canonical_node_id is required"}

    source_node_id = strip_lone_surrogates(source_node_id.strip())
    canonical_node_id = strip_lone_surrogates(canonical_node_id.strip())
    if source_node_id == canonical_node_id:
        return {"success": False, "error": "source_node_id and canonical_node_id must differ"}

    try:
        ended = sanitize_iso_temporal(ended, "ended")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()

    source_logical = _logical_drawer_id_for_any_id(col, source_node_id)
    canonical_logical = _logical_drawer_id_for_any_id(col, canonical_node_id)

    if _logical_drawer_record(col, source_logical) is None:
        return {"success": False, "error": f"source node not found: {source_node_id}"}
    if _logical_drawer_record(col, canonical_logical) is None:
        return {"success": False, "error": f"canonical node not found: {canonical_node_id}"}

    chains = _canonical_chains([source_logical, canonical_logical])
    for node in (source_logical, canonical_logical):
        if "error" in chains[node]:
            return {"success": False, "error": chains[node]["error"]}

    source_canonical = chains[source_logical]["chain"][-1]
    target_canonical = chains[canonical_logical]["chain"][-1]

    if source_canonical == target_canonical:
        return {
            "success": True,
            "merged": False,
            "reason": "already resolved to same canonical node",
            "source_node_id": source_canonical,
            "canonical_node_id": target_canonical,
        }

    _wal_log(
        "apply_merge",
        {
            "source_node_id": source_canonical,
            "canonical_node_id": target_canonical,
            "ended": ended,
            "invalidate_source_edges": bool(invalidate_source_edges),
        },
    )

    try:
        result = _call_kg(
            lambda kg: kg.apply_merge(
                source_canonical,
                target_canonical,
                ended=ended,
                merge_predicate=_MERGED_INTO,
                lineage_predicate=_SYNTHESIZED_FROM,
                retire_lineage=bool(invalidate_source_edges),
            )
        )
    except ValueError as e:
        return {"success": False, "error": str(e)}

    return {
        "success": True,
        "merged": True,
        "source_node_id": source_canonical,
        "canonical_node_id": target_canonical,
        "merged_edge_added": result["merged_edge_added"],
        "invalidated_prior_merged_into": result["invalidated_prior_merged_into"],
        "invalidated_lineage_edges": result["invalidated_lineage_edges"],
        "ended": result["ended"],
    }
