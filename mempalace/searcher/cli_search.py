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
    )
    hits = result.get("results", [])

    print(
        "\n  NOTICE: vector search disabled — HNSW index has diverged from SQLite.\n"
        "          Showing BM25-only results. Run `mempalace repair` to restore "
        "vector search.\n"
    )
    print(f"{'=' * 60}")
    print(f'  Results for: "{query}"')
    if wing:
        print(f"  Wing: {wing}")
    if room:
        print(f"  Room: {room}")
    print(f"{'=' * 60}\n")

    if not hits:
        print(f'  No results found for: "{query}"')
        return

    for i, hit in enumerate(hits, 1):
        bm25 = hit.get("bm25_score", 0.0)
        wing_name = hit.get("wing", "?")
        room_name = hit.get("room", "?")
        source = Path(hit.get("source_file", "?")).name

        print(f"  [{i}] {wing_name} / {room_name}")
        print(f"      Source: {source}")
        print(f"      Match:  bm25={bm25}  (vector disabled)")
        print()
        for line in (hit.get("text", "") or "").strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'-' * 56}")

    print()


def search(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    n_results: int = 5,
    since: str = None,
    before: str = None,
    collection=None,
):
    """
    Search the palace. Returns verbatim drawer content.
    Optionally filter by wing (project) or room (aspect), and/or narrow to
    drawers whose ``filed_at`` falls in the ``[since, before)`` window —
    same semantics as ``search_memories``/``list_drawers`` (#1128/#463).
    """
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
            )

        col = _open_collection_or_explain(palace_path, opener=get_collection, read_only=True)
        if col is None:
            if not os.path.isdir(palace_path):
                raise SearchError(f"No palace found at {palace_path}")
            raise SearchError(f"No palace database at {palace_path}")

    # Alert the user if this palace predates hnsw:space=cosine being set on
    # creation — their similarity scores will be junk until they run repair.
    _warn_if_legacy_metric(col)

    committed_tokens = _committed_generation_tokens(col)
    where = _visible_drawer_where(build_where_filter(wing, room), committed_tokens)

    try:
        kwargs = {
            "query_texts": [query],
            # The window is a post-filter (ChromaDB can't range-compare
            # string metadata), so widen the fetch the same way the
            # programmatic path does and trim back after filtering.
            "n_results": _candidate_pool_size(n_results, date_window_active)
            if date_window_active
            else n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = _query_drawers_with_filter_fallback(
            col,
            kwargs,
            query,
            n_results,
            wing,
            room,
            committed_tokens=committed_tokens,
        )

    except Exception as e:
        print(f"\n  Search error: {e}")
        raise SearchError(f"Search error: {e}") from e

    docs = _first_or_empty(results, "documents")
    metas = _first_or_empty(results, "metadatas")
    dists = _first_or_empty(results, "distances")
    stored_ids = _aligned_query_ids(results, len(docs))

    visible = [
        (stored_id, doc, meta, dist)
        for stored_id, doc, meta, dist in zip(stored_ids, docs, metas, dists)
        if not _is_staged_metadata(meta, committed_tokens)
    ]
    stored_ids = [item[0] for item in visible]
    docs = [item[1] for item in visible]
    metas = [item[2] for item in visible]
    dists = [item[3] for item in visible]

    if date_window_active:
        kept = [
            (stored_id, doc, meta, dist)
            for stored_id, doc, meta, dist in zip(stored_ids, docs, metas, dists)
            if filed_at_in_window((meta or {}).get("filed_at"), since_dt, before_dt)
        ]
        # Keep the whole in-window pool here; the hybrid re-rank below must
        # see every survivor before the display cut to n_results, or a
        # BM25-strong drawer deep in the pool could never surface.
        stored_ids = [k[0] for k in kept]
        docs = [k[1] for k in kept]
        metas = [k[2] for k in kept]
        dists = [k[3] for k in kept]

    if not docs:
        print(f'\n  No results found for: "{query}"')
        return

    # Pure-cosine retrieval on the CLI path was missing lexical matches:
    # a drawer whose text contains every query term can still score distance
    # >= 1.0 against the natural-language query when the drawer is a
    # mechanical artifact (directory listing, diff, log fragment) that
    # embeds as file-tree noise rather than as prose about its subject.
    # The MCP tool path already hybridizes BM25 with vector sim via
    # `_hybrid_rank`; do the same here so CLI results match what agents
    # see via `mempalace_search`.
    metric = _metric_for_collection(col)
    hits = [
        {
            "drawer_id": _result_drawer_id(meta, stored_id),
            "text": doc or "",
            "distance": float(dist),
            "metadata": meta or {},
            "created_at": (meta or {}).get("filed_at", ""),
            "_logical_generation_id": (meta or {}).get("logical_drawer_id"),
            "_physical_drawer_id": stored_id,
            "_active_generation": (meta or {}).get("mine_generation_token") in committed_tokens,
        }
        for stored_id, doc, meta, dist in zip(stored_ids, docs, metas, dists)
    ]
    hits = _collapse_logical_generation_hits(hits)
    vector_weight, bm25_weight = _resolve_hybrid_rank_weights()
    hits = _hybrid_rank(
        hits,
        query,
        vector_weight=vector_weight,
        bm25_weight=bm25_weight,
        metric=metric,
        stop_words=stop_words,
    )
    if date_window_active:
        # The widened fetch exists only to survive the window filter; the
        # display contract stays "top n_results", now cut AFTER the re-rank.
        hits = hits[:n_results]

    print(f"\n{'=' * 60}")
    print(f'  Results for: "{query}"')
    if wing:
        print(f"  Wing: {wing}")
    if room:
        print(f"  Room: {room}")
    if since:
        print(f"  Since: {since}")
    if before:
        print(f"  Before: {before}")
    print(f"{'=' * 60}\n")

    for i, hit in enumerate(hits, 1):
        vec_sim = round(_distance_to_similarity(hit["distance"], metric), 3)
        bm25 = hit.get("bm25_score", 0.0)
        meta = hit["metadata"]
        source = Path(meta.get("source_file", "?")).name
        wing_name = meta.get("wing", "?")
        room_name = meta.get("room", "?")

        print(f"  [{i}] {wing_name} / {room_name}")
        print(f"      Source: {source}")
        print(f"      Match:  {metric}_sim={vec_sim}  bm25={bm25}")
        print()
        # Print the verbatim text, indented
        for line in hit["text"].strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'-' * 56}")

    print()
