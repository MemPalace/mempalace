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
    except (UnknownEmbeddingModelError, BackendError) as e:
        # A misspelled model, or a palace built with a different embedding
        # model: the message names the fix (revert the model, or re-embed).
        refused = model_error_result(e, palace_path=palace_path, log=logger)
        if refused is not None:
            return None, _search_error_result(refused.pop("error"), **refused)
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
    """
    where = dkwargs.get("where")
    try:
        return drawers_col.query(**dkwargs)
    except Exception as filter_err:
        if not where or _model_error_class(filter_err) is not None:
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
        wings = set(palace_wing_aliases(wing, drawers_col)) if wing else None
        for stored_drawer_id, doc, meta, dist in zip(
            raw_ids,
            raw_docs,
            _first_or_empty(raw, "metadatas"),
            _first_or_empty(raw, "distances"),
        ):
            meta = meta or {}
            from ..backends._filters import matches_where

            if not matches_where(meta, where):
                continue
            if wings and meta.get("wing") not in wings:
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


def _query_drawer_candidates(
    col,
    kwargs,
    query,
    n_results,
    wing,
    room,
    source_file,
    strategy,
    where,
    stop_words,
    window_active,
    lexical_result=None,
):
    """Independent bounded retrievals overlap; each call owns and joins its task."""

    def vector():
        return _query_drawers_with_filter_fallback(
            col, kwargs, query, n_results, wing, room, source_file
        )

    if strategy != "union" or lexical_result is not None:
        return vector(), lexical_result
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        lexical = executor.submit(
            _fetch_union_lexical_candidates, col, query, n_results, where, stop_words, window_active
        )
        # The executor also joins the lexical read if the vector query raises.
        vectors = vector()
        return vectors, lexical.result()


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
    lines, so BM25 is the better signal anyway. Vector closet distances are
    returned on the cosine scale whatever metric the closet collection uses.

    AAAK summaries an older ``compress`` left in the closets are dropped
    before ranking, and the window is widened once if they crowded it.
    """
    lexical = _resolve_candidate_strategy(None, closets_col, where) == "union"
    closet_metric = None if lexical else _metric_for_collection(closets_col)

    def fetch(n_hits):
        """``(document, metadata, distance)`` per hit, in rank order."""
        if lexical:
            result = closets_col.union_lexical_search(
                query=query, n_results=n_hits, where=where or None, closet=True
            )
            return [
                (hit.document, hit.metadata, 0.0) for hit in getattr(result, "hits", None) or []
            ]
        ckwargs = {
            "query_texts": [query],
            "n_results": n_hits,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            ckwargs["where"] = where
        closet_results = closets_col.query(**ckwargs)
        return [
            (cdoc, cmeta, _cosine_scale_distance(cdist, closet_metric))
            for cdoc, cmeta, cdist in zip(
                _first_or_empty(closet_results, "documents"),
                _first_or_empty(closet_results, "metadatas"),
                _first_or_empty(closet_results, "distances"),
            )
        ]

    n_hits = max(1, n_results * 2)
    rows = fetch(n_hits)
    closets = [row for row in rows if not is_aaak_row(row[1])]
    if len(closets) < len(rows) and len(rows) >= n_hits:
        rows = fetch(n_hits * 4)
        closets = [row for row in rows if not is_aaak_row(row[1])]

    boosts: dict = {}
    for rank, (doc, meta, distance) in enumerate(closets):
        source = (meta or {}).get("source_file", "")
        if source and source not in boosts:
            boosts[source] = (rank, distance, (doc or "")[:200])
    return boosts


# Folding copies can leave fewer distinct passages than asked for while the
# vector pool was full, so more candidates exist. The search then repeats with
# a pool four times larger, up to _MAX_POOL_SCALE times the normal pool and
# never past _MAX_FOLD_POOL candidates.
_MAX_POOL_SCALE = 16
_MAX_FOLD_POOL = 500


def _scaled_pool_limits(limits: tuple, scale: int) -> tuple:
    if scale <= 1:
        return limits
    pool_size, pre_enrichment_limit = limits
    return (
        min(pool_size * scale, max(_MAX_FOLD_POOL, pool_size)),
        min(pre_enrichment_limit * scale, max(_MAX_FOLD_POOL, pre_enrichment_limit)),
    )


def _pool_left_results_short(
    hits, n_results, candidates_fetched, pool_size, bookkeeping_dropped=0
) -> bool:
    """Whether folded copies or skipped bookkeeping rows left the page short
    while the pool came back full, so more candidates exist."""
    return (
        len(hits) < n_results
        and candidates_fetched >= pool_size
        and (bookkeeping_dropped > 0 or any(hit.get("also_in") for hit in hits))
    )


def _search_drawer_memories(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    source_file: str = None,
    since: str = None,
    before: str = None,
    n_results: int = 5,
    max_distance: Optional[float] = None,
    vector_disabled: bool = False,
    candidate_strategy: Optional[str] = None,
    collection_name: str = None,
    lang: Optional[str] = None,
    _pool_scale: int = 1,
    _lexical_result=None,
    include_media: bool = False,
    query_task: str = "search",
    wings: Optional[list[str]] = None,
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
            stored source_file value verbatim.
        since: Optional inclusive ISO date/datetime lower bound on a
            drawer's ``filed_at`` (ingest time, the ``created_at`` shown in
            results) — ``[since, before)`` window semantics shared with
            ``list_drawers``: compared as instants (see ``date_window``), drawers
            with missing/unparseable ``filed_at`` excluded while a bound is
            active. Filtering happens after retrieval (ChromaDB rejects
            string operands for ``$gte``/``$lt``), so the candidate pool is
            widened via ``_candidate_pool_size`` — see
            ``date_filter_pool_truncated`` in the response.
        before: Optional exclusive ISO upper bound; see ``since``.
        n_results: Max results to return.
        max_distance: Optional cutoff on the cosine scale — 0 = identical,
            2 = opposite — whatever metric the collection declares (``l2`` and
            ``ip`` distances are converted before comparing). Candidates past
            it are dropped and counted in ``filtered_by_max_distance``.
            ``None`` (the default), ``0`` or a negative value applies no cutoff.
        vector_disabled: When True, route to the sqlite-only BM25 fallback.
            Set by the MCP server when the HNSW capacity probe
            detects a divergence that would segfault chromadb on segment
            load.
        candidate_strategy: How candidates for the hybrid re-rank are gathered.
            Applies to drawer-only search. Experimental ``include_media`` /
            ``query_task="code"`` search always uses shared-space vector ranking.

            * ``None`` (default) — union when the backend declares bounded lexical
              search; vector otherwise.
            * ``"vector"`` — preserves historical behavior: top
              ``n_results * 4`` rows from the vector index are the rerank pool.
              Cheap; works well when query and target docs agree in the
              embedding space.
            * ``"union"`` — also pull top ``n_results * 3`` lexical candidates
              through the backend's bounded scope-local path and merge
              them into the rerank pool (deduped by stored row ID). Date windows
              widen the lexical pool. Queries use at most eight selected terms;
              long queries without literals or proper names keep vector candidates.
              Finds vector-distant drawers with strong BM25 signal.
              Unbounded backends or filter shapes use vector candidates, even
              for explicit union, with ``lexical_unbounded_for_filter``.

              When ``max_distance`` is also set, a BM25-only candidate whose
              computed vector distance is past it is dropped; one whose
              distance cannot be computed is kept with ``distance=None``.
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

    if include_media or query_task != "search":
        if vector_disabled:
            return _search_error_result(
                "Media/code vector search is disabled until the index is repaired"
            )
        from .media_search import search_with_media

        return search_with_media(
            query,
            palace_path,
            include_media=include_media,
            query_task=query_task,
            n_results=n_results,
            wing=wing,
            room=room,
            source_file=source_file,
            since=since,
            before=before,
            max_distance=max_distance,
            collection_name=collection_name,
        )

    # Resolve stop words once up-front so every BM25 site (the vector path's
    # `_hybrid_rank`, the `vector_disabled` fallback, and the union-merge
    # candidate gather) tokenizes against the same locale.
    rank_lang = _resolve_rank_lang(lang)
    stop_words = _resolve_stop_words(rank_lang)

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
        **({"wings": wings} if wings is not None else {}),
    )
    if short_circuit is not None:
        if rank_lang:
            short_circuit["ranking_lang"] = rank_lang
        return short_circuit

    drawers_col, open_error = _open_search_collection(palace_path, collection_name)
    if open_error:
        return open_error

    metric = _metric_for_collection(drawers_col)
    where = build_where_filter(wing, room, source_file, col=drawers_col, wings=wings)
    strategy_stats = {}
    candidate_strategy = _resolve_candidate_strategy(
        candidate_strategy, drawers_col, where, strategy_stats
    )

    # Hybrid retrieval: always query drawers directly (the floor), then use
    # closet hits to boost rankings. Closets are a ranking SIGNAL, never a
    # GATE — direct drawer search is always the baseline.
    #
    # This avoids the "weak-closets regression" where narrative content
    # produces low-signal closets (regex extraction matches few topics)
    # and closet-first routing hides drawers that direct search would find.
    pool_size, pre_enrichment_limit = _scaled_pool_limits(
        _candidate_pool_limits(candidate_strategy, n_results, date_window_active),
        _pool_scale,
    )
    try:
        dkwargs = {
            "query_texts": [query],
            "n_results": pool_size,  # over-fetch for re-ranking
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            dkwargs["where"] = where
        drawer_results, lexical_result = _query_drawer_candidates(
            drawers_col,
            dkwargs,
            query,
            n_results,
            wing,
            room,
            source_file,
            candidate_strategy,
            where,
            stop_words,
            date_window_active,
            lexical_result=_lexical_result,
        )
    except Exception as e:
        # The configured embedding function could not be built: the
        # read-only open succeeded, but embedding the query refused (C).
        refused = model_error_result(e, palace_path=palace_path, log=logger)
        if refused is not None:
            return _search_error_result(refused.pop("error"), **refused)
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
    stats: dict = dict(strategy_stats)
    # Every candidate the vector path measured, kept or dropped, so union
    # mode neither re-admits nor re-counts a drawer this loop already gated.
    measured: dict = {}
    bookkeeping_dropped = 0
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
        if stored_drawer_id:
            measured[stored_drawer_id] = dist
        if is_bookkeeping(meta, stored_drawer_id):
            bookkeeping_dropped += 1
            continue
        gate = _candidate_out_of_scope(dist, meta, max_distance, since_dt, before_dt, metric)
        if gate == "distance":
            stats["filtered_by_max_distance"] = stats.get("filtered_by_max_distance", 0) + 1
        if gate:
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

        # On the cosine scale, so l2 and ip palaces rank the same way.
        effective_dist = _boosted_cosine_distance(dist, boost, metric)
        entry = {
            "_stored_id": stored_drawer_id,
            "drawer_id": _result_drawer_id(meta, stored_drawer_id),
            "text": doc,
            "wing": meta.get("wing", "unknown"),
            "room": meta.get("room", "unknown"),
            # source_file is the basename (display); source_path is the full
            # stored value, the round-trippable key for the source_file filter.
            "source_file": Path(source).name if source else "?",
            "source_path": source,
            **_result_date_fields(meta),
            **_result_author_fields(meta),
            **_result_topic_fields(meta),
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
            "_index_metadata": {INDEX_SPANS: meta.get(INDEX_SPANS)},
        }
        if closet_preview:
            entry["closet_preview"] = closet_preview
        scored.append(entry)

    scored.sort(key=lambda h: h["_sort_key"])
    hits = scored[:pre_enrichment_limit]

    hits = _enrich_closet_hits(hits, drawers_col, where=where)

    # Candidate strategy hook: widen the rerank pool's *source*
    # before ranking. "vector" is a no-op; "union" merges top-K
    # backend lexical candidates. See `_apply_candidate_strategy`.
    # ``max_distance`` is forwarded so union mode checks its BM25-only
    # candidates against the same cutoff where their distance is computable.
    # The helper also runs the final BM25 hybrid re-rank and strips internal
    # dedup fields before returning.
    hits, _ = _finalize_candidate_hits(
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
        stats=stats,
        known_distances=measured,
        where=where,
        lexical_result=lexical_result,
    )
    candidates_fetched = len(_first_or_empty(drawer_results, "documents"))
    envelope = _search_result_envelope(
        query=query,
        wing=wing,
        room=room,
        source_file=source_file,
        since=since,
        before=before,
        hits=hits,
        candidates_fetched=candidates_fetched,
        pool_size=pool_size,
        date_window_active=date_window_active,
        stats=stats,
        distance_metric=metric,
    )
    if wings is not None:
        envelope["filters"]["wings"] = wings
    if rank_lang:
        envelope["ranking_lang"] = rank_lang
    if _pool_left_results_short(
        hits, n_results, candidates_fetched, pool_size, bookkeeping_dropped
    ):
        if _pool_scale < _MAX_POOL_SCALE and pool_size < _MAX_FOLD_POOL:
            return _search_drawer_memories(
                query,
                palace_path,
                wing=wing,
                room=room,
                source_file=source_file,
                since=since,
                before=before,
                n_results=n_results,
                max_distance=max_distance,
                vector_disabled=vector_disabled,
                candidate_strategy=candidate_strategy,
                collection_name=collection_name,
                lang=lang,
                _pool_scale=_pool_scale * 4,
                _lexical_result=lexical_result,
                wings=wings,
            )
        envelope["distinct_results_truncated"] = True
    return envelope


_DRAWER_SEARCH_SIGNATURE = inspect.signature(_search_drawer_memories)


def search_memories(
    query: str, palace_path: str, *args, include_facts: bool = True, **kwargs
) -> dict:
    """Search facts and drawers independently; drawer defaults come from one place.

    Drawer parameters pass through untouched to ``_search_drawer_memories``.
    ``include_facts=False`` skips the optional fact path entirely.
    """
    result = _search_drawer_memories(query, palace_path, *args, **kwargs)
    if result.get("error"):
        return result
    fact_result = {"facts": []}
    if include_facts:
        try:
            from .facts import palace_facts

            bound = _DRAWER_SEARCH_SIGNATURE.bind(query, palace_path, *args, **kwargs)
            bound.apply_defaults()
            controls = {"wing": bound.arguments["wing"], "limit": bound.arguments["n_results"]}
            if bound.arguments["wings"] is not None:
                controls["wings"] = bound.arguments["wings"]
            fact_result = palace_facts(query, palace_path, **controls)
        except Exception:
            logger.warning("Fact index unavailable; preserving drawer search", exc_info=True)
            fact_result = {"facts": [], "facts_error": "unavailable"}
    return {**fact_result, **result}


# Keep introspection accurate without copying drawer parameters or defaults.
search_memories.__signature__ = _DRAWER_SEARCH_SIGNATURE.replace(
    parameters=[
        *_DRAWER_SEARCH_SIGNATURE.parameters.values(),
        inspect.Parameter(
            "include_facts", inspect.Parameter.KEYWORD_ONLY, default=True, annotation=bool
        ),
    ]
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
