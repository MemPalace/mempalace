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


def tool_kg_query(entity: str, as_of: str = None, direction: str = "both"):
    """Query the knowledge graph for an entity's relationships."""
    try:
        entity = sanitize_kg_value(entity, "entity")
        as_of = sanitize_iso_temporal(as_of, "as_of")
    except ValueError as e:
        return {"error": str(e)}

    if direction not in ("outgoing", "incoming", "both"):
        return {"error": "direction must be 'outgoing', 'incoming', or 'both'"}

    results = _call_kg(lambda kg: kg.query_entity(entity, as_of=as_of, direction=direction))
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


def tool_kg_timeline(entity: str = None):
    """Get chronological timeline of facts, optionally for one entity."""
    if entity is not None:
        try:
            entity = sanitize_kg_value(entity, "entity")
        except ValueError as e:
            return {"error": str(e)}
    results = _call_kg(lambda kg: kg.timeline(entity))
    return {"entity": entity or "all", "timeline": results, "count": len(results)}


def tool_kg_stats():
    """Knowledge graph overview: entities, triples, relationship types."""
    return _call_kg(lambda kg: kg.stats())
