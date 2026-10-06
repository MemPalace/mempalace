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
    source_file: str = None,
    since_dt=None,
    before_dt=None,
    json_output: bool = False,
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
        source_file=source_file,
        n_results=n_results,
        stop_words=stop_words,
        since_dt=since_dt,
        before_dt=before_dt,
    )
    hits = result.get("results", [])

    if json_output:
        # Normalize the BM25-only response into the same shape the vector
        # path emits so callers like staging_watcher can still parse it.
        out = []
        for hit in hits:
            full_source = hit.get("source_path") or hit.get("source_file", "?")
            out.append(
                {
                    "drawer_id": hit.get("drawer_id") or hit.get("_id", "?"),
                    "source_file": full_source,
                    "source_file_name": Path(full_source).name,
                    "wing": hit.get("wing", "?"),
                    "room": hit.get("room", "?"),
                    "text": hit.get("text", ""),
                    "distance": None,
                    "similarity": None,
                    "bm25_score": hit.get("bm25_score", 0.0),
                }
            )
        print(json.dumps({"query": query, "results": out}, ensure_ascii=False, indent=2))
        return

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
    if source_file:
        print(f"  Source: {source_file}")
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
        _print_also_in(hit)
        print(f"      Match:  bm25={bm25}  (vector disabled)")
        print()
        for line in (hit.get("text", "") or "").strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'-' * 56}")

    print()


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


def _print_also_in(hit: dict) -> None:
    """Name the other files holding this exact passage (folded copies)."""
    copies = hit.get("also_in") or []
    if not copies:
        return
    names = ", ".join(c.get("source_file") or "?" for c in copies[:5])
    more = f" and {len(copies) - 5} more" if len(copies) > 5 else ""
    print(f"      Also in: {names}{more}")


def _cli_ranked_hits(
    col, query, *, fetch, n_results, where, wing, room, window, metric, stop_words
):
    """``(hits, fetched)``: the top ``fetch`` drawers, re-ranked and folded.

    ``hits`` is not cut to ``n_results``; the caller widens ``fetch`` when
    folded copies leave it short.
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
        print(f"\n  Search error: {e}")
        raise SearchError(f"Search error: {e}") from e

    docs = _first_or_empty(results, "documents")
    metas = _first_or_empty(results, "metadatas")
    dists = _first_or_empty(results, "distances")
    ids = _first_or_empty(results, "ids")
    fetched = len(docs)
    if window is not None:
        # Keep the whole in-window pool: the hybrid re-rank must see every
        # survivor before the display cut, or a BM25-strong drawer deep in the
        # pool could never surface.
        kept = [
            (rid, doc, meta, dist)
            for rid, doc, meta, dist in zip(ids, docs, metas, dists)
            if filed_at_in_window((meta or {}).get("filed_at"), *window)
        ]
        ids = [k[0] for k in kept]
        docs = [k[1] for k in kept]
        metas = [k[2] for k in kept]
        dists = [k[3] for k in kept]

    # Pure-cosine retrieval on the CLI path was missing lexical matches: a
    # drawer whose text contains every query term can still score distance
    # >= 1.0 when it is a mechanical artifact (directory listing, diff, log
    # fragment). Hybridize with BM25 as `mempalace_search` does.
    hits = [
        {"id": rid, "text": doc or "", "distance": float(dist), "metadata": meta or {}}
        for rid, doc, meta, dist in zip(ids, docs, metas, dists)
    ]
    vector_weight, bm25_weight = _resolve_hybrid_rank_weights()
    hits = _hybrid_rank(
        hits,
        query,
        vector_weight=vector_weight,
        bm25_weight=bm25_weight,
        metric=metric,
        stop_words=stop_words,
    )
    return _fold_copies_across_sources(hits, _cli_hit_source, _cli_hit_ref), fetched


def search(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    source_file: str = None,
    n_results: int = 5,
    since: str = None,
    before: str = None,
    collection=None,
    json_output: bool = False,
):
    """
    Search the palace. Returns verbatim drawer content.
    Optionally filter by wing (project), room (aspect), or source_file,
    and/or narrow to drawers whose ``filed_at`` falls in the
    ``[since, before)`` window — same semantics as
    ``search_memories``/``list_drawers`` (#1128/#463).
    With json_output=True, emits a machine-readable JSON array on stdout.
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
                source_file=source_file,
                since_dt=since_dt,
                before_dt=before_dt,
                json_output=json_output,
            )

        col = _open_collection_or_explain(palace_path, opener=get_collection, read_only=True)
        if col is None:
            if not os.path.isdir(palace_path):
                raise SearchError(f"No palace found at {palace_path}")
            raise SearchError(f"No palace database at {palace_path}")

    # Alert the user if this palace predates hnsw:space=cosine being set on
    # creation — their similarity scores will be junk until they run repair.
    _warn_if_legacy_metric(col)

    where = build_where_filter(wing, room, source_file)

    metric = _metric_for_collection(col)
    fetch = _candidate_pool_size(n_results, date_window_active)
    while True:
        hits, fetched = _cli_ranked_hits(
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
        )
        # Folded copies can leave the page short while the fetch came back
        # full, so more candidates exist: widen, as search_memories does.
        if (
            len(hits) >= n_results
            or fetched < fetch
            or fetch >= _MAX_FOLD_POOL
            or not any(hit.get("also_in") for hit in hits)
        ):
            break
        fetch = min(fetch * 4, _MAX_FOLD_POOL)
    hits = hits[:n_results]
    if not hits:
        if json_output:
            print(json.dumps({"query": query, "results": []}, ensure_ascii=False))
        else:
            print(f'\n  No results found for: "{query}"')
        return

    if json_output:
        out = []
        for hit in hits:
            meta = hit["metadata"]
            out.append(
                {
                    "drawer_id": hit.get("id") or meta.get("drawer_id", "?"),
                    "parent_drawer_id": meta.get("parent_drawer_id"),
                    "source_file": meta.get("source_file", "?"),
                    "source_file_name": Path(meta.get("source_file", "?")).name,
                    "wing": meta.get("wing", "?"),
                    "room": meta.get("room", "?"),
                    "text": hit["text"],
                    "distance": hit["distance"],
                    "similarity": round(_distance_to_similarity(hit["distance"], metric), 3),
                    "bm25_score": hit.get("bm25_score", 0.0),
                }
            )
        print(json.dumps({"query": query, "results": out}, ensure_ascii=False, indent=2))
        return

    print(f"\n{'=' * 60}")
    print(f'  Results for: "{query}"')
    if wing:
        print(f"  Wing: {wing}")
    if room:
        print(f"  Room: {room}")
    if source_file:
        print(f"  Source: {source_file}")
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
        drawer_id = hit.get("id") or meta.get("drawer_id", "?")
        parent_drawer_id = meta.get("parent_drawer_id")

        print(f"  [{i}] {wing_name} / {room_name}")
        print(f"      Drawer: {drawer_id}")
        if parent_drawer_id and parent_drawer_id != drawer_id:
            print(f"      Parent drawer: {parent_drawer_id}")
        print(f"      Source: {source}")
        _print_also_in(hit)
        print(f"      Match:  {metric}_sim={vec_sim}  bm25={bm25}")
        print()
        # Print the verbatim text, indented
        for line in hit["text"].strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'-' * 56}")

    print()
