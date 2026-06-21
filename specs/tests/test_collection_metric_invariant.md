# Spec: Collection Metric Invariant Tests

## Purpose

This is a behavioral invariant test suite that locks the contract that **every** storage-collection-creation path configures its vector index to use the **cosine** distance metric (tests/test_collection_metric_invariant.py:L1-L13). The rationale captured in the suite: the default index distance for the backend is Euclidean (L2); under L2, the searcher's `max(0, 1 - distance)` similarity formula systematically floors to 0 because L2 distances on normalized 384-dimension vectors routinely exceed 1.0, causing every search result to show a similarity of 0.0 with no signal that the palace is broken (tests/test_collection_metric_invariant.py:L4-L8). The suite exists to catch any future refactor that drops the cosine-metric configuration from any creation path at test time rather than letting search quality silently degrade (tests/test_collection_metric_invariant.py:L10-L12).

## Constants and Observable Contract

- The required metric value is the string `cosine` (tests/test_collection_metric_invariant.py:L19).
- A correctly created collection must expose metadata in which the key `hnsw:space` maps to the value `cosine` (tests/test_collection_metric_invariant.py:L25-L29). This `hnsw:space` metadata key is the externally observable contract that distinguishes a healthy palace from a broken one.

## Shared Assertion Behavior

A helper performs the cosine assertion against any collection object together with a context label describing which path produced it (tests/test_collection_metric_invariant.py:L22-L29):

- It resolves a metadata dictionary from the collection: if the collection exposes a `metadata` attribute that is used directly, otherwise it falls back to the metadata of an underlying inner collection (tests/test_collection_metric_invariant.py:L23).
- It requires the resolved metadata to be a dictionary; otherwise the check fails with a message naming the context label and the unexpected value (tests/test_collection_metric_invariant.py:L24).
- It requires `metadata["hnsw:space"]` to equal `cosine`; otherwise the check fails with a message naming the context label, the observed metadata, and a warning that a non-cosine collection silently breaks the searcher's similarity formula (tests/test_collection_metric_invariant.py:L25-L29).

## Covered Creation Paths (each a separate test case)

Each test below uses an isolated temporary directory as the palace storage location, expressed as a string path, and the collection name `mempalace_drawers` unless otherwise noted.

1. **Legacy get-or-create path**: Creating a backend instance and calling its `get_or_create_collection(path, "mempalace_drawers")` must return a cosine collection (tests/test_collection_metric_invariant.py:L32-L35).

2. **Legacy create path**: Creating a backend instance and calling its `create_collection(path, "mempalace_drawers")` must return a cosine collection (tests/test_collection_metric_invariant.py:L38-L41).

3. **Typed get-collection with create**: Calling the backend's `get_collection(path, "mempalace_drawers", create=True)` — the path used by the miner and init flow — must return a cosine collection (tests/test_collection_metric_invariant.py:L44-L49).

4. **Public module-level get-collection**: Calling the public `get_collection(path, "mempalace_drawers", create=True)` (the entry point most callers use) must return a cosine collection (tests/test_collection_metric_invariant.py:L52-L56).

5. **Reopen preserves metric**: After one backend instance creates the collection via `create_collection(path, "mempalace_drawers")`, a separate freshly-constructed backend instance (simulating a process restart) opening the same path via `get_collection(path, "mempalace_drawers", create=False)` must still expose the cosine metadata. This guards against a regression where reopening an existing palace drops or overwrites its metadata (tests/test_collection_metric_invariant.py:L59-L68).

6. **Full-stack new palace, two collections**: Building a palace through the public `get_collection(..., create=True)` API the way a new user would must yield a cosine collection for `mempalace_drawers`, and additionally a `mempalace_closets` collection created via the same API must also be cosine (tests/test_collection_metric_invariant.py:L71-L87).

## Invariants

- Across all creation entry points (legacy get-or-create, legacy create, typed get-collection-with-create, and the public module function), the resulting collection's distance metric is always cosine (tests/test_collection_metric_invariant.py:L32-L56).
- The cosine metric is durable across process restarts: a collection created in one process and reopened (without recreation) in another still reports cosine (tests/test_collection_metric_invariant.py:L59-L68).
- The invariant applies independently to each named collection in a palace, including both `mempalace_drawers` and `mempalace_closets` (tests/test_collection_metric_invariant.py:L82-L87).

## Side Effects

- Each test materializes a persistent palace on disk under a temporary directory; storage is persisted such that a second backend instance can reopen it (tests/test_collection_metric_invariant.py:L63-L67). The suite intentionally relies on session-end temporary-directory cleanup rather than mid-test release of persistent storage file handles, to avoid file-lock contention on systems that lock open storage files (tests/test_collection_metric_invariant.py:L74-L81).
