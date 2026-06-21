# Behavior Spec: searcher (derived from tests/test_searcher.py)

This spec describes the externally observable behavior of the search subsystem,
distilled from its test suite. Two public entry points are covered: `search_memories`
(structured API returning a result object) and `search` (CLI/print function). It also
covers the internal lexical-scoring helpers `_tokenize` and `_bm25_scores`, and the
error type `SearchError` (tests/test_searcher.py:L12).

## Public surface

- `search_memories(query, palace_path, wing=?, room=?, n_results=?, collection_name=?)` — structured API returning a result object (tests/test_searcher.py:L20, L26, L30, L34, L38, L97).
- `search(query, palace_path, wing=?, room=?, n_results=?)` — CLI function that prints to stdout/stderr and returns nothing meaningful on success (tests/test_searcher.py:L254, L259, L264, L269, L296).
- `SearchError` — error type raised by the CLI `search` path on failure (tests/test_searcher.py:L12, L275, L292).
- `_tokenize(text)` — tokenizer helper returning a list of tokens (tests/test_searcher.py:L216-L218).
- `_bm25_scores(query, documents)` — lexical scorer returning one score per document (tests/test_searcher.py:L228-L235).

## `search_memories` — inputs and outputs

`query` is a string; `palace_path` is the path to a palace. Optional filters `wing` and
`room` are strings; `n_results` is an integer cap; `collection_name` selects an explicit
backing collection (tests/test_searcher.py:L20, L26, L30, L34, L38, L97).

On success the returned object contains a `"results"` list (non-empty when matches
exist), a `"query"` field echoing the input query verbatim, and a `"filters"` object
echoing the applied `wing` and `room` filters (tests/test_searcher.py:L21-L23, L107-L108).

Each result hit contains the fields `text`, `wing`, `room`, `source_file`, `similarity`,
and `created_at`. `similarity` is a floating-point number (tests/test_searcher.py:L48-L54).

### Filtering and limits

When `wing` is supplied, every returned hit has that `wing` value
(tests/test_searcher.py:L26-L27). When `room` is supplied, every returned hit has that
`room` value (tests/test_searcher.py:L30-L31). When both are supplied, every hit matches
both (tests/test_searcher.py:L34-L35). `n_results` caps the number of returned hits at
or below the requested count (tests/test_searcher.py:L38-L39).

### `created_at` semantics

`created_at` surfaces the drawer's `filed_at` metadata value verbatim, e.g.
`"2026-01-01T00:00:00"` (tests/test_searcher.py:L56-L60). When the drawer metadata has no
`filed_at`, `created_at` defaults to the literal string `"unknown"`
(tests/test_searcher.py:L62-L75).

### Collection selection

When `collection_name` is given, the underlying drawer collection is retrieved using that
exact name with creation disabled (the collection is opened, never created)
(tests/test_searcher.py:L87-L103).

### Error behavior

If no palace exists at `palace_path`, the result object contains an `"error"` field rather
than results (tests/test_searcher.py:L41-L43). If the underlying query fails, the result
object contains an `"error"` field whose value includes the underlying failure message,
e.g. `"query failed"` (tests/test_searcher.py:L77-L85). These are returned as data, not
raised — `search_memories` does not throw on these conditions.

### None-metadata robustness

When the drawer result set contains a `None` metadata entry, that hit must still render
using sentinel fallback values rather than failing: `wing` and `room` become `"unknown"`,
and the source falls back to a sentinel (described as `?`). The remaining well-formed hits
render normally, and the total result count is preserved (tests/test_searcher.py:L110-L139).

### Hybrid scoring: drawers + closets, and distance clamping

Search combines a drawer (vector) collection with an optional closet collection to form a
hybrid score. A matching closet entry applies a boost (up to 0.40) that reduces a drawer's
effective distance. If the closet collection cannot be opened, search degrades gracefully
to pure drawer search (tests/test_searcher.py:L123-L132, L165-L181).

The effective distance is clamped to the valid cosine-distance range `[0, 2]`. As a
consequence, every hit's `similarity` stays within `[0.0, 1.0]` and every hit's
`effective_distance` stays within `[0.0, 2.0]`, even when a strong boost would otherwise
drive the raw value negative (tests/test_searcher.py:L141-L192).

The clamp must not invert ranking: a closet-boosted near-exact drawer (base distance 0.08
with a 0.40 boost) still ranks ahead of an unboosted drawer (base distance 0.35). The
top hit reports `matched_via == "drawer+closet"`. Hits expose an `effective_distance`
field and a `matched_via` field (tests/test_searcher.py:L182-L197).

Closet entries are only considered when their distance is within a closet-distance cap of
1.5 (tests/test_searcher.py:L172-L173).

## Lexical scoring helpers

`_tokenize(None)` returns an empty list (tests/test_searcher.py:L216-L218). `_tokenize("")`
returns an empty list (tests/test_searcher.py:L220-L223). Tokenization is case-insensitive
(matching is performed on lowercased text) (tests/test_searcher.py:L210-L212).

`_bm25_scores(query, documents)` returns exactly one score per input document. A `None`
document yields score `0.0` for that position; documents that lexically match the query
yield a score strictly greater than `0.0`. A `None` document must not cause a failure
(tests/test_searcher.py:L225-L235).

## `search` (CLI) behavior

`search` prints human-readable output to stdout. For a matching query it prints content
containing the query terms (tests/test_searcher.py:L253-L256). When a `wing` filter is
applied, output contains a `"Results for"` header (tests/test_searcher.py:L258-L261). When
a `room` filter is applied, output contains a `"Room:"` label (tests/test_searcher.py:L263-L266).
When both filters are applied, output contains both a `"Wing:"` and a `"Room:"` label
(tests/test_searcher.py:L268-L272).

Result blocks are numbered with bracketed indices starting at `[1]`; the `n_results` cap
limits how many blocks are printed (tests/test_searcher.py:L295-L299). An empty collection
either prints a `"No results"` message or the function returns nothing
(tests/test_searcher.py:L278-L284).

### CLI error behavior

If no palace exists at `palace_path`, `search` raises `SearchError` with a message matching
`"No palace found"` (tests/test_searcher.py:L274-L276). If the underlying query fails,
`search` raises `SearchError` with a message matching `"Search error"`
(tests/test_searcher.py:L286-L293). (Contrast with `search_memories`, which returns these
as error data rather than raising.)

### CLI hybrid rerank

The CLI applies the same hybrid (BM25 + vector) rerank as the API path. When all
candidates have a vector distance >= 1.0 (zero cosine similarity) but one candidate
contains every query term, that lexical match ranks first. The first rendered block then
shows a non-zero BM25 score, reported in the form `bm25=<value>` (and must not be
`bm25=0.0` for the winning lexical match) (tests/test_searcher.py:L301-L345).

The CLI also reports a metric-labeled vector similarity for transparency in the form
`<metric>_sim=`, e.g. `cosine_sim=`, reflecting the backend's actual metric rather than a
hard-coded label (tests/test_searcher.py:L346-L349).

### CLI distance-metric warning

The CLI inspects the collection's `hnsw:space` metadata. When that metadata is absent
(legacy palace, implicitly using L2), the CLI writes a warning to stderr that mentions
`cosine` and points the user at `mempalace repair` (tests/test_searcher.py:L351-L367). When
`hnsw:space` is set to `"cosine"`, no such warning is emitted to stderr
(tests/test_searcher.py:L369-L382).

### CLI None-data robustness

A `None` metadata entry in the result set must not crash the CLI mid-render. Earlier hits
print normally, and the `None`-metadata hit renders with fallback `?` sentinel values; both
blocks (`[1]` and `[2]`) are printed and the document text still appears
(tests/test_searcher.py:L384-L401). Likewise, a `None` document entry must not crash; both
numbered blocks are still printed (tests/test_searcher.py:L403-L415).

## Filesystem-first state checks (CLI)

Before reaching the backend, `search` performs filesystem-first state checks against the
palace directory. A palace directory containing a `chroma.sqlite3` file passes these checks
and proceeds to query the backend; absence of the palace causes the "No palace found"
error described above (tests/test_searcher.py:L241-L249, L274-L276).

<promise>SPEC_WRITTEN path=specs/tests/test_searcher.md citations=58</promise>
