# Behavior Spec: tests/benchmarks/benchmark_dialect.py

A micro-benchmark module exercising the entity-detection routine of the AAAK compression dialect. It contains a single test that measures throughput; it asserts no correctness conditions.

## Public Surface

- `test_detect_entities_benchmark()` — a parameterless test function that runs an entity-detection benchmark and prints timing output (`tests/benchmarks/benchmark_dialect.py:L7-L14`). It takes no inputs, returns nothing, and produces no return value or thrown assertion on success.

## Dependencies

The benchmark depends on a `Dialect` type exposing a method `_detect_entities_in_text(text: str)` that accepts a single text string (`tests/benchmarks/benchmark_dialect.py:L5-L8`, `tests/benchmarks/benchmark_dialect.py:L13`).

## Behavior

The test constructs a `Dialect` instance with no constructor arguments (`tests/benchmarks/benchmark_dialect.py:L8`). It defines a fixed multi-sentence English input string containing personal names (Alice, Bob, Dr. Chen) and placeholder tokens (Name, Name2, SomeName) used to exercise entity detection (`tests/benchmarks/benchmark_dialect.py:L9`).

It invokes `_detect_entities_in_text(text)` repeatedly for a fixed iteration count of `10000` and measures total elapsed wall-clock time for all iterations (`tests/benchmarks/benchmark_dialect.py:L12-L13`).

## Observable Output Contract

On completion the test prints a single line to standard output of the form `Dialect._detect_entities_in_text benchmark: <time> seconds for 10000 iterations`, where `<time>` is the total elapsed time formatted to four decimal places and preceded by a leading newline (`tests/benchmarks/benchmark_dialect.py:L14`).

## Invariants and Edge Cases

- The iteration count is hardcoded to `10000` and is not configurable (`tests/benchmarks/benchmark_dialect.py:L12`).
- The input text is fixed and not parameterized (`tests/benchmarks/benchmark_dialect.py:L9`).
- The test contains no assertions; it passes unless `_detect_entities_in_text` raises an error during any iteration (`tests/benchmarks/benchmark_dialect.py:L7-L14`).
- The return value of `_detect_entities_in_text` is discarded and never inspected (`tests/benchmarks/benchmark_dialect.py:L13`).

## Side Effects

- Writes one formatted timing line to standard output (`tests/benchmarks/benchmark_dialect.py:L14`).
- No filesystem, network, environment, or process side effects beyond stdout.

## Notes

The module imports a regular-expression facility and a test framework that are not otherwise referenced by the visible code path of this test (`tests/benchmarks/benchmark_dialect.py:L1-L3`); only the timing facility and the `Dialect` type are used in the benchmark body.
