# Spec: tests/_backend_conformance.py

## Purpose

This module provides shared, reusable assertion helpers that verify the storage-backend isolation contract (RFC 001). It ships assertions, not test cases, and is intentionally NOT named `test_*` so that the test collector does not pick it up directly; instead, backend-specific test modules import and call its helpers (tests/_backend_conformance.py:L1-L12).

The isolation guarantees it proves are: per-partition-id isolation (required of every backend) and per-namespace isolation (required only of backends advertising the `supports_namespace_isolation` capability) (tests/_backend_conformance.py:L5-L9).

## Module Constants (probe fixtures)

A single probe record is used for all isolation checks. Its identity is the fixed string `conformance-isolation-probe`, its document text is `partition isolation probe document`, and its default embedding vector is the 4-dimensional vector `[1.0, 0.0, 0.0, 0.0]` (tests/_backend_conformance.py:L14-L16).

## Public Surface

### `assert_partition_isolation(backend, writer, other, *, embedding=None)`

Asserts that two collection handles, `writer` and `other`, are isolated partitions of the same `backend` — meaning a record written through `writer` is invisible and immutable through `other`, and survives in `writer` even after `other` attempts to delete it (tests/_backend_conformance.py:L19-L31).

Parameters:
- `backend` — the backend under test; its `capabilities` collection is inspected (tests/_backend_conformance.py:L32).
- `writer` — the partition the probe record is written into (tests/_backend_conformance.py:L44).
- `other` — the partition that must NOT see, modify, or delete the probe record (tests/_backend_conformance.py:L35,L47-L61).
- `embedding` (keyword-only, optional) — an explicit vector to use instead of the default probe embedding; when `None`, the default `[1.0, 0.0, 0.0, 0.0]` is used (tests/_backend_conformance.py:L19,L33).

Returns nothing; it communicates exclusively through assertion failures (tests/_backend_conformance.py:L51-L61).

## Behavior and Ordering

1. **Capability detection.** The helper decides whether the backend requires explicit vectors by checking whether the string `requires_explicit_embeddings` is present in `backend.capabilities`. This single flag (`explicit`) drives both the add and query paths so the same assertion works for text-embedding backends (e.g. Chroma) and explicit-vector backends (e.g. qdrant, sqlite_exact) (tests/_backend_conformance.py:L28-L33).

2. **Vector resolution.** A working vector is computed as a copy of the supplied `embedding`, or the default probe embedding when none is given (tests/_backend_conformance.py:L33).

3. **Baseline capture.** Before writing, the current record count of `other` is captured as `baseline_other`, so the post-write count can be compared against the pre-write state rather than against zero (tests/_backend_conformance.py:L35).

4. **Write to `writer`.** The probe record is added to `writer` with: id list `[conformance-isolation-probe]`, document list `[partition isolation probe document]`, and metadata list `[{"wing": "conformance"}]`. If the backend is explicit-vector, an `embeddings` list `[vector]` is also included in the add call; otherwise no embedding is passed (tests/_backend_conformance.py:L37-L44).

5. **Query isolation check.** A query is issued against `other` requesting up to 10 results. For explicit-vector backends the query is by embedding (`query_embeddings=[vector]`); for text-embedding backends it is by text (`query_texts=[probe document]`). The result's first hit list is examined (the first element of `leaked.ids`, or an empty list when `leaked.ids` is falsy). The probe id MUST NOT appear among those hit ids, else the failure message is `query() leaked a record across the isolation boundary` (tests/_backend_conformance.py:L46-L51).

6. **Get isolation check.** A `get` against `other` for the probe id MUST return an empty id list (`[]`), else the failure message is `get() leaked a record across the isolation boundary` (tests/_backend_conformance.py:L53-L55).

7. **Count isolation check.** The count of `other` after the write MUST equal the pre-write `baseline_other`, else the failure message is `count() leaked a record across the isolation boundary` (tests/_backend_conformance.py:L56).

8. **Delete isolation check.** A `delete` for the probe id is issued against `other`. Afterwards a `get` against `writer` for the probe id MUST return an id list exactly equal to `[conformance-isolation-probe]`, proving the cross-partition delete did not touch the writer's record; otherwise the failure message is `delete() crossed the isolation boundary` (tests/_backend_conformance.py:L58-L61).

## Externally Observable Contracts (collection-handle interface)

The helper assumes each partition handle (`writer`, `other`) exposes the following operations with these shapes:
- `add(ids=[...], documents=[...], metadatas=[...], [embeddings=[...]])` — write records; `embeddings` supplied only for explicit-vector backends (tests/_backend_conformance.py:L37-L44).
- `query(query_embeddings=[...] | query_texts=[...], n_results=N)` returning an object with an `ids` attribute that is a list-of-lists (one inner list per query) (tests/_backend_conformance.py:L46-L50).
- `get(ids=[...])` returning an object with an `ids` attribute that is a flat list (tests/_backend_conformance.py:L53,L60-L61).
- `count()` returning an integer record count (tests/_backend_conformance.py:L35,L56).
- `delete(ids=[...])` removing records by id from that partition only (tests/_backend_conformance.py:L59).
- `backend.capabilities` — a membership-testable collection of capability strings (tests/_backend_conformance.py:L32).

## Side Effects

Calling the helper mutates the backend: it adds the probe record to `writer` and issues a delete against `other`. The probe record remains in `writer` after the call returns (it is asserted to survive); the helper does not clean up the written record itself (tests/_backend_conformance.py:L44,L58-L61).
