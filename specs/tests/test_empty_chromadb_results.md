# Spec: `tests/test_empty_chromadb_results.py`

## Purpose

Regression test suite for issue #195: an `IndexError` raised when search paths
indexed the first element of an empty result-set list. The tests pin the
behavior of the helper `_first_or_empty`, which normalizes the various
result shapes produced by an empty vector-store collection (or a filter that
excludes every document) into a graceful empty result instead of a crash
(`tests/test_empty_chromadb_results.py:L1-L8`).

## Subject Under Test

The tests exercise a single public helper imported from the searcher module:
`_first_or_empty(results, key)` (`tests/test_empty_chromadb_results.py:L12-L12`).

### Contract of `_first_or_empty(results, key)`

`results` is a mapping whose values are lists-of-lists (the shape returned by
the vector store). `key` is a field name such as `"documents"`, `"metadatas"`,
or `"distances"`. The function returns the inner list at index 0 of
`results[key]`, or an empty list when no usable inner list exists. The required
input-to-output behavior is:

- When the outer list for `key` is empty (`{"documents": []}`), return `[]`.
  This applies independently for `"documents"`, `"metadatas"`, and
  `"distances"` (`tests/test_empty_chromadb_results.py:L15-L20`).
- When the outer list contains an empty inner list (`{"documents": [[]]}`),
  return `[]` (`tests/test_empty_chromadb_results.py:L23-L26`).
- When `key` is absent from the mapping entirely (`{}`), return `[]`
  (`tests/test_empty_chromadb_results.py:L29-L30`).
- When the inner element at index 0 is a null/absent value
  (`{"documents": [None]}`), return `[]` without error
  (`tests/test_empty_chromadb_results.py:L33-L35`).
- When a normal populated inner list is present
  (`{"documents": [["a", "b", "c"]]}`), return that inner list unchanged:
  `["a", "b", "c"]` (`tests/test_empty_chromadb_results.py:L38-L40`).

### Documented original failure mode

One test documents the pre-fix bug by asserting that naively indexing element 0
of an empty outer list (`{"documents": []}` then taking `["documents"][0]`)
raises an `IndexError`. This establishes why `_first_or_empty` must exist and
must not itself raise (`tests/test_empty_chromadb_results.py:L43-L48`).

## Invariants

- `_first_or_empty` never raises for any of the documented input shapes; it
  always returns a list (`tests/test_empty_chromadb_results.py:L15-L40`).
- An empty or missing or null result maps to `[]`; only a genuinely populated
  inner list is returned verbatim
  (`tests/test_empty_chromadb_results.py:L17-L40`).

## Side Effects

None. The tests are pure assertions over in-memory values with no filesystem,
network, process, or environment interaction
(`tests/test_empty_chromadb_results.py:L15-L48`).
