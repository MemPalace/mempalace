"""Small search helpers with no storage or embedding imports.

``mempalace.layers`` needs these on the wake-up path. Importing them from
``mempalace.searcher`` would load the backend stack.
"""

import math
import os
import time


def _first_or_empty(results, key: str) -> list:
    """Return the first inner list of a query result field, or [].

    Accepts both the typed :class:`QueryResult` (attribute access) and the
    pre-typed chroma dict shape; this polymorphism is retained so test mocks
    still work and callers mid-migration do not crash. When no queries
    returned hits the outer list may be empty, and indexing ``[0]`` would raise.
    """
    outer = getattr(results, key, None) if not isinstance(results, dict) else results.get(key)
    if not outer:
        return []
    return outer[0] or []


_KNOWN_WINGS_TTL = 30.0
_known_wings_cache: dict = {}


def _chroma_stamp(palace_path):
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    stamps = []
    for path in (db_path, db_path + "-wal"):
        try:
            st = os.stat(path)
        except OSError:
            stamps.append(None)
        else:
            stamps.append((st.st_mtime_ns, st.st_size))
    return tuple(stamps)


def _cached_wings(key, stamp, read) -> frozenset:
    now = time.monotonic()
    hit = _known_wings_cache.get(key)
    if hit is not None and hit[1] == stamp and now - hit[0] < _KNOWN_WINGS_TTL:
        return hit[2]
    try:
        names = frozenset(read() or ())
    except Exception:
        names = frozenset()
    _known_wings_cache[key] = (now, stamp, names)
    return names


def chroma_known_wings(palace_path: str) -> frozenset:
    """:func:`known_wings` for a Chroma palace, read without opening it."""
    from .backends.chroma_sqlite import sqlite_wing_names

    return _cached_wings(
        ("chroma", palace_path),
        _chroma_stamp(palace_path),
        lambda: sqlite_wing_names(palace_path),
    )


def known_wings(col) -> frozenset:
    """The wing names ``col``'s palace holds drawers under.

    One metadata read, as ``list_wings`` does: Chroma's from the sqlite
    metadata index, a backend with metadata facets from ``facet_counts``.
    Kept for 30 seconds, and for Chroma until the database changes, so a hot
    path does not pay a read per query. Empty when neither read is available,
    which leaves ``wing_aliases`` to the spellings it derives.
    """
    if col is None:
        return frozenset()
    inner = getattr(col, "_inner", col)
    palace_path = getattr(inner, "_palace_path", None)
    from .backends.chroma import ChromaCollection

    if isinstance(inner, ChromaCollection):
        if isinstance(palace_path, str):
            return chroma_known_wings(palace_path)
        return frozenset()
    capabilities = getattr(getattr(inner, "_backend", None), "capabilities", None)
    if not isinstance(capabilities, (set, frozenset)):
        return frozenset()
    if "supports_metadata_facets" not in capabilities:
        return frozenset()
    key = (type(inner).__name__, palace_path if isinstance(palace_path, str) else id(inner))
    return _cached_wings(key, None, lambda: inner.facet_counts("wing", limit=10_000))


def palace_wing_aliases(wing: str, col=None, known=None) -> list:
    """``wing_aliases`` for ``wing``, with ``col``'s stored wing names
    (or ``known``, when the caller already has them)."""
    from .config import wing_aliases

    return wing_aliases(wing, known_wings(col) if known is None else known)


def build_where_filter(
    wing: str = None, room: str = None, source_file: str = None, col=None, known=None
) -> dict:
    """Build a ChromaDB where filter from optional wing/room/source_file.

    ChromaDB needs a ``$and`` only when ≥2 clauses are present; a single
    clause is returned bare and zero clauses yield an empty filter. A wing
    matches every spelling its project was filed under (``wing_aliases``),
    including the stored wings of ``col``'s palace (or ``known``) that slug
    to it.
    """
    clauses = []
    if wing:
        clauses.append({"wing": {"$in": palace_wing_aliases(wing, col, known)}})
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
    * ``l2`` — Chroma's squared Euclidean ∈ [0, ∞): ``1 / (1 + d)`` (1 at
      d=0, →0 as d→∞).
    * ``ip`` — Chroma's ``1 - dot`` (lower = closer), unbounded for
      non-unit vectors: logistic squash ``1 / (1 + e^d)``. Only legacy Chroma
      palaces created with ``hnsw:space=ip`` use it; pgvector, qdrant,
      sqlite_exact and milvus report cosine.

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
    # Cosine is the default metric.
    return max(0.0, 1.0 - distance)


def _cosine_scale_distance(distance: float, metric: str = "cosine") -> float:
    """Express a backend ``distance`` on the cosine scale (0 = identical, 2 = opposite).

    ``max_distance`` is documented on the cosine scale, so a cutoff means the
    same thing whatever metric the collection declares. Distances follow
    Chroma's conventions: ``l2`` is squared Euclidean, ``ip`` is ``1 - dot``.
    For unit-length embeddings, which every in-tree embedder produces,
    squared ``l2`` is ``2 * cos_dist`` and ``ip`` already equals ``cos_dist``.
    """
    m = (metric or "cosine").lower()
    if m == "l2":
        return distance / 2.0
    return distance


def _boosted_cosine_distance(distance: float, boost: float, metric: str = "cosine") -> float:
    """A closet-boosted distance on the cosine scale, clamped to ``[0, 2]``.

    Without the clamp a boost larger than the distance goes negative and
    sorts the best hybrid matches last.
    """
    return max(0.0, min(2.0, _cosine_scale_distance(distance, metric) - boost))


def _metric_scale_distance(distance: float, metric: str = "cosine") -> float:
    """Inverse of :func:`_cosine_scale_distance`: a cosine-scale distance back
    on ``metric``'s own scale."""
    m = (metric or "cosine").lower()
    if m == "l2":
        return distance * 2.0
    return distance


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
