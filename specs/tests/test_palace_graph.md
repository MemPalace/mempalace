# Behavior Spec: `palace_graph` (derived from `tests/test_palace_graph.py`)

This spec describes the observable behavior of the graph-traversal layer
(`palace_graph`) as exercised by its test suite. The module operates over a
collection of "drawer" records, each carrying metadata fields. All collection
access in the tests is mocked, so the spec defines the contract the real
collection must satisfy and the behavior the module must exhibit
(tests/test_palace_graph.py:L1-L6).

## Data Source Contract: the Collection

The graph functions accept a `col` (collection) object. A valid collection
exposes a `count()` returning the number of records, and a `get(limit, offset, include)`
returning a batch shaped as `{"ids": [...], "metadatas": [...]}`, where records
are paged via offset/limit slicing of the underlying list (tests/test_palace_graph.py:L9-L23).
The default batch read uses `limit=1000` (tests/test_palace_graph.py:L17-L20).

Each metadata record (when present) is a mapping with fields: `room`, `wing`,
`hall`, and `date` (tests/test_palace_graph.py:L66-L71). A record's metadata may
also be absent (a `None` entry), representing legacy or partial-write drawers
(tests/test_palace_graph.py:L57-L72).

## Public Surface

The module exposes: `_fuzzy_match`, `build_graph`, `find_tunnels`,
`graph_stats`, `invalidate_graph_cache`, and `traverse`
(tests/test_palace_graph.py:L28-L35).

## `build_graph(col=...)`

Returns a pair `(nodes, edges)` where `nodes` is a mapping keyed by room name
and `edges` is a list (tests/test_palace_graph.py:L47-L49).

### Empty / falsy collection
An empty collection yields `nodes == {}` and `edges == []`
(tests/test_palace_graph.py:L45-L49). When `col` is explicitly falsy (e.g. `0`),
`build_graph` returns the same empty result without attempting any read
(tests/test_palace_graph.py:L51-L55).

### None-metadata resilience (invariant)
Records whose metadata is `None` MUST be skipped silently and MUST NOT abort the
build. Given three records where the middle one is `None`, the two real records
are still processed and the resulting node for the shared room reflects only the
real records (`count == 2`) (tests/test_palace_graph.py:L57-L76).

### Node aggregation
Each node carries at least a `count` field equal to the number of drawers in
that room (tests/test_palace_graph.py:L78-L87) and a `dates` field listing dates
(tests/test_palace_graph.py:L132-L140). The `dates` list is capped at a maximum
of 5 entries even when more distinct dates exist
(tests/test_palace_graph.py:L132-L140).

### Edge creation (cross-wing tunnels)
A room appearing under a single wing produces no edges
(tests/test_palace_graph.py:L78-L88). A room appearing under two different wings
produces exactly one edge. The edge is a mapping with `wing_a`, `wing_b`, and
`hall`; for a room in `wing_code` and `wing_project` sharing hall `databases`,
the edge has `wing_a == "wing_code"`, `wing_b == "wing_project"`,
`hall == "databases"` (tests/test_palace_graph.py:L90-L112).

### Exclusion rules (invariants)
The room named `general` is excluded from `nodes`
(tests/test_palace_graph.py:L114-L121). Any record with an empty `wing` value is
excluded; its room does not appear in `nodes`
(tests/test_palace_graph.py:L123-L130).

### Caching (invariant)
Results are cached with a time-to-live. A second call within the TTL returns the
cached `(nodes, edges)` and does NOT re-scan, even if a different collection is
passed. Calling with a different (empty) collection still returns the originally
cached non-empty result (tests/test_palace_graph.py:L142-L157). Callers that
switch collections must invalidate the cache first
(tests/test_palace_graph.py:L145-L148).

## `invalidate_graph_cache()`

Clears the cache so the next `build_graph` performs a fresh scan. After
building from a populated collection then invalidating, building from an empty
collection yields `nodes == {}` and `edges == []`
(tests/test_palace_graph.py:L159-L169).

## `traverse(room, col=..., max_hops=...)`

### Known room
For a known room, returns a list of room records. Each element is a mapping with
a `room` key. The result includes the start room itself, plus rooms reachable
through shared wings. Example: traversing `auth` (in `wing_code`) also returns
`login` because `login` shares `wing_code`
(tests/test_palace_graph.py:L188-L196).

### Unknown room
For an unknown room, returns a mapping (not a list) containing both an `error`
key and a `suggestions` key (tests/test_palace_graph.py:L197-L202).

### Hop limiting
With `max_hops=0`, the result contains only the start room itself: a single
element whose `room` equals the start room
(tests/test_palace_graph.py:L204-L209).

## `find_tunnels(wing_a=None, wing_b=None, col=...)`

Returns a list of tunnel records, each a mapping with a `room` key
(tests/test_palace_graph.py:L228-L232). A tunnel is a room that appears under
more than one wing. Given three records where only `chromadb` spans
`wing_code` and `wing_project`, the unfiltered result has length 1 with
`room == "chromadb"` (tests/test_palace_graph.py:L219-L232).

### Single-wing filter
With `wing_a` set to a wing that participates in a tunnel, the matching tunnel
is returned (length 1) (tests/test_palace_graph.py:L234-L237). With `wing_a` set
to a nonexistent wing, the result is `[]`
(tests/test_palace_graph.py:L239-L242).

### Both-wing filter
With both `wing_a` and `wing_b` specifying the two endpoints of a tunnel, the
matching tunnel is returned (length 1, `room == "chromadb"`)
(tests/test_palace_graph.py:L244-L248).

## `graph_stats(col=...)`

Returns a mapping with keys `total_rooms`, `tunnel_rooms`, `total_edges`, and
`rooms_per_wing` (tests/test_palace_graph.py:L258-L277).

### Empty graph
For an empty collection: `total_rooms == 0`, `tunnel_rooms == 0`,
`total_edges == 0` (tests/test_palace_graph.py:L258-L263).

### Populated graph
Counts reflect distinct rooms and tunnels. For records covering rooms `chromadb`
(two wings) and `auth` (one wing): `total_rooms == 2`, `tunnel_rooms == 1`
(the cross-wing `chromadb`), `total_edges == 1`, and `rooms_per_wing` contains
a key for `wing_code` (tests/test_palace_graph.py:L265-L277).

## `_fuzzy_match(query, nodes, n=...)`

Matches a query string against node keys and returns a list of matching keys
(tests/test_palace_graph.py:L284-L297).

### Substring / partial-word matching
A query that is a substring of a node key matches it: `"chromadb"` matches
`"chromadb-setup"` (tests/test_palace_graph.py:L284-L287). A query matching a
hyphen-delimited word matches: `"auth"` matches `"auth-module"`
(tests/test_palace_graph.py:L289-L292). A multi-word hyphenated query matches a
node key containing those words: `"riley-college"` matches
`"riley-college-apps"` (tests/test_palace_graph.py:L299-L302).

### No match
A query with no overlap returns `[]` (e.g. `"zzzzz"` against unrelated keys)
(tests/test_palace_graph.py:L294-L297).

### Result cap
The `n` parameter caps the number of returned matches: with 20 candidate nodes
matching `"room"` and `n=3`, the result length is at most 3
(tests/test_palace_graph.py:L304-L307).

## Test Isolation Note

Each graph test class resets shared state by calling
`invalidate_graph_cache()` before each test method, because the build cache
persists across calls (tests/test_palace_graph.py:L42-L43, L176-L177,
L216-L217, L255-L256).
