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
    authoritative Python re-filter (offset drop, unparseable exclusion —
    mirroring the wing/room double-check) can't recover it. Day
    granularity costs at most one extra day of candidates per bound;
    Python decides the exact window.
    """
    prefilters = []
    if since_dt is not None:
        prefilters.append((">=", since_dt.date().isoformat()))
    if before_dt is not None:
        try:
            upper = (before_dt + timedelta(days=1)).date().isoformat()
        except OverflowError:
            # before at the calendar ceiling ("9999-12-31" as an open-ended
            # sentinel): there is no next day to bound by, so skip the SQL
            # narrowing entirely — the Python re-filter stays authoritative
            # and such a window is effectively unbounded above anyway.
            upper = None
        if upper is not None:
            prefilters.append(("<", upper))
    return prefilters


def _sqlite_staged_value_sql(conn) -> str:
    """Return a staged-flag expression compatible with old Chroma schemas."""
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(embedding_metadata)")}
    except sqlite3.Error:
        columns = set()
    if "bool_value" in columns:
        return "COALESCE(staged.bool_value, staged.int_value, 0)"
    return "COALESCE(staged.int_value, 0)"


def _sqlite_active_generation_rows(
    db_path: str,
    collection_name: str,
    logical_ids: set[str],
) -> dict[str, dict]:
    """Hydrate marker-selected physical rows for logical IDs from sqlite."""
    if not logical_ids:
        return {}
    conn = sqlite3.connect(sqlite_read_uri(db_path), uri=True)
    try:
        placeholders = ",".join("?" for _ in logical_ids)
        active = conn.execute(
            f"""
            SELECT e.id, e.embedding_id, logical.string_value
            FROM embedding_metadata logical
            JOIN embeddings e ON e.id = logical.id
            JOIN segments s ON e.segment_id = s.id
            JOIN collections c ON s.collection = c.id
            JOIN embedding_metadata token
              ON token.id = e.id AND token.key = 'mine_generation_token'
            JOIN embedding_metadata marker
              ON marker.key = 'mine_generation_commit'
             AND marker.string_value = token.string_value
            JOIN embeddings marker_embedding ON marker_embedding.id = marker.id
            JOIN segments marker_segment ON marker_segment.id = marker_embedding.segment_id
            JOIN collections marker_collection ON marker_collection.id = marker_segment.collection
            WHERE c.name = ?
              AND marker_collection.name = ?
              AND logical.key = 'logical_drawer_id'
              AND logical.string_value IN ({placeholders})
            """,
            (collection_name, collection_name, *sorted(logical_ids)),
        ).fetchall()
        if not active:
            return {}
        internal_ids = [row[0] for row in active]
        by_internal = {
            row[0]: {
                "drawer_id": row[1],
                "logical_id": row[2],
                "metadata": {},
                "document": "",
            }
            for row in active
        }
        id_placeholders = ",".join("?" for _ in internal_ids)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(embedding_metadata)")}
        value_columns = [
            column
            for column in ("string_value", "int_value", "float_value", "bool_value")
            if column in columns
        ]
        metadata_rows = conn.execute(
            f"""
            SELECT id, key, {", ".join(value_columns)}
            FROM embedding_metadata
            WHERE id IN ({id_placeholders})
            """,
            internal_ids,
        ).fetchall()
        for row in metadata_rows:
            target = by_internal.get(row[0])
            if target is None or not row[1]:
                continue
            value = next((value for value in row[2:] if value is not None), None)
            if row[1] == "chroma:document":
                target["document"] = value or ""
            elif value is not None:
                target["metadata"][row[1]] = value
        return {item["logical_id"]: item for item in by_internal.values()}
    finally:
        conn.close()


def _resolve_sqlite_generation_candidates(
    candidates,
    query,
    db_path,
    collection_name,
    committed_tokens=frozenset(),
    tokened_source_modes=frozenset(),
):
    logical_ids = {
        candidate.get("_logical_generation_id")
        for candidate in candidates
        if candidate.get("_logical_generation_id")
    }
    try:
        active = _sqlite_active_generation_rows(db_path, collection_name, logical_ids)
    except sqlite3.Error:
        logger.warning("Could not resolve sqlite active generations", exc_info=True)
        active = {}
    resolved = []
    emitted = set()
    for candidate in candidates:
        logical_id = candidate.get("_logical_generation_id")
        if not logical_id:
            resolved.append(candidate)
            continue
        if logical_id in emitted:
            continue
        emitted.add(logical_id)
        current = active.get(logical_id)
        if current is None:
            if not candidate.get("_generation_token") and _is_visible_generation_metadata(
                {
                    "source_file": candidate.get("_source_file_full"),
                    "extract_mode": candidate.get("_extract_mode"),
                    "ingest_mode": candidate.get("_ingest_mode"),
                },
                committed_tokens,
                tokened_source_modes,
            ):
                resolved.append(candidate)
            continue
        if _bm25_scores(query, [current["document"]])[0] <= 0:
            continue
        metadata = current["metadata"]
        full_source = metadata.get("source_file", "") or ""
        replacement = dict(candidate)
        replacement.update(
            {
                "drawer_id": logical_id,
                "text": current["document"],
                "wing": metadata.get("wing", "unknown"),
                "room": metadata.get("room", "unknown"),
                "source_file": Path(full_source).name if full_source else "?",
                "source_path": full_source,
                "created_at": metadata.get("filed_at", "unknown"),
                "authored_at": metadata.get("authored_at", metadata.get("filed_at", "unknown")),
                "_source_file_full": full_source,
                "_chunk_index": metadata.get("chunk_index"),
                "_physical_drawer_id": current["drawer_id"],
                "_active_generation": True,
            }
        )
        resolved.append(replacement)
    return resolved


def _sqlite_generation_commit_state(conn, collection_name: str) -> tuple[set, set]:
    """Published generation tokens and source/mode pairs from an open sqlite conn."""
    committed_tokens: set = set()
    tokened_source_modes: set = set()
    for token, src, mode, ingest in conn.execute(
        """
            SELECT marker.string_value, src.string_value,
                   mode.string_value, ingest.string_value
            FROM embedding_metadata marker
            JOIN embeddings e ON e.id = marker.id
            JOIN segments s ON e.segment_id = s.id
            JOIN collections c ON s.collection = c.id
            LEFT JOIN embedding_metadata src
              ON src.id = e.id AND src.key = 'source_file'
            LEFT JOIN embedding_metadata mode
              ON mode.id = e.id AND mode.key = 'extract_mode'
            LEFT JOIN embedding_metadata ingest
              ON ingest.id = e.id AND ingest.key = 'ingest_mode'
            WHERE c.name = ?
              AND marker.key = 'mine_generation_commit'
              AND marker.string_value IS NOT NULL
            """,
        (collection_name,),
    ):
        if not token:
            continue
        committed_tokens.add(token)
        source_mode = _source_mode_commit_key(
            {
                "source_file": src,
                "extract_mode": mode,
                "ingest_mode": ingest,
            }
        )
        if source_mode is not None:
            tokened_source_modes.add(source_mode)
    return committed_tokens, tokened_source_modes


def _sqlite_bm25_candidates_from_drawers(
    drawers,
    *,
    wing,
    room,
    source_file,
    window_active,
    since_dt,
    before_dt,
    committed_tokens,
    tokened_source_modes,
):
    """Apply wing/room/generation filters to sqlite drawer dicts."""
    candidates = []
    for d in drawers.values():
        meta = d["metadata"]
        if wing and meta.get("wing") != wing:
            continue
        if room and meta.get("room") != room:
            continue
        if source_file and meta.get("source_file") != source_file:
            continue
        if not _is_visible_generation_metadata(meta, committed_tokens, tokened_source_modes):
            continue
        if window_active and not filed_at_in_window(meta.get("filed_at"), since_dt, before_dt):
            continue
        full_source = meta.get("source_file", "") or ""
        candidates.append(
            {
                "drawer_id": _result_drawer_id(meta, d["_stored_drawer_id"]),
                "text": d["text"],
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "source_file": Path(full_source).name if full_source else "?",
                "source_path": full_source,
                **_result_date_fields(meta),
                "similarity": None,
                "distance": None,
                "matched_via": "bm25_sqlite",
                "_source_file_full": full_source,
                "_chunk_index": meta.get("chunk_index"),
                "_logical_generation_id": meta.get("logical_drawer_id"),
                "_physical_drawer_id": d["_stored_drawer_id"],
                "_active_generation": meta.get("mine_generation_token") in committed_tokens,
                "_generation_token": meta.get("mine_generation_token"),
                "_extract_mode": meta.get("extract_mode"),
                "_ingest_mode": meta.get("ingest_mode"),
            }
        )
    return candidates


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

    Used when HNSW is diverged or unloadable (#1222). Bypasses chromadb's
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

    staged_value_sql = "COALESCE(staged.int_value, 0)"

    def _metadata_filter_sql(row_id_expr: str) -> tuple[str, list[str]]:
        clauses = [
            f"""
            AND (
                NOT EXISTS (
                    SELECT 1
                    FROM embedding_metadata staged
                    WHERE staged.id = {row_id_expr}
                      AND staged.key = 'mine_staged'
                      AND {staged_value_sql} = 1
                )
                OR EXISTS (
                    SELECT 1
                    FROM embedding_metadata token
                    JOIN embedding_metadata marker
                      ON marker.key = 'mine_generation_commit'
                     AND marker.string_value = token.string_value
                    JOIN embeddings marker_embedding
                      ON marker_embedding.id = marker.id
                    JOIN segments marker_segment
                      ON marker_segment.id = marker_embedding.segment_id
                    JOIN collections marker_collection
                      ON marker_collection.id = marker_segment.collection
                    WHERE token.id = {row_id_expr}
                      AND token.key = 'mine_generation_token'
                      AND marker_collection.name = ?
                )
            )
            """,
            f"""
            AND (
                NOT EXISTS (
                    SELECT 1 FROM embedding_metadata tokened
                    WHERE tokened.id = {row_id_expr}
                      AND tokened.key = 'mine_generation_token'
                )
                OR EXISTS (
                    SELECT 1
                    FROM embedding_metadata tokened
                    JOIN embedding_metadata marker
                      ON marker.key = 'mine_generation_commit'
                     AND marker.string_value = tokened.string_value
                    JOIN embeddings marker_embedding
                      ON marker_embedding.id = marker.id
                    JOIN segments marker_segment
                      ON marker_segment.id = marker_embedding.segment_id
                    JOIN collections marker_collection
                      ON marker_collection.id = marker_segment.collection
                    WHERE tokened.id = {row_id_expr}
                      AND tokened.key = 'mine_generation_token'
                      AND marker_collection.name = ?
                )
            )
            """,
            f"""
            AND (
                EXISTS (
                    SELECT 1 FROM embedding_metadata tokened
                    WHERE tokened.id = {row_id_expr}
                      AND tokened.key = 'mine_generation_token'
                )
                OR NOT EXISTS (
                    SELECT 1
                    FROM embedding_metadata marker
                    JOIN embeddings marker_embedding
                      ON marker_embedding.id = marker.id
                    JOIN segments marker_segment
                      ON marker_segment.id = marker_embedding.segment_id
                    JOIN collections marker_collection
                      ON marker_collection.id = marker_segment.collection
                    JOIN embedding_metadata marker_src
                      ON marker_src.id = marker.id
                     AND marker_src.key = 'source_file'
                    JOIN embedding_metadata row_src
                      ON row_src.id = {row_id_expr}
                     AND row_src.key = 'source_file'
                     AND row_src.string_value = marker_src.string_value
                    LEFT JOIN embedding_metadata marker_mode
                      ON marker_mode.id = marker.id
                     AND marker_mode.key = 'extract_mode'
                    LEFT JOIN embedding_metadata marker_ingest
                      ON marker_ingest.id = marker.id
                     AND marker_ingest.key = 'ingest_mode'
                    LEFT JOIN embedding_metadata row_mode
                      ON row_mode.id = {row_id_expr}
                     AND row_mode.key = 'extract_mode'
                    LEFT JOIN embedding_metadata row_ingest
                      ON row_ingest.id = {row_id_expr}
                     AND row_ingest.key = 'ingest_mode'
                    WHERE marker.key = 'mine_generation_commit'
                      AND marker.string_value IS NOT NULL
                      AND marker.string_value != ''
                      AND marker_collection.name = ?
                      AND (
                        CASE
                          WHEN marker_mode.string_value IS NOT NULL
                            THEN marker_mode.string_value
                          WHEN marker_ingest.string_value IS NULL
                            OR marker_ingest.string_value = 'convos'
                            THEN 'exchange'
                        END
                      ) IS (
                        CASE
                          WHEN row_mode.string_value IS NOT NULL
                            THEN row_mode.string_value
                          WHEN row_ingest.string_value IS NULL
                            OR row_ingest.string_value = 'convos'
                            THEN 'exchange'
                        END
                      )
                )
            )
            """,
        ]
        params = [collection_name, collection_name, collection_name]
        for key, value in (("wing", wing), ("room", room), ("source_file", source_file)):
            if not value:
                continue
            clauses.append(
                f"""
                AND EXISTS (
                    SELECT 1
                    FROM embedding_metadata mf
                    WHERE mf.id = {row_id_expr}
                      AND mf.key = ?
                      AND COALESCE(
                        mf.string_value,
                        CAST(mf.int_value AS TEXT),
                        CAST(mf.float_value AS TEXT),
                        CAST(mf.bool_value AS TEXT)
                      ) = ?
                )
                """
            )
            params.extend([key, value])
        for op, sql_bound in _window_sql_prefilters(since_dt, before_dt):
            clauses.append(
                f"""
                AND EXISTS (
                    SELECT 1
                    FROM embedding_metadata mf
                    WHERE mf.id = {row_id_expr}
                      AND mf.key = 'filed_at'
                      AND mf.string_value {op} ?
                )
                """
            )
            params.append(sql_bound)
        return "".join(clauses), params

    try:
        conn = connect_sqlite_read(db_path)
    except sqlite3.Error as e:
        return _search_error_result(f"sqlite open failed: {e}")

    staged_value_sql = _sqlite_staged_value_sql(conn)

    window_active = since_dt is not None or before_dt is not None
    committed_tokens = set()
    tokened_source_modes = set()
    try:
        # FTS5 MATCH expects whitespace-separated tokens. Drop tokens
        # shorter than 3 chars (trigram tokenizer can't match them).
        tokens = [t for t in _tokenize(query) if len(t) >= 3]
        candidate_ids: list[int] = []
        use_recency_fallback = not tokens
        if tokens:
            fts_query = " OR ".join(tokens)
            filter_sql, filter_params = _metadata_filter_sql("embedding_fulltext_search.rowid")
            try:
                rows = conn.execute(
                    f"""
                    SELECT embedding_fulltext_search.rowid
                    FROM embedding_fulltext_search
                    JOIN embeddings e ON e.id = embedding_fulltext_search.rowid
                    JOIN segments s ON e.segment_id = s.id
                    JOIN collections c ON s.collection = c.id
                    WHERE embedding_fulltext_search MATCH ?
                      AND c.name = ?
                    {filter_sql}
                    LIMIT ?
                    """,
                    (fts_query, collection_name, *filter_params, max_candidates),
                ).fetchall()
                candidate_ids = [r[0] for r in rows]
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
            # Wrapped in try/except because the schema may differ on legacy
            # palaces (older chromadb without ``created_at``, missing
            # ``segments`` rows after partial restore, etc.); on schema
            # mismatch we fall back to ordering by primary-key id and finally
            # to an empty result rather than letting search raise.
            try:
                filter_sql, filter_params = _metadata_filter_sql("e.id")
                rows = conn.execute(
                    f"""
                    SELECT e.id
                    FROM embeddings e
                    JOIN segments s ON e.segment_id = s.id
                    JOIN collections c ON s.collection = c.id
                    WHERE c.name = ?
                    {filter_sql}
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    (collection_name, *filter_params, max_candidates),
                ).fetchall()
                candidate_ids = [r[0] for r in rows]
            except sqlite3.Error:
                logger.debug(
                    "recency-window query failed; trying id-ordered fallback",
                    exc_info=True,
                )
                try:
                    filter_sql, filter_params = _metadata_filter_sql("e.id")
                    rows = conn.execute(
                        f"""
                        SELECT e.id
                        FROM embeddings e
                        JOIN segments s ON e.segment_id = s.id
                        JOIN collections c ON s.collection = c.id
                        WHERE c.name = ?
                        {filter_sql}
                        ORDER BY e.id DESC
                        LIMIT ?
                        """,
                        (collection_name, *filter_params, max_candidates),
                    ).fetchall()
                    candidate_ids = [r[0] for r in rows]
                except sqlite3.Error:
                    logger.debug("id-ordered fallback also failed", exc_info=True)
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

        placeholders = ",".join(["?"] * len(candidate_ids))
        meta_rows = conn.execute(
            f"""
            SELECT m.id, e.embedding_id, m.key, m.string_value, m.int_value
            FROM embedding_metadata AS m
            JOIN embeddings AS e ON e.id = m.id
            WHERE m.id IN ({placeholders})
            """,
            candidate_ids,
        ).fetchall()
        committed_tokens, tokened_source_modes = _sqlite_generation_commit_state(
            conn, collection_name
        )
    finally:
        conn.close()

    # Group metadata rows into per-drawer dicts.
    drawers: dict[int, dict] = {}
    for emb_id, stored_drawer_id, key, sval, ival in meta_rows:
        d = drawers.setdefault(
            emb_id,
            {
                "_id": emb_id,
                "_stored_drawer_id": stored_drawer_id,
                "metadata": {},
                "text": "",
            },
        )
        if key == "chroma:document":
            d["text"] = sval or ""
        else:
            d["metadata"][key] = sval if sval is not None else ival

    # Apply wing/room filters in Python (FTS5 candidates may include
    # entries from other wings).
    candidates = _sqlite_bm25_candidates_from_drawers(
        drawers,
        wing=wing,
        room=room,
        source_file=source_file,
        window_active=window_active,
        since_dt=since_dt,
        before_dt=before_dt,
        committed_tokens=committed_tokens,
        tokened_source_modes=tokened_source_modes,
    )

    # Local BM25 over the candidate set.
    candidates = _resolve_sqlite_generation_candidates(
        candidates, query, db_path, collection_name, committed_tokens, tokened_source_modes
    )
    candidates = _collapse_logical_generation_hits(candidates)
    docs = [c["text"] for c in candidates]
    bm25_raw = _bm25_scores(query, docs, stop_words=stop_words)
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0
    for c, raw in zip(candidates, bm25_raw):
        c["bm25_score"] = round(raw, 3)
        c["_score"] = (raw / max_bm25) if max_bm25 > 0 else 0.0
    candidates.sort(key=lambda c: c["_score"], reverse=True)
    hits = candidates[:n_results]
    for h in hits:
        h.pop("_score", None)
        # Strip internal fields by default so the public BM25-only fallback
        # response stays clean. Callers that need chunk-precise dedup
        # (notably the union-merge path) opt in via _include_internal.
        if not _include_internal:
            h.pop("_source_file_full", None)
            h.pop("_chunk_index", None)
            h.pop("_logical_generation_id", None)
            h.pop("_physical_drawer_id", None)
            h.pop("_active_generation", None)
            h.pop("_generation_token", None)
            h.pop("_extract_mode", None)
            h.pop("_ingest_mode", None)

    result = {
        "query": query,
        "filters": {"wing": wing, "room": room, "source_file": source_file},
        "total_before_filter": len(candidates),
        "results": hits,
        "fallback": "bm25_only_via_sqlite",
        "fallback_reason": "vector_search_disabled",
    }
    if window_pool_truncated:
        result["date_filter_pool_truncated"] = True
    return result


def _resolve_lexical_generation_hits(
    drawers_col, hits, query, committed_tokens, tokened_source_modes=frozenset()
):
    """Replace stale lexical hits with their newest visible physical generation."""
    logical_metas = [
        hit.metadata or {} for hit in hits if (hit.metadata or {}).get("logical_drawer_id")
    ]
    current_ids = _current_generation_ids_for_query(
        drawers_col,
        {"metadatas": [logical_metas]},
        committed_tokens,
        tokened_source_modes,
    )
    by_physical = {hit.id: hit for hit in hits}
    missing_ids = sorted(set(current_ids.values()) - set(by_physical))
    if missing_ids:
        try:
            fetched = drawers_col.get(
                ids=missing_ids,
                include=["documents", "metadatas"],
            )
            fetched_ids = fetched.get("ids") or []
            fetched_docs = fetched.get("documents") or []
            fetched_metas = fetched.get("metadatas") or []
            for index, physical_id in enumerate(fetched_ids):
                document = fetched_docs[index] if index < len(fetched_docs) else ""
                metadata = fetched_metas[index] if index < len(fetched_metas) else {}
                score = _bm25_scores(query, [document or ""])[0]
                if score > 0:
                    by_physical[physical_id] = SimpleNamespace(
                        id=physical_id,
                        document=document or "",
                        metadata=metadata or {},
                        score=score,
                    )
        except Exception:
            logger.warning("Could not hydrate current lexical generations", exc_info=True)

    resolved = []
    emitted_logical = set()
    for hit in hits:
        logical_id = (hit.metadata or {}).get("logical_drawer_id")
        if not logical_id:
            resolved.append(hit)
            continue
        if logical_id in emitted_logical:
            continue
        emitted_logical.add(logical_id)
        current = by_physical.get(current_ids.get(logical_id))
        if current is not None:
            resolved.append(current)
        elif _is_visible_generation_metadata(
            hit.metadata or {}, committed_tokens, tokened_source_modes
        ):
            resolved.append(hit)
    return resolved


def _fetch_resolved_lexical_hits(
    drawers_col, query, where, target_results, committed_tokens, tokened_source_modes=frozenset()
):
    limit = max(1, target_results)
    total = None
    while True:
        result = drawers_col.lexical_search(
            query=query,
            n_results=limit,
            where=where or None,
        )
        resolved = _resolve_lexical_generation_hits(
            drawers_col, result.hits, query, committed_tokens, tokened_source_modes
        )
        if len(resolved) >= target_results or len(result.hits) < limit:
            return resolved
        if total is None:
            try:
                total = max(1, int(drawers_col.count()))
            except (AttributeError, TypeError, ValueError):
                return resolved
        if limit >= total:
            return resolved
        limit = min(total, max(limit + 1, limit * 2))


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

    BM25-only additions carry ``distance=None`` unless a strict
    ``max_distance`` threshold is set. Under a threshold, union mode loads
    stored embeddings for lexical hits and computes their vector distance
    before admitting them, preserving the same distance guarantee as the
    vector-only path.
    """
    committed_tokens, tokened_source_modes = _committed_generation_state(drawers_col)
    where = _visible_drawer_where(
        build_where_filter(wing, room, source_file), committed_tokens, tokened_source_modes
    )
    try:
        lexical_hits = _fetch_resolved_lexical_hits(
            drawers_col,
            query,
            where,
            n_results * 3,
            committed_tokens,
            tokened_source_modes,
        )
    except UnsupportedCapabilityError:
        raise
    except Exception:
        logger.debug("candidate_strategy=union: lexical fetch failed", exc_info=True)
        return

    metric = _metric_for_collection(drawers_col)
    lexical_distances = (
        _lexical_hit_vector_distances(drawers_col, query, lexical_hits, metric)
        if max_distance > 0.0
        else {}
    )

    bm25_extra = []
    for hit in lexical_hits:
        meta = hit.metadata or {}
        if not _is_visible_generation_metadata(meta, committed_tokens, tokened_source_modes):
            continue
        # The window applies to every candidate source; a lexically strong
        # drawer outside [since, before) must not enter through this side
        # door (the vector-path candidates are filtered upstream).
        if (since_dt is not None or before_dt is not None) and not filed_at_in_window(
            meta.get("filed_at"), since_dt, before_dt
        ):
            continue
        full_source = meta.get("source_file", "") or ""
        distance = lexical_distances.get(hit.id)
        if max_distance > 0.0:
            if distance is None or distance > max_distance:
                continue
            distance = round(distance, 4)
        bm25_extra.append(
            {
                "drawer_id": _result_drawer_id(meta, hit.id),
                "text": hit.document or "",
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "source_file": Path(full_source).name if full_source else "?",
                "source_path": full_source,
                **_result_date_fields(meta),
                "similarity": (
                    None
                    if distance is None
                    else round(_distance_to_similarity(distance, metric), 3)
                ),
                "distance": distance,
                "effective_distance": distance,
                "closet_boost": 0.0,
                "matched_via": "bm25_backend",
                "bm25_score": round(float(hit.score), 3),
                "_source_file_full": full_source,
                "_chunk_index": meta.get("chunk_index"),
                "_logical_generation_id": meta.get("logical_drawer_id"),
                "_physical_drawer_id": hit.id,
                "_active_generation": meta.get("mine_generation_token") in committed_tokens,
            }
        )

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
