# Spec: test_hallways_pagination

Regression test for issue #1619 covering how `compute_hallways_for_wing` fetches drawers from a storage collection (tests/test_hallways_pagination.py:L1-L9).

## Subject Under Test

The test exercises `compute_hallways_for_wing(wing, col=...)` from the `hallways` module (tests/test_hallways_pagination.py:L14, tests/test_hallways_pagination.py:L59). It accepts a wing name (string) and a collection object via the `col` keyword argument, and returns a list of hallway records (tests/test_hallways_pagination.py:L59-L60).

## Observable Contract Being Verified

The function MUST fetch drawers by paginating: first calling `count()` on the collection, then issuing repeated `get(limit=, offset=)` calls and filtering drawers by wing on the caller side (tests/test_hallways_pagination.py:L3-L8). It MUST NOT fetch via a single filtered query of the form `get(where={"wing": wing})` with no limit, because that overflows the underlying SQLite bound-variable cap (`SQLITE_MAX_VARIABLE_NUMBER` = 32766) on wings larger than ~32k drawers, silently leaving the hallway graph unbuilt (tests/test_hallways_pagination.py:L4-L8).

## Collection Interface Contract

The collection object that `compute_hallways_for_wing` consumes exposes:
- `count()` returning the total number of drawers (tests/test_hallways_pagination.py:L31).
- `get(limit=None, offset=0, include=None, where=None, ids=None, ...)` returning a mapping with key `"ids"` (a list of drawer id strings) and key `"metadatas"` (a list of drawer metadata records) (tests/test_hallways_pagination.py:L33, tests/test_hallways_pagination.py:L43-L46).

In the test's stub, a `get` call that supplies a `where` filter but no `limit` (i.e. an unbounded filtered fetch) raises an error simulating SQLite variable overflow, with a message containing "too many SQL variables" (tests/test_hallways_pagination.py:L34-L35). A paginated `get` with `limit`/`offset` returns the slice `drawers[offset : offset + limit]`, and the returned ids are formatted as `d{index}` for indices in `[offset, offset + page_size)` (tests/test_hallways_pagination.py:L42-L46). When a `where` filter naming `"wing"` is combined with a `limit`, the stub filters drawers to those whose metadata `wing` equals the target before slicing (tests/test_hallways_pagination.py:L37-L42).

## Drawer Metadata Shape

Each drawer is a metadata record with fields `wing` (string), `room` (string), and `entities` (a delimited string of entity names joined by `;`) (tests/test_hallways_pagination.py:L57). In the test, three identical drawers each place both `Alice` and `Bob` in wing `wing_alpha`, room `diary` (tests/test_hallways_pagination.py:L57).

## Hallway Output Shape

The returned hallway records are mappings exposing `entity_a` and `entity_b` keys naming the two co-occurring entities (tests/test_hallways_pagination.py:L60). With three drawers co-placing the same entity pair, at least one hallway connecting `{Alice, Bob}` is produced at the implied minimum co-occurrence threshold of 2 (tests/test_hallways_pagination.py:L56-L60). The pairing is unordered: the assertion treats `{entity_a, entity_b}` as a set when matching `{"Alice", "Bob"}` (tests/test_hallways_pagination.py:L60).

## Pass/Fail Criterion

The test passes when the result contains at least one hallway whose entity pair equals `{Alice, Bob}` (tests/test_hallways_pagination.py:L60-L62). An empty result indicates the unbounded filtered-fetch path was taken and crashed, which is the regression the test guards against (tests/test_hallways_pagination.py:L61-L62).

## Side Effects and Isolation

The test redirects the hallway persistence file to a temporary path: `_get_hallway_file` is overridden to return `<tmp>/hallways.json`, and `_legacy_hallway_file` is overridden to return `<tmp>/legacy-hallways.json` (tests/test_hallways_pagination.py:L17-L24). This implies `compute_hallways_for_wing` resolves an on-disk hallway file location through these two functions, so the implementation has a configurable primary hallway file path and a legacy fallback path (tests/test_hallways_pagination.py:L18-L24). The underlying ChromaDB module is stubbed out at import time so the module loads without a real vector backend (tests/test_hallways_pagination.py:L13-L14).
