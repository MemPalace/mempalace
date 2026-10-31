# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


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
    fetches the WHOLE entry rather than the one chunk that matched.
    Kept in sync with ``mcp_server._PARENT_ID_KEYS``.
    """
    meta = meta or {}
    return meta.get("parent_drawer_id") or meta.get("parent_entry_id") or stored_drawer_id


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
    """Resolve the vector/BM25 weights for the hybrid re-rank.

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


def _vector_distance(
    query_vector: list[float], candidate_vector: list[float], metric: str
) -> float:
    """Return the distance Chroma's own query reports for ``metric``.

    Chroma's ``l2`` space reports squared Euclidean distance and its ``ip``
    space reports ``1 - dot``, so a recomputed candidate ranks and filters on
    the same scale as a queried one.
    """
    if not query_vector or not candidate_vector or len(query_vector) != len(candidate_vector):
        raise ValueError("embedding dimensions do not match")

    if metric == "l2":
        return sum((a - b) ** 2 for a, b in zip(query_vector, candidate_vector))

    dot = sum(a * b for a, b in zip(query_vector, candidate_vector))
    if metric == "ip":
        return 1.0 - dot

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

        query_vector = _embed_texts([query], query=True)[0]
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

    * A candidate with a ``closet_boost`` is scored from its boosted
      distance (cosine scale, taken back to ``metric``'s scale), the distance
      the vector pass ranked it by, so re-scoring doesn't drop the boost.

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
    bm25_raw = bm25_scores(query, docs, stop_words=stop_words)
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0
    bm25_norm = [s / max_bm25 for s in bm25_raw] if max_bm25 > 0 else [0.0] * len(bm25_raw)

    scored = []
    for r, raw, norm in zip(results, bm25_raw, bm25_norm):
        distance = r.get("distance")
        boost = r.get("closet_boost") or 0.0
        if boost and distance is not None:
            boosted = _boosted_cosine_distance(distance, boost, metric)
            distance = _metric_scale_distance(boosted, metric)
        vec_sim = _distance_to_similarity(distance, metric)
        r["bm25_score"] = round(raw, 3)
        scored.append((vector_weight * vec_sim + bm25_weight * norm, r))

    # Break exact score ties by raw vector distance first: boosted drawers closer
    # than their boost all clamp to the same score and must keep vector order.
    # Then toward the more recently authored drawer, so equal-score candidates rank
    # chronologically instead of in arbitrary backend order. Compare timestamps
    # by instant across UTC offsets; missing dates and distances sort last.
    # authored_at lives at the top level on the search_memories path and nested under
    # "metadata" on the candidate-union path; check both so the tie-break works for each.
    scored.sort(
        key=lambda pair: (
            pair[0],
            -pair[1]["distance"] if pair[1].get("distance") is not None else -math.inf,
            recency_key(
                pair[1].get("authored_at") or pair[1].get("metadata", {}).get("authored_at")
            ),
        ),
        reverse=True,
    )
    results[:] = [r for _, r in scored]
    return results
