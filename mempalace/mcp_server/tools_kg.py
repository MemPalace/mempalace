# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


# ==================== KNOWLEDGE GRAPH ====================


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

    queue = deque([(entity, 0)])
    seen_nodes = {entity}
    seen_facts = set()
    traversed = []

    while queue:
        current, depth = queue.popleft()
        for fact in _filtered_facts(current):
            subject = fact.get("subject")
            predicate_value = fact.get("predicate")
            object_value = fact.get("object")
            fact_key = (
                subject,
                predicate_value,
                object_value,
                fact.get("valid_from"),
                fact.get("valid_to"),
                fact.get("current"),
            )
            if fact_key not in seen_facts:
                seen_facts.add(fact_key)
                enriched = dict(fact)
                enriched["depth"] = depth
                traversed.append(enriched)

            if depth >= max_depth:
                continue

            neighbors = []
            if direction in ("outgoing", "both") and isinstance(object_value, str) and object_value:
                neighbors.append(object_value)
            if direction in ("incoming", "both") and isinstance(subject, str) and subject:
                neighbors.append(subject)

            for neighbor in neighbors:
                if neighbor in seen_nodes:
                    continue
                seen_nodes.add(neighbor)
                queue.append((neighbor, depth + 1))

    payload = {
        "entity": entity,
        "as_of": as_of,
        "direction": direction,
        "predicate": predicate,
        "recurse": True,
        "max_depth": max_depth,
        "visited_nodes": len(seen_nodes),
        "facts": traversed,
        "count": len(traversed),
    }

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
    return {"success": True, "triple_id": triple_id, "fact": f"{subject} → {predicate} → {object}"}


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


def _kg_related_nodes(node_id: str, predicate: str, direction: str, field: str) -> list[str]:
    """Fetch related node IDs for one predicate/direction from the KG.

    A thin projection of one string field over :func:`_kg_active_facts`, so the
    "current fact for this predicate/direction" filter lives in a single place.
    """
    nodes = []
    for fact in _kg_active_facts(node_id, predicate, direction):
        value = fact.get(field)
        if isinstance(value, str) and value:
            nodes.append(value)
    return nodes


def _kg_active_facts(node_id: str, predicate: str, direction: str) -> list[dict]:
    """Return current KG fact rows for one predicate/direction."""
    facts = _call_kg(lambda kg: kg.query_entity(node_id, direction=direction))
    rows = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        if fact.get("predicate") != predicate:
            continue
        if fact.get("current") is False:
            continue
        rows.append(fact)
    return rows


def _ancestor_closure(node_id: str, max_depth: int) -> set[str]:
    """Return all synthesized-from ancestors reachable within max_depth."""
    queue = deque([(node_id, 0)])
    seen = {node_id}
    ancestors = set()

    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        parents = sorted(
            set(
                _kg_related_nodes(
                    current,
                    predicate="synthesized-from",
                    direction="outgoing",
                    field="object",
                )
            )
        )
        for parent in parents:
            if parent in seen:
                continue
            seen.add(parent)
            ancestors.add(parent)
            queue.append((parent, depth + 1))

    return ancestors


def _merge_scan_node_ids(col, wing: str = None, room: str = None) -> list[str]:
    """List logical drawer IDs available for graph-level merge analysis."""
    ids, docs, metas = _fetch_drawer_rows(col, include_documents=False)
    drawers = _collapse_drawer_rows(ids, docs, metas)

    filtered = []
    for drawer in drawers:
        if wing and drawer.get("wing") != wing:
            continue
        if room and drawer.get("room") != room:
            continue
        filtered.append(drawer["drawer_id"])

    return sorted(filtered)


def _closet_records(wing: str = None, room: str = None) -> list[dict]:
    """List closet records with minimal metadata for graph-validity checks."""
    from ..palace import get_closets_collection

    try:
        closets_col = get_closets_collection(_config.palace_path, create=False)
    except Exception:
        logger.debug("closet collection lookup failed", exc_info=True)
        return []

    if closets_col is None:
        return []

    where = {}
    if wing:
        where["wing"] = wing
    if room:
        where["room"] = room

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

    records = []
    for idx, closet_id in enumerate(ids):
        records.append(
            {
                "closet_id": closet_id,
                "metadata": _safe_meta(metadatas[idx] if idx < len(metadatas) else {}),
            }
        )
    return records


def _entity_exists_as_drawer_or_closet(col, entity_id: str) -> bool:
    """Return True when an entity id resolves to a drawer row or a closet row."""
    if _logical_drawer_record(col, entity_id) is not None:
        return True

    from ..palace import get_closets_collection

    try:
        closets_col = get_closets_collection(_config.palace_path, create=False)
    except Exception:
        logger.debug("closet collection lookup failed", exc_info=True)
        return False

    if closets_col is None:
        return False

    try:
        result = closets_col.get(ids=[entity_id], include=[])
        return bool(_get_result_ids(result))
    except Exception:
        logger.debug("closet existence probe failed for %s", entity_id, exc_info=True)
        return False


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
    """
    try:
        wing = _sanitize_optional_name(wing, "wing")
        room = _sanitize_optional_name(room, "room")
    except ValueError as e:
        return {"error": str(e)}

    limit = max(1, min(int(limit or 20), 500))
    offset = max(0, int(offset or 0))

    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()

    closet_records = _closet_records(wing=wing, room=room)
    if not closet_records:
        return {
            "orphans": [],
            "count": 0,
            "total": 0,
            "limit": limit,
            "offset": offset,
            "target": "closets",
        }

    issues = []
    for record in closet_records:
        closet_id = record["closet_id"]
        meta = record["metadata"]

        canonical = tool_resolve_canonical(closet_id)
        if "error" in canonical:
            issues.append(
                {
                    "closet_id": closet_id,
                    "wing": meta.get("wing", ""),
                    "room": meta.get("room", ""),
                    "issues": ["canonical_resolution_failed"],
                    "canonical_error": canonical.get("error"),
                }
            )
            continue

        canonical_id = canonical["canonical_node_id"]
        if not include_merged and canonical_id != closet_id:
            continue

        # Guard against false positives for ordinary mined closets that do not
        # participate in lineage edges at all.
        participates = bool(
            _call_kg(lambda kg, cid=closet_id: kg.query_entity(cid, direction="both"))
        )
        if not participates:
            continue

        source_facts = _kg_active_facts(closet_id, "synthesized-from", direction="outgoing")
        source_ids = sorted(
            {
                fact.get("object")
                for fact in source_facts
                if isinstance(fact.get("object"), str) and fact.get("object")
            }
        )

        found_issues = []
        missing_sources = []
        unresolvable_sources = []
        stale_sources = []

        if not source_ids:
            found_issues.append("no_active_sources")

        for source_id in source_ids:
            resolved_source = tool_resolve_canonical(source_id)
            if "error" in resolved_source:
                unresolvable_sources.append(source_id)
                continue

            canonical_source_id = resolved_source["canonical_node_id"]
            if canonical_source_id != source_id:
                stale_sources.append(
                    {"source_node_id": source_id, "canonical_node_id": canonical_source_id}
                )

            if not _entity_exists_as_drawer_or_closet(col, canonical_source_id):
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
        computed_height = tool_get_height(closet_id)
        computed_value = None
        if isinstance(computed_height, dict) and "error" not in computed_height:
            computed_value = _coerce_non_negative_int(computed_height.get("height"), default=0)

        if stored_height is not None and computed_value is not None:
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


def tool_resolve_canonical(node_id: str, max_hops: int = 50):
    """Resolve canonical node by following active merged-into links."""
    try:
        node_id = sanitize_kg_value(node_id, "node_id")
    except ValueError as e:
        return {"error": str(e)}

    max_hops = max(1, min(int(max_hops or 50), 200))

    chain = [node_id]
    current = node_id
    for _ in range(max_hops):
        next_nodes = sorted(
            set(
                _kg_related_nodes(
                    current, predicate="merged-into", direction="outgoing", field="object"
                )
            )
        )
        if not next_nodes:
            break
        nxt = next_nodes[0]
        if nxt in chain:
            return {
                "error": "merged-into cycle detected",
                "chain": chain + [nxt],
            }
        chain.append(nxt)
        current = nxt

    return {
        "node_id": node_id,
        "canonical_node_id": current,
        "hops": len(chain) - 1,
        "chain": chain,
    }


def tool_get_height(node_id: str):
    """Compute canonical lineage height over active ``synthesized-from`` edges.

    Height is defined as the longest outgoing path from the canonical node to a
    leaf source (no outgoing lineage edges). Returns both computed height and
    any stored metadata hint for observability.
    """
    resolved = tool_resolve_canonical(node_id)
    if "error" in resolved:
        return resolved

    start = resolved["canonical_node_id"]
    memo = {}

    def _height(current: str, trail: set[str]) -> int:
        if current in memo:
            return memo[current]
        if current in trail:
            return 0
        parents = sorted(
            set(
                _kg_related_nodes(
                    current,
                    predicate="synthesized-from",
                    direction="outgoing",
                    field="object",
                )
            )
        )
        if not parents:
            memo[current] = 0
            return 0
        next_trail = set(trail)
        next_trail.add(current)
        computed = max(_height(parent, next_trail) for parent in parents) + 1
        memo[current] = computed
        return computed

    computed_height = _height(start, set())

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


def _batch_duplicate_matches_for_records(
    col,
    seed_records: list[dict],
    threshold: float,
) -> tuple[dict[str, list[dict]], dict | None]:
    """Run one batched vector query and map thresholded matches by seed drawer ID."""
    try:
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
                dist = row_dists[i]
                similarity = round(_distance_to_similarity(dist, metric), 3)
                if similarity >= threshold:
                    matches.append({"id": match_id, "similarity": similarity})
        matches_by_seed[seed] = matches

    return matches_by_seed, None


def _resolve_canonical_cached(node: str, canonical_cache: dict[str, str | None]) -> str | None:
    """Resolve canonical node ID with memoization and error-to-None normalization."""
    if node in canonical_cache:
        return canonical_cache[node]
    resolved = tool_resolve_canonical(node)
    if "error" in resolved:
        canonical_cache[node] = None
        return None
    canonical = resolved["canonical_node_id"]
    canonical_cache[node] = canonical
    return canonical


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
        seed_record = _logical_drawer_record(col, seed)
        if seed_record is None:
            return {"error": f"drawer_id not found: {drawer_id.strip()}"}
        seeds = [seed]
    else:
        seeds = _merge_scan_node_ids(col, wing=wing, room=room)[:max_nodes]

    if not seeds:
        return {
            "candidates": [],
            "count": 0,
            "scanned_nodes": 0,
            "require_topological_distance": bool(require_topological_distance),
        }

    _refresh_vector_disabled_flag()
    if _vector_disabled:
        return {
            "candidates": [],
            "count": 0,
            "scanned_nodes": len(seeds),
            "threshold": threshold,
            "require_topological_distance": bool(require_topological_distance),
            "vector_disabled": True,
            "vector_disabled_reason": _vector_disabled_reason,
        }

    seed_records = []
    if drawer_id is not None:
        seed_records = [seed_record]
    else:
        for seed in seeds:
            rec = _logical_drawer_record(col, seed)
            if rec:
                seed_records.append(rec)

    if not seed_records:
        return {
            "candidates": [],
            "count": 0,
            "scanned_nodes": len(seeds),
            "threshold": threshold,
            "require_topological_distance": bool(require_topological_distance),
        }

    matches_by_seed, batch_error = _batch_duplicate_matches_for_records(
        col,
        seed_records,
        threshold,
    )
    if batch_error:
        return batch_error

    all_match_ids = _collect_match_ids(matches_by_seed)
    match_logical_ids = _logical_drawer_ids_for_any_ids(col, all_match_ids)

    canonical_cache = {}
    ancestor_cache = {}
    seen_pairs = set()
    candidates = []

    for seed_record in seed_records:
        seed = seed_record["drawer_id"]

        seed_canonical = _resolve_canonical_cached(seed, canonical_cache)
        if not seed_canonical:
            continue

        matches = matches_by_seed.get(seed, [])

        for match in matches:
            match_id = match.get("id")
            if not isinstance(match_id, str) or not match_id:
                continue

            match_logical_id = match_logical_ids.get(match_id, match_id)
            if match_logical_id == seed:
                continue

            target_canonical = _resolve_canonical_cached(match_logical_id, canonical_cache)
            if not target_canonical or target_canonical == seed_canonical:
                continue

            pair_key = tuple(sorted((seed_canonical, target_canonical)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            seed_ancestors = ancestor_cache.get(seed_canonical)
            if seed_ancestors is None:
                seed_ancestors = _ancestor_closure(seed_canonical, max_depth=max_depth)
                ancestor_cache[seed_canonical] = seed_ancestors

            target_ancestors = ancestor_cache.get(target_canonical)
            if target_ancestors is None:
                target_ancestors = _ancestor_closure(target_canonical, max_depth=max_depth)
                ancestor_cache[target_canonical] = target_ancestors

            common_ancestors = sorted(seed_ancestors.intersection(target_ancestors))
            topologically_distant = len(common_ancestors) == 0
            if require_topological_distance and not topologically_distant:
                continue

            candidates.append(
                {
                    "source_node_id": seed,
                    "target_node_id": match_logical_id,
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

    return {
        "candidates": capped,
        "count": len(capped),
        "scanned_nodes": len(seeds),
        "threshold": threshold,
        "require_topological_distance": bool(require_topological_distance),
    }


def tool_apply_merge(
    source_node_id: str,
    canonical_node_id: str,
    ended: str = None,
    invalidate_source_edges: bool = True,
):
    """Apply a deterministic merge by wiring merged-into and retiring stale edges."""
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

    source_record = _logical_drawer_record(col, source_logical)
    if source_record is None:
        return {"success": False, "error": f"source node not found: {source_node_id}"}

    target_record = _logical_drawer_record(col, canonical_logical)
    if target_record is None:
        return {"success": False, "error": f"canonical node not found: {canonical_node_id}"}

    source_resolved = tool_resolve_canonical(source_logical)
    if "error" in source_resolved:
        return {"success": False, "error": source_resolved["error"]}

    target_resolved = tool_resolve_canonical(canonical_logical)
    if "error" in target_resolved:
        return {"success": False, "error": target_resolved["error"]}

    source_canonical = source_resolved["canonical_node_id"]
    target_canonical = target_resolved["canonical_node_id"]

    if source_canonical == target_canonical:
        return {
            "success": True,
            "merged": False,
            "reason": "already resolved to same canonical node",
            "source_node_id": source_canonical,
            "canonical_node_id": target_canonical,
        }

    prior_merge_facts = _kg_active_facts(source_canonical, "merged-into", direction="outgoing")
    invalidated_prior_merged_into = 0
    has_active_target_edge = False

    for fact in prior_merge_facts:
        object_id = fact.get("object")
        if not isinstance(object_id, str) or not object_id:
            continue
        if object_id == target_canonical:
            has_active_target_edge = True
            continue
        invalidate = tool_kg_invalidate(
            subject=source_canonical,
            predicate="merged-into",
            object=object_id,
            ended=ended,
        )
        if invalidate.get("success"):
            invalidated_prior_merged_into += 1

    merged_edge_added = False
    if not has_active_target_edge:
        add_result = tool_kg_add(
            subject=source_canonical,
            predicate="merged-into",
            object=target_canonical,
            source_drawer_id=source_canonical,
        )
        if not add_result.get("success"):
            return {
                "success": False,
                "error": add_result.get("error", "failed to add merged-into edge"),
            }
        merged_edge_added = True

    invalidated_lineage_edges = 0
    if invalidate_source_edges:
        active_synth_edges = _kg_active_facts(
            source_canonical, "synthesized-from", direction="outgoing"
        )
        for fact in active_synth_edges:
            object_id = fact.get("object")
            if not isinstance(object_id, str) or not object_id:
                continue
            invalidate = tool_kg_invalidate(
                subject=source_canonical,
                predicate="synthesized-from",
                object=object_id,
                ended=ended,
            )
            if invalidate.get("success"):
                invalidated_lineage_edges += 1

    return {
        "success": True,
        "merged": True,
        "source_node_id": source_canonical,
        "canonical_node_id": target_canonical,
        "merged_edge_added": merged_edge_added,
        "invalidated_prior_merged_into": invalidated_prior_merged_into,
        "invalidated_lineage_edges": invalidated_lineage_edges,
        "ended": ended or date.today().isoformat(),
    }
