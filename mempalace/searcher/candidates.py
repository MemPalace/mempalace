# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


def _candidate_pool_size(n_results: int, date_window_active: bool) -> int:
    """Rerank-pool size for the drawer vector query.

    Without a date window this is the historical ``n_results * 3``
    over-fetch. With one, the window filters the pool AFTER retrieval
    (ChromaDB rejects string operands for ``$gte``/``$lt``, so ``filed_at``
    can't be range-filtered server-side), and a narrow window over a large
    palace would starve a 3x pool even though matching drawers exist —
    recall is the design requirement. Widen to ``n_results * 15``, capped
    at 500 (the ceiling the filter-fallback path already uses) — except
    the pool never drops below ``n_results`` itself, or an oversized
    request could return fewer rows than an unfiltered query would.
    ``date_filter_pool_truncated`` in the response flags a full pool so a
    capped result is never silent.
    """
    if not date_window_active:
        return n_results * 3
    return max(min(n_results * 15, 500), n_results)


_CLOSET_RESULT_POOL_MULTIPLIER = 4
_MAX_HYDRATION_CHARS = 10000


def _candidate_pool_limits(
    candidate_strategy: str,
    n_results: int,
    date_window_active: bool = False,
) -> tuple[int, int]:
    """Return vector-query and pre-enrichment candidate limits.

    Date-window searches retain the wider current-develop pool because the
    timestamp constraint is applied after retrieval. The normal vector path
    also remains at least four times wide through closet enrichment. Union
    mode keeps only the requested top vector candidates before lexical merge.
    """
    base_query_limit = _candidate_pool_size(
        n_results,
        date_window_active,
    )

    if candidate_strategy == "union":
        return base_query_limit, n_results

    widened = max(
        base_query_limit,
        n_results * _CLOSET_RESULT_POOL_MULTIPLIER,
    )
    return widened, widened


def _enrich_closet_hits(
    hits: list,
    drawers_col,
    query: str,
    stop_words: frozenset = frozenset(),
    committed_tokens=None,
    tokened_source_modes=None,
) -> list:
    """Hydrate closet-boosted hits and memoise each source/group fetch."""
    query_terms = set(
        _tokenize(
            query,
            stop_words,
        )
    )
    source_cache: dict = {}
    if committed_tokens is None or tokened_source_modes is None:
        committed_tokens, tokened_source_modes = _committed_generation_state(drawers_col)
    tokened_source_modes = tokened_source_modes or frozenset()

    for hit in hits:
        if hit.get("matched_via") == "drawer":
            continue

        full_source = hit.get("_source_file_full") or ""

        if not full_source:
            continue

        parent_drawer_id = hit.get("_parent_drawer_id") or None
        cache_key = (
            full_source,
            parent_drawer_id,
        )

        if cache_key not in source_cache:
            try:
                source_drawers = drawers_col.get(
                    where=(
                        _scoped_source_filter(
                            full_source,
                            parent_drawer_id,
                        )
                    ),
                    include=[
                        "documents",
                        "metadatas",
                    ],
                )
            except Exception:
                logger.debug(
                    "Neighbor fetch failed for %s",
                    full_source,
                    exc_info=True,
                )
                source_cache[cache_key] = None
            else:
                source_cache[cache_key] = (
                    list(
                        (
                            source_drawers.get("ids", [])
                            if isinstance(source_drawers, dict)
                            else getattr(source_drawers, "ids", None)
                        )
                        or []
                    ),
                    list(
                        getattr(
                            source_drawers,
                            "documents",
                            None,
                        )
                        or []
                    ),
                    list(
                        getattr(
                            source_drawers,
                            "metadatas",
                            None,
                        )
                        or []
                    ),
                )

        cached = source_cache[cache_key]

        if cached is None:
            continue

        physical_ids, docs, metadatas = cached
        if len(physical_ids) < len(docs):
            physical_ids.extend(
                f"_hydrated_row_{index}" for index in range(len(physical_ids), len(docs))
            )

        if len(docs) <= 1:
            continue

        indexed = []

        source_rows = _collapse_physical_generation_rows(
            list(zip(physical_ids, docs, metadatas)),
            committed_tokens,
            tokened_source_modes,
        )
        for index, (_, document, metadata) in enumerate(source_rows):
            chunk_index = (
                metadata.get(
                    "chunk_index",
                    index,
                )
                if isinstance(
                    metadata,
                    dict,
                )
                else index
            )

            if not isinstance(
                chunk_index,
                int,
            ):
                chunk_index = index

            indexed.append(
                (
                    chunk_index,
                    document or "",
                )
            )

        indexed.sort(key=lambda pair: pair[0])
        ordered_docs = [document for _, document in indexed]

        best_index = 0
        best_score = -1

        for index, document in enumerate(ordered_docs):
            lowered = document.lower()
            score = sum(1 for term in query_terms if term in lowered)

            if score > best_score:
                best_score = score
                best_index = index

        start = max(
            0,
            best_index - 1,
        )
        end = min(
            len(ordered_docs),
            best_index + 2,
        )
        expanded = "\n\n".join(ordered_docs[start:end])

        if len(expanded) > _MAX_HYDRATION_CHARS:
            expanded = expanded[:_MAX_HYDRATION_CHARS] + (
                f"\n\n[...truncated. "
                f"{len(ordered_docs)} total drawers. "
                "Use mempalace_get_drawer "
                "for full content.]"
            )

        hit["text"] = expanded
        hit["drawer_index"] = best_index
        hit["total_drawers"] = len(ordered_docs)

    return hits


def _dedupe_rendered_hits(
    hits: list,
) -> list:
    """Drop repeated closet-rendered passages while preserving rank order.

    Plain drawer hits retain their historical behavior, including legitimate
    exact repeats at different source positions. A duplicate is removed only
    when either occurrence came through closet enrichment.
    """
    unique = []
    first_by_key: dict = {}

    for hit in hits:
        source = hit.get("_source_file_full") or hit.get("source_path") or hit.get("source_file")
        text = hit.get("text")

        if not source or not isinstance(
            text,
            str,
        ):
            unique.append(hit)
            continue

        key = (
            source,
            text,
        )
        previous = first_by_key.get(key)

        if previous is None:
            first_by_key[key] = hit
            unique.append(hit)
            continue

        previous_is_closet = previous.get("matched_via") == "drawer+closet"
        current_is_closet = hit.get("matched_via") == "drawer+closet"

        if previous_is_closet or current_is_closet:
            continue

        unique.append(hit)

    return unique


def _collection_get_rows(result) -> tuple[list, list, list]:
    """Normalize a collection.get() payload to (ids, documents, metadatas)."""
    if isinstance(result, dict):
        ids = result.get("ids") or []
        docs = result.get("documents") or []
        metas = result.get("metadatas") or []
    else:
        ids = getattr(result, "ids", None) or []
        docs = getattr(result, "documents", None) or []
        metas = getattr(result, "metadatas", None) or []
    return list(ids), list(docs), list(metas)


def _parent_ids_from_hits(hits: list) -> list:
    parent_ids = []
    seen = set()
    for hit in hits:
        parent_id = hit.get("_parent_drawer_id") or hit.get("_parent_entry_id")
        if not parent_id:
            parent_id = _logical_parent_id(hit.get("metadata"))
        if parent_id and parent_id not in seen:
            seen.add(parent_id)
            parent_ids.append(parent_id)
    return parent_ids


def _sibling_search_hit(physical_id, doc, meta, distance, committed_tokens, metric="cosine"):
    """Build a drawer hit from a parent-linked sibling loaded via get()."""
    source = (meta or {}).get("source_file", "") or ""
    bounded = max(0.0, min(2.0, distance))
    return {
        "drawer_id": _result_drawer_id(meta, physical_id),
        "text": doc,
        "wing": (meta or {}).get("wing", "unknown"),
        "room": (meta or {}).get("room", "unknown"),
        "source_file": Path(source).name if source else "?",
        "source_path": source,
        **_result_date_fields(meta or {}),
        "similarity": round(_distance_to_similarity(distance, metric), 3),
        "distance": round(distance, 4),
        "effective_distance": round(bounded, 4),
        "closet_boost": 0.0,
        "matched_via": "drawer",
        "_sort_key": distance,
        "_source_file_full": source,
        "_chunk_index": (meta or {}).get("chunk_index"),
        "_parent_drawer_id": (meta or {}).get("parent_drawer_id"),
        "_parent_entry_id": (meta or {}).get("parent_entry_id"),
        "_logical_generation_id": _logical_generation_id(meta),
        "_physical_drawer_id": physical_id,
        "_active_generation": (meta or {}).get("mine_generation_token") in committed_tokens,
    }


def _sibling_matches_request_filters(meta, wing=None, room=None, source_file=None) -> bool:
    """True when a refilled sibling satisfies the caller's search filters."""
    meta = meta or {}
    if wing and meta.get("wing") != wing:
        return False
    if room and meta.get("room") != room:
        return False
    if source_file and meta.get("source_file") != source_file:
        return False
    return True


def _mixed_generation_leftover_ids(ids, metas, parent_id, committed_tokens, tokened_source_modes):
    """Physical IDs that still carry ``logical_drawer_id`` beside a stripped prefix.

    A failed shrink leaves rewritten prefix chunks without generation identity
    and the unread tail still carrying ``logical_drawer_id``. Ordinary parent
    groups share one identity and must not be refilled wholesale.
    """
    stripped = False
    leftovers = []
    for index, physical_id in enumerate(ids):
        meta = metas[index] if index < len(metas) else {}
        if _logical_parent_id(meta) != parent_id:
            continue
        if not _is_visible_generation_metadata(meta, committed_tokens, tokened_source_modes):
            continue
        if meta.get("logical_drawer_id"):
            leftovers.append(physical_id)
        else:
            stripped = True
    if not stripped:
        return set()
    return set(leftovers)


def _include_matching_parent_siblings(
    hits: list,
    drawers_col,
    query: str,
    committed_tokens,
    tokened_source_modes,
    stop_words=frozenset(),
    metric: str = "cosine",
    wing=None,
    room=None,
    source_file=None,
) -> list:
    """Add leftover parent chunks that HNSW omitted but get() can still read.

    A failed shrink delete leaves the new prefix without ``logical_drawer_id``
    and the old tail still parent-linked. Vector query can drop that tail
    after the prefix upsert; logical drawer reads already recover it via
    parent ``get()``. Search must return the same verbatim leftover chunk
    without admitting every query-term sibling into the pre-rerank pool.
    """
    if not hits or not query:
        return hits
    query_terms = set(_tokenize(query, stop_words))
    if not query_terms:
        return hits

    seen_ids = {hit.get("_physical_drawer_id") for hit in hits if hit.get("_physical_drawer_id")}
    added = []
    for parent_id in _parent_ids_from_hits(hits):
        try:
            result = drawers_col.get(
                where=_logical_parent_where(parent_id),
                include=["documents", "metadatas"],
            )
        except Exception:
            logger.debug("parent sibling refill failed for %s", parent_id, exc_info=True)
            continue
        ids, docs, metas = _collection_get_rows(result)
        leftover_ids = _mixed_generation_leftover_ids(
            ids, metas, parent_id, committed_tokens, tokened_source_modes
        )
        if not leftover_ids:
            continue
        parent_distances = [
            hit.get("distance")
            for hit in hits
            if (hit.get("_parent_drawer_id") or hit.get("_parent_entry_id")) == parent_id
            and hit.get("distance") is not None
        ]
        seed_dist = min(parent_distances) if parent_distances else 2.0
        for index, physical_id in enumerate(ids):
            if physical_id not in leftover_ids or physical_id in seen_ids:
                continue
            meta = metas[index] if index < len(metas) else {}
            if not _sibling_matches_request_filters(
                meta, wing=wing, room=room, source_file=source_file
            ):
                continue
            doc = docs[index] if index < len(docs) else ""
            doc = doc or ""
            lowered = doc.lower()
            if not any(term in lowered for term in query_terms):
                continue
            seen_ids.add(physical_id)
            added.append(
                _sibling_search_hit(
                    physical_id,
                    doc,
                    meta,
                    seed_dist,
                    committed_tokens,
                    metric=metric,
                )
            )
    return hits + added


# Strategy dispatch — keeps search_memories' branch count under the
# project's complexity ceiling (C901 max-complexity=25). New strategies
# register here.
_CANDIDATE_MERGERS = {
    "vector": None,  # default no-op
    "union": _merge_bm25_union_candidates,
}


def _validate_candidate_strategy(strategy: str) -> None:
    """Raise ``ValueError`` for unknown strategies.

    Called eagerly at the top of ``search_memories`` so invalid values
    fail consistently regardless of whether the call routes through the
    vector path, the BM25-only fallback, or returns an early error dict.
    """
    if strategy not in _CANDIDATE_MERGERS:
        raise ValueError(
            f"candidate_strategy must be one of {tuple(_CANDIDATE_MERGERS)}, got {strategy!r}"
        )


def _apply_candidate_strategy(
    strategy: str,
    hits: list,
    drawers_col,
    query: str,
    wing: str,
    room: str,
    n_results: int,
    max_distance: float = 0.0,
    source_file: str = None,
    since_dt=None,
    before_dt=None,
) -> None:
    """Dispatch to the registered merger for ``strategy``.

    Strategy validity is assumed (``_validate_candidate_strategy`` runs
    earlier); ``"vector"`` is a no-op.
    """
    merger = _CANDIDATE_MERGERS[strategy]
    if merger is not None:
        merger(
            hits,
            drawers_col,
            query,
            wing,
            room,
            n_results,
            max_distance=max_distance,
            source_file=source_file,
            since_dt=since_dt,
            before_dt=before_dt,
        )


def _finalize_candidate_hits(
    *,
    candidate_strategy: str,
    hits: list,
    drawers_col,
    query: str,
    wing: str,
    room: str,
    n_results: int,
    max_distance: float,
    source_file: str = None,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
) -> tuple:
    try:
        _apply_candidate_strategy(
            candidate_strategy,
            hits,
            drawers_col,
            query,
            wing,
            room,
            n_results,
            max_distance=max_distance,
            source_file=source_file,
            since_dt=since_dt,
            before_dt=before_dt,
        )
    except UnsupportedCapabilityError:
        return [], _search_error_result(
            "candidate_strategy='union' requires a backend with lexical_search support",
            unsupported_capability="supports_lexical_search",
            hint=(
                "Use candidate_strategy='vector' or select a backend that supports lexical search."
            ),
        )
    except GenerationStateError as e:
        return [], _search_error_result(str(e))

    hits[:] = _collapse_logical_generation_hits(hits)
    vector_weight, bm25_weight = _resolve_hybrid_rank_weights()
    ranked = _hybrid_rank(
        hits,
        query,
        vector_weight=vector_weight,
        bm25_weight=bm25_weight,
        metric=_metric_for_collection(drawers_col),
        stop_words=stop_words,
    )
    hits = _dedupe_rendered_hits(ranked)[:n_results]

    for hit in hits:
        hit.pop("_sort_key", None)
        hit.pop("_source_file_full", None)
        hit.pop("_chunk_index", None)
        hit.pop("_parent_drawer_id", None)
        hit.pop("_parent_entry_id", None)
        hit.pop("_logical_generation_id", None)
        hit.pop("_physical_drawer_id", None)
        hit.pop("_active_generation", None)

    return hits, None


def _search_error_result(error: str, **extra) -> dict:
    """Error envelope for programmatic search callers.

    Always includes ``results: []`` so callers can safely index
    ``result["results"]`` without a KeyError when the palace failed to
    open or the query raised mid-flight (Windows CI flake surface).
    """
    out = {"error": error, "results": []}
    out.update(extra)
    return out


def _backend_mismatch_result(error: BackendMismatchError) -> dict:
    return _search_error_result(
        "Backend mismatch",
        details=str(error),
        hint="Select the matching backend or use a fresh palace directory.",
    )


def _unknown_backend_result(error: KeyError) -> dict:
    return _search_error_result(
        "Unknown backend",
        details=str(error),
        hint="Check MEMPALACE_BACKEND or the configured backend name.",
    )


def _search_result_envelope(
    *,
    query: str,
    wing,
    room,
    source_file,
    since,
    before,
    hits: list,
    candidates_fetched: int,
    pool_size: int,
    date_window_active: bool,
) -> dict:
    """Assemble the ``search_memories`` response dict.

    When a date window is active and the widened candidate pool came back
    full, drawers beyond the pool never got a chance to match the window —
    ``date_filter_pool_truncated`` flags it so a thin result under a date
    filter is never mistaken for "that's all there was".
    """
    result = {
        "query": query,
        "filters": {
            "wing": wing,
            "room": room,
            "source_file": source_file,
            "since": since,
            "before": before,
        },
        "total_before_filter": candidates_fetched,
        "results": hits,
    }
    if date_window_active and candidates_fetched >= pool_size:
        result["date_filter_pool_truncated"] = True
    return result


def _window_and_fallback_gate(
    since,
    before,
    vector_disabled: bool,
    *,
    query: str,
    palace_path: str,
    wing,
    room,
    n_results: int,
    collection_name,
    source_file,
    stop_words: frozenset = frozenset(),
):
    """Front gate for ``search_memories``: parse the window, route the fallback.

    Returns ``(since_dt, before_dt, active, short_circuit)``.
    ``short_circuit`` is a complete response to return verbatim — the
    ``{"error": ...}`` payload for an invalid/inverted window, or the
    BM25-only fallback result when ``vector_disabled`` is set — and ``None``
    when the vector path should proceed. Extracted so the window plumbing
    doesn't push ``search_memories`` over the C901 complexity ceiling.
    """
    try:
        since_dt, before_dt = parse_window(since, before)
    except ValueError as e:
        return None, None, False, {"error": str(e)}
    active = since_dt is not None or before_dt is not None
    if vector_disabled:
        return (
            since_dt,
            before_dt,
            active,
            _vector_disabled_with_window(
                query=query,
                palace_path=palace_path,
                wing=wing,
                room=room,
                n_results=n_results,
                collection_name=collection_name,
                source_file=source_file,
                since=since,
                before=before,
                since_dt=since_dt,
                before_dt=before_dt,
                stop_words=stop_words,
            ),
        )
    return since_dt, before_dt, active, None


def _candidate_out_of_scope(
    dist,
    meta,
    max_distance,
    since_dt,
    before_dt,
    committed_tokens=frozenset(),
    tokened_source_modes=frozenset(),
) -> bool:
    """True when a drawer candidate fails the distance or date-window gate.

    Distance is checked on the raw value before rounding to avoid precision
    loss (pre-existing behavior); the date window applies whenever a bound
    is set, with the shared ``[since, before)`` semantics.
    """
    if not _is_visible_generation_metadata(meta, committed_tokens, tokened_source_modes):
        return True
    if max_distance > 0.0 and dist > max_distance:
        return True
    if (since_dt is not None or before_dt is not None) and not filed_at_in_window(
        meta.get("filed_at"), since_dt, before_dt
    ):
        return True
    return False


def _vector_disabled_with_window(
    *,
    query: str,
    palace_path: str,
    wing: str,
    room: str,
    n_results: int,
    collection_name: str,
    source_file: str,
    since: str,
    before: str,
    since_dt,
    before_dt,
    stop_words: frozenset = frozenset(),
) -> dict:
    """Run the BM25-only route and echo the raw window strings.

    The fallback helper takes parsed bounds; the caller's raw ``since``/
    ``before`` strings are stitched into the ``filters`` envelope here so
    both search paths report the same shape.
    """
    result = _vector_disabled_search(
        query=query,
        palace_path=palace_path,
        wing=wing,
        room=room,
        n_results=n_results,
        collection_name=collection_name,
        source_file=source_file,
        since_dt=since_dt,
        before_dt=before_dt,
        stop_words=stop_words,
    )
    if "filters" in result:
        result["filters"]["since"] = since
        result["filters"]["before"] = before
    return result


def _vector_disabled_search(
    *,
    query: str,
    palace_path: str,
    wing: str,
    room: str,
    n_results: int,
    collection_name: str,
    source_file: str = None,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
) -> dict:
    try:
        backend_name = resolve_backend_name(palace_path)
    except BackendMismatchError as e:
        return _backend_mismatch_result(e)
    except KeyError as e:
        return _unknown_backend_result(e)
    if backend_name != "chroma":
        return _search_error_result(
            "vector_disabled fallback is Chroma-only",
            unsupported_capability="chroma_hnsw_fallback",
            backend=backend_name,
            hint="Disable vector_disabled for non-Chroma backends.",
        )
    return _bm25_only_via_sqlite(
        query,
        palace_path,
        wing=wing,
        room=room,
        source_file=source_file,
        n_results=n_results,
        collection_name=collection_name,
        stop_words=stop_words,
        since_dt=since_dt,
        before_dt=before_dt,
    )
