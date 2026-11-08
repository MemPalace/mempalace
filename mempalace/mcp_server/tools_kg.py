# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


# ==================== KNOWLEDGE GRAPH ====================


def tool_kg_search(
    query: str,
    limit: int = 5,
    wings=None,
    sensitivities=None,
    as_of=None,
    *,
    query_is_sanitized=False,
):
    """Fact-only half of search, so gateway drawer fan-out needs one lookup."""
    try:
        from ..searcher.facts import search_facts

        facts = _call_kg(
            lambda kg: search_facts(
                kg,
                query,
                limit=max(1, min(limit, _MAX_RESULTS)),
                wings=wings,
                sensitivities=sensitivities,
                as_of=as_of,
                query_is_sanitized=query_is_sanitized,
            ),
            read=True,
        )
    except Exception:
        logger.warning("Fact index unavailable; preserving drawer search", exc_info=True)
        return {"facts": [], "facts_error": "unavailable"}
    return {"facts": facts}


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
    wings: list = None,
    sensitivities: list = None,
    search: bool = False,
    limit: int = 5,
):
    """Query the knowledge graph for an entity's relationships.

    ``wings`` / ``sensitivities`` limit the answer to facts in those wings and
    of those sensitivities. Facts filed before facts had wings match no wing.
    """
    try:
        entity = (
            sanitize_query(entity)["clean_query"] if search else sanitize_kg_value(entity, "entity")
        )
        as_of = sanitize_iso_temporal(as_of, "as_of")
        wings, sensitivities = visibility(wings, sensitivities)
    except ValueError as e:
        return {"error": str(e)}

    if search:
        return tool_kg_search(entity, limit, wings, sensitivities, as_of, query_is_sanitized=True)

    if direction not in ("outgoing", "incoming", "both"):
        return {"error": "direction must be 'outgoing', 'incoming', or 'both'"}

    results = _call_kg(
        lambda kg: kg.query_entity(
            entity,
            as_of=as_of,
            direction=direction,
            wings=wings,
            sensitivities=sensitivities,
        ),
        read=True,
    )
    if as_of is None:
        now_key = utc_now_z()
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
    if results:
        found = _call_kg(lambda kg: kg.sources([r["id"] for r in results]), read=True)
        for row in results:
            row["sources"] = found.get(row["id"], [])
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
        candidates = _call_kg(
            lambda kg: kg.find_entity_candidates(entity, wings, sensitivities), read=True
        )
        if candidates:
            payload["candidates"] = candidates
    return _with_kg_notice(payload)


def _kg_mutation_scope(
    thread_scope,
    kind,
    subject,
    predicate,
    objects,
    wing,
    sensitivities,
    single_valued=False,
    valid_from=None,
    source_thread=None,
    source_url=None,
):
    from ..knowledge_graph import valid_fact_entries

    if thread_scope is None:
        return None, None
    if not isinstance(thread_scope, dict):
        return None, {"success": False, "error": "thread_scope is invalid"}
    if set(thread_scope) == {"allow"} and valid_fact_entries(thread_scope["allow"]):
        return thread_scope["allow"], None
    if (
        thread_scope.get("probe") is not True
        or set(thread_scope) - {"probe", "after", "closet"}
        or (thread_scope.get("after") is not None and not isinstance(thread_scope["after"], str))
        or ("closet" in thread_scope and not isinstance(thread_scope["closet"], str))
    ):
        return None, {"success": False, "error": "thread_scope is invalid"}
    page = _call_kg(
        lambda kg: kg.mutation_candidates(
            kind,
            subject,
            predicate,
            objects,
            wing,
            sensitivities,
            after=thread_scope.get("after"),
            single_valued=single_valued,
            valid_from=valid_from,
            source_thread=source_thread,
            source_url=source_url,
        ),
        read=True,
    )
    result = {"success": True, "probe": page}
    if "closet" in thread_scope:
        try:
            found = read_associations("closet", thread_scope["closet"])["associations"]
            pair = [found[0].get("wing"), found[0].get("room")] if found else None
            if pair is not None and not all(isinstance(x, str) and x.strip() for x in pair):
                pair = None
        except Exception:
            pair = None
        result["cited"] = {"closet": pair}

    return None, result


def tool_kg_add(
    subject: str,
    predicate: str,
    object: str,
    valid_from: str = None,
    valid_to: str = None,
    source_closet: str = None,
    source_file: str = None,
    source_drawer_id: str = None,
    source_thread: str = None,
    source_seq_start: int = None,
    source_seq_end: int = None,
    source_url: str = None,
    wing: str = None,
    sensitivity: str = None,
    sensitivities: list = None,
    thread_scope: dict = None,
):
    """Add a relationship to the knowledge graph.

    All temporal and provenance fields are optional. ``valid_to`` lets callers
    backfill historical facts with a known end date/time in a single call
    instead of a separate ``kg_invalidate`` call.

    Temporal values accept either ``YYYY-MM-DD`` or canonical UTC datetimes in
    the form ``YYYY-MM-DDTHH:MM:SSZ``.

    ``source_thread`` plus ``source_seq_start``/``source_seq_end`` name the
    chat messages the fact came from. ``wing`` scopes the fact and
    ``sensitivity`` says how far it may travel.
    """
    try:
        subject = sanitize_kg_value(subject, "subject")
        predicate = sanitize_name(predicate, "predicate")
        object = sanitize_kg_value(object, "object")
        valid_from = sanitize_iso_temporal(valid_from, "valid_from")
        valid_to = sanitize_iso_temporal(valid_to, "valid_to")
        source_thread, source_seq_start, source_seq_end = source_range(
            source_thread, source_seq_start, source_seq_end
        )
        wing = fact_wing(wing)
        sensitivity = fact_sensitivity(sensitivity)
        _, sensitivities = visibility(None, sensitivities)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    allow, scoped_reply = _kg_mutation_scope(
        thread_scope, "add", subject, predicate, [object], wing, sensitivities
    )
    if scoped_reply is not None:
        return scoped_reply

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
            "source_thread": source_thread,
            "source_seq_start": source_seq_start,
            "source_seq_end": source_seq_end,
            "source_url": source_url,
            "wing": wing,
            "sensitivity": sensitivity,
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
                source_thread=source_thread,
                source_seq_start=source_seq_start,
                source_seq_end=source_seq_end,
                source_url=source_url,
                wing=wing,
                sensitivity=sensitivity,
                sensitivities=sensitivities,
                allow=allow,
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
        "wing": wing,
        "sources": _call_kg(lambda kg: kg.sources([triple_id]), read=True).get(triple_id, []),
    }
    _wal_result("kg_add", outcome)
    return _with_kg_notice(outcome)


def tool_kg_save(
    subject: str,
    predicate: str,
    object: str,
    valid_from: str = None,
    confidence: float = 1.0,
    source_thread: str = None,
    source_seq_start: int = None,
    source_seq_end: int = None,
    source_url: str = None,
    source_drawer_id: str = None,
    source_file: str = None,
    wing: str = None,
    sensitivity: str = None,
    sensitivities: list = None,
    single_valued: bool = None,
    thread_scope: dict = None,
):
    """Save a fact, merging it into the same open fact or replacing a single value.

    See :meth:`KnowledgeGraph.save_fact`. ``single_valued`` defaults to whether
    the predicate is in ``single_valued_predicates``.
    """
    try:
        subject = sanitize_kg_value(subject, "subject")
        predicate = sanitize_name(predicate, "predicate")
        object = sanitize_kg_value(object, "object")
        valid_from = sanitize_iso_temporal(valid_from, "valid_from")
        source_thread, source_seq_start, source_seq_end = source_range(
            source_thread, source_seq_start, source_seq_end
        )
        wing = fact_wing(wing)
        sensitivity = fact_sensitivity(sensitivity)
        _, sensitivities = visibility(None, sensitivities)
        if single_valued is not None and not isinstance(single_valued, bool):
            raise ValueError("single_valued must be true or false")
    except ValueError as e:
        return {"success": False, "error": str(e)}
    if single_valued is None:
        single_valued = predicate.lower().replace(" ", "_") in _config.single_valued_predicates

    allow, scoped_reply = _kg_mutation_scope(
        thread_scope,
        "save",
        subject,
        predicate,
        [object],
        wing,
        sensitivities,
        single_valued=single_valued,
        valid_from=valid_from,
        source_thread=source_thread,
        source_url=source_url,
    )
    if scoped_reply is not None:
        return scoped_reply

    _wal_log(
        "kg_save",
        {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "valid_from": valid_from,
            "source_thread": source_thread,
            "source_seq_start": source_seq_start,
            "source_seq_end": source_seq_end,
            "source_url": source_url,
            "source_drawer_id": source_drawer_id,
            "source_file": source_file,
            "confidence": confidence,
            "wing": wing,
            "sensitivity": sensitivity,
            "sensitivities": sensitivities,
            "single_valued": single_valued,
        },
    )
    try:
        outcome = _call_kg(
            lambda kg: kg.save_fact(
                subject,
                predicate,
                object,
                valid_from=valid_from,
                confidence=confidence,
                source_thread=source_thread,
                source_seq_start=source_seq_start,
                source_seq_end=source_seq_end,
                source_url=source_url,
                source_drawer_id=source_drawer_id,
                source_file=source_file,
                wing=wing,
                sensitivity=sensitivity,
                sensitivities=sensitivities,
                allow=allow,
                single_valued=single_valued,
            )
        )
    except ValueError as e:
        outcome = {"success": False, "error": str(e)}
        _wal_result("kg_save", outcome)
        return outcome
    rows = _call_kg(
        lambda kg: kg.query_entity(
            subject, wings=[wing] if wing else None, sensitivities=sensitivities
        ),
        read=True,
    )
    saved = next((row for row in rows if row["id"] == outcome["triple_id"]), {})
    # Previous/current ids and values are also echoes of stored facts. Bind
    # them to their own sources so the gateway can drop an unreadable echo.
    related_ids = list(outcome.get("superseded", []))
    if outcome.get("current"):
        related_ids.append(outcome["current"])
    if related_ids:
        sources = _call_kg(lambda kg: kg.sources(related_ids), read=True)
        by_id = {row["id"]: row for row in rows}
        outcome["related_facts"] = [
            {**by_id.get(key, {"fact": "", "wing": wing}), "sources": sources.get(key, [])}
            for key in related_ids
        ]
    from mempalace.fact_provenance import COLUMNS

    outcome = {
        "success": True,
        **{key: saved.get(key) for key in (*COLUMNS, "fact_status")},
        **outcome,
        "fact": f"{subject} → {predicate} → {object}",
        "wing": wing,
        "sources": _call_kg(lambda kg: kg.sources([outcome["triple_id"]]), read=True).get(
            outcome["triple_id"], []
        ),
    }
    _wal_result("kg_save", outcome)
    return _with_kg_notice(outcome)


def tool_kg_invalidate(
    subject: str,
    predicate: str,
    object: str,
    ended: str = None,
    wing: str = None,
    sensitivities: list = None,
    thread_scope: dict = None,
):
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
        wing = fact_wing(wing)
        _, sensitivities = visibility(None, sensitivities)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resolved_ended = ended or date.today().isoformat()

    allow, scoped_reply = _kg_mutation_scope(
        thread_scope, "invalidate", subject, predicate, [object], wing, sensitivities
    )
    if scoped_reply is not None:
        return scoped_reply

    _wal_log(
        "kg_invalidate",
        {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "ended": resolved_ended,
            "wing": wing,
        },
    )

    closed = _call_kg(
        lambda kg: kg.invalidate(
            subject,
            predicate,
            object,
            ended=resolved_ended,
            wing=wing,
            sensitivities=sensitivities,
            allow=allow,
        )
    )
    return _with_kg_notice(
        {
            "success": True,
            "fact": f"{subject} → {predicate} → {object}",
            "ended": resolved_ended,
            "closed": closed,
        }
    )


def tool_kg_supersede(
    subject: str,
    predicate: str,
    old_object: str,
    new_object: str,
    at: str = None,
    source_thread: str = None,
    source_seq_start: int = None,
    source_seq_end: int = None,
    source_url: str = None,
    wing: str = None,
    sensitivity: str = None,
    sensitivities: list = None,
    thread_scope: dict = None,
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
        source_thread, source_seq_start, source_seq_end = source_range(
            source_thread, source_seq_start, source_seq_end
        )
        wing = fact_wing(wing)
        sensitivity = fact_sensitivity(sensitivity)
        _, sensitivities = visibility(None, sensitivities)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    allow, scoped_reply = _kg_mutation_scope(
        thread_scope, "supersede", subject, predicate, [old_object, new_object], wing, sensitivities
    )
    if scoped_reply is not None:
        return scoped_reply

    _wal_log(
        "kg_supersede",
        {
            "subject": subject,
            "predicate": predicate,
            "old_object": old_object,
            "new_object": new_object,
            "at": at,
            "source_thread": source_thread,
            "source_seq_start": source_seq_start,
            "source_seq_end": source_seq_end,
            "source_url": source_url,
            "wing": wing,
            "sensitivity": sensitivity,
        },
    )

    # Domain ValueErrors from kg.supersede (e.g. inverted boundary) are left to
    # bubble to the dispatcher, matching tool_kg_add / tool_kg_invalidate: the
    # -32000 response carries error_class + message in error.data. Only input
    # sanitization above returns the {success: False} envelope.
    triple_id = _call_kg(
        lambda kg: kg.supersede(
            subject,
            predicate,
            old_object,
            new_object,
            at=at,
            source_thread=source_thread,
            source_seq_start=source_seq_start,
            source_seq_end=source_seq_end,
            source_url=source_url,
            wing=wing,
            sensitivity=sensitivity,
            sensitivities=sensitivities,
            allow=allow,
        )
    )
    return _with_kg_notice(
        {
            "success": True,
            "triple_id": triple_id,
            "fact": f"{subject} → {predicate} → {new_object}",
            "superseded": old_object,
            "wing": wing,
            "sources": _call_kg(lambda kg: kg.sources([triple_id]), read=True).get(triple_id, []),
        }
    )


def tool_kg_timeline(
    entity: str = None,
    limit: int = 100,
    offset: int = 0,
    wings: list = None,
    sensitivities: list = None,
):
    """Get chronological timeline of facts, optionally for one entity.

    Paginated with ``limit``/``offset`` following the ``tool_list_drawers``
    convention; defaults match the historical behavior (first 100 facts).
    """
    limit = max(1, min(limit, _MAX_RESULTS))
    offset = max(0, offset)
    try:
        if entity is not None:
            entity = sanitize_kg_value(entity, "entity")
        wings, sensitivities = visibility(wings, sensitivities)
    except ValueError as e:
        return {"error": str(e)}

    def _query(kg):
        return {
            "timeline": kg.timeline(
                entity, limit=limit, offset=offset, wings=wings, sensitivities=sensitivities
            ),
            "total": kg.timeline_total(entity, wings=wings, sensitivities=sensitivities),
        }

    result = _call_kg(_query, read=True)
    sources = _call_kg(lambda kg: kg.sources([row["id"] for row in result["timeline"]]), read=True)
    for row in result["timeline"]:
        row["sources"] = sources.get(row["id"], [])
    return _with_kg_notice(
        {
            "entity": entity or "all",
            "timeline": result["timeline"],
            "count": len(result["timeline"]),
            "total": result["total"],
            "offset": offset,
            "limit": limit,
        }
    )


def tool_kg_stats():
    """Knowledge graph overview: entities, triples, relationship types."""
    return _with_kg_notice(_call_kg(lambda kg: kg.stats(), read=True))


def tool_kg_list_wings():
    """List fact wings without counting entities, facts or predicates."""
    return {"wings": _call_kg(lambda kg: kg.list_wings(), read=True)}
