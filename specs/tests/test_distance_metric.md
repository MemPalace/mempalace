# Behavior Spec: Distance-Metric-Aware Similarity (RFC 001)

This is a test suite that pins the externally observable contract for backend-declared
distance metrics and the metric-aware distance→similarity conversion used during search
ranking. The motivation is a regression: a fixed `max(0, 1 - distance)` conversion is
correct only for cosine distance; backends reporting L2 or inner-product distances (or a
legacy store built without a cosine space) were silently mis-ranked because L2 distances
routinely exceed 1.0 and floored every result's similarity to 0 (tests/test_distance_metric.py:L1-L10).

The suite exercises four units of the system under test: a backend metric declaration
contract, a per-metric `distance → similarity` function, a metric resolver for a
collection object, and a hybrid ranking function (tests/test_distance_metric.py:L17-L23).

## Contract Surface: Declared Metric Default

A backend type exposes a `distance_metric` attribute whose default value is the string
`"cosine"` (tests/test_distance_metric.py:L31-L32). A concrete collection implementing the
minimal collection interface (operations `add`, `upsert`, `query`, `get`, `delete`, and
`count` returning a count) inherits the same default of `"cosine"` for its
`distance_metric` (tests/test_distance_metric.py:L35-L46).

## `_distance_to_similarity(distance, metric)` → similarity

Converts a raw vector distance into a similarity score in the range `[0, 1]`, dispatching
on the named metric.

### Cosine metric (`"cosine"`)

- Distance `0.0` maps to similarity `1.0` (tests/test_distance_metric.py:L54-L55).
- Distance `2.0` maps to similarity `0.0` (tests/test_distance_metric.py:L56).
- Any cosine distance greater than 1 floors at `0.0` and never goes negative; e.g.
  distance `1.5` → `0.0` and distance `1.7` → `0.0`
  (tests/test_distance_metric.py:L57-L58, tests/test_distance_metric.py:L74).
  This is consistent with the legacy `max(0, 1 - distance)` shape.

### L2 metric (`"l2"`)

- Distance `0.0` maps to similarity `1.0` (tests/test_distance_metric.py:L62).
- Distance `1.0` maps to similarity approximately `0.5` (tests/test_distance_metric.py:L63).
- The conversion is strictly decreasing and bounded in the open interval `(0, 1)` for
  positive distances: for distances `5.0` (far) and `1.0` (near), the resulting
  similarities satisfy `0.0 < far < near < 1.0` (tests/test_distance_metric.py:L66-L68).
- A large L2 distance must NOT floor to `0` the way the cosine formula did; e.g. distance
  `1.7` under L2 yields a strictly positive similarity (`> 0.0`)
  (tests/test_distance_metric.py:L71-L75).

### Inner-product metric (`"ip"`)

Inner-product distance is signed and unbounded, where a lower distance means closer. The
conversion is a logistic squash producing a strictly decreasing similarity in `(0, 1)`.

- Distance `0.0` maps to similarity approximately `0.5` (tests/test_distance_metric.py:L82).
- It is monotonically decreasing: similarity at `-5.0` is greater than at `0.0`, and
  similarity at `0.0` is greater than at `5.0`
  (tests/test_distance_metric.py:L81-L83).
- A huge positive distance must not overflow or produce a non-finite value: distance
  `1e6` yields a finite similarity approximately `0.0` (within `1e-9`), and the result is
  neither infinite nor NaN — the exponent is clamped to prevent overflow
  (tests/test_distance_metric.py:L86-L90).

### Null distance and unknown metrics

- A distance of `None` (used for candidates with no vector signal, such as lexical-only
  BM25 candidates) maps to similarity `0.0` regardless of metric (cosine or l2)
  (tests/test_distance_metric.py:L93-L96).
- An unrecognized metric string (e.g. `"weird"`) or a `None` metric falls back to cosine
  behavior: the result equals the cosine conversion for the same distance
  (tests/test_distance_metric.py:L99-L101).

## `_metric_for_collection(collection)` → metric string

Resolves the effective metric for a collection object, normalizing and guarding against
failures, always returning a valid lowercase metric string (defaulting to `"cosine"`).

- Reads the collection's declared `distance_metric`; a collection declaring `"l2"`
  resolves to `"l2"` (tests/test_distance_metric.py:L109-L111).
- Normalizes case to lowercase: a declared `"L2"` resolves to `"l2"`
  (tests/test_distance_metric.py:L114-L115).
- Garbage or null declarations fall back to `"cosine"`: a declared `"nonsense"` resolves
  to `"cosine"`, and a declared `None` resolves to `"cosine"`
  (tests/test_distance_metric.py:L116-L117).
- When the attribute is absent entirely, it defaults to `"cosine"`
  (tests/test_distance_metric.py:L120-L121).
- Attribute resolution follows delegation: a wrapper object that forwards attribute access
  to an inner collection reporting `"ip"` resolves to `"ip"`
  (tests/test_distance_metric.py:L124-L134).
- If reading the `distance_metric` attribute raises an error (e.g. a backend is down), the
  resolver swallows the error and returns `"cosine"` (tests/test_distance_metric.py:L137-L143).

### Delegation through a wrapping collection (non-shadowing)

A wrapping collection over an inner collection must surface the inner collection's metric
rather than masking it with the base default. When the inner collection declares `"l2"`,
the wrapper's own `distance_metric` reports `"l2"` and the resolver also returns `"l2"`.
This is a regression guard: because the base collection defines `distance_metric` as a
resolvable property, attribute-forwarding alone would never fire and the wrapper would
otherwise report the base `"cosine"` default, masking a wrapped non-cosine backend
(tests/test_distance_metric.py:L146-L166).

## Chroma Collection Metric From Stored Metadata

A Chroma-style collection derives its `distance_metric` from the inner collection's
`metadata`, specifically the `hnsw:space` key.

- When `hnsw:space` is `"cosine"`, it reports `"cosine"`
  (tests/test_distance_metric.py:L179-L180).
- When `hnsw:space` is `"l2"` (a legacy pre-cosine store), it reports `"l2"` so the
  searcher maps distances correctly instead of flooring to 0
  (tests/test_distance_metric.py:L183-L186).
- When `hnsw:space` is absent, empty, or an unrecognized value, it reports `"l2"`. The
  rationale: a collection that never had cosine explicitly set is genuinely using the
  default L2 space, and reporting cosine would reintroduce the floor-to-0 bug. Concretely,
  metadata `{}`, `{"hnsw:space": ""}`, and `{"hnsw:space": "bogus"}` all report `"l2"`
  (tests/test_distance_metric.py:L189-L195).

## `_hybrid_rank(results, query, metric)` → ranked results

Ranks a list of candidate result records by combining a lexical term and a vector term,
where the vector term is computed via the metric-aware distance→similarity conversion.
Each candidate is a record carrying at least a `text` field and a `distance` field
(possibly `None`) (tests/test_distance_metric.py:L207-L211).

- Ranking respects the metric. Given two candidates with identical (zero) lexical overlap
  to the query so that only the vector term decides — one with `distance=None` and one
  with `distance=1.6` — under the `"l2"` metric the `distance=1.6` candidate stays
  positive and ranks first (real vector signal beats vector-unknown). The query
  `"zzzznomatch"` ensures no lexical overlap (tests/test_distance_metric.py:L203-L212).
- Cosine ranking preserves the legacy `max(0, 1 - distance)` behavior. Given candidates
  with `distance=0.1` ("near") and `distance=0.9` ("far") under the `"cosine"` metric, the
  output is ordered near first, far second (tests/test_distance_metric.py:L215-L223).
- Empty input is a no-op: ranking an empty candidate list returns an empty list
  (tests/test_distance_metric.py:L226-L227).

## Side Effects and Observability

This file is a pure unit-test module: it imports the subjects under test and asserts their
return values; it performs no filesystem, network, process, or environment side effects
(tests/test_distance_metric.py:L12-L23). All contracts above are observable purely through
return values of the four named units.
