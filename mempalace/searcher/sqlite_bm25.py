# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


def _result_date_fields(meta: dict) -> dict:
    """Expose stored dates and disclose the legacy authored-at fallback.

    ``created_at`` and ``authored_at`` retain their historical values;
    the source identifies the field supplying ``authored_at``, not a
    guarantee of authorship. Content-date provenance is only reported
    when stored, never reconstructed from a legacy drawer's path or text.
    """
    filed_at = meta.get("filed_at", "unknown")
    authored_at = meta.get("authored_at", filed_at)
    authored_at_source = "authored_at" if "authored_at" in meta else "filed_at"
    if not authored_at or authored_at == "unknown":
        authored_at_source = "unknown"
    content_date = meta.get("content_date")
    content_date_source = meta.get("content_date_source") or "unknown"
    if not content_date or content_date == "unknown":
        content_date_source = "unknown"
    return {
        "created_at": filed_at,
        "filed_at": filed_at,
        "authored_at": authored_at,
        "authored_at_source": authored_at_source,
        "content_date": content_date,
        "content_date_source": content_date_source,
    }


def _window_sql_prefilters(since_dt, before_dt) -> list:
    """(operator, bound-string) pairs for the SQL date-window narrowing.

    A SQL-side *narrowing* on the ISO ``filed_at`` string, kept at
    whole-DAY granularity so it is provably wider than the window for
    every ISO-8601 spelling that shares the YYYY-MM-DD prefix (bare date,
    space separator, minute precision, Z/offset suffixes) — a
    full-isoformat bound would sort after some of those on the boundary
    day and drop an in-window row at the SQL layer, where the
    authoritative Python re-filter (instant comparison, unparseable
    exclusion — mirroring the wing/room double-check) can't recover it.
    Stored values are UTC or naive local, so a stored date can sit up to a
    day either side of the bound's UTC date; the narrowing widens by that
    day. Python decides the exact window.
    """
    prefilters = []
    if since_dt is not None:
        try:
            lower = (localize(since_dt).astimezone(timezone.utc) - timedelta(days=1)).date()
            prefilters.append((">=", lower.isoformat()))
        except OverflowError:
            # since at the calendar floor: nothing to narrow by.
            pass
    if before_dt is not None:
        try:
            upper = (localize(before_dt).astimezone(timezone.utc) + timedelta(days=2)).date()
            prefilters.append(("<", upper.isoformat()))
        except OverflowError:
            # before at the calendar ceiling ("9999-12-31" as an open-ended
            # sentinel): there is no next day to bound by, so skip the SQL
            # narrowing entirely — the Python re-filter stays authoritative
            # and such a window is effectively unbounded above anyway.
            pass
    return prefilters


def _flag_truncations(result: dict, window_pool_truncated: bool, candidates_truncated: bool):
    """Mark a fallback result whose candidate pool was cut short."""
    if window_pool_truncated:
        result["date_filter_pool_truncated"] = True
    if candidates_truncated:
        # A missing whole word was searched for past the ranked window and
        # the read budget ran out: drawers holding it may not have been seen.
        result["candidates_truncated"] = True
    return result


def _pick_fts_candidates(
    conn, collection_name, query, *, max_candidates, equalities, fts_filter, row_filter, stop_words
) -> list[int]:
    """Row ids for the BM25 re-rank, whole-word matches first.

    The trigram index matches inside words, and the first matches in storage
    order were the oldest substring hits. A scope filter matching few drawers
    is read directly (``row_filter`` refers to ``e.id``) instead of ranking
    the palace-wide match set (``fts_filter``). ``equalities`` are the
    stored ``(key, values)`` the scope allows.
    """
    candidate_ids = _filtered_candidate_rows(
        conn,
        collection_name,
        query,
        limit=max_candidates,
        equalities=equalities,
        filter_sql=row_filter[0],
        filter_params=row_filter[1],
        stop_words=stop_words,
    )
    if candidate_ids is None:
        candidate_ids = _fts_candidate_rows(
            conn,
            collection_name,
            query,
            limit=max_candidates,
            filter_sql=fts_filter[0],
            filter_params=fts_filter[1],
            stop_words=stop_words,
        )
    return candidate_ids


def _bm25_only_via_sqlite(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    source_file: str = None,
    n_results: int = 5,
    max_candidates: int = 500,
    _include_internal: bool = False,
    collection_name: str = None,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
) -> dict:
    """BM25-only search reading drawers directly from chroma.sqlite3.

    Used when HNSW is diverged or unloadable. Bypasses chromadb's
    Python client entirely so a corrupt vector segment can't segfault the
    MCP server. Routes through chromadb's own FTS5 trigram index
    (``embedding_fulltext_search``) for candidate selection, then re-ranks
    with the same Okapi-BM25 used in :func:`_hybrid_rank` so the result
    shape matches the vector path.

    The query is split into ≥3-char trigram-tokens and OR-joined for the
    FTS5 MATCH — chromadb writes the index with ``tokenize='trigram'``,
    so single-character tokens never match. When no usable token survives
    (e.g. "is a"), candidate selection falls back to the most-recent
    ``max_candidates`` rows so we still return *something* rather than
    nothing.
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return _search_error_result(
            "No palace found",
            hint="Run: mempalace init <dir> && mempalace mine <dir>",
        )
    if collection_name is None:
        from ..config import get_configured_collection_name

        collection_name = get_configured_collection_name()
    wing_names = wing_aliases(wing, chroma_known_wings(palace_path)) if wing else []
    scope = {
        "wing": wing_names,
        "room": [room] if room else [],
        "source_file": [source_file] if source_file else [],
    }

    equalities = [
        (key, tuple(encode_filter(value) for value in values))
        for key, values in scope.items()
        if values
    ]

    def _metadata_filter_sql(row_id_expr: str) -> tuple[str, list[str]]:
        sql, params = _match_sql(equalities, row_id_expr, "mf")
        for op, sql_bound in _window_sql_prefilters(since_dt, before_dt):
            sql += (
                " AND EXISTS (SELECT 1 FROM embedding_metadata mf"
                f" WHERE mf.id = {row_id_expr} AND mf.key = 'filed_at'"
                f" AND mf.string_value {op} ?)"
            )
            params.append(sql_bound)
        return sql, params

    try:
        conn = open_palace_reader(db_path)
    except sqlite3.Error as e:
        return _search_error_result(f"sqlite open failed: {e}")

    window_active = since_dt is not None or before_dt is not None
    try:
        # FTS5 MATCH expects whitespace-separated tokens. Drop tokens
        # shorter than 3 chars (trigram tokenizer can't match them).
        tokens = [t for t in tokenize(query) if len(t) >= 3]
        candidate_ids: list[int] = []
        candidates_truncated = False
        use_recency_fallback = not tokens
        if tokens:
            filter_sql, filter_params = _metadata_filter_sql("embedding_fulltext_search.rowid")
            try:
                candidate_ids = _pick_fts_candidates(
                    conn,
                    collection_name,
                    query,
                    max_candidates=max_candidates,
                    equalities=equalities,
                    fts_filter=(filter_sql, filter_params),
                    row_filter=_metadata_filter_sql("e.id"),
                    stop_words=stop_words,
                )
                candidates_truncated = getattr(candidate_ids, "truncated", False)
            except sqlite3.Error:
                # FTS5 tokenizer mismatch or syntax error — fall through
                # to the recency-window selector below.
                logger.debug("FTS5 MATCH failed; using recency fallback", exc_info=True)
                use_recency_fallback = True

        if not candidate_ids and use_recency_fallback:
            # No usable FTS tokens, or FTS itself failed — pull the most
            # recent rows for the drawers segment so we can BM25-rank
            # something rather than return empty-handed. A clean FTS miss
            # must stay empty, especially after wing/room filtering, because
            # recency fallback would return unrelated scoped drawers.
            # On a legacy schema (older chromadb without ``created_at``,
            # missing ``segments`` rows after partial restore) the window is
            # ordered by id, and failing that is empty rather than raising.
            try:
                candidate_ids = _newest_row_ids(
                    conn, collection_name, max_candidates, *_metadata_filter_sql("e.id")
                )
            except sqlite3.Error:
                logger.debug("recency-window queries failed", exc_info=True)
                candidate_ids = []

        # A full candidate page means rows beyond it never got a chance to
        # match the window — mirror the vector path's truncation honesty
        # (``date_filter_pool_truncated``) instead of a silently thin result.
        window_pool_truncated = window_active and len(candidate_ids) >= max_candidates

        if not candidate_ids:
            return {
                "query": query,
                "filters": {"wing": wing, "room": room, "source_file": source_file},
                "total_before_filter": 0,
                "results": [],
                "fallback": "bm25_only_via_sqlite",
            }

        # In row order: the BM25 sort below keeps it for ties.
        rows = _decoded_rows(conn, sorted(candidate_ids))
    finally:
        conn.close()

    # Apply wing/room filters in Python (FTS5 candidates may include
    # entries from other wings).
    candidates = []
    wings = set(wing_names)
    for stored_drawer_id, text, meta in rows:
        text, meta = text or "", meta or {}
        if is_bookkeeping(meta, stored_drawer_id):
            continue
        if wings and meta.get("wing") not in wings:
            continue
        if room and meta.get("room") != room:
            continue
        if source_file and meta.get("source_file") != source_file:
            continue
        if window_active and not filed_at_in_window(meta.get("filed_at"), since_dt, before_dt):
            continue
        full_source = meta.get("source_file", "") or ""
        candidates.append(
            {
                "drawer_id": _result_drawer_id(meta, stored_drawer_id),
                "text": text,
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "source_file": Path(full_source).name if full_source else "?",
                "source_path": full_source,
                **_result_date_fields(meta),
                # No vector distance available in BM25-only mode.
                "similarity": None,
                "distance": None,
                "matched_via": "bm25_sqlite",
                # Internal: full path + chunk_index let callers (notably
                # candidate_strategy="union") dedupe at chunk granularity
                # rather than basename — two files in different directories
                # may share a basename, and one source_file is split across
                # multiple chunks. Stripped before this helper returns.
                "_source_file_full": full_source,
                "_chunk_index": meta.get("chunk_index"),
            }
        )

    # Local BM25 over the candidate set.
    docs = [c["text"] for c in candidates]
    bm25_raw = bm25_scores(query, docs, stop_words=stop_words)
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0
    for c, raw in zip(candidates, bm25_raw):
        c["bm25_score"] = round(raw, 3)
        c["_score"] = (raw / max_bm25) if max_bm25 > 0 else 0.0
    candidates.sort(key=lambda c: c["_score"], reverse=True)
    hits = _fold_copies_across_sources(candidates, _search_hit_source, _search_hit_ref)[:n_results]
    for h in hits:
        h.pop("_score", None)
        # Strip internal fields by default so the public BM25-only fallback
        # response stays clean. Callers that need chunk-precise dedup
        # (notably the union-merge path) opt in via _include_internal.
        if not _include_internal:
            h.pop("_source_file_full", None)
            h.pop("_chunk_index", None)

    result = {
        "query": query,
        "filters": {"wing": wing, "room": room, "source_file": source_file},
        "total_before_filter": len(candidates),
        "results": hits,
        "fallback": "bm25_only_via_sqlite",
        "fallback_reason": "vector_search_disabled",
    }
    return _flag_truncations(result, window_pool_truncated, candidates_truncated)


def _merge_bm25_union_candidates(
    hits: list,
    drawers_col,
    query: str,
    wing: str,
    room: str,
    n_results: int,
    max_distance: float = 0.0,
    source_file: str = None,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
    stats: dict = None,
    known_distances: dict = None,
) -> None:
    """Append top-K backend lexical candidates into ``hits`` in place.

    Used by ``search_memories(..., candidate_strategy="union")`` to widen
    the rerank pool's *source* (not just its size) — vector-only candidate
    selection skips docs whose embeddings are far from the query even when
    BM25 signal is strong.

    Dedup is chunk-precise: the key is ``(_source_file_full, _chunk_index)``
    so two files sharing a basename in different directories don't collide,
    and a vector hit on chunk N of a file doesn't block BM25 from
    contributing chunk M of the same file. Falls back to ``source_file``
    only when full-path/chunk metadata is absent.

    BM25-only additions carry ``distance=None`` unless a ``max_distance``
    cutoff is set. Under a cutoff, union mode loads stored embeddings for
    lexical hits and computes their vector distance; a hit past the cutoff is
    dropped and counted in ``stats["filtered_by_max_distance"]``. A hit whose
    distance cannot be computed is kept with ``distance=None`` and
    ``fallback_reason="distance_unavailable"`` rather than lost. A hit the
    vector path already measured reuses that distance from
    ``known_distances``; if the vector path dropped it, it stays dropped and
    is not counted twice.
    """
    stats = {} if stats is None else stats
    known_distances = known_distances or {}
    where = build_where_filter(wing, room, source_file, col=drawers_col)
    try:
        lexical = drawers_col.lexical_search(
            query=query,
            n_results=n_results * 3,
            where=where or None,
        )
    except UnsupportedCapabilityError:
        raise
    except Exception:
        logger.warning("candidate_strategy=union: lexical fetch failed", exc_info=True)
        stats["candidate_strategy_fallback"] = "lexical_search_failed"
        return

    metric = _metric_for_collection(drawers_col)
    unmeasured = [hit for hit in lexical.hits if hit.id not in known_distances]
    lexical_distances = (
        _lexical_hit_vector_distances(drawers_col, query, unmeasured, metric)
        if _has_distance_cutoff(max_distance) and unmeasured
        else {}
    )

    bm25_extra = []
    for hit in lexical.hits:
        meta = hit.metadata or {}
        if is_bookkeeping(meta, hit.id):
            continue
        # The window applies to every candidate source; a lexically strong
        # drawer outside [since, before) must not enter through this side
        # door (the vector-path candidates are filtered upstream).
        if (since_dt is not None or before_dt is not None) and not filed_at_in_window(
            meta.get("filed_at"), since_dt, before_dt
        ):
            continue
        full_source = meta.get("source_file", "") or ""
        if hit.id in known_distances:
            distance = known_distances[hit.id]
            if _beyond_max_distance(distance, max_distance, metric):
                continue  # the vector path already dropped and counted it
        else:
            distance = lexical_distances.get(hit.id)
            if distance is not None and _beyond_max_distance(distance, max_distance, metric):
                stats["filtered_by_max_distance"] = stats.get("filtered_by_max_distance", 0) + 1
                continue
        if distance is not None:
            distance = round(distance, 4)
        entry = {
            "drawer_id": _result_drawer_id(meta, hit.id),
            "text": hit.document or "",
            "wing": meta.get("wing", "unknown"),
            "room": meta.get("room", "unknown"),
            "source_file": Path(full_source).name if full_source else "?",
            "source_path": full_source,
            **_result_date_fields(meta),
            "similarity": (
                None if distance is None else round(_distance_to_similarity(distance, metric), 3)
            ),
            "distance": distance,
            "effective_distance": (
                None if distance is None else round(_cosine_scale_distance(distance, metric), 4)
            ),
            "closet_boost": 0.0,
            "matched_via": "bm25_backend",
            "bm25_score": round(float(hit.score), 3),
            "_source_file_full": full_source,
            "_chunk_index": meta.get("chunk_index"),
        }
        if distance is None and _has_distance_cutoff(max_distance):
            # The cutoff could not be checked; keep the hit and say why.
            entry["fallback_reason"] = "distance_unavailable"
        bm25_extra.append(entry)

    def _dedup_key(entry: dict):
        full = entry.get("_source_file_full")
        ci = entry.get("_chunk_index")
        if full and ci is not None:
            return (full, ci)
        # Fall back to basename only when richer metadata is missing —
        # avoids silently dropping candidates on legacy data while still
        # giving chunk-precise dedup whenever the metadata is present.
        return entry.get("source_file")

    seen = {_dedup_key(h) for h in hits}
    for bh in bm25_extra:
        key = _dedup_key(bh)
        if not key or key == "?" or key in seen:
            continue
        bh["closet_boost"] = 0.0
        hits.append(bh)
        seen.add(key)
