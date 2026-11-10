# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


def _print_search_results_bm25_only(
    query: str,
    palace_path: str,
    wing: str,
    room: str,
    n_results: int,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
    facts=(),
    wings=None,
) -> None:
    """CLI fallback printer for when HNSW divergence fences off vector search.

    Mirrors the vector-path output shape so users get lexical matches in
    the format they expect, plus a clear notice pointing at
    ``mempalace repair``. Replaces the silent SIGBUS users otherwise hit
    when the CLI called ``col.query()`` against a diverged segment.

    ``stop_words`` reaches the BM25 scorer here for the same reason
    :func:`_vector_disabled_search` forwards it on the MCP side: this path
    still ranks by BM25, so dropping the filter would rank a diverged
    palace by different rules than a healthy one.

    An active ``[since_dt, before_dt)`` window is forwarded to the BM25
    reader, which post-filters on it. A diverged index degrades the
    ranking; it must never widen the result set past the window the
    caller asked for.
    """
    result = _bm25_only_via_sqlite(
        query=query,
        palace_path=palace_path,
        wing=wing,
        room=room,
        n_results=n_results,
        stop_words=stop_words,
        since_dt=since_dt,
        before_dt=before_dt,
        **({"wings": wings} if wings is not None else {}),
    )
    hits = result.get("results", [])
    print(
        render_cli_search(query, hits, wing=wing, room=room, facts=facts, vector_disabled=True),
        end="",
    )


def _cli_hit_source(hit: dict):
    return (hit.get("metadata") or {}).get("source_file")


def _cli_hit_ref(hit: dict) -> dict:
    meta = hit.get("metadata") or {}
    source = meta.get("source_file")
    return {
        "source_file": Path(source).name if source else "?",
        "source_path": source,
        "wing": meta.get("wing"),
        "room": meta.get("room"),
        "chunk_index": meta.get("chunk_index"),
    }


def _cli_also_in(hit: dict) -> str:
    """Name the other files holding this exact passage (folded copies)."""
    copies = hit.get("also_in") or []
    if not copies:
        return ""
    names = ", ".join(c.get("source_file") or "?" for c in copies[:5])
    more = f" and {len(copies) - 5} more" if len(copies) > 5 else ""
    return f"      Also in: {names}{more}"


def _cli_ranked_hits(
    col,
    query,
    *,
    fetch,
    n_results,
    where,
    wing,
    room,
    window,
    metric,
    stop_words,
    candidate_strategy,
):
    """``(hits, fetched, skipped)``: the top ``fetch`` drawers, re-ranked and
    folded, and how many bookkeeping rows the fetch held.

    ``hits`` is not cut to ``n_results``; the caller widens ``fetch`` when
    folded copies or skipped rows leave it short.
    """
    try:
        kwargs = {
            "query_texts": [query],
            # The window is a post-filter (ChromaDB can't range-compare
            # string metadata), and copies folded below free their slots,
            # so fetch a pool and trim back after both.
            "n_results": fetch,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        results = _query_drawers_with_filter_fallback(col, kwargs, query, n_results, wing, room)
    except Exception as e:
        if _model_error_class(e) is not None:
            # The configured embedding function could not be built (C): the
            # message names the fix, so print it plainly.
            import sys

            print(f"mempalace: {e}", file=sys.stderr)
            raise SearchError(str(e)) from e
        print(f"\n  Search error: {e}")
        raise SearchError(f"Search error: {e}") from e

    docs = _first_or_empty(results, "documents")
    metas = _first_or_empty(results, "metadatas")
    dists = _first_or_empty(results, "distances")
    stored_ids = _aligned_query_ids(results, len(docs))
    known_distances = dict(zip(stored_ids, dists))
    fetched = len(docs)
    if window is not None:
        # Keep the whole in-window pool: the hybrid re-rank must see every
        # survivor before the display cut, or a BM25-strong drawer deep in the
        # pool could never surface.
        kept = [
            (stored_id, doc, meta, dist)
            for stored_id, doc, meta, dist in zip(stored_ids, docs, metas, dists)
            if filed_at_in_window((meta or {}).get("filed_at"), *window)
        ]
        stored_ids = [k[0] for k in kept]
        docs = [k[1] for k in kept]
        metas = [k[2] for k in kept]
        dists = [k[3] for k in kept]

    # Pure-cosine retrieval on the CLI path was missing lexical matches: a
    # drawer whose text contains every query term can still score distance
    # >= 1.0 when it is a mechanical artifact (directory listing, diff, log
    # fragment). Hybridize with BM25 as `mempalace_search` does.
    hits = [
        {
            "_stored_id": stored_id,
            "text": doc or "",
            "distance": float(dist),
            "metadata": meta or {},
            "source_file": Path((meta or {}).get("source_file") or "?").name,
            "_source_file_full": (meta or {}).get("source_file"),
            "_chunk_index": (meta or {}).get("chunk_index"),
        }
        for stored_id, doc, meta, dist in zip(stored_ids, docs, metas, dists)
        if not is_bookkeeping(meta)
    ]
    # Bookkeeping rows held slots in the fetch, so the caller widens for them too.
    skipped = len(docs) - len(hits)
    stats = {}
    _apply_candidate_strategy(
        candidate_strategy,
        hits,
        col,
        query,
        wing,
        room,
        n_results,
        since_dt=window[0] if window else None,
        before_dt=window[1] if window else None,
        known_distances=known_distances,
        stats=stats,
        where=where,
        stop_words=stop_words,
    )
    for hit in hits:
        if "metadata" not in hit:
            hit["metadata"] = {
                "wing": hit["wing"],
                "room": hit["room"],
                "source_file": hit["source_path"],
                "chunk_index": hit.get("_chunk_index"),
            }
    vector_weight, bm25_weight = _resolve_hybrid_rank_weights()
    hits = _hybrid_rank(
        hits,
        query,
        vector_weight=vector_weight,
        bm25_weight=bm25_weight,
        metric=metric,
        stop_words=stop_words,
    )
    return _fold_copies_across_sources(hits, _cli_hit_source, _cli_hit_ref), fetched, skipped


def _cli_fact_matches(query, palace_path, wing, n_results, wings=None):
    """Optional facts can report a generic failure but never stop CLI drawers."""
    try:
        from .facts import palace_facts

        result = palace_facts(
            query,
            palace_path,
            wing=wing,
            limit=n_results,
            **({"wings": wings} if wings is not None else {}),
        )
        if result.get("facts_error"):
            print("  facts_error: unavailable")
        return result["facts"]
    except Exception:
        logger.warning("Fact search unavailable; preserving CLI drawers", exc_info=True)
        print("  facts_error: unavailable")
        return []


def _cli_match(hit, metric, vector_disabled):
    bm25 = hit.get("bm25_score", 0.0)
    if vector_disabled:
        return f"      Match:  bm25={bm25}  (vector disabled)"
    if hit.get("distance") is None:
        return f"      Match:  bm25={bm25}  (lexical candidate)"
    # Structured hits already advertise similarity rounded from the original
    # distance; recomputing from their rounded distance can change the display.
    vec_sim = hit.get("similarity")
    if vec_sim is None:
        vec_sim = round(_distance_to_similarity(hit["distance"], metric), 3)
    return f"      Match:  {metric}_sim={vec_sim}  bm25={bm25}"


def render_cli_search(
    query,
    hits,
    *,
    wing=None,
    room=None,
    since=None,
    before=None,
    metric="cosine",
    facts=(),
    vector_disabled=False,
    flat=False,
):
    """Pure canonical CLI formatting over header inputs and authorized hits.

    Local CLI hits carry metadata; the gateway explicitly selects flat rows,
    whose metadata may contain unrelated import authorization information.
    Authorization must finish before calling this formatter.
    """
    from io import StringIO

    from ..fact_render import format_facts

    output = StringIO()

    def emit(*args):
        print(*args, file=output)

    output.write(format_facts(facts))
    if vector_disabled:
        emit(
            "\n  NOTICE: vector search disabled — HNSW index has diverged from SQLite.\n"
            "          Showing BM25-only results. Run `mempalace repair` to restore "
            "vector search.\n"
        )
    elif not hits:
        emit(f'\n  No results found for: "{query}"')
        return output.getvalue()

    emit(("" if vector_disabled else "\n") + "=" * 60)
    emit(f'  Results for: "{query}"')
    if wing:
        emit(f"  Wing: {wing}")
    if room:
        emit(f"  Room: {room}")
    if since and not vector_disabled:
        emit(f"  Since: {since}")
    if before and not vector_disabled:
        emit(f"  Before: {before}")
    emit("=" * 60 + "\n")
    if not hits:
        emit(f'  No results found for: "{query}"')
        return output.getvalue()

    for i, hit in enumerate(hits, 1):
        meta = hit if flat else hit.get("metadata", hit)
        source = meta.get("source_file", "?")
        if (flat or "metadata" not in hit) and not vector_disabled:
            source = hit.get("source_path", source)
        emit(f"  [{i}] {meta.get('wing', '?')} / {meta.get('room', '?')}")
        emit(f"      Source: {Path(source if source is not None else '?').name}")
        copies = _cli_also_in(hit)
        if copies:
            emit(copies)
        emit(_cli_match(hit, metric, vector_disabled))
        emit()
        for line in (hit.get("text") or "").strip().split("\n"):
            emit(f"      {line}")
        emit()
        emit(f"  {'-' * 56}")
    emit()
    return output.getvalue()


def search(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    n_results: int = 5,
    since: str = None,
    before: str = None,
    collection=None,
    include_media: bool = False,
    query_task: str = "search",
    candidate_strategy: Optional[str] = None,
    facts=None,
    wings=None,
):
    """
    Search the palace. Returns verbatim drawer content.
    Optionally filter by wing (project) or room (aspect), and/or narrow to
    drawers whose ``filed_at`` falls in the ``[since, before)`` window —
    same semantics as ``search_memories``/``list_drawers``.
    """
    _validate_candidate_strategy(candidate_strategy)
    if include_media or query_task != "search":
        result = search_memories(
            query,
            palace_path,
            wing=wing,
            room=room,
            n_results=n_results,
            since=since,
            before=before,
            include_media=include_media,
            query_task=query_task,
            candidate_strategy=candidate_strategy,
        )
        if result.get("error"):
            if result.get("error_class"):
                # A model error: the details carry the explanation and the fix.
                import sys

                print(f"mempalace: {result['details']}", file=sys.stderr)
            else:
                print(result["error"])
            raise SearchError(result["error"])
        from ..fact_render import format_facts

        print(format_facts(result.get("facts", [])), end="")
        for index, hit in enumerate(result["results"], 1):
            kind = hit.get("media_type") or "text/code"
            print(f"{index}. [{kind}] {hit['source_file']}  similarity={hit['similarity']:.3f}")
            print(f"   {hit['source_path']}")
            if hit.get("result_type") == "asset":
                if not hit.get("available"):
                    print("   File is missing; the indexed reference is retained.")
                if hit.get("drawer_id"):
                    print(f"   Related drawer: {hit['drawer_id']}")
            else:
                print(hit["text"])
        if not result["results"]:
            print("No results found.")
        return None
    # Resolved before the fence below: both exits from this function rank by
    # BM25, so the filter has to be in hand on either branch.
    stop_words = _resolve_stop_words(None)

    # Parse the window before probing the palace: an inverted or malformed
    # bound is a caller error and must raise identically whether or not the
    # index turns out to be diverged.
    try:
        since_dt, before_dt = parse_window(since, before)
    except ValueError as e:
        print(f"\n  {e}")
        raise SearchError(str(e)) from e
    date_window_active = since_dt is not None or before_dt is not None
    if facts is None:
        facts = _cli_fact_matches(
            query,
            palace_path,
            wing,
            n_results,
            **({"wings": wings} if wings is not None else {}),
        )

    # Probe a Chroma palace before get_collection(). Opening the client can
    # load native index state, and embedder-identity enforcement may call
    # collection.count(); both happen before the old query-only guard and can
    # hit the same native crash. Non-Chroma backends never use Chroma's HNSW
    # files or sqlite-specific fallback and proceed normally.
    col = collection
    if col is None:
        try:
            backend_name = resolve_backend_name(palace_path)
        except (BackendMismatchError, KeyError):
            # Preserve _open_collection_or_explain's state-specific diagnostics
            # for mixed artifacts and unknown backend selections. This probe is
            # only an early Chroma safety fence; it must not become a second,
            # less-helpful backend validation path.
            backend_name = None

        if backend_name == "chroma" and _hnsw_capacity_diverged(palace_path):
            return _print_search_results_bm25_only(
                query,
                palace_path,
                wing,
                room,
                n_results,
                stop_words=stop_words,
                since_dt=since_dt,
                before_dt=before_dt,
                facts=facts,
                **({"wings": wings} if wings is not None else {}),
            )

        col = _open_collection_or_explain(palace_path, opener=get_collection, read_only=True)
        if col is None:
            if not os.path.isdir(palace_path):
                raise SearchError(f"No palace found at {palace_path}")
            raise SearchError(f"No palace database at {palace_path}")

    # Alert the user if this palace predates hnsw:space=cosine being set on
    # creation — their similarity scores will be junk until they run repair.
    _warn_if_legacy_metric(col)

    where = build_where_filter(wing, room, col=col, wings=wings)
    candidate_strategy = _resolve_candidate_strategy(candidate_strategy, col, where)

    metric = _metric_for_collection(col)
    fetch = _candidate_pool_size(n_results, date_window_active)
    while True:
        hits, fetched, skipped = _cli_ranked_hits(
            col,
            query,
            fetch=fetch,
            n_results=n_results,
            where=where,
            wing=wing,
            room=room,
            window=(since_dt, before_dt) if date_window_active else None,
            metric=metric,
            stop_words=stop_words,
            candidate_strategy=candidate_strategy,
        )
        # Folded copies or skipped bookkeeping rows can leave the page short
        # while the fetch came back full, so more candidates exist: widen, as
        # search_memories does.
        if (
            len(hits) >= n_results
            or fetched < fetch
            or fetch >= _MAX_FOLD_POOL
            or not (skipped or any(hit.get("also_in") for hit in hits))
        ):
            break
        fetch = min(fetch * 4, _MAX_FOLD_POOL)
    hits = hits[:n_results]
    print(
        render_cli_search(
            query,
            hits,
            wing=wing,
            room=room,
            since=since,
            before=before,
            metric=metric,
            facts=facts,
        ),
        end="",
    )
