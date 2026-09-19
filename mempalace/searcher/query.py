# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


def _open_search_collection(palace_path: str, collection_name: str):
    try:
        return get_collection(
            palace_path, collection_name=collection_name, create=False, read_only=True
        ), None
    except BackendMismatchError as e:
        return None, _backend_mismatch_result(e)
    except KeyError as e:
        return None, _unknown_backend_result(e)
    except (CollectionNotInitializedError, PalaceNotFoundError) as e:
        logger.error("No palace found at %s: %s", palace_path, e)
        return None, _search_error_result(
            "No palace found",
            hint="Run: mempalace init <dir> && mempalace mine <dir>",
        )
    except BackendError as e:
        logger.error("Backend error opening palace at %s: %s", palace_path, e)
        return None, _search_error_result(
            "Backend error",
            details=str(e),
            hint="Check the selected backend configuration and availability.",
        )
    except Exception as e:
        logger.error("No palace found at %s: %s", palace_path, e)
        return None, _search_error_result(
            "No palace found",
            hint="Run: mempalace init <dir> && mempalace mine <dir>",
        )


def _query_drawers_with_filter_fallback(
    drawers_col, dkwargs, query, n_results, wing, room, source_file=None
):
    """Run the filtered drawer query, falling back to an unfiltered query plus a
    Python-side post-filter when ChromaDB raises on the filtered query.

    A ChromaDB HNSW/SQLite index mismatch makes filtered queries fail with
    "Error finding id" even when unfiltered search works fine — it happens when
    drawers are ingested via two different paths (e.g. bulk import vs MCP tool
    calls), leaving the vector index inconsistent with the metadata store. We
    retry unfiltered (over-fetching) and re-apply the wing/room/source_file filter in Python.
    See #1245 / #1035.
    """
    where = dkwargs.get("where")
    try:
        return drawers_col.query(**dkwargs)
    except Exception as filter_err:
        if not where:
            raise
        logger.warning(
            "Filtered search failed (%s); falling back to unfiltered + post-filter",
            filter_err,
        )
        raw = drawers_col.query(
            query_texts=[query],
            n_results=min(n_results * 15, 500),
            include=["documents", "metadatas", "distances"],
        )
        raw_docs = _first_or_empty(raw, "documents")
        raw_ids = _aligned_query_ids(raw, len(raw_docs))
        fids, fdocs, fmetas, fdists = [], [], [], []
        for stored_drawer_id, doc, meta, dist in zip(
            raw_ids,
            raw_docs,
            _first_or_empty(raw, "metadatas"),
            _first_or_empty(raw, "distances"),
        ):
            meta = meta or {}
            if wing and meta.get("wing") != wing:
                continue
            if room and meta.get("room") != room:
                continue
            if source_file and meta.get("source_file") != source_file:
                continue
            fids.append(stored_drawer_id)
            fdocs.append(doc)
            fmetas.append(meta)
            fdists.append(dist)
        return {
            "ids": [fids],
            "documents": [fdocs],
            "metadatas": [fmetas],
            "distances": [fdists],
        }


def _backend_capabilities(col) -> frozenset:
    backend = getattr(col, "_backend", None)
    if backend is None:
        inner = getattr(col, "_inner", None)
        backend = getattr(inner, "_backend", None) if inner is not None else None
    caps = getattr(backend, "capabilities", None) if backend is not None else None
    return caps if isinstance(caps, (set, frozenset)) else frozenset()


def _closet_boosts(closets_col, *, query: str, n_results: int, where: dict) -> dict:
    """Best-per-source closet hits used as a rank boost, never a gate.

    sqlite_exact (and any lexical backend) uses FTS instead of a second
    exact-cosine scan over the closet collection — closets are pointer
    lines, so BM25 is the better signal anyway.
    """
    boosts: dict = {}
    n_hits = max(1, n_results * 2)
    if "supports_lexical_search" in _backend_capabilities(closets_col):
        result = closets_col.lexical_search(query=query, n_results=n_hits, where=where or None)
        hits = getattr(result, "hits", None) or []
        for rank, hit in enumerate(hits):
            meta = hit.metadata or {}
            source = meta.get("source_file", "")
            if source and source not in boosts:
                preview = (hit.document or "")[:200]
                boosts[source] = (rank, 0.0, preview)
        return boosts

    ckwargs = {
        "query_texts": [query],
        "n_results": n_hits,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        ckwargs["where"] = where
    closet_results = closets_col.query(**ckwargs)
    for rank, (cdoc, cmeta, cdist) in enumerate(
        zip(
            _first_or_empty(closet_results, "documents"),
            _first_or_empty(closet_results, "metadatas"),
            _first_or_empty(closet_results, "distances"),
        )
    ):
        cmeta = cmeta or {}
        source = cmeta.get("source_file", "")
        if source and source not in boosts:
            boosts[source] = (rank, cdist, (cdoc or "")[:200])
    return boosts


# Cross-wing expansion: a baseline whose best hit sits above this
# effective_distance is a weak answer — expansion tries to do better.
_THIN_TOP_EFFECTIVE_DISTANCE = 1.0


def _baseline_is_thin(hits: list, n_results: int) -> bool:
    """True when the baseline under-served the query: fewer hits than asked
    for, or a top hit whose effective distance marks it as a weak match."""
    if len(hits) < n_results:
        return True
    top = hits[0]
    score = top.get("effective_distance")
    if score is None:
        score = top.get("distance", 9.9)
    return score > _THIN_TOP_EFFECTIVE_DISTANCE


def _bm25_baseline_is_thin(hits: list, n_results: int) -> bool:
    """BM25 fallback thin check — count-only; bm25 scores are not
    threshold-reliable enough to judge top-hit quality."""
    return len(hits) < n_results


def _expand_result_dict(
    result: dict,
    *,
    wings_to_try: list,
    fetch_wing,
    n_results: int,
    rank_key,
) -> dict:
    """Merge wing-scoped results into a finished result dict.

    Additive only: baseline hits are never removed. Dedupes on
    ``drawer_id``, re-ranks the merged pool, cuts to ``n_results``, and
    annotates ``wing_expansion`` with exactly what was applied.
    """
    hits = result.get("results") or []
    seen = {h.get("drawer_id") for h in hits if isinstance(h, dict)}
    seen_text = {h.get("text") for h in hits if isinstance(h, dict)}
    added = []
    for w in wings_to_try:
        try:
            extra = fetch_wing(w)
        except Exception:
            logger.debug("wing expansion query failed for wing %r", w, exc_info=True)
            continue
        for h in (extra or {}).get("results") or []:
            if not isinstance(h, dict):
                continue
            did = h.get("drawer_id")
            if (did is not None and did in seen) or h.get("text") in seen_text:
                continue
            if did is not None:
                seen.add(did)
            seen_text.add(h.get("text"))
            added.append(h)
    merged = hits + added
    merged.sort(key=rank_key)
    result["results"] = merged[:n_results]
    result["wing_expansion"] = {
        "applied": bool(added),
        "wings": wings_to_try,
        "added": len(added),
    }
    return result


def _maybe_expand_across_wings(
    result: dict,
    *,
    query: str,
    palace_path: str,
    collection_name,
    drawers_col,
    expand_wings: bool,
    wing,
    room,
    source_file,
    n_results: int,
    fetch_wing,
    rank_key,
    thin_check,
) -> dict:
    """Thin-gated additive wing expansion on a finished result dict.

    Expansion fires only when the caller enabled it, no explicit
    wing/room/source_file filter was given, and the baseline is thin.
    Anything else returns ``result`` untouched.
    """
    if not expand_wings or wing or room or source_file or not isinstance(result, dict):
        return result
    hits = result.get("results")
    if not isinstance(hits, list) or not thin_check(hits):
        return result

    from mempalace.wing_affinity import expand_wings as _score_expand

    # Bind affinity scoring to the actual search target: the opened
    # collection for graph signals, a config carrying this palace and
    # collection for palace-level files.
    bound_cfg = MempalaceConfig(palace_path=palace_path, collection_name=collection_name)
    wings = _score_expand(query, col=drawers_col, config=bound_cfg)
    if not wings:
        return result
    return _expand_result_dict(
        result,
        wings_to_try=wings,
        fetch_wing=fetch_wing,
        n_results=n_results,
        rank_key=rank_key,
    )


def search_memories(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    source_file: str = None,
    since: str = None,
    before: str = None,
    n_results: int = 5,
    max_distance: float = 0.0,
    vector_disabled: bool = False,
    candidate_strategy: str = "vector",
    collection_name: str = None,
    lang: Optional[str] = None,
    expand_wings: bool = True,
) -> dict:
    """Programmatic search — returns a dict instead of printing.

    Used by the MCP server and other callers that need data.

    Each hit exposes ``filed_at`` (also retained as ``created_at``), plus
    the legacy ``authored_at`` value and ``authored_at_source`` indicating
    whether it came from stored ``authored_at``, the ``filed_at`` fallback,
    or is ``unknown``. ``content_date`` is a separate inferred date with
    ``content_date_source`` (filename/frontmatter/body/mtime when recorded,
    otherwise ``unknown``). Neither inference nor filing proves authorship.

    Args:
        query: Natural language search query.
        palace_path: Path to the ChromaDB palace directory.
        wing: Optional wing filter.
        room: Optional room filter.
        source_file: Optional exact source_file filter. Matches the full
            stored source_file value verbatim (#1815).
        since: Optional inclusive ISO date/datetime lower bound on a
            drawer's ``filed_at`` (ingest time, the ``created_at`` shown in
            results) — ``[since, before)`` window semantics shared with
            ``list_drawers`` (#1128): wall-clock naive comparison, drawers
            with missing/unparseable ``filed_at`` excluded while a bound is
            active. Filtering happens after retrieval (ChromaDB rejects
            string operands for ``$gte``/``$lt``), so the candidate pool is
            widened via ``_candidate_pool_size`` — see
            ``date_filter_pool_truncated`` in the response.
        before: Optional exclusive ISO upper bound; see ``since``.
        n_results: Max results to return.
        max_distance: Max cosine distance threshold. The palace collection uses
            cosine distance (hnsw:space=cosine) — 0 = identical, 2 = opposite.
            Results with distance > this value are filtered out. A value of
            0.0 disables filtering. Typical useful range: 0.3–1.0.
        vector_disabled: When True, route to the sqlite-only BM25 fallback
            (#1222). Set by the MCP server when the HNSW capacity probe
            detects a divergence that would segfault chromadb on segment
            load.
        candidate_strategy: How candidates for the hybrid re-rank are gathered.

            * ``"vector"`` (default) — preserves historical behavior: top
              ``n_results * 4`` rows from the vector index are the rerank pool.
              Cheap; works well when query and target docs agree in the
              embedding space.
            * ``"union"`` — also pull top ``n_results * 3`` lexical candidates
              through the backend's ``lexical_search`` capability and merge
              them into the rerank pool (deduped by source_file). Catches docs
              with strong BM25 signal that are vector-distant from the query.
              Perf depends on the selected backend; opt in until the cost is
              characterized.

              When ``max_distance > 0.0`` is also set, BM25-only candidates
              are admitted only if their stored embeddings can be loaded and
              their computed vector distance satisfies that threshold.
        expand_wings: When True (default) and no wing/room/source_file
            filter is given, a thin baseline triggers additive cross-wing
            expansion: the most structurally relevant wings (passive
            same-room connections, hallways, room names) get their own
            scoped queries and their hits merge, dedupe, and rerank into
            the baseline — which is never filtered or reduced.
        lang: Locale code for BM25 stop-word filtering (opt-in). When
            omitted, reads ``MempalaceConfig().lang_explicit`` — returns an
            empty set unless the user has set ``MEMPALACE_LANG`` /
            ``MEMPAL_LANG`` or ``config.json["lang"]``. Palaces without an
            explicit language skip filtering entirely, preserving pre-PR
            byte-identical scoring.
    """
    # Validate the strategy eagerly so invalid values fail the same way
    # regardless of whether the call routes through the vector path or
    # the BM25-only fallback below.
    _validate_candidate_strategy(candidate_strategy)

    # Resolve stop words once up-front so every BM25 site (the vector path's
    # `_hybrid_rank`, the `vector_disabled` fallback, and the union-merge
    # candidate gather) tokenizes against the same locale.
    stop_words = _resolve_stop_words(lang)

    since_dt, before_dt, date_window_active, short_circuit = _window_and_fallback_gate(
        since,
        before,
        vector_disabled,
        query=query,
        palace_path=palace_path,
        wing=wing,
        room=room,
        n_results=n_results,
        collection_name=collection_name,
        source_file=source_file,
        stop_words=stop_words,
    )
    if short_circuit is not None:
        # The vector-disabled/BM25 fallback returns here — expansion applies
        # on this path too so the fallback scope matches the vector path.
        return _maybe_expand_across_wings(
            short_circuit,
            query=query,
            palace_path=palace_path,
            collection_name=collection_name,
            drawers_col=None,
            expand_wings=expand_wings,
            wing=wing,
            room=room,
            source_file=source_file,
            n_results=n_results,
            fetch_wing=lambda w: _vector_disabled_search(
                query=query,
                palace_path=palace_path,
                wing=w,
                room=None,
                n_results=n_results,
                collection_name=collection_name,
                stop_words=stop_words,
                since_dt=since_dt,
                before_dt=before_dt,
            ),
            rank_key=lambda h: -(h.get("bm25_score") or 0.0),
            thin_check=lambda hits: _bm25_baseline_is_thin(hits, n_results),
        )

    drawers_col, open_error = _open_search_collection(palace_path, collection_name)
    if open_error:
        return open_error

    metric = _metric_for_collection(drawers_col)
    where = build_where_filter(wing, room, source_file)

    # Hybrid retrieval: always query drawers directly (the floor), then use
    # closet hits to boost rankings. Closets are a ranking SIGNAL, never a
    # GATE — direct drawer search is always the baseline.
    #
    # This avoids the "weak-closets regression" where narrative content
    # produces low-signal closets (regex extraction matches few topics)
    # and closet-first routing hides drawers that direct search would find.
    pool_size, pre_enrichment_limit = _candidate_pool_limits(
        candidate_strategy,
        n_results,
        date_window_active,
    )
    try:
        dkwargs = {
            "query_texts": [query],
            "n_results": pool_size,  # over-fetch for re-ranking
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            dkwargs["where"] = where
        drawer_results = _query_drawers_with_filter_fallback(
            drawers_col, dkwargs, query, n_results, wing, room, source_file
        )
    except Exception as e:
        return _search_error_result(f"Search error: {e}")

    # Gather closet hits (best-per-source) to build a boost lookup.
    closet_boost_by_source: dict = {}  # source_file -> (rank, closet_dist, preview)
    try:
        closets_col = get_closets_collection(palace_path, create=False, read_only=True)
        closet_boost_by_source = _closet_boosts(
            closets_col, query=query, n_results=n_results, where=where
        )
    except Exception:
        # No closets yet — hybrid degrades to pure drawer search.
        logger.debug("Closet collection unavailable; using drawer-only search", exc_info=True)

    # Rank-based boost. The ordinal signal ("which closet matched best") is
    # more reliable than absolute distance on narrative content, where
    # closet distances cluster in 1.2-1.5 range regardless of match quality.
    CLOSET_RANK_BOOSTS = [0.40, 0.25, 0.15, 0.08, 0.04]
    CLOSET_DISTANCE_CAP = 1.5  # cosine dist > 1.5 = too weak to use as signal

    scored: list = []
    drawer_docs = _first_or_empty(drawer_results, "documents")
    stored_drawer_ids = _aligned_query_ids(drawer_results, len(drawer_docs))
    for stored_drawer_id, doc, meta, dist in zip(
        stored_drawer_ids,
        drawer_docs,
        _first_or_empty(drawer_results, "metadatas"),
        _first_or_empty(drawer_results, "distances"),
    ):
        meta = meta or {}
        doc = doc or ""
        if _candidate_out_of_scope(dist, meta, max_distance, since_dt, before_dt):
            continue

        meta = meta or {}
        source = meta.get("source_file", "") or ""
        boost = 0.0
        matched_via = "drawer"
        closet_preview = None
        if source in closet_boost_by_source:
            c_rank, c_dist, c_preview = closet_boost_by_source[source]
            if c_dist <= CLOSET_DISTANCE_CAP and c_rank < len(CLOSET_RANK_BOOSTS):
                boost = CLOSET_RANK_BOOSTS[c_rank]
                matched_via = "drawer+closet"
                closet_preview = c_preview

        # Clamp to the valid cosine-distance range [0, 2]. When a strong
        # closet boost (up to 0.40) exceeds the raw distance, the subtraction
        # can go negative — which (a) yields ``similarity > 1.0`` downstream
        # and (b) makes the sort key land *below* ordinary positive distances,
        # inverting the ranking so the best hybrid matches sort last.
        effective_dist = max(0.0, min(2.0, dist - boost))
        entry = {
            "drawer_id": _result_drawer_id(meta, stored_drawer_id),
            "text": doc,
            "wing": meta.get("wing", "unknown"),
            "room": meta.get("room", "unknown"),
            # source_file is the basename (display); source_path is the full
            # stored value, the round-trippable key for the source_file filter.
            "source_file": Path(source).name if source else "?",
            "source_path": source,
            **_result_date_fields(meta),
            # Similarity is the raw vector score. Closet boost ranks via
            # effective_distance but must not inflate the advertised score.
            "similarity": round(_distance_to_similarity(dist, metric), 3),
            "distance": round(dist, 4),
            "effective_distance": round(effective_dist, 4),
            "closet_boost": round(boost, 3),
            "matched_via": matched_via,
            # Internal: retain the full source_file path + chunk_index so the
            # enrichment step below doesn't have to reverse-lookup via
            # basename-suffix matching (which silently collides when two
            # files share a basename across different directories).
            "_sort_key": effective_dist,
            "_source_file_full": source,
            "_chunk_index": meta.get("chunk_index"),
            "_parent_drawer_id": meta.get("parent_drawer_id"),
        }
        if closet_preview:
            entry["closet_preview"] = closet_preview
        scored.append(entry)

    scored.sort(key=lambda h: h["_sort_key"])
    hits = scored[:pre_enrichment_limit]

    # Drawer-grep enrichment: retain the wider pool until repeated
    # closet-rendered passages can be replaced by distinct candidates.
    _enrich_closet_hits(
        hits,
        drawers_col,
        query,
        stop_words=stop_words,
    )

    # Candidate strategy hook: optionally widen the rerank pool's *source*
    # before ranking. Default ("vector") is a no-op; "union" merges top-K
    # backend lexical candidates. See `_apply_candidate_strategy`.
    # ``max_distance`` is forwarded so union mode can refuse to inject
    # BM25-only (distance=None) candidates that would silently bypass the
    # caller's strict distance threshold.
    # The helper also runs the final BM25 hybrid re-rank and strips internal
    # dedup fields before returning.
    hits, strategy_error = _finalize_candidate_hits(
        candidate_strategy=candidate_strategy,
        hits=hits,
        drawers_col=drawers_col,
        query=query,
        wing=wing,
        room=room,
        n_results=n_results,
        max_distance=max_distance,
        source_file=source_file,
        stop_words=stop_words,
        since_dt=since_dt,
        before_dt=before_dt,
    )
    if strategy_error:
        return strategy_error

    result = _search_result_envelope(
        query=query,
        wing=wing,
        room=room,
        source_file=source_file,
        since=since,
        before=before,
        hits=hits,
        candidates_fetched=len(_first_or_empty(drawer_results, "documents")),
        pool_size=pool_size,
        date_window_active=date_window_active,
    )
    return _maybe_expand_across_wings(
        result,
        query=query,
        palace_path=palace_path,
        collection_name=collection_name,
        drawers_col=drawers_col,
        expand_wings=expand_wings,
        wing=wing,
        room=room,
        source_file=source_file,
        n_results=n_results,
        # Wing-scoped expansion re-enters search_memories with expansion
        # disabled, so each scoped query gets the full pipeline — closets,
        # union candidates, date window — applied identically.
        fetch_wing=lambda w: search_memories(
            query=query,
            palace_path=palace_path,
            wing=w,
            since=since,
            before=before,
            n_results=n_results,
            max_distance=max_distance,
            vector_disabled=vector_disabled,
            candidate_strategy=candidate_strategy,
            collection_name=collection_name,
            lang=lang,
            expand_wings=False,
        ),
        rank_key=lambda h: (
            h.get("effective_distance")
            if h.get("effective_distance") is not None
            else h.get("distance", 9.9)
        ),
        thin_check=lambda h: _baseline_is_thin(h, n_results),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Virtual line numbering — read-time grid for drawers (3.3.6).
#
# Drawers are stored verbatim on disk. The reader applies a line-number grid
# at read time so any drawer — numbered or not — can be sectioned by a closet
# pointer like ``→2026-01-18:L55-L72`` without rewriting the corpus. Pure
# functions, no I/O. Source drawer text is never mutated.
# See docs/virtual-line-numbering.md for the full design rationale.
# ─────────────────────────────────────────────────────────────────────────────


# A line is "already numbered" iff it starts with [<digits>].
_ALREADY_NUMBERED_RE = re.compile(r"^\[\d+\]")
