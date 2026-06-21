# Behavior Spec: HNSW Capacity Probe & BM25-Only Fallback

This file is a test suite that pins the externally observable contracts of the
"#1222" HNSW capacity probe, its BM25-only SQLite fallback, the repair status
command, and the MCP server's vector-disable preflight. The tests synthesize the
on-disk shape directly without loading the HNSW vector segment
(tests/test_hnsw_capacity.py:L1-L7). The contracts below are derived from the
assertions; the underlying implementations they exercise live in
`mempalace/backends/chroma.py`, `mempalace/searcher.py`, `mempalace/repair.py`,
and `mempalace/mcp_server.py` (tests/test_hnsw_capacity.py:L17-L22).

The fixed collection name used throughout is `mempalace_drawers`
(tests/test_hnsw_capacity.py:L25-L25).

## On-disk shape (synthesized test inputs — observable contract)

A palace directory contains a SQLite database file named `chroma.sqlite3`
(tests/test_hnsw_capacity.py:L49-L49). The probe reads these tables: `collections`
(`id`, `name`), `collection_metadata` (`collection_id`, `key`, `str_value`,
`int_value`, `float_value`, `bool_value`), `segments` (`id`, `collection`,
`scope`), `embeddings` (`id`, `segment_id`, `embedding_id`, `seq_id`,
`created_at`), `embedding_metadata` (`id`, `key`, `string_value`, `int_value`,
`float_value`, `bool_value`), and an FTS5 virtual table
`embedding_fulltext_search(string_value)` tokenized with `trigram`
(tests/test_hnsw_capacity.py:L52-L91).

A collection row links to two segment rows: one with scope `VECTOR` and one with
scope `METADATA`, both referencing the same collection id
(tests/test_hnsw_capacity.py:L101-L108). When `hnsw:sync_threshold` is present, it
is stored as an `int_value` row in `collection_metadata` keyed by collection id
(tests/test_hnsw_capacity.py:L95-L100). Each embedding row carries an
auto-incrementing integer id, the vector segment id, a text embedding id
(`d-<n>`), and an 8-byte `seq_id` blob (tests/test_hnsw_capacity.py:L109-L114).

The HNSW element count is persisted on disk under
`<palace>/<segment_id>/index_metadata.pickle` (tests/test_hnsw_capacity.py:L128-L130).
The serialized state is a mapping with keys `dimensionality` (384),
`total_elements_added` (the element count), `max_seq_id`, `id_to_label`,
`label_to_id`, and `id_to_seq_id` (tests/test_hnsw_capacity.py:L131-L140). The
element count is read from the `id_to_label` map size (tests/test_hnsw_capacity.py:L135-L135).

## `_vector_segment_id(palace, collection) -> str | None`

Returns the segment id of the `VECTOR`-scope segment belonging to the named
collection. Given a seeded palace and the correct collection name, it returns the
exact segment UUID that was written (tests/test_hnsw_capacity.py:L146-L149). When
the palace has no database (empty directory), it returns `None`
(tests/test_hnsw_capacity.py:L152-L153). When the collection name is not found, it
returns `None` (tests/test_hnsw_capacity.py:L156-L159).

## `_hnsw_element_count(palace, segment_id) -> int | None`

Returns the count of elements recorded in the segment's
`index_metadata.pickle` (the `id_to_label` map size). With a pickle written for 42
elements, returns `42` (tests/test_hnsw_capacity.py:L165-L169). When the segment
directory / pickle does not exist (no flush ever happened), returns `None`
(tests/test_hnsw_capacity.py:L172-L176).

Security contract: deserialization is restricted by an allowlist. A tampered
`index_metadata.pickle` whose bytes reference an unallowed class (e.g. a GLOBAL
opcode naming `os.system`) MUST NOT deserialize or trigger any code execution; the
function returns `None` instead (tests/test_hnsw_capacity.py:L179-L202).

## `hnsw_capacity_status(palace, collection) -> dict`

Returns a status dict comparing the SQLite embedding count against the HNSW
element count. Observable fields: `status` (`"ok"` | `"diverged"` | `"unknown"`),
`diverged` (boolean), `sqlite_count` (int), `hnsw_count` (int | None), `divergence`
(int | None), and `message` (string) (tests/test_hnsw_capacity.py:L212-L250).

### Balanced / OK
When SQLite count is 1000 and HNSW count is 950, `status` is `"ok"`, `diverged` is
`False`, `sqlite_count` is `1000`, `hnsw_count` is `950`
(tests/test_hnsw_capacity.py:L208-L216).

### Severe divergence
When SQLite has 20,000 rows but HNSW is frozen at 2,000, `status` is `"diverged"`,
`diverged` is `True`, `divergence` equals `18_000` (sqlite minus hnsw), and the
`message` contains the word "repair" (case-insensitive)
(tests/test_hnsw_capacity.py:L219-L228).

### Flush-lag tolerance
A few hundred entries behind is normal: SQLite 5,000 vs HNSW 4,500 yields
`diverged` `False` and `status` `"ok"` (tests/test_hnsw_capacity.py:L231-L238).

### Unflushed / inconclusive
When no pickle exists but SQLite has many rows (10,000), the result is inconclusive
rather than divergent: `diverged` is `False`, `status` is `"unknown"`, `divergence`
is `None`, `hnsw_count` is `None`, and `message` contains both "capacity
unavailable" and "leaving vector search enabled"
(tests/test_hnsw_capacity.py:L241-L251).

### Empty palace
For an empty palace (no database), `diverged` is `False` and `status` is
`"unknown"` (tests/test_hnsw_capacity.py:L277-L280).

### Divergence floor scales with `hnsw:sync_threshold`
The divergence floor is `2 × sync_threshold` (tests/test_hnsw_capacity.py:L286-L294).

- SQLite 100,000 vs HNSW 50,000 with `sync_threshold=50_000`: `divergence` is
  `50_000`, which is within `2× = 100,000`, so `diverged` is `False` and `status`
  is `"ok"` (tests/test_hnsw_capacity.py:L297-L303).
- SQLite 200,000 vs HNSW 16,384 with `sync_threshold=50_000`: `divergence` is
  `183_616`, far past the 100,000 floor and past 10% of 200,000 (20,000), so
  `diverged` is `True` and `status` is `"diverged"`
  (tests/test_hnsw_capacity.py:L306-L320).
- Without any `hnsw:sync_threshold` row, the default sync_threshold is 1000, giving
  a floor of 2000. SQLite 10,000 vs HNSW 7,500 gives `divergence` `2_500`, which
  exceeds `max(2000 floor, 10% of 10,000 = 1000)`, so `diverged` is `True`
  (tests/test_hnsw_capacity.py:L323-L337).

The effective divergence threshold is `max(2 × sync_threshold, 10% of
sqlite_count)`; a value strictly above it flags divergence
(tests/test_hnsw_capacity.py:L317-L317, tests/test_hnsw_capacity.py:L335-L337).

### Unflushed branch also uses the dynamic floor
When the pickle is absent but SQLite count (30,000) is under the dynamic floor
(`2 × 50,000 = 100,000`), `hnsw_count` is `None` and `diverged` is `False`
(tests/test_hnsw_capacity.py:L340-L352).

## MCP preflight: `_refresh_vector_disabled_flag()`

The MCP server holds module state `_vector_disabled` (bool),
`_vector_disabled_reason` (str), and `_vector_capacity_status` (the status dict),
and reads palace path / collection name from `_config`
(tests/test_hnsw_capacity.py:L254-L274). When the capacity probe returns `status`
`"unknown"` (unflushed metadata, large SQLite count), calling
`_refresh_vector_disabled_flag()` MUST re-enable vectors: it sets `_vector_disabled`
to `False` and `_vector_disabled_reason` to the empty string, even if they were
previously set to a divergence state (tests/test_hnsw_capacity.py:L265-L272). After
the refresh, `_vector_capacity_status["status"]` is `"unknown"` and its `message`
contains "leaving vector search enabled" (tests/test_hnsw_capacity.py:L273-L274).
The contract: an unflushed-metadata signal MUST NOT route all searches to BM25
(tests/test_hnsw_capacity.py:L254-L255).

## BM25-only SQLite fallback: `_bm25_only_via_sqlite(query, palace, ...)`

Performs full-text search directly against the SQLite FTS5 table without loading
the vector segment. Parameters observed: `query` (str), `palace` (str), `wing`
(str, optional), `room` (str, optional), `n_results` (int), `max_candidates` (int)
(tests/test_hnsw_capacity.py:L437-L545).

Document text is stored in `embedding_metadata` under key `chroma:document` and
mirrored into `embedding_fulltext_search`; arbitrary metadata keys are stored as
`string_value` or `int_value` rows (tests/test_hnsw_capacity.py:L368-L395).

### Success shape
Returns a dict with `fallback` equal to `"bm25_only_via_sqlite"` and a `results`
list (tests/test_hnsw_capacity.py:L437-L441). Each result carries `text` (the
verbatim drawer text), `matched_via` equal to `"bm25_sqlite"`, and vector fields
intentionally absent: `similarity` is `None` and `distance` is `None`
(tests/test_hnsw_capacity.py:L442-L447). Results are ranked by BM25 relevance; for
query "segfault chromadb" the top result is the incident drawer containing
"segfault" (tests/test_hnsw_capacity.py:L438-L443). Each result also exposes `wing`
and `room` fields derived from drawer metadata
(tests/test_hnsw_capacity.py:L450-L515).

### Wing / room filtering applied before candidate limiting
A `wing` filter restricts results to that wing only
(tests/test_hnsw_capacity.py:L450-L454). The wing/room filters MUST be applied
before the FTS candidate limit (`max_candidates`) is enforced, so a matching drawer
inside the target wing is not crowded out by a higher-ranked drawer outside it.
With `max_candidates=1`: `total_before_filter` is `1` and the single returned
result belongs to the target wing (tests/test_hnsw_capacity.py:L457-L482). The same
ordering holds for the combined wing+room filter
(tests/test_hnsw_capacity.py:L484-L516) and for the recency-window candidate path
(tests/test_hnsw_capacity.py:L518-L549).

### Empty filtered candidate set
When the wing filter matches no candidate drawers, `total_before_filter` is `0` and
`results` is an empty list (tests/test_hnsw_capacity.py:L552-L570).

### No palace
When the palace directory has no database, the result dict contains an `error` key
(tests/test_hnsw_capacity.py:L573-L575).

### Short / unmatchable query
A single-character query token is unmatchable in trigram FTS5. This MUST NOT crash;
the function falls back to a recency window, still returning `fallback` equal to
`"bm25_only_via_sqlite"` and a list-typed `results`
(tests/test_hnsw_capacity.py:L578-L584). The `created_at` column on embeddings drives
recency ordering and can be set per row (tests/test_hnsw_capacity.py:L401-L409,
tests/test_hnsw_capacity.py:L537-L545).

## Repair status command: `repair.status(palace_path=...)`

Prints a human-readable report to standard output and returns a status dict
(tests/test_hnsw_capacity.py:L590-L613).

On a diverged palace (SQLite 20,000 vs HNSW 2,000), the printed output contains the
token "DIVERGED" and recommends rebuild via a command containing
`` mempalace repair` ``; the returned dict has `drawers.diverged` equal to `True`
(tests/test_hnsw_capacity.py:L590-L601). On a healthy palace (SQLite 500 vs HNSW
480), the printed output contains neither "DIVERGED" nor "Recommended"
(tests/test_hnsw_capacity.py:L604-L613).

## MCP tool status SQLite short-circuit: `_tool_status_via_sqlite()`

When `_vector_disabled` is set on the MCP server, the status tool reads counts from
SQLite instead of opening a vector client (tests/test_hnsw_capacity.py:L619-L635).
The returned dict contains: `vector_disabled` equal to `True`,
`vector_disabled_reason` equal to the configured reason string, `total_drawers`
equal to the SQLite drawer count (3 for the fixture), and a `wings` map giving
per-wing drawer counts (e.g. `ops` -> 2, `design` -> 1)
(tests/test_hnsw_capacity.py:L635-L642).

<promise>SPEC_WRITTEN path=specs/tests/test_hnsw_capacity.md citations=46</promise>
