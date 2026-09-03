#!/usr/bin/env python3
"""
searcher.py — Find anything. Exact words.

Hybrid search: BM25 keyword matching + vector semantic similarity. The
drawer query is the floor — always runs — and closet hits add a rank-based
boost when they agree. Closets are a ranking *signal*, never a gate, so
weak closets (regex extraction on narrative content) can only help, never
hide drawers the direct path would have found.
"""

import functools
import logging
import math
import os
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Optional

from .backends import (
    BackendError,
    BackendMismatchError,
    CollectionNotInitializedError,
    PalaceNotFoundError,
    UnsupportedCapabilityError,
)
from .config import MempalaceConfig, sqlite_read_uri
from .date_window import filed_at_in_window, parse_window
from .i18n import _canonical_lang, get_stopwords
from .palace import (
    _open_collection_or_explain,
    get_closets_collection,
    get_collection,
    resolve_backend_name,
)

# Closet pointer line format: "topic|entities|→drawer_id_a,drawer_id_b"
# Multiple lines may join with newlines inside one closet document.
_CLOSET_DRAWER_REF_RE = re.compile(r"→([\w,]+)")

logger = logging.getLogger("mempalace_mcp")


class SearchError(Exception):
    """Raised when search cannot proceed (e.g. no palace found)."""


_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)


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
    return meta.get("parent_drawer_id") or meta.get("parent_entry_id") or stored_drawer_id


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
        from .backends.embedding_wrapper import _embed_texts

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


def _extract_drawer_ids_from_closet(closet_doc: str) -> list:
    """Parse all `→drawer_id_a,drawer_id_b` pointers out of a closet document.

    Preserves order and dedupes.
    """
    seen: dict = {}
    for match in _CLOSET_DRAWER_REF_RE.findall(closet_doc):
        for did in match.split(","):
            did = did.strip()
            if did and did not in seen:
                seen[did] = None
    return list(seen.keys())


def _scoped_source_filter(source_file: str, parent_drawer_id=None) -> dict:
    """Build a Chroma ``where`` clause that scopes a query to ``source_file``,
    additionally constrained by ``parent_drawer_id`` when one is supplied.

    Two unrelated oversized ``tool_add_drawer`` writes (chunked path from
    #1539) can pass the same ``source_file`` (e.g. two pastes tagged
    ``"chat.log"``); each call stores its own ``parent_drawer_id`` group
    of chunks but the bare ``source_file`` filter pulls chunks from both
    groups as if they were siblings (#1580). When the matched chunk
    carries a ``parent_drawer_id`` the filter narrows to that logical
    group. Otherwise (pre-#1539 drawers, single-chunk writes, and
    ``diary_ingest`` drawers grouped by real file path) the original
    file-global shape is preserved. Mirrors the conditional-``$and``
    precedent in ``build_where_filter``.
    """
    if parent_drawer_id:
        return {
            "$and": [
                {"source_file": source_file},
                {"parent_drawer_id": parent_drawer_id},
            ]
        }
    return {"source_file": source_file}


def _expand_with_neighbors(drawers_col, matched_doc: str, matched_meta: dict, radius: int = 1):
    """Expand a matched drawer with its ±radius sibling chunks in the same source file.

    Motivation — "drawer-grep context" feature: a closet hit returns one
    drawer, but the chunk boundary may clip mid-thought (e.g., the matched
    chunk says "here's a breakdown:" and the actual breakdown lives in the
    next chunk). Fetching the small neighborhood around the match gives
    callers enough context without forcing a follow-up ``get_drawer`` call.

    Returns a dict with:
        ``text``            combined chunks in chunk_index order
        ``drawer_index``    the matched chunk's index in the source file
        ``total_drawers``   total drawer count for the source file (or None)

    On any ChromaDB failure or missing metadata, falls back to returning the
    matched drawer alone so search never breaks because neighbor expansion
    failed.
    """
    src = matched_meta.get("source_file")
    chunk_idx = matched_meta.get("chunk_index")
    if not src or not isinstance(chunk_idx, int):
        return {"text": matched_doc, "drawer_index": chunk_idx, "total_drawers": None}

    # Narrow by ``parent_drawer_id`` when present so chunks from unrelated
    # logical drawers sharing ``source_file`` do not stitch (#1580). See
    # ``_scoped_source_filter`` for the contract.
    parent_id = matched_meta.get("parent_drawer_id")
    target_indexes = [chunk_idx + offset for offset in range(-radius, radius + 1)]
    neighbor_clauses = [
        {"source_file": src},
        {"chunk_index": {"$in": target_indexes}},
    ]
    if parent_id:
        neighbor_clauses.append({"parent_drawer_id": parent_id})
    try:
        neighbors = drawers_col.get(
            where={"$and": neighbor_clauses},
            include=["documents", "metadatas"],
        )
    except Exception:
        return {"text": matched_doc, "drawer_index": chunk_idx, "total_drawers": None}

    indexed_docs = []
    for doc, meta in zip(neighbors.documents, neighbors.metadatas):
        ci = meta.get("chunk_index")
        if isinstance(ci, int):
            indexed_docs.append((ci, doc))
    indexed_docs.sort(key=lambda pair: pair[0])

    if not indexed_docs:
        combined_text = matched_doc
    else:
        combined_text = "\n\n".join(doc for _, doc in indexed_docs)

    # Cheap total_drawers lookup. When ``parent_drawer_id`` is present the
    # count is scoped to that group so the returned number matches the
    # text the caller gets back. Without a parent id, the legacy
    # file-global count is preserved.
    total_drawers = None
    try:
        all_meta = drawers_col.get(
            where=_scoped_source_filter(src, parent_id),
            include=["metadatas"],
        )
        total_drawers = len(all_meta.ids) if all_meta.ids else None
    except Exception:
        logger.debug("total_drawers lookup failed for %s", src, exc_info=True)

    return {
        "text": combined_text,
        "drawer_index": chunk_idx,
        "total_drawers": total_drawers,
    }


def _warn_if_legacy_metric(col) -> None:
    """Print a one-line notice if the palace was created without
    ``hnsw:space=cosine``.

    ChromaDB's default is L2 (Euclidean), under which cosine-based
    similarity interpretation falls apart — distances routinely exceed
    1.0 and the display ``max(0, 1 - dist)`` floors every result to 0.
    Legacy palaces (mined before this metadata was consistently set)
    need ``mempalace repair`` to rebuild with the correct metric.

    The warning fires only for palaces that clearly have the wrong
    metric; palaces with no metadata table at all (empty dict) also
    fall under this check since that is the signal of a pre-metadata
    palace.
    """
    try:
        meta = getattr(col, "metadata", None)
    except Exception:
        return
    if not isinstance(meta, dict):
        return
    space = meta.get("hnsw:space")
    if space == "cosine":
        return
    # Either missing or set to something else — both are suspect.
    import sys as _sys

    detail = f"hnsw:space={space!r}" if space else "no hnsw:space metadata"
    print(
        f"\n  NOTICE: this palace was created without cosine distance ({detail}).\n"
        "          Semantic similarity scores will not be meaningful.\n"
        "          Run `mempalace repair` to rebuild the index with the correct metric.",
        file=_sys.stderr,
    )


def _hnsw_capacity_diverged(palace_path: str) -> bool:
    """Return True if HNSW divergence is severe enough to crash ChromaDB.

    Thin, exception-safe wrapper around
    :func:`mempalace.backends.chroma.hnsw_capacity_status`. Used by the
    CLI search path to short-circuit to the BM25-only fallback before
    opening a Chroma client. Client construction and collection identity
    checks can themselves touch the damaged index, so guarding only
    ``col.query()`` is too late (#1222 covers the MCP path via the module-level
    ``_vector_disabled`` flag; this covers the CLI path).

    A probe that raises falls through to ``False`` so the caller proceeds
    to the normal vector path — the underlying query then either succeeds
    (probe was a false negative) or raises its own diagnostic error. The
    probe itself must never be the thing that crashes search.
    """
    try:
        from .backends.chroma import hnsw_capacity_status
        from .config import get_configured_collection_name

        info = hnsw_capacity_status(palace_path, get_configured_collection_name())
        return bool(info.get("diverged"))
    except Exception:
        logger.debug("HNSW capacity probe raised; proceeding to vector path", exc_info=True)
        return False


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

        col = _open_collection_or_explain(palace_path, opener=get_collection)
        if col is None:
            if not os.path.isdir(palace_path):
                raise SearchError(f"No palace found at {palace_path}")
            raise SearchError(f"No palace database at {palace_path}")

    # Alert the user if this palace predates hnsw:space=cosine being set on
    # creation — their similarity scores will be junk until they run repair.
    _warn_if_legacy_metric(col)

    where = build_where_filter(wing, room)

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

        results = col.query(**kwargs)

    except Exception as e:
        print(f"\n  Search error: {e}")
        raise SearchError(f"Search error: {e}") from e

    docs = _first_or_empty(results, "documents")
    metas = _first_or_empty(results, "metadatas")
    dists = _first_or_empty(results, "distances")

    if date_window_active:
        kept = [
            (doc, meta, dist)
            for doc, meta, dist in zip(docs, metas, dists)
            if filed_at_in_window((meta or {}).get("filed_at"), since_dt, before_dt)
        ]
        # Keep the whole in-window pool here; the hybrid re-rank below must
        # see every survivor before the display cut to n_results, or a
        # BM25-strong drawer deep in the pool could never surface.
        docs = [k[0] for k in kept]
        metas = [k[1] for k in kept]
        dists = [k[2] for k in kept]

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
        {"text": doc or "", "distance": float(dist), "metadata": meta or {}}
        for doc, meta, dist in zip(docs, metas, dists)
    ]
    hits = _hybrid_rank(hits, query, metric=metric, stop_words=stop_words)
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
        from .config import get_configured_collection_name

        collection_name = get_configured_collection_name()

    def _metadata_filter_sql(row_id_expr: str) -> tuple[str, list[str]]:
        clauses = []
        params = []
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
        conn = sqlite3.connect(sqlite_read_uri(db_path), uri=True)
    except sqlite3.Error as e:
        return _search_error_result(f"sqlite open failed: {e}")

    window_active = since_dt is not None or before_dt is not None
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
    candidates = []
    for d in drawers.values():
        meta = d["metadata"]
        if wing and meta.get("wing") != wing:
            continue
        if room and meta.get("room") != room:
            continue
        if source_file and meta.get("source_file") != source_file:
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
                "created_at": meta.get("filed_at", "unknown"),
                "authored_at": meta.get("authored_at", meta.get("filed_at", "unknown")),
                # No vector distance available in BM25-only mode.
                "similarity": None,
                "distance": None,
                "matched_via": "bm25_sqlite",
                # Internal: full path + chunk_index let callers (notably
                # candidate_strategy="union") dedupe at chunk granularity
                # rather than basename — two files in different directories
                # may share a basename, and one source_file is split across
                # multiple chunks. Stripped before this helper returns.
                "_source_file_full": full_source,
                "_chunk_index": meta.get("chunk_index"),
            }
        )

    # Local BM25 over the candidate set.
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
    where = build_where_filter(wing, room, source_file)
    try:
        lexical = drawers_col.lexical_search(
            query=query,
            n_results=n_results * 3,
            where=where or None,
        )
    except UnsupportedCapabilityError:
        raise
    except Exception:
        logger.debug("candidate_strategy=union: lexical fetch failed", exc_info=True)
        return

    metric = _metric_for_collection(drawers_col)
    lexical_distances = (
        _lexical_hit_vector_distances(drawers_col, query, lexical.hits, metric)
        if max_distance > 0.0
        else {}
    )

    bm25_extra = []
    for hit in lexical.hits:
        meta = hit.metadata or {}
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
                "created_at": meta.get("filed_at", "unknown"),
                "authored_at": meta.get("authored_at", meta.get("filed_at", "unknown")),
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


def _candidate_pool_size(n_results: int, date_window_active: bool) -> int:
    """Rerank-pool size for the drawer vector query.

    Without a date window this is the historical ``n_results * 3``
    over-fetch. With one, the window filters the pool AFTER retrieval
    (ChromaDB rejects string operands for ``$gte``/``$lt``, so ``filed_at``
    can't be range-filtered server-side), and a narrow window over a large
    palace would starve a 3x pool even though matching drawers exist —
    recall is the design requirement. Widen to ``n_results * 15``, capped
    at 500 (the ceiling the filter-fallback path already uses) — except
    the pool never drops below ``n_results`` itself, or an oversized
    request could return fewer rows than an unfiltered query would.
    ``date_filter_pool_truncated`` in the response flags a full pool so a
    capped result is never silent.
    """
    if not date_window_active:
        return n_results * 3
    return max(min(n_results * 15, 500), n_results)


_CLOSET_RESULT_POOL_MULTIPLIER = 4
_MAX_HYDRATION_CHARS = 10000


def _candidate_pool_limits(
    candidate_strategy: str,
    n_results: int,
    date_window_active: bool = False,
) -> tuple[int, int]:
    """Return vector-query and pre-enrichment candidate limits.

    Date-window searches retain the wider current-develop pool because the
    timestamp constraint is applied after retrieval. The normal vector path
    also remains at least four times wide through closet enrichment. Union
    mode keeps only the requested top vector candidates before lexical merge.
    """
    base_query_limit = _candidate_pool_size(
        n_results,
        date_window_active,
    )

    if candidate_strategy == "union":
        return base_query_limit, n_results

    widened = max(
        base_query_limit,
        n_results * _CLOSET_RESULT_POOL_MULTIPLIER,
    )
    return widened, widened


def _enrich_closet_hits(
    hits: list,
    drawers_col,
    query: str,
    stop_words: frozenset = frozenset(),
) -> list:
    """Hydrate closet-boosted hits and memoise each source/group fetch."""
    query_terms = set(
        _tokenize(
            query,
            stop_words,
        )
    )
    source_cache: dict = {}

    for hit in hits:
        if hit.get("matched_via") == "drawer":
            continue

        full_source = hit.get("_source_file_full") or ""

        if not full_source:
            continue

        parent_drawer_id = hit.get("_parent_drawer_id") or None
        cache_key = (
            full_source,
            parent_drawer_id,
        )

        if cache_key not in source_cache:
            try:
                source_drawers = drawers_col.get(
                    where=(
                        _scoped_source_filter(
                            full_source,
                            parent_drawer_id,
                        )
                    ),
                    include=[
                        "documents",
                        "metadatas",
                    ],
                )
            except Exception:
                logger.debug(
                    "Neighbor fetch failed for %s",
                    full_source,
                    exc_info=True,
                )
                source_cache[cache_key] = None
            else:
                source_cache[cache_key] = (
                    list(
                        getattr(
                            source_drawers,
                            "documents",
                            None,
                        )
                        or []
                    ),
                    list(
                        getattr(
                            source_drawers,
                            "metadatas",
                            None,
                        )
                        or []
                    ),
                )

        cached = source_cache[cache_key]

        if cached is None:
            continue

        docs, metadatas = cached

        if len(docs) <= 1:
            continue

        indexed = []

        for index, (
            document,
            metadata,
        ) in enumerate(
            zip(
                docs,
                metadatas,
            )
        ):
            chunk_index = (
                metadata.get(
                    "chunk_index",
                    index,
                )
                if isinstance(
                    metadata,
                    dict,
                )
                else index
            )

            if not isinstance(
                chunk_index,
                int,
            ):
                chunk_index = index

            indexed.append(
                (
                    chunk_index,
                    document or "",
                )
            )

        indexed.sort(key=lambda pair: pair[0])
        ordered_docs = [document for _, document in indexed]

        best_index = 0
        best_score = -1

        for index, document in enumerate(ordered_docs):
            lowered = document.lower()
            score = sum(1 for term in query_terms if term in lowered)

            if score > best_score:
                best_score = score
                best_index = index

        start = max(
            0,
            best_index - 1,
        )
        end = min(
            len(ordered_docs),
            best_index + 2,
        )
        expanded = "\n\n".join(ordered_docs[start:end])

        if len(expanded) > _MAX_HYDRATION_CHARS:
            expanded = expanded[:_MAX_HYDRATION_CHARS] + (
                f"\n\n[...truncated. "
                f"{len(ordered_docs)} total drawers. "
                "Use mempalace_get_drawer "
                "for full content.]"
            )

        hit["text"] = expanded
        hit["drawer_index"] = best_index
        hit["total_drawers"] = len(ordered_docs)

    return hits


def _dedupe_rendered_hits(
    hits: list,
) -> list:
    """Drop repeated closet-rendered passages while preserving rank order.

    Plain drawer hits retain their historical behavior, including legitimate
    exact repeats at different source positions. A duplicate is removed only
    when either occurrence came through closet enrichment.
    """
    unique = []
    first_by_key: dict = {}

    for hit in hits:
        source = hit.get("_source_file_full") or hit.get("source_path") or hit.get("source_file")
        text = hit.get("text")

        if not source or not isinstance(
            text,
            str,
        ):
            unique.append(hit)
            continue

        key = (
            source,
            text,
        )
        previous = first_by_key.get(key)

        if previous is None:
            first_by_key[key] = hit
            unique.append(hit)
            continue

        previous_is_closet = previous.get("matched_via") == "drawer+closet"
        current_is_closet = hit.get("matched_via") == "drawer+closet"

        if previous_is_closet or current_is_closet:
            continue

        unique.append(hit)

    return unique


# Strategy dispatch — keeps search_memories' branch count under the
# project's complexity ceiling (C901 max-complexity=25). New strategies
# register here.
_CANDIDATE_MERGERS = {
    "vector": None,  # default no-op
    "union": _merge_bm25_union_candidates,
}


def _validate_candidate_strategy(strategy: str) -> None:
    """Raise ``ValueError`` for unknown strategies.

    Called eagerly at the top of ``search_memories`` so invalid values
    fail consistently regardless of whether the call routes through the
    vector path, the BM25-only fallback, or returns an early error dict.
    """
    if strategy not in _CANDIDATE_MERGERS:
        raise ValueError(
            f"candidate_strategy must be one of {tuple(_CANDIDATE_MERGERS)}, got {strategy!r}"
        )


def _apply_candidate_strategy(
    strategy: str,
    hits: list,
    drawers_col,
    query: str,
    wing: str,
    room: str,
    n_results: int,
    max_distance: float = 0.0,
    source_file: str = None,
    since_dt=None,
    before_dt=None,
) -> None:
    """Dispatch to the registered merger for ``strategy``.

    Strategy validity is assumed (``_validate_candidate_strategy`` runs
    earlier); ``"vector"`` is a no-op.
    """
    merger = _CANDIDATE_MERGERS[strategy]
    if merger is not None:
        merger(
            hits,
            drawers_col,
            query,
            wing,
            room,
            n_results,
            max_distance=max_distance,
            source_file=source_file,
            since_dt=since_dt,
            before_dt=before_dt,
        )


def _finalize_candidate_hits(
    *,
    candidate_strategy: str,
    hits: list,
    drawers_col,
    query: str,
    wing: str,
    room: str,
    n_results: int,
    max_distance: float,
    source_file: str = None,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
) -> tuple:
    try:
        _apply_candidate_strategy(
            candidate_strategy,
            hits,
            drawers_col,
            query,
            wing,
            room,
            n_results,
            max_distance=max_distance,
            source_file=source_file,
            since_dt=since_dt,
            before_dt=before_dt,
        )
    except UnsupportedCapabilityError:
        return [], _search_error_result(
            "candidate_strategy='union' requires a backend with lexical_search support",
            unsupported_capability="supports_lexical_search",
            hint=(
                "Use candidate_strategy='vector' or select a backend that supports lexical search."
            ),
        )

    ranked = _hybrid_rank(
        hits,
        query,
        metric=_metric_for_collection(drawers_col),
        stop_words=stop_words,
    )
    hits = _dedupe_rendered_hits(ranked)[:n_results]

    for hit in hits:
        hit.pop("_sort_key", None)
        hit.pop("_source_file_full", None)
        hit.pop("_chunk_index", None)
        hit.pop("_parent_drawer_id", None)

    return hits, None


def _search_error_result(error: str, **extra) -> dict:
    """Error envelope for programmatic search callers.

    Always includes ``results: []`` so callers can safely index
    ``result["results"]`` without a KeyError when the palace failed to
    open or the query raised mid-flight (Windows CI flake surface).
    """
    out = {"error": error, "results": []}
    out.update(extra)
    return out


def _backend_mismatch_result(error: BackendMismatchError) -> dict:
    return _search_error_result(
        "Backend mismatch",
        details=str(error),
        hint="Select the matching backend or use a fresh palace directory.",
    )


def _unknown_backend_result(error: KeyError) -> dict:
    return _search_error_result(
        "Unknown backend",
        details=str(error),
        hint="Check MEMPALACE_BACKEND or the configured backend name.",
    )


def _search_result_envelope(
    *,
    query: str,
    wing,
    room,
    source_file,
    since,
    before,
    hits: list,
    candidates_fetched: int,
    pool_size: int,
    date_window_active: bool,
) -> dict:
    """Assemble the ``search_memories`` response dict.

    When a date window is active and the widened candidate pool came back
    full, drawers beyond the pool never got a chance to match the window —
    ``date_filter_pool_truncated`` flags it so a thin result under a date
    filter is never mistaken for "that's all there was".
    """
    result = {
        "query": query,
        "filters": {
            "wing": wing,
            "room": room,
            "source_file": source_file,
            "since": since,
            "before": before,
        },
        "total_before_filter": candidates_fetched,
        "results": hits,
    }
    if date_window_active and candidates_fetched >= pool_size:
        result["date_filter_pool_truncated"] = True
    return result


def _window_and_fallback_gate(
    since,
    before,
    vector_disabled: bool,
    *,
    query: str,
    palace_path: str,
    wing,
    room,
    n_results: int,
    collection_name,
    source_file,
    stop_words: frozenset = frozenset(),
):
    """Front gate for ``search_memories``: parse the window, route the fallback.

    Returns ``(since_dt, before_dt, active, short_circuit)``.
    ``short_circuit`` is a complete response to return verbatim — the
    ``{"error": ...}`` payload for an invalid/inverted window, or the
    BM25-only fallback result when ``vector_disabled`` is set — and ``None``
    when the vector path should proceed. Extracted so the window plumbing
    doesn't push ``search_memories`` over the C901 complexity ceiling.
    """
    try:
        since_dt, before_dt = parse_window(since, before)
    except ValueError as e:
        return None, None, False, {"error": str(e)}
    active = since_dt is not None or before_dt is not None
    if vector_disabled:
        return (
            since_dt,
            before_dt,
            active,
            _vector_disabled_with_window(
                query=query,
                palace_path=palace_path,
                wing=wing,
                room=room,
                n_results=n_results,
                collection_name=collection_name,
                source_file=source_file,
                since=since,
                before=before,
                since_dt=since_dt,
                before_dt=before_dt,
                stop_words=stop_words,
            ),
        )
    return since_dt, before_dt, active, None


def _candidate_out_of_scope(dist, meta, max_distance, since_dt, before_dt) -> bool:
    """True when a drawer candidate fails the distance or date-window gate.

    Distance is checked on the raw value before rounding to avoid precision
    loss (pre-existing behavior); the date window applies whenever a bound
    is set, with the shared ``[since, before)`` semantics.
    """
    if max_distance > 0.0 and dist > max_distance:
        return True
    if (since_dt is not None or before_dt is not None) and not filed_at_in_window(
        meta.get("filed_at"), since_dt, before_dt
    ):
        return True
    return False


def _vector_disabled_with_window(
    *,
    query: str,
    palace_path: str,
    wing: str,
    room: str,
    n_results: int,
    collection_name: str,
    source_file: str,
    since: str,
    before: str,
    since_dt,
    before_dt,
    stop_words: frozenset = frozenset(),
) -> dict:
    """Run the BM25-only route and echo the raw window strings.

    The fallback helper takes parsed bounds; the caller's raw ``since``/
    ``before`` strings are stitched into the ``filters`` envelope here so
    both search paths report the same shape.
    """
    result = _vector_disabled_search(
        query=query,
        palace_path=palace_path,
        wing=wing,
        room=room,
        n_results=n_results,
        collection_name=collection_name,
        source_file=source_file,
        since_dt=since_dt,
        before_dt=before_dt,
        stop_words=stop_words,
    )
    if "filters" in result:
        result["filters"]["since"] = since
        result["filters"]["before"] = before
    return result


def _vector_disabled_search(
    *,
    query: str,
    palace_path: str,
    wing: str,
    room: str,
    n_results: int,
    collection_name: str,
    source_file: str = None,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
) -> dict:
    try:
        backend_name = resolve_backend_name(palace_path)
    except BackendMismatchError as e:
        return _backend_mismatch_result(e)
    except KeyError as e:
        return _unknown_backend_result(e)
    if backend_name != "chroma":
        return _search_error_result(
            "vector_disabled fallback is Chroma-only",
            unsupported_capability="chroma_hnsw_fallback",
            backend=backend_name,
            hint="Disable vector_disabled for non-Chroma backends.",
        )
    return _bm25_only_via_sqlite(
        query,
        palace_path,
        wing=wing,
        room=room,
        source_file=source_file,
        n_results=n_results,
        collection_name=collection_name,
        stop_words=stop_words,
        since_dt=since_dt,
        before_dt=before_dt,
    )


def _open_search_collection(palace_path: str, collection_name: str):
    try:
        return get_collection(palace_path, collection_name=collection_name, create=False), None
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
) -> dict:
    """Programmatic search — returns a dict instead of printing.

    Used by the MCP server and other callers that need data.

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
        return short_circuit

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
        closets_col = get_closets_collection(palace_path, create=False)
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
            "created_at": meta.get("filed_at", "unknown"),
            "authored_at": meta.get("authored_at", meta.get("filed_at", "unknown")),
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

    return _search_result_envelope(
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


def render_with_line_numbers(text: "str | None", start_line: int = 1) -> str:
    """Prefix each line of ``text`` with ``[N] `` for read-time grid display.

    Lines that already begin with ``[<digits>]`` pass through unchanged,
    but the counter still advances on them so callers can rely on positional
    alignment with the original line indices.

    ``None`` is treated as empty string. Pure function.
    """
    if not text:
        return ""
    out = []
    for i, line in enumerate(text.split("\n"), start=start_line):
        if _ALREADY_NUMBERED_RE.match(line):
            out.append(line)
        else:
            out.append(f"[{i}] {line}")
    return "\n".join(out)


def extract_line_range(text: str, line_start: int, line_end: int) -> str:
    """Return the 1-indexed inclusive slice ``[line_start, line_end]`` rendered with line numbers.

    This is the closet-pointer read path. A pointer like ``→2026-01-18:L55-L72``
    resolves by opening the day-drawer and calling ``extract_line_range(drawer_text, 55, 72)``.
    Out-of-bounds ranges are clamped. Invalid ranges return ``""``.
    """
    if not text:
        return ""
    if line_end < line_start:
        return ""

    lines = text.split("\n")
    effective_start = max(1, line_start)
    effective_end = min(len(lines), line_end)

    if effective_start > effective_end:
        return ""

    section = "\n".join(lines[effective_start - 1 : effective_end])
    return render_with_line_numbers(section, start_line=effective_start)
