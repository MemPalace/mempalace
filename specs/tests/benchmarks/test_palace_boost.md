# Spec: tests/benchmarks/test_palace_boost.py

## Purpose

This is a benchmark test module that quantifies the retrieval improvement ("palace boost") gained from wing/room spatial filtering in the MemPalace search system. It measures recall and latency with and without filtering at multiple palace scales using planted "needle" entries (tests/benchmarks/test_palace_boost.py:L1-L7). All test classes/methods are tagged with the `benchmark` marker, meaning they are part of the benchmark suite and are gated behind that marker (tests/benchmarks/test_palace_boost.py:L17-L18, L80-L81, L134-L135).

## External dependencies (collaborator contracts)

- `PalaceDataGenerator` is constructed with keyword args `seed` and `scale`, and exposes `populate_palace_directly(palace_path, n_drawers, include_needles)` plus attributes `wings` (an ordered list) and `rooms_by_wing` (a mapping from wing to an ordered list of rooms) (tests/benchmarks/test_palace_boost.py:L13, L26-L30, L92-L93). `populate_palace_directly` returns a 3-tuple whose third element is `needle_info` — a list of needle descriptors, each a mapping with keys `query` (string), `wing` (string), and `room` (string) (tests/benchmarks/test_palace_boost.py:L28-L30, L39-L64).
- `record_metric(category, name, value)` records a benchmark metric under a named category; value may be a number or a structured object such as a list of maps (tests/benchmarks/test_palace_boost.py:L14, L73-L77, L172-L176).
- `search_memories(query, palace_path=..., wing=..., room=..., n_results=...)` performs a search against a populated palace and returns a mapping. The result's `results` key (absent ⇒ empty list) is a list of hit maps, each with a `text` key (string) (tests/benchmarks/test_palace_boost.py:L32, L41-L42, L158).
- The pytest fixtures `tmp_path` (a temporary directory) and `bench_scale` (the benchmark scale value) are injected into every test (tests/benchmarks/test_palace_boost.py:L24, L84, L138).

## Needle hit detection contract

A search "hits" the planted needle when any of the first 5 returned result texts contains the literal substring `NEEDLE_` (tests/benchmarks/test_palace_boost.py:L43, L51, L63, L158, L164). The planted-needle entries are therefore expected to carry text containing `NEEDLE_`, which is the observable marker contract between the generator and these tests.

## TestFilteredVsUnfilteredRecall.test_palace_boost_recall

Parametrized over `n_drawers` in `[1000, 2500, 5000]` (tests/benchmarks/test_palace_boost.py:L21-L24). For each size it builds a generator with `seed=42` and the injected `bench_scale`, creates a palace under `<tmp_path>/palace`, and populates it with the given drawer count including needles (tests/benchmarks/test_palace_boost.py:L26-L30).

It evaluates up to `min(10, len(needle_info))` needle queries (tests/benchmarks/test_palace_boost.py:L34, L39). For each needle it runs three searches with `n_results=5`: (1) unfiltered, (2) wing-filtered using the needle's `wing`, (3) wing+room-filtered using the needle's `wing` and `room`. Each search that hits the needle increments its respective counter `unfiltered_hits` / `wing_filtered_hits` / `room_filtered_hits` (tests/benchmarks/test_palace_boost.py:L40-L64).

Recall is computed as hits divided by `max(n_queries, 1)` (guarding against division by zero when there are no needles) (tests/benchmarks/test_palace_boost.py:L66-L68). Boost is the filtered recall minus the unfiltered recall, computed separately for wing and room filtering (tests/benchmarks/test_palace_boost.py:L70-L71). Five metrics are recorded under category `palace_boost`, each suffixed with `_at_{n_drawers}` and rounded to 3 decimals: `recall_unfiltered_at_{n}`, `recall_wing_filtered_at_{n}`, `recall_room_filtered_at_{n}`, `wing_boost_at_{n}`, `room_boost_at_{n}` (tests/benchmarks/test_palace_boost.py:L73-L77).

## TestFilterLatencyBenefit.test_filter_speedup

Builds a generator (`seed=42`, injected `bench_scale`), creates a palace under `<tmp_path>/palace`, and populates it with 5000 drawers and no needles (tests/benchmarks/test_palace_boost.py:L86-L88). It selects the first wing (`gen.wings[0]`) and that wing's first room (`gen.rooms_by_wing[wing][0]`), and uses the fixed query string `"authentication middleware optimization"` (tests/benchmarks/test_palace_boost.py:L92-L94).

It performs `n_runs = 10` timed searches for each of three filter modes — no filter, wing filter, wing+room filter — all with `n_results=5` (tests/benchmarks/test_palace_boost.py:L95-L116). Each run's latency is the wall-clock duration of the single `search_memories` call converted to milliseconds (elapsed seconds × 1000) (tests/benchmarks/test_palace_boost.py:L99-L102, L106-L109, L113-L116). The per-mode average is the mean of its 10 latencies (tests/benchmarks/test_palace_boost.py:L118-L120).

Three metrics are recorded under category `filter_latency`, rounded to 1 decimal: `avg_unfiltered_ms`, `avg_wing_filtered_ms`, `avg_room_filtered_ms` (tests/benchmarks/test_palace_boost.py:L122-L124). Only when the unfiltered average is strictly greater than 0, two additional speedup-percentage metrics are recorded, rounded to 1 decimal: `wing_speedup_pct` and `room_speedup_pct`, each computed as `(1 - avg_filtered / avg_unfiltered) * 100` (tests/benchmarks/test_palace_boost.py:L125-L131).

## TestBoostAtIncreasingScale.test_boost_scaling

Tests the hypothesis that the palace boost grows with palace size. It iterates over sizes `[500, 1000, 2500]` (tests/benchmarks/test_palace_boost.py:L140-L143). For each size it builds a fresh generator (`seed=42`, injected `bench_scale`) and a separate palace under `<tmp_path>/palace_{size}`, populated with that many drawers including needles (tests/benchmarks/test_palace_boost.py:L144-L148).

For each size it evaluates up to `min(8, len(needle_info))` needle queries (tests/benchmarks/test_palace_boost.py:L152, L156), running an unfiltered and a wing-filtered search per needle (each `n_results=5`) and counting needle hits among the first 5 results (tests/benchmarks/test_palace_boost.py:L156-L165). It computes unfiltered recall, wing-filtered recall (each divided by `max(n_queries, 1)`), and the boost as their difference, appending `{"size": size, "boost": boost}` to a list (tests/benchmarks/test_palace_boost.py:L167-L170).

It records the full per-size list as metric `boosts_by_size` under category `boost_scaling` (tests/benchmarks/test_palace_boost.py:L172). When at least 2 sizes were measured, it records a boolean metric `trend_positive` under the same category, true when the last size's boost is greater than or equal to the first size's boost (tests/benchmarks/test_palace_boost.py:L174-L176).

## Invariants and ordering guarantees

- The same fixed seed `42` is used everywhere, making generator output deterministic across runs (tests/benchmarks/test_palace_boost.py:L26, L86, L144).
- Recall ordering and boost computation always reference the same needle list slice across filter modes, so unfiltered, wing-filtered, and room-filtered searches are evaluated over the identical set of needles within a test (tests/benchmarks/test_palace_boost.py:L39-L64, L156-L165).
- Hit detection only ever inspects the first 5 results (`texts[:5]` / `[:5]`), independent of how many results are returned (tests/benchmarks/test_palace_boost.py:L43, L51, L63, L158, L164).

## Side effects

- Creates and populates palace directories on the filesystem under the pytest-provided temporary path; distinct subdirectory names (`palace`, `palace_{size}`) keep palaces isolated (tests/benchmarks/test_palace_boost.py:L27, L87, L145).
- Emits benchmark metrics via `record_metric` into categories `palace_boost`, `filter_latency`, and `boost_scaling` (tests/benchmarks/test_palace_boost.py:L73-L77, L122-L131, L172-L176).
- These tests record metrics but contain no assertions, so they never fail on a recall/latency threshold — they are pure measurement harnesses (tests/benchmarks/test_palace_boost.py:L66-L77, L118-L131, L167-L176).
