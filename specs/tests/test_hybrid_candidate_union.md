# Behavior Spec: `candidate_strategy="union"` for memory search

This document specifies the externally observable behavior asserted by the test
suite for the `candidate_strategy` parameter of the memory search operation and
for the hybrid rerank's tolerance of missing vector distances
(`tests/test_hybrid_candidate_union.py:L1-L14`).

## Surface under test

Two operations are exercised:

- `search_memories(query, palace_path, n_results=..., candidate_strategy=..., vector_disabled=..., max_distance=...)` — the search entry point (`tests/test_hybrid_candidate_union.py:L16-L17`, `tests/test_hybrid_candidate_union.py:L65-L68`).
- `_hybrid_rank(results, query)` — the rerank function that scores candidates (`tests/test_hybrid_candidate_union.py:L222-L230`).

A collection/corpus is populated through `get_collection(palace_path, create=True)` followed by `upsert(ids=..., documents=..., metadatas=...)` (`tests/test_hybrid_candidate_union.py:L30-L50`).

## Result shape

`search_memories` returns an object whose `results` field is a list of hits (`tests/test_hybrid_candidate_union.py:L69-L70`, `tests/test_hybrid_candidate_union.py:L104`). Each hit carries at least:

- `source_file` — the metadata value supplied at upsert time, e.g. `"ticket_D1.md"`, `"brand_voice_D4.md"` (`tests/test_hybrid_candidate_union.py:L44-L49`, `tests/test_hybrid_candidate_union.py:L69-L70`).
- `distance` — a numeric vector distance, which MAY be absent/`None` for BM25-only candidates (`tests/test_hybrid_candidate_union.py:L166-L171`).

Reading `results` defensively yields an empty list when no `results` field is present (`tests/test_hybrid_candidate_union.py:L104`).

Each upserted document has metadata `wing`, `room`, and `source_file` (`tests/test_hybrid_candidate_union.py:L44-L49`, `tests/test_hybrid_candidate_union.py:L194-L198`).

## `candidate_strategy` values and routing

### `"vector"` is the default and is identity-equivalent to omission

Calling search with no `candidate_strategy` argument and calling it with `candidate_strategy="vector"` MUST produce the same ordered list of `source_file` values for the same query, palace, and `n_results` (`tests/test_hybrid_candidate_union.py:L61-L71`). The default strategy gathers candidates from the vector index only (`tests/test_hybrid_candidate_union.py:L3-L6`).

### `"union"` merges BM25-only candidates into the rerank pool

Under `candidate_strategy="union"`, candidates from the vector index are merged with top-K BM25-only candidates drawn from the full-text (FTS) index, and the combined pool is reranked (`tests/test_hybrid_candidate_union.py:L8-L10`, `tests/test_hybrid_candidate_union.py:L73-L83`). A document that has strong BM25 signal for the query but is vector-distant MUST be retrievable in union mode, even when vector-only mode misses it (`tests/test_hybrid_candidate_union.py:L73-L83`).

Union mode MUST be additive: it MUST NOT drop any document that vector-only mode returns for the same query and `n_results`. Formally, the set of `source_file` values from vector mode minus the set from union mode MUST be empty (`tests/test_hybrid_candidate_union.py:L85-L97`).

### Invalid values are rejected

Any `candidate_strategy` value other than the supported ones (e.g. `"bogus"`) MUST raise a `ValueError` whose message references `candidate_strategy`, rather than silently falling back to a default (`tests/test_hybrid_candidate_union.py:L106-L113`).

This validation MUST occur before any `vector_disabled` early-return routing: passing `vector_disabled=True` together with an invalid `candidate_strategy` MUST still raise the same `ValueError` referencing `candidate_strategy` (`tests/test_hybrid_candidate_union.py:L115-L129`).

## Invariants

### Empty palace

When the collection has no drawers, union mode MUST return an empty `results` list rather than erroring (`tests/test_hybrid_candidate_union.py:L99-L104`).

### Result count cap

The final result list MUST be trimmed to at most `n_results`, even when the merged union candidate pool is larger than `n_results`. With `n_results=2`, the returned list length MUST be `<= 2` (`tests/test_hybrid_candidate_union.py:L131-L142`).

### `max_distance` excludes BM25-only candidates

`max_distance` is a vector-distance threshold. When `max_distance` is set, union mode MUST NOT inject BM25-only candidates (which have `distance=None`). Every returned hit MUST have a non-`None` `distance`, and that distance MUST be `<= max_distance` (e.g. with `max_distance=0.5`, every hit's `distance <= 0.5`) (`tests/test_hybrid_candidate_union.py:L144-L171`). Without `max_distance`, the same union query surfaces the BM25-strong document (`tests/test_hybrid_candidate_union.py:L151-L155`).

### Deduplication is chunk/full-path precise, not basename

Union deduplication MUST key on the full path (or a chunk-level key), not the file basename. Two documents whose `source_file` share the same basename but reside in different directories MUST NOT collide; both MUST be able to surface in results (`tests/test_hybrid_candidate_union.py:L173-L214`). In the test, two entries with `source_file` `"alpha/README.md"` and `"beta/README.md"` are seeded, and a union query that hits BM25 for both MUST return at least two README hits (`tests/test_hybrid_candidate_union.py:L194-L213`).

Note: the test counts hits whose `source_file == "README.md"` and asserts at least two (`tests/test_hybrid_candidate_union.py:L209-L213`), implying that hit `source_file` is reported as the basename while dedup is performed on the full path.

## `_hybrid_rank` tolerance of missing distance

`_hybrid_rank(results, query)` MUST accept candidates whose `distance` is `None`, which is required for BM25-only candidates injected by union mode (`tests/test_hybrid_candidate_union.py:L217-L219`).

Given a list mixing a candidate with a numeric `distance` and a candidate with `distance=None`, the rerank MUST NOT crash and MUST return every input candidate (the returned list length equals the input length) (`tests/test_hybrid_candidate_union.py:L221-L233`). A `distance=None` candidate is treated as having zero vector similarity, so it ranks on its BM25 signal alone (`tests/test_hybrid_candidate_union.py:L221-L229`).

The rerank MUST add a `bm25_score` field to every returned candidate (`tests/test_hybrid_candidate_union.py:L231`).

## Side effects

The search and collection operations are scoped to a per-test temporary palace directory path string (`tests/test_hybrid_candidate_union.py:L63-L64`, `tests/test_hybrid_candidate_union.py:L102`). Collection creation occurs only when `create=True` is passed to `get_collection` (`tests/test_hybrid_candidate_union.py:L30`, `tests/test_hybrid_candidate_union.py:L102`, `tests/test_hybrid_candidate_union.py:L180`). No network or other external side effects are asserted by this suite.
