# Spec: tests/benchmarks/test_recall_threshold.py

## Purpose

A benchmark test module that measures the recall ceiling of the search subsystem when all drawers are concentrated into a single wing+room bucket. The intent is to isolate the embedding model's retrieval limit, since room-filtering cannot help when every drawer shares the same room (tests/benchmarks/test_recall_threshold.py:L1-L7, L104-L109). It is a benchmark, not a pass/fail correctness test: it records metrics rather than asserting thresholds.

## Constants / Fixtures

### NEEDLE_TOPICS

An ordered list of exactly 10 distinct technical sentences used as the verbatim "needle" content planted into the palace (tests/benchmarks/test_recall_threshold.py:L20-L31). Each entry is a unique factual statement (e.g. "Fibonacci sequence optimization uses memoization with O(n) space complexity").

### NEEDLE_QUERIES

An ordered list of exactly 10 query strings, positionally aligned with `NEEDLE_TOPICS` (index `i` query targets the index `i` topic) (tests/benchmarks/test_recall_threshold.py:L33-L44). Each query is a short keyword phrase derived from the corresponding topic.

The positional alignment between `NEEDLE_TOPICS` and `NEEDLE_QUERIES` is a load-bearing invariant: the test for query `i` checks whether needle `i` (the topic at the same index) was retrieved (tests/benchmarks/test_recall_threshold.py:L125-L140).

## Helper: _populate_single_room(palace_path, n_drawers, n_needles=10)

Populates a persistent palace store at `palace_path` such that all drawers — both needles and noise — live in the same wing `"concentrated"` and room `"single_room"` (tests/benchmarks/test_recall_threshold.py:L47-L100).

### Inputs

- `palace_path` (string): filesystem directory where the persistent store is created. The directory is created if absent (tests/benchmarks/test_recall_threshold.py:L50).
- `n_drawers` (integer): total number of drawers to populate (needles + noise) (tests/benchmarks/test_recall_threshold.py:L47, L76).
- `n_needles` (integer, default 10): number of needle drawers to plant (tests/benchmarks/test_recall_threshold.py:L47, L58).

### Behavior

1. A data generator is seeded deterministically with seed `42` at scale `"small"`; this makes noise content reproducible across runs (tests/benchmarks/test_recall_threshold.py:L49).
2. A persistent store is opened at `palace_path` and a collection named `"mempalace_drawers"` is obtained or created (tests/benchmarks/test_recall_threshold.py:L51-L52).
3. **Needles**: For each `i` in `0..n_needles-1`, one drawer is created (tests/benchmarks/test_recall_threshold.py:L58-L73):
   - A needle identifier string `NEEDLE_{i:04d}` (zero-padded to 4 digits, e.g. `NEEDLE_0000`) (tests/benchmarks/test_recall_threshold.py:L59).
   - Document text is `"{needle_id}: {NEEDLE_TOPICS[i]}. Unique planted needle for threshold test."` — the needle id appears verbatim at the start of the stored text (tests/benchmarks/test_recall_threshold.py:L60).
   - Drawer id is `drawer_single_room_` followed by the first 16 hex characters of the MD5 hash of the needle id string (tests/benchmarks/test_recall_threshold.py:L61).
   - Metadata: `wing="concentrated"`, `room="single_room"`, `source_file="needle_{i}.txt"`, `chunk_index=0`, `added_by="threshold_bench"`, `filed_at` = current local time in ISO-8601 format (tests/benchmarks/test_recall_threshold.py:L64-L73).
4. **Noise**: `remaining = n_drawers - n_needles` noise drawers are created (tests/benchmarks/test_recall_threshold.py:L76-L91):
   - Document text is random text of length between 400 and 800 (generator-defined units) (tests/benchmarks/test_recall_threshold.py:L78).
   - Drawer id is `drawer_single_room_` followed by the first 16 hex characters of the MD5 hash of the string `noise_{i}` (tests/benchmarks/test_recall_threshold.py:L79).
   - Metadata: `wing="concentrated"`, `room="single_room"`, `source_file="noise_{i:06d}.txt"` (6-digit zero-padded), `chunk_index = i % 10`, `added_by="threshold_bench"`, `filed_at` = current ISO-8601 timestamp (tests/benchmarks/test_recall_threshold.py:L82-L90).
5. Drawers are written in batches: whenever the pending buffer reaches `batch_size = 500` documents it is flushed to the collection, then any trailing remainder is flushed at the end (tests/benchmarks/test_recall_threshold.py:L54, L93-L98).

### Edge cases / invariants

- Needles are always planted first; if `n_drawers < n_needles`, `remaining` is negative and no noise loop iterations occur (tests/benchmarks/test_recall_threshold.py:L76-L77).
- Batch flushing only triggers inside the noise loop; needle-only buffers under 500 are flushed by the trailing flush (tests/benchmarks/test_recall_threshold.py:L93-L98).
- Drawer ids are content-derived hashes; identical content strings would collide to the same id (tests/benchmarks/test_recall_threshold.py:L61, L79).

### Return value

Returns a tuple `(client, col)` — the persistent store handle and the drawer collection (tests/benchmarks/test_recall_threshold.py:L100). The callers in this module ignore the return value (tests/benchmarks/test_recall_threshold.py:L117, L157).

## Test class: TestRecallThresholdSingleRoom

Marked as a benchmark test group (tests/benchmarks/test_recall_threshold.py:L103-L104). Declares the parameterized sizes `SIZES = [250, 500, 1000, 2000, 3000, 5000]` used by both test methods (tests/benchmarks/test_recall_threshold.py:L111).

### test_single_room_recall(n_drawers, tmp_path)

Parameterized over each value in `SIZES` (tests/benchmarks/test_recall_threshold.py:L113-L114).

1. Builds a palace under a temporary directory subpath `palace` with `n_drawers` total and 10 needles (tests/benchmarks/test_recall_threshold.py:L116-L117).
2. For each query `i` in `NEEDLE_QUERIES`, performs a search filtered to `wing="concentrated"`, `room="single_room"`, requesting `n_results=10` (tests/benchmarks/test_recall_threshold.py:L125-L132).
3. If a search returns an object containing the key `error`, that query is skipped (counts as a miss for both recall buckets) (tests/benchmarks/test_recall_threshold.py:L133-L134).
4. Otherwise it extracts the `text` field of each entry in the result's `results` list (tests/benchmarks/test_recall_threshold.py:L136). The result contract consumed here is: a mapping with a `results` key holding a list of hits, each hit a mapping with a `text` key (tests/benchmarks/test_recall_threshold.py:L136).
5. A query `i` is a hit-at-5 if the substring `NEEDLE_{i:04d}` appears in any of the first 5 returned texts, and hit-at-10 if it appears in any of the first 10 (tests/benchmarks/test_recall_threshold.py:L137-L145).
6. Recall@5 and Recall@10 are computed as hits divided by the number of queries (10) (tests/benchmarks/test_recall_threshold.py:L147-L148).
7. Two metrics are recorded under group `"single_room_recall"`: key `recall_at_5_at_{n_drawers}` and key `recall_at_10_at_{n_drawers}`, each rounded to 3 decimal places (tests/benchmarks/test_recall_threshold.py:L150-L151).

### test_single_room_no_filter_recall(n_drawers, tmp_path)

Identical in structure to `test_single_room_recall` but performs the search WITHOUT any wing/room filter (only `palace_path` and `n_results=10`) — a pure unfiltered search (tests/benchmarks/test_recall_threshold.py:L153-L166). The same per-query error-skip, substring hit detection, and recall computation apply (tests/benchmarks/test_recall_threshold.py:L167-L179). Metrics are recorded under group `"single_room_unfiltered"` with keys `recall_at_5_at_{n_drawers}` and `recall_at_10_at_{n_drawers}`, rounded to 3 decimals (tests/benchmarks/test_recall_threshold.py:L181-L182).

## Observable contracts (cross-module dependencies)

- `search_memories(query, palace_path, [wing], [room], n_results)` returns either a mapping with an `error` key on failure, or a mapping with a `results` list of hit mappings each carrying a `text` string (tests/benchmarks/test_recall_threshold.py:L126-L136, L166-L170).
- `record_metric(group, key, value)` records a single named numeric metric into a benchmark report (tests/benchmarks/test_recall_threshold.py:L17, L150-L151, L181-L182).
- `PalaceDataGenerator(seed, scale)._random_text(min_len, max_len)` produces deterministic random filler text given a fixed seed (tests/benchmarks/test_recall_threshold.py:L16, L49, L78).

## Side effects

- Creates a directory at `palace_path` and writes a persistent drawer store there (tests/benchmarks/test_recall_threshold.py:L50-L52, L94, L98). Each test uses an isolated temporary directory, so runs do not interfere (tests/benchmarks/test_recall_threshold.py:L116, L156).
- Reads the system clock for `filed_at` timestamps; this value is non-deterministic across runs (tests/benchmarks/test_recall_threshold.py:L71, L89).
- Emits benchmark metrics via `record_metric`; no assertions, so the tests do not fail on low recall (tests/benchmarks/test_recall_threshold.py:L150-L151, L181-L182).
