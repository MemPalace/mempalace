# Behavior Specification: `tests/benchmarks/test_layers_bench.py`

A performance benchmark suite for the memory wake-up stack (`layers.py`). It measures
latency, memory growth (RSS), and token-budget compliance of the layered memory
system (`MemoryStack`, `Layer1`, `Layer2`, `Layer3`) across palaces of varying sizes
(tests/benchmarks/test_layers_bench.py:L1-L6). It produces no assertions about
business correctness beyond a few smoke checks and token-budget caps; its primary
observable output is a set of recorded benchmark metrics.

## Test Markers and Grouping

All test classes are tagged as benchmarks via a `benchmark` marker
(tests/benchmarks/test_layers_bench.py:L31, L65, L124, L156, L182). Each of the five
test classes groups a related measurement: wake-up cost, Layer1 unbounded fetch,
wake-up token budget, Layer2 retrieval, and Layer3 search.

## Shared Helper: Resident Memory Measurement

A helper returns the process's resident set size (RSS) in megabytes
(tests/benchmarks/test_layers_bench.py:L16-L28). When a process-introspection library
is available, it returns `rss_bytes / (1024 * 1024)`
(tests/benchmarks/test_layers_bench.py:L17-L20). When unavailable, it falls back to OS
resource usage: on Darwin (macOS) the raw max-RSS value is in bytes and is divided by
`1024 * 1024`; on other platforms the raw value is in kilobytes and is divided by `1024`
(tests/benchmarks/test_layers_bench.py:L21-L28). The observable contract is that the
returned value is a count of megabytes regardless of platform.

## Common Setup Pattern

Every test constructs a deterministic data generator seeded with `42` and a scale
(tests/benchmarks/test_layers_bench.py:L40, L74, L96, L133, L162, L188), then
populates a palace directly on disk under a `palace` subdirectory of the test's
temporary path, requesting `n_drawers` drawers with `include_needles=False`
(tests/benchmarks/test_layers_bench.py:L41-L42, L75-L76, L97-L98, L134-L135,
L163-L164, L189-L190). Several tests additionally write an identity text file
containing a short identity description (e.g. "I am a test AI. Traits: precise, fast.")
to an `identity.txt` path before constructing the stack
(tests/benchmarks/test_layers_bench.py:L45-L47, L137-L139, L192-L194).

Side effects: each test creates a palace directory and (where applicable) an identity
file on the filesystem under a per-test temporary directory; it imports and exercises
the memory-stack modules; it records metrics via a reporting sink.

## TestWakeUpCost — wake_up() latency vs. palace size

Parameterized over drawer counts `[500, 1000, 2500, 5000]`
(tests/benchmarks/test_layers_bench.py:L35, L37). It uses a configurable benchmark
scale fixture for data generation (tests/benchmarks/test_layers_bench.py:L40). It
constructs a `MemoryStack` from the palace path and identity path
(tests/benchmarks/test_layers_bench.py:L51), then calls `wake_up()` five times,
measuring each call's wall-clock latency in milliseconds
(tests/benchmarks/test_layers_bench.py:L53-L58). After each call it asserts the
returned text contains at least one of the markers `"L0"`, `"L1"`, `"IDENTITY"`, or
`"ESSENTIAL"` (tests/benchmarks/test_layers_bench.py:L59). It records the average of
the five latencies (rounded to one decimal) under metric group `"layers_wakeup"` with
key `avg_ms_at_<n_drawers>` (tests/benchmarks/test_layers_bench.py:L61-L62).

Observable contract: `MemoryStack.wake_up()` returns a text string that includes a
recognizable section marker from {L0, L1, IDENTITY, ESSENTIAL}
(tests/benchmarks/test_layers_bench.py:L59).

## TestLayer1UnboundedFetch — RSS growth and wing filtering

Parameterized over drawer counts `[500, 1000, 2500, 5000]`
(tests/benchmarks/test_layers_bench.py:L69, L71); data generation uses a fixed
`"small"` scale (tests/benchmarks/test_layers_bench.py:L74). It constructs a `Layer1`
from the palace path (tests/benchmarks/test_layers_bench.py:L80), samples RSS before
generation, times a single `generate()` call, then samples RSS after
(tests/benchmarks/test_layers_bench.py:L82-L87). It computes the RSS delta
(tests/benchmarks/test_layers_bench.py:L88) and asserts the generated text contains
the marker `"L1"` (tests/benchmarks/test_layers_bench.py:L89). It records two metrics
under group `"layer1"`: latency in milliseconds (`latency_ms_at_<n>`, one decimal) and
RSS delta in megabytes (`rss_delta_mb_at_<n>`, two decimals)
(tests/benchmarks/test_layers_bench.py:L91-L92).

A second test measures wing filtering. It populates a palace of 2000 drawers
(tests/benchmarks/test_layers_bench.py:L96-L98) and selects the first wing from the
generator's wing list (tests/benchmarks/test_layers_bench.py:L102). It times an
unfiltered `Layer1.generate()` (tests/benchmarks/test_layers_bench.py:L105-L108), then
times a wing-filtered `Layer1` constructed with a `wing` argument
(tests/benchmarks/test_layers_bench.py:L111-L114). It records `unfiltered_ms` and
`filtered_ms` (one decimal each) under group `"layer1_filter"`
(tests/benchmarks/test_layers_bench.py:L116-L117). Only when the unfiltered time is
strictly positive does it additionally record a `speedup_pct` computed as
`(1 - filtered_ms / unfiltered_ms) * 100`, rounded to one decimal
(tests/benchmarks/test_layers_bench.py:L118-L121).

Observable contracts: `Layer1` accepts an optional `wing` constructor argument that
restricts the drawers fetched (tests/benchmarks/test_layers_bench.py:L111);
`Layer1.generate()` returns text containing the `"L1"` marker
(tests/benchmarks/test_layers_bench.py:L89).

## TestWakeUpTokenBudget — token budget enforcement at scale

Parameterized over drawer counts `[500, 1000, 2500, 5000]`
(tests/benchmarks/test_layers_bench.py:L128, L130); data generation uses `"small"`
scale (tests/benchmarks/test_layers_bench.py:L133). It builds a `MemoryStack` and
calls `wake_up()` once (tests/benchmarks/test_layers_bench.py:L143-L144). It estimates
token count as character-length integer-divided by 4
(tests/benchmarks/test_layers_bench.py:L145). It records the token estimate
(`tokens_at_<n>`) and raw character count (`chars_at_<n>`) under group
`"wakeup_budget"` (tests/benchmarks/test_layers_bench.py:L148-L149). It asserts the
token estimate is strictly less than 1200 regardless of palace size, with a failure
message reporting the estimate and drawer count
(tests/benchmarks/test_layers_bench.py:L151-L153).

Observable contract / invariant: the combined L0+L1 wake-up text must stay under
~1200 estimated tokens (chars/4) at any palace size up to 5000 drawers; the underlying
`Layer1` is documented to enforce a 3200-character cap
(tests/benchmarks/test_layers_bench.py:L132, L147, L151-L153).

## TestLayer2Retrieval — on-demand retrieval latency

Uses the configurable benchmark scale fixture and a palace of 2000 drawers
(tests/benchmarks/test_layers_bench.py:L160-L164). It constructs a `Layer2` from the
palace path and selects the first generated wing
(tests/benchmarks/test_layers_bench.py:L168-L169). It calls
`retrieve(wing=<wing>, n_results=10)` ten times, timing each in milliseconds
(tests/benchmarks/test_layers_bench.py:L171-L176). It records the average latency (one
decimal) under group `"layer2"` with key `avg_retrieval_ms`
(tests/benchmarks/test_layers_bench.py:L178-L179).

Observable contract: `Layer2.retrieve` accepts `wing` and `n_results` keyword
arguments (tests/benchmarks/test_layers_bench.py:L174).

## TestLayer3Search — semantic search latency through the stack

Uses the configurable benchmark scale fixture, a palace of 2000 drawers, and an
identity file (tests/benchmarks/test_layers_bench.py:L186-L194). It constructs a
`MemoryStack` (tests/benchmarks/test_layers_bench.py:L198). For each of five fixed
query strings — `"authentication"`, `"database"`, `"deployment"`, `"testing"`,
`"monitoring"` — it calls `stack.search(query, n_results=5)` and times each call in
milliseconds (tests/benchmarks/test_layers_bench.py:L200-L206). It records the average
search latency (one decimal) under group `"layer3"` with key `avg_search_ms`
(tests/benchmarks/test_layers_bench.py:L208-L209).

Observable contract: `MemoryStack.search` accepts a query string and an `n_results`
keyword argument (tests/benchmarks/test_layers_bench.py:L204).

## Recorded Metric Contract

All measurements are emitted through a metric-recording sink taking a group name, a
metric key, and a numeric value (tests/benchmarks/test_layers_bench.py:L62, L91-L92,
L116-L121, L148-L149, L179, L209). Latencies are milliseconds rounded to one decimal,
RSS deltas are megabytes rounded to two decimals, token/char counts are integers, and
the wing speedup is a percentage rounded to one decimal. These recorded metrics are the
suite's principal externally observable output.

## Error and Edge-Case Behavior

The only hard failure conditions are the smoke assertions on wake-up/Layer1 text
markers (tests/benchmarks/test_layers_bench.py:L59, L89) and the token-budget ceiling
assertion (tests/benchmarks/test_layers_bench.py:L151-L153); a wake-up exceeding ~1200
estimated tokens fails the test. The wing-speedup metric is conditionally skipped to
avoid division by zero when the unfiltered timing is not positive
(tests/benchmarks/test_layers_bench.py:L118-L121).
