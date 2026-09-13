# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


def _first_or_empty(results, key: str) -> list:
    """Return the first inner list of a query result field, or [].

    Accepts both the typed :class:`QueryResult` (attribute access) and the
    pre-typed chroma dict shape; this polymorphism is retained so test mocks
    still work and callers mid-migration do not crash. Preserves the empty-
    collection semantics from issue #195: when no queries returned hits, the
    outer list may be empty and indexing ``[0]`` would raise.
    """
    outer = getattr(results, key, None) if not isinstance(results, dict) else results.get(key)
    if not outer:
        return []
    return outer[0] or []


def _aligned_query_ids(results, document_count: int) -> list:
    """Return query IDs padded to match the document result column.

    Production backends return an ID for every document. Some legacy test
    mocks omit IDs, so pad with ``None`` instead of letting ``zip`` discard
    otherwise valid mocked results.
    """
    ids = list(_first_or_empty(results, "ids"))
    if len(ids) < document_count:
        ids.extend([None] * (document_count - len(ids)))
    return ids[:document_count]


def _result_drawer_id(meta, stored_drawer_id):
    """Return the ID that round-trips through ``mempalace_get_drawer``.

    Chunk metadata carries the logical-group id under ``parent_drawer_id``
    (``tool_add_drawer``) or ``parent_entry_id`` (``tool_diary_write``);
    resolving both means a hit on a chunked diary entry reports the id that
    fetches the WHOLE entry rather than the one chunk that matched (#2185).
    Kept in sync with ``mcp_server._PARENT_ID_KEYS``.
    """
    meta = meta or {}
    return (
        meta.get("parent_drawer_id")
        or meta.get("parent_entry_id")
        or meta.get("logical_drawer_id")
        or stored_drawer_id
    )


def _collapse_logical_generation_hits(hits: list) -> list:
    """Keep the newest visible physical generation for each stable logical id."""
    chosen = {}
    passthrough = []
    for hit in hits:
        logical_id = hit.get("_logical_generation_id")
        if not logical_id:
            passthrough.append(hit)
            continue
        key = (
            bool(hit.get("_active_generation")),
            hit.get("created_at") or "",
            hit.get("_physical_drawer_id") or "",
        )
        current = chosen.get(logical_id)
        if current is None or key > current[0]:
            chosen[logical_id] = (key, hit)
    return passthrough + [item[1] for item in chosen.values()]


def _collapse_physical_generation_rows(
    rows, committed_tokens, tokened_source_modes=frozenset()
) -> list:
    """Keep the active physical row per logical id; retain ordinary rows."""
    chosen = {}
    ordinary = []
    for physical_id, document, metadata in rows:
        metadata = metadata or {}
        if not _is_visible_generation_metadata(metadata, committed_tokens, tokened_source_modes):
            continue
        logical_id = metadata.get("logical_drawer_id")
        if not logical_id:
            ordinary.append((physical_id, document, metadata))
            continue
        key = (
            metadata.get("mine_generation_token") in committed_tokens,
            metadata.get("filed_at", ""),
            physical_id,
        )
        if logical_id not in chosen or key > chosen[logical_id][0]:
            chosen[logical_id] = (key, (physical_id, document, metadata))
    return ordinary + [item[1] for item in chosen.values()]


def _tokenize(text: str, stop_words: frozenset = frozenset()) -> list:
    """Lowercase + strip to alphanumeric tokens of length ≥ 2.

    Tolerates ``None`` documents — Chroma can return ``None`` in the
    ``documents`` field for drawers without text content, which would
    otherwise raise ``AttributeError`` mid-rerank.

    When ``stop_words`` is non-empty, filters tokens that match any entry.
    The set is expected to already be lowercased so callers can share one
    instance across query + document tokenization.
    """
    if not text:
        return []
    tokens = _TOKEN_RE.findall(text.lower())
    if stop_words:
        return [t for t in tokens if t not in stop_words]
    return tokens


@functools.lru_cache(maxsize=16)
def _stopwords_for_canonical(canonical_lang: str) -> frozenset:
    """Cached stop-word set keyed by a canonical locale code.

    Splitting canonicalization out of the cache key avoids thrashing when
    callers pass equivalent variants (``"EN"``, ``"en"``, ``"en-US"``) —
    they all hit the same cache slot.
    """
    return frozenset(get_stopwords(canonical_lang))


def _stopwords_for_lang(lang: str) -> frozenset:
    """Resolve raw ``lang`` to its canonical form before cache lookup.

    Kept as the public-shaped helper (callers and tests reach for this
    name) while the lru_cache lives on ``_stopwords_for_canonical`` to
    keep the cache key normalized.
    """
    canonical = _canonical_lang(lang) or lang.lower()
    return _stopwords_for_canonical(canonical)


def _resolve_stop_words(lang: Optional[str]) -> frozenset:
    """Return the BM25 stop-word set for ``lang`` as an opt-in feature.

    When ``lang`` is an explicit string, loads that locale's stop words.
    When ``lang`` is ``None``, resolution order is:

    1. ``MEMPALACE_LANG`` / ``MEMPAL_LANG`` environment variable.
    2. ``MempalaceConfig().lang_explicit`` (which itself reads the env vars
       first, then ``config.json["lang"]``).

    The env-var fast path avoids constructing ``MempalaceConfig`` (which
    reads ``config.json`` from disk) on the hot search path when the user
    has set the env var — the common case for explicit-locale palaces.
    Palaces that never configured a language get an empty set, preserving
    pre-PR scoring byte-for-byte.
    """
    if lang is None:
        env_val = os.environ.get("MEMPALACE_LANG") or os.environ.get("MEMPAL_LANG")
        if env_val and env_val.strip():
            lang = env_val.strip()
        else:
            try:
                lang = MempalaceConfig().lang_explicit
            except Exception:
                logger.debug("lang resolution failed, skipping stop-word filter", exc_info=True)
                return frozenset()
        if lang is None:
            return frozenset()
    return _stopwords_for_lang(lang)


def _resolve_hybrid_rank_weights() -> tuple:
    """Resolve the vector/BM25 weights for the hybrid re-rank (#2298).

    Reads ``MempalaceConfig().hybrid_rank_vector_weight`` /
    ``.hybrid_rank_bm25_weight`` — which themselves resolve env var
    (``MEMPALACE_HYBRID_VECTOR_WEIGHT`` / ``MEMPALACE_HYBRID_BM25_WEIGHT``)
    > ``config.json`` > the built-in defaults (0.6 / 0.4). Any failure — a
    config that cannot be read or a setting that failed validation — falls
    back to the same defaults, so a bad config value never takes a search
    down. Leaving the settings unset reproduces the previously-hardcoded
    blend byte-for-byte.
    """
    try:
        cfg = MempalaceConfig()
        vector_weight = float(cfg.hybrid_rank_vector_weight)
        bm25_weight = float(cfg.hybrid_rank_bm25_weight)
        return vector_weight, bm25_weight
    except Exception:
        logger.debug("hybrid rank weight resolution failed, using defaults", exc_info=True)
        return 0.6, 0.4


def _bm25_scores(
    query: str,
    documents: list,
    k1: float = 1.5,
    b: float = 0.75,
    stop_words: frozenset = frozenset(),
) -> list:
    """Compute Okapi-BM25 scores for ``query`` against each document.

    IDF is computed over the *provided corpus* using the Lucene/BM25+
    smoothed formula ``log((N - df + 0.5) / (df + 0.5) + 1)``, which is
    always non-negative. This is well-defined for re-ranking a small
    candidate set returned by vector retrieval — IDF then reflects how
    discriminative each query term is *within the candidates*, exactly
    what's needed to reorder them.

    Parameters mirror Okapi-BM25 conventions:
        k1 — term-frequency saturation (1.2-2.0 typical, 1.5 default)
        b  — length normalization (0.0 = none, 1.0 = full, 0.75 default)

    Returns a list of scores in the same order as ``documents``.
    """
    n_docs = len(documents)
    query_terms = set(_tokenize(query, stop_words))
    if not query_terms or n_docs == 0:
        return [0.0] * n_docs

    tokenized = [_tokenize(d, stop_words) for d in documents]
    doc_lens = [len(toks) for toks in tokenized]
    if not any(doc_lens):
        return [0.0] * n_docs
    avgdl = sum(doc_lens) / n_docs or 1.0

    # Document frequency: how many docs contain each query term?
    df = {term: 0 for term in query_terms}
    for toks in tokenized:
        seen = set(toks) & query_terms
        for term in seen:
            df[term] += 1

    idf = {term: math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1) for term in query_terms}

    scores = []
    for toks, dl in zip(tokenized, doc_lens):
        if dl == 0:
            scores.append(0.0)
            continue
        tf: dict = {}
        for t in toks:
            if t in query_terms:
                tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for term, freq in tf.items():
            num = freq * (k1 + 1)
            den = freq + k1 * (1 - b + b * dl / avgdl)
            score += idf[term] * num / den
        scores.append(score)
    return scores


def _distance_to_similarity(distance, metric: str = "cosine") -> float:
    """Map a backend-reported ``distance`` to a [0, 1]-ish similarity.

    The backend contract for the ``distances`` field is *lower = closer*
    regardless of metric (RFC 001, backend metric declaration), so every
    mapping here is monotonic decreasing in ``distance``. The output stays
    bounded so it is
    commensurable with the min-max-normalized BM25 term in
    :func:`_hybrid_rank`.

    * ``cosine`` — distance ∈ [0, 2], 0 = identical: ``max(0, 1 - d)``.
    * ``l2`` — Euclidean ∈ [0, ∞): ``1 / (1 + d)`` (1 at d=0, →0 as d→∞).
    * ``ip`` — inner-product distance (e.g. pgvector ``<#>`` = -dot, lower =
      closer), unbounded and signed: logistic squash ``1 / (1 + e^d)``.
      Provisional until a real ip backend exercises it; no in-tree backend
      uses ip today.

    ``distance is None`` (vector-unknown, e.g. a BM25-only candidate) maps to
    0.0 so the candidate scores on its BM25 contribution alone.
    """
    if distance is None:
        return 0.0
    m = (metric or "cosine").lower()
    if m == "l2":
        return 1.0 / (1.0 + max(0.0, distance))
    if m == "ip":
        # Clamp the exponent so a large positive distance can't overflow.
        return 1.0 / (1.0 + math.exp(min(60.0, distance)))
    # cosine (default)
    return max(0.0, 1.0 - distance)


def _metric_for_collection(col) -> str:
    """Resolve a collection's declared distance metric, defaulting to cosine.

    Reads the ``distance_metric`` exposed by the backend collection (the
    RFC 001 backend metric declaration). ``EmbeddingCollection`` delegates the
    attribute to its inner collection; legacy Chroma palaces report their
    actual ``hnsw:space``.
    Any failure falls back to ``"cosine"`` — the value all in-tree backends
    use and the only metric MemPalace created palaces with historically.
    """
    try:
        metric = getattr(col, "distance_metric", "cosine")
    except Exception:
        return "cosine"
    metric = str(metric or "cosine").lower()
    return metric if metric in ("cosine", "l2", "ip") else "cosine"


def _vector_distance(
    query_vector: list[float], candidate_vector: list[float], metric: str
) -> float:
    """Return a backend-style distance between two already-normalized vectors."""
    if not query_vector or not candidate_vector or len(query_vector) != len(candidate_vector):
        raise ValueError("embedding dimensions do not match")

    if metric == "l2":
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(query_vector, candidate_vector)))

    dot = sum(a * b for a, b in zip(query_vector, candidate_vector))
    if metric == "ip":
        return -dot

    q_norm = math.sqrt(sum(a * a for a in query_vector))
    c_norm = math.sqrt(sum(b * b for b in candidate_vector))
    if q_norm == 0.0 or c_norm == 0.0:
        raise ValueError("zero-norm embedding")
    return 1.0 - (dot / (q_norm * c_norm))


def _lexical_hit_vector_distances(drawers_col, query: str, lexical_hits: list, metric: str) -> dict:
    """Compute vector distances for lexical hits when stored embeddings are available."""
    ids = [hit.id for hit in lexical_hits if getattr(hit, "id", None)]
    if not ids:
        return {}

    try:
        from ..backends.embedding_wrapper import _embed_texts

        query_vector = _embed_texts([query])[0]
        stored = drawers_col.get(ids=ids, include=["embeddings"])
    except Exception:
        logger.debug(
            "candidate_strategy=union: failed to load lexical hit embeddings", exc_info=True
        )
        return {}

    stored_ids = getattr(stored, "ids", None) if not isinstance(stored, dict) else stored.get("ids")
    embeddings = (
        getattr(stored, "embeddings", None)
        if not isinstance(stored, dict)
        else stored.get("embeddings")
    )
    if not stored_ids or not embeddings:
        return {}

    distances = {}
    for doc_id, candidate_vector in zip(stored_ids, embeddings):
        try:
            distances[doc_id] = _vector_distance(query_vector, candidate_vector, metric)
        except Exception:
            logger.debug(
                "candidate_strategy=union: failed to score lexical hit %s", doc_id, exc_info=True
            )
    return distances


def _hybrid_rank(
    results: list,
    query: str,
    vector_weight: float = 0.6,
    bm25_weight: float = 0.4,
    metric: str = "cosine",
    stop_words: frozenset = frozenset(),
) -> list:
    """Re-rank ``results`` by a convex combination of vector similarity and BM25.

    * Vector similarity is derived from each candidate's backend-reported
      ``distance`` via :func:`_distance_to_similarity`, interpreted in the
      collection's declared ``metric`` (per RFC 001) rather than assuming
      cosine. Absolute (not relative-to-max) means adding/removing a
      candidate can't reshuffle the others.
    * BM25 is real Okapi-BM25 with corpus-relative IDF over the candidates
      themselves. Since the absolute scale is unbounded, BM25 is min-max
      normalized within the candidate set so weights are commensurable.

    Candidates with ``distance=None`` are treated as vector-unknown
    (no vector signal available) and scored on BM25 contribution alone.
    Used by candidate-union mode to merge BM25-only candidates that the
    vector index didn't surface.

    Mutates each result dict to add ``bm25_score`` and reorders the list
    in place. Returns the same list for convenience.
    """
    if not results:
        return results

    docs = [r.get("text", "") for r in results]
    bm25_raw = _bm25_scores(query, docs, stop_words=stop_words)
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0
    bm25_norm = [s / max_bm25 for s in bm25_raw] if max_bm25 > 0 else [0.0] * len(bm25_raw)

    scored = []
    for r, raw, norm in zip(results, bm25_raw, bm25_norm):
        vec_sim = _distance_to_similarity(r.get("distance"), metric)
        r["bm25_score"] = round(raw, 3)
        scored.append((vector_weight * vec_sim + bm25_weight * norm, r))

    # Break exact score ties toward the more recently authored drawer so equal-score
    # candidates rank chronologically instead of in arbitrary backend order. ISO-8601
    # ``authored_at`` strings sort chronologically; missing dates sort oldest.
    # authored_at lives at the top level on the search_memories path and nested under
    # "metadata" on the candidate-union path; check both so the tie-break works for each.
    scored.sort(
        key=lambda pair: (
            pair[0],
            pair[1].get("authored_at") or pair[1].get("metadata", {}).get("authored_at") or "",
        ),
        reverse=True,
    )
    results[:] = [r for _, r in scored]
    return results
