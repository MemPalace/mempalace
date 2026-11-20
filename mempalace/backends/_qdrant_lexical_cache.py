"""Row cache for ``QdrantCollection.lexical_search``.

``lexical_search`` used to scroll every point matching the Qdrant text filter *with payload* and
re-tokenize every document on every call. For a collection of a few thousand short documents
(e.g. closets) that is ~100 ms of REST transfer, JSON decoding and tokenization per search, all of
it repeated for rows that did not change.

This module keeps, per (Qdrant URL, remote collection), the rows seen so far in pre-tokenized form.
A search still asks Qdrant which points match the filter -- but without payload -- and only fetches
the rows it has not cached yet. BM25 is computed over the cached rows with the same formula and the
same summation order as ``lexical.bm25_scores`` so scores are bit-identical.

Validity:

* Membership always comes from Qdrant, so new points show up and deleted points vanish at once.
* Every write that goes through ``_QdrantRESTClient`` (upsert, delete, delete_collection) drops the
  affected rows and bumps a generation; a search that loaded rows before a write never stores them.
* A write by *another process* that replaces a document under the same id is picked up when the
  cache epoch expires (the TTL below).
* ``MAX_ROWS`` bounds the size of the cache.

The cache is **opt-in**. It is off unless ``MEMPALACE_QDRANT_LEXICAL_CACHE_TTL_S`` is set to a
positive number of seconds (e.g. ``300``); unset, ``0``, negative or unparsable values keep the
original full-scroll behavior of ``lexical_search``.
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Any, Optional

ENV_TTL = "MEMPALACE_QDRANT_LEXICAL_CACHE_TTL_S"
DEFAULT_TTL_S = 0.0  # opt-in: 0 = cache off
MAX_ROWS = 50_000
_K1 = 1.5
_B = 0.75


def ttl_seconds() -> float:
    """Cache epoch length in seconds; ``0`` (the default) means the cache is off."""
    try:
        ttl = float(os.environ.get(ENV_TTL, DEFAULT_TTL_S))
    except ValueError:
        return DEFAULT_TTL_S
    return ttl if ttl > 0 else 0.0  # also maps NaN and negative values to "off"


class Row:
    """One document, tokenized once. ``terms``: token -> (frequency, position of first occurrence)."""

    __slots__ = ("id", "document", "metadata", "length", "terms")

    def __init__(self, row: dict, tokens: list[str]) -> None:
        self.id = row["id"]
        self.document = row["document"]
        self.metadata = row["metadata"]
        self.length = len(tokens)
        terms: dict[str, tuple[int, int]] = {}
        for pos, token in enumerate(tokens):
            old = terms.get(token)
            terms[token] = (1, pos) if old is None else (old[0] + 1, old[1])
        self.terms = terms


class _Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: dict[str, Row] = {}
        self.generation = 0
        self.epoch_start = 0.0


_caches: dict[tuple[str, str], _Cache] = {}
_caches_lock = threading.Lock()


def _cache_for(url: str, collection: str) -> _Cache:
    with _caches_lock:
        return _caches.setdefault((str(url), str(collection)), _Cache())


def invalidate(url: str, collection: str, point_ids: Optional[list[str]] = None) -> None:
    """Drop the given points (``None`` = all) and make in-flight searches discard what they loaded."""
    cache = _cache_for(url, collection)
    with cache.lock:
        cache.generation += 1
        if point_ids is None:
            cache.rows.clear()
        else:
            for point_id in point_ids:
                cache.rows.pop(str(point_id), None)


def rows_for(url: str, collection: str, point_ids: list[str], load: Any) -> list[Row]:
    """Rows for ``point_ids`` in that order. ``load(missing_ids) -> {point_id: Row}`` fetches the rest."""
    cache = _cache_for(url, collection)
    now = time.monotonic()
    ttl = ttl_seconds()
    with cache.lock:
        if cache.rows and now - cache.epoch_start > ttl:
            cache.generation += 1
            cache.rows.clear()
        generation = cache.generation
        have = {pid: cache.rows[pid] for pid in point_ids if pid in cache.rows}
    missing = [pid for pid in dict.fromkeys(point_ids) if pid not in have]
    if missing:
        fresh = load(missing)
        have.update(fresh)
        with cache.lock:
            if cache.generation == generation and len(cache.rows) + len(fresh) <= MAX_ROWS:
                if not cache.rows:
                    cache.epoch_start = time.monotonic()
                cache.rows.update(fresh)
    # A point deleted between the id listing and the load is simply absent, as in a fresh scroll.
    return [have[pid] for pid in point_ids if pid in have]


def bm25_scores(query_terms: set[str], rows: list[Row]) -> list[float]:
    """``lexical.bm25_scores`` over pre-counted rows: same formula, same summation order."""
    n_docs = len(rows)
    if not query_terms or n_docs == 0:
        return [0.0] * n_docs
    lengths = [row.length for row in rows]
    if not any(lengths):
        return [0.0] * n_docs
    avgdl = sum(lengths) / n_docs or 1.0
    df = dict.fromkeys(query_terms, 0)
    for row in rows:
        for term in query_terms:
            if term in row.terms:
                df[term] += 1
    idf = {t: math.log((n_docs - df[t] + 0.5) / (df[t] + 0.5) + 1.0) for t in query_terms}
    scores = []
    for row in rows:
        if row.length == 0:
            scores.append(0.0)
            continue
        # The original sums in order of first occurrence in the document.
        hits = sorted((row.terms[t][1], t, row.terms[t][0]) for t in query_terms if t in row.terms)
        score = 0.0
        for _pos, term, freq in hits:
            num = freq * (_K1 + 1)
            den = freq + _K1 * (1 - _B + _B * row.length / avgdl)
            score += idf[term] * num / den
        scores.append(score)
    return scores
