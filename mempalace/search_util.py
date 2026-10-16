"""Small search helpers with no storage or embedding imports.

``mempalace.layers`` needs these on the wake-up path. Importing them from
``mempalace.searcher`` would load the backend stack.
"""

import math


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


def build_where_filter(wing: str = None, room: str = None, source_file: str = None) -> dict:
    """Build a ChromaDB where filter from optional wing/room/source_file.

    ChromaDB needs a ``$and`` only when ≥2 clauses are present; a single
    clause is returned bare and zero clauses yield an empty filter (#1815).
    """
    clauses = []
    if wing:
        clauses.append({"wing": wing})
    if room:
        clauses.append({"room": room})
    if source_file:
        clauses.append({"source_file": source_file})
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


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
