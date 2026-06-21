# Behavior Spec: `mempalace.layers` (derived from `tests/test_layers.py`)

This spec describes the externally observable behavior of the L0–L3 memory wake-up
stack and the `MemoryStack` aggregator, as pinned by the test suite. Public surface
exercised: `Layer0`, `Layer1`, `Layer2`, `Layer3`, `MemoryStack`
(tests/test_layers.py:L6-L6).

The stack depends on two collaborators that are mockable injection points: a
`MempalaceConfig` whose `.palace_path` attribute names the palace directory, and a
`_get_collection(...)` function returning a storage collection object exposing
`get(...)`, `query(...)`, and `count()` (tests/test_layers.py:L86-L88,
tests/test_layers.py:L104-L108).

## Layer0 — Identity

`Layer0(identity_path=<str>)` loads a single identity text file and exposes
`render()` and `token_estimate()`. With no argument, its `.path` defaults to the
expansion of `~/.mempalace/identity.txt` (tests/test_layers.py:L64-L67).

`render()` reads the identity file and returns its text content; the content is
surfaced verbatim enough that named substrings present in the file (e.g. "Atlas",
"Alice") appear in the output (tests/test_layers.py:L12-L18). Leading and trailing
whitespace, including trailing blank lines, is stripped from the returned text
(tests/test_layers.py:L56-L61).

`render()` caches the text on first call: a later modification to the underlying
file is NOT reflected; subsequent `render()` calls return the originally read,
stripped content (tests/test_layers.py:L21-L29).

If the identity file does not exist, `render()` returns a default placeholder
message containing both the phrase "No identity configured" and the filename
"identity.txt" (tests/test_layers.py:L32-L37).

`token_estimate()` returns an integer estimate computed as character count divided
by 4: 400 characters yields 100 (tests/test_layers.py:L40-L46); an empty file
yields 0 (tests/test_layers.py:L49-L53).

## Layer1 — Essential Story

`Layer1(palace_path=<str>, wing=<str|None>)` exposes `generate()` returning a
formatted string, and carries a mutable `MAX_CHARS` cap and a `.wing` attribute
(tests/test_layers.py:L88-L89, tests/test_layers.py:L140-L141,
tests/test_layers.py:L177).

When the palace path does not exist, `generate()` returns a helpful message
containing "No palace found" or "No memories" (tests/test_layers.py:L84-L90).

When entries exist, `generate()` returns text beginning with / containing the
header "ESSENTIAL STORY" and includes snippet text drawn from the stored documents
(tests/test_layers.py:L93-L114). When the collection returns no documents,
the output contains "No memories" (tests/test_layers.py:L116-L127).

Entries are read by paginated `get()` calls: the loop consumes batches until an
empty batch (empty `documents`/`metadatas`) signals end of pagination
(tests/test_layers.py:L73-L81). If a `get()` batch raises an exception, the
pagination loop breaks gracefully and the already-collected entries are still
rendered (output still contains "ESSENTIAL STORY")
(tests/test_layers.py:L204-L219).

Wing filtering: when constructed with `wing="project_x"`, the first `get()` call
passes `where={"wing": "project_x"}` (tests/test_layers.py:L130-L146).

Long document snippets are truncated; truncated output contains the marker "..."
(tests/test_layers.py:L149-L162). The total output is capped at `MAX_CHARS`: once
the cap is reached the loop stops adding entries and the output contains the marker
"more in L3 search" (tests/test_layers.py:L165-L180).

Importance/weight resolution: per-entry importance is read by trying metadata keys
in order — `importance`, then `emotional_weight`, then `weight` — defaulting to `3`
when none is present (tests/test_layers.py:L183-L201).

None-entry tolerance: a `None` value inside the `metadatas` list must not raise;
the loop skips/coerces it and renders the remaining entries (output still contains
"ESSENTIAL STORY" and the valid snippet) (tests/test_layers.py:L660-L687). A `None`
value inside the `documents` list must likewise not raise; render still succeeds
and returns non-empty output (tests/test_layers.py:L689-L706).

## Layer2 — On-Demand Retrieval

`Layer2(palace_path=<str>)` exposes `retrieve(wing=<str|None>, room=<str|None>)`
returning a formatted string (tests/test_layers.py:L228-L229).

When the palace path does not exist, `retrieve(...)` returns a message containing
"No palace found" (tests/test_layers.py:L225-L230).

On success the output contains the header marker "ON-DEMAND" (and the full marker
"L2 — ON-DEMAND") plus the retrieved snippet text
(tests/test_layers.py:L233-L248, tests/test_layers.py:L726). When the collection
returns no documents, the output contains "No drawers found"
(tests/test_layers.py:L287-L298).

Filter construction passed to `get(where=...)`:
- `wing` only or `room` only produces a single-key filter (tests/test_layers.py:L233-L265).
- both `wing` and `room` produce a compound `where` filter whose top level contains
  the key `$and` (tests/test_layers.py:L268-L284).
- no filters: `get()` is called with NO `where` keyword at all
  (tests/test_layers.py:L301-L314).

If the underlying `get()` raises, `retrieve(...)` returns a message containing
"Retrieval error" rather than propagating (tests/test_layers.py:L317-L328).

Long snippets are truncated with the marker "..." (tests/test_layers.py:L331-L345).
A `None` entry in the `metadatas` list must not raise; the rest renders, output
contains "L2 — ON-DEMAND" (tests/test_layers.py:L709-L726).

## Layer3 — Deep Search

`Layer3(palace_path=<str>)` exposes `search(query, wing=, room=)` returning a
formatted string, and `search_raw(query, wing=, room=)` returning a list of dicts
(tests/test_layers.py:L362-L371). Results are obtained via the collection's
`query(...)` method, which returns nested lists keyed `documents`, `metadatas`,
`distances` (each wrapped one level deep) (tests/test_layers.py:L351-L356).

When the palace path does not exist: `search(...)` returns a message containing
"No palace found" (tests/test_layers.py:L359-L364); `search_raw(...)` returns an
empty list `[]` (tests/test_layers.py:L367-L372).

`search(...)` success output contains the header "SEARCH RESULTS", the matched
document text, and a similarity rendering. Similarity is `1 - distance`: a distance
of `0.2` renders as "sim=0.8" (tests/test_layers.py:L375-L392). When `query`
returns no documents, output contains "No results found"
(tests/test_layers.py:L395-L406). On a `query` exception, output contains
"Search error" (tests/test_layers.py:L466-L477). Long documents are truncated with
the marker "..." (tests/test_layers.py:L480-L495).

Filter construction passed to `query(where=...)`:
- `wing` only → `where == {"wing": <value>}` (tests/test_layers.py:L409-L425).
- `room` only → `where == {"room": <value>}` (tests/test_layers.py:L428-L444).
- both → `where` top level contains key `$and` (tests/test_layers.py:L447-L463).

`search_raw(...)` returns a list of result dicts. Each dict carries at least:
`text` (the document string), `wing` (from metadata), `similarity` (= `1 - distance`,
so distance `0.3` → `0.7`), and a `metadata` key holding the raw metadata
(tests/test_layers.py:L498-L517). Filter construction mirrors `search`: combining
`wing` and `room` produces a `where` whose top level contains `$and`
(tests/test_layers.py:L520-L536). On a `query` exception, `search_raw(...)` returns
`[]` (tests/test_layers.py:L539-L550).

## MemoryStack — Aggregator

`MemoryStack(palace_path=<str>, identity_path=<str>)` composes the four layers and
exposes `wake_up(wing=)`, `recall(wing=, room=)`, `search(query)`, and `status()`.
It also exposes the underlying `l1` layer (tests/test_layers.py:L556-L585).

`wake_up()` returns a combined string that includes the L0 identity content (e.g.
"Atlas") and the L1 essential-story output; against a nonexistent palace the L1
portion contributes "No palace" or "No memories"
(tests/test_layers.py:L556-L570). `wake_up(wing="my_project")` sets
`stack.l1.wing` to that wing before generating, and still includes the identity
content (tests/test_layers.py:L573-L586).

`recall(wing="test")` delegates to L2; against a nonexistent palace it returns a
string containing "No palace found" (tests/test_layers.py:L589-L601).
`search("test query")` delegates to L3; against a nonexistent palace it returns a
string containing "No palace found" (tests/test_layers.py:L604-L616).

`status()` returns a dictionary (not a string). It always contains the keys
`palace_path` (echoing the configured path), `total_drawers`, `L0_identity`,
`L1_essential`, `L2_on_demand`, and `L3_deep_search`
(tests/test_layers.py:L619-L636). Against a nonexistent palace,
`total_drawers` is `0` (tests/test_layers.py:L631-L632). When a palace collection
exists, `total_drawers` equals the collection's `count()` value (e.g. `42`), and
`status()["L0_identity"]["exists"]` is `True` when the identity file exists
(tests/test_layers.py:L639-L657).
