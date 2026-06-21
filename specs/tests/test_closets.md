# Behavior Spec: test_closets.py

This is a test module. The behaviors below are the *contracts the production
code must satisfy* as asserted by these tests. Each claim cites the asserting
test lines in `tests/test_closets.py`. This file exercises the closet
(searchable index) layer, mine-lock serialization, entity metadata extraction,
hybrid BM25+vector search, diary ingest, cross-wing tunnels, and drawer-grep
neighbor expansion (tests/test_closets.py:L1-L24).

## Subject Surface Under Test

The module imports and pins the behavior of: `mine`,
`_extract_entities_for_metadata`, `_load_known_entities` from the miner;
`CLOSET_CHAR_LIMIT`, `build_closet_lines`, `get_closets_collection`,
`get_collection`, `mine_lock`, `purge_file_closets`, `upsert_closet_lines` from
the palace layer; `create_tunnel`, `delete_tunnel`, `follow_tunnels`,
`list_tunnels` from the palace graph; and `_bm25_scores`,
`_expand_with_neighbors`, `_extract_drawer_ids_from_closet`, `_hybrid_rank`,
`search_memories` from the searcher (tests/test_closets.py:L37-L63).

## mine_lock — inter-process serialization

`mine_lock(target)` is a context manager. On entry it creates the lock
directory `~/.mempalace/locks` (an expanded home path) if absent, and on exit
releases the lock; re-acquiring the same target after release succeeds
effectively instantly (under 1 second) (tests/test_closets.py:L88-L97).

The lock provides mutual exclusion across separate OS processes (not merely
threads). When two processes each acquire `mine_lock` on the same target and
each holds it for a fixed interval, their critical-section `[enter, exit]`
time intervals must be disjoint: sorting the two intervals by entry time,
`enter_a < exit_a <= enter_b < exit_b` must hold — the second process cannot
enter until the first has exited (tests/test_closets.py:L99-L146). A worker
inside the lock records `"<name> <enter_ts> <exit_ts>"` lines appended to a log
file (tests/test_closets.py:L69-L84).

## build_closet_lines — pointer-line shape

`build_closet_lines(source_file, drawer_ids, content, wing, room,
drawer_metas=None)` returns a list of one or more index "closet" lines; it
always emits at least one line (tests/test_closets.py:L160-L167).

In the legacy 3-segment form, each line is `topic|entities|→refs`: pipe-split
into exactly 3 parts, the third part begins with `→`, and the line contains a
`→` arrow (tests/test_closets.py:L168-L173, L269-L274).

Section headers in the content (lines beginning with `#` / `##`) are extracted
and surface as topics in the emitted lines (tests/test_closets.py:L174-L179).

Entity extraction applies a stoplist: common capitalized sentence-starters
("When", "After", "The") that repeat are dropped and must not appear in the
`;`-separated entity segment (the second pipe segment)
(tests/test_closets.py:L181-L197). Genuine proper nouns ("Igor", "Milla") that
repeat survive the stoplist and appear in the entity segment
(tests/test_closets.py:L199-L208).

When nothing extractable exists in the content, exactly one fallback line is
emitted whose topic is `wing/room/<filename-stem>` (e.g. `wing/room/notes`) and
which references the drawer ids (e.g. `→d1`) (tests/test_closets.py:L210-L215).

The pointer references at most the first three drawer ids: given many ids, every
line contains `→drawer_0,drawer_1,drawer_2` (tests/test_closets.py:L217-L220).

### build_closet_lines — date + line-range segment (Tier 6a)

When `drawer_metas` is provided and each meta carries `filed_at` plus
`line_start` and `line_end`, every pointer line gains a 4th pipe segment of
shape `YYYY-MM-DD:Lstart-Lend` placed between the entities segment and the
`→drawer_ids` segment. The line splits into exactly 4 parts; part index 2 is
e.g. `2026-05-21:L42-L78` and part index 3 begins with `→`
(tests/test_closets.py:L224-L267).

The date is only the `YYYY-MM-DD` prefix of `filed_at` (not the full ISO
timestamp): the segment starts `2026-05-21:L` and must not contain the time
separator `T` (tests/test_closets.py:L299-L318).

When a meta also carries `content_date`, the date segment prefers
`content_date` (content-time) over `filed_at` (ingestion-time): e.g. a meta with
`filed_at=2026-05-21...` and `content_date=2024-11-08` produces segment
`2024-11-08:L42-L78` (tests/test_closets.py:L320-L347).

Backward compatibility: if `drawer_metas` is omitted entirely, lines use the
3-segment form (tests/test_closets.py:L269-L274). If metas are present but lack
`line_start`/`line_end` keys, lines also fall back to 3 segments — never a
broken pointer containing `None` or empty values (tests/test_closets.py:L276-L297).

## upsert_closet_lines — overwrite semantics and char packing

`upsert_closet_lines(collection, closet_id_base, lines, metadata)` writes
numbered closet documents under ids `<base>_NN` (zero-padded two digits, e.g.
`<base>_01`) (tests/test_closets.py:L354-L368, L382-L386).

It is a pure overwrite, never an append: a second call with entirely different
lines fully replaces the prior content — the new lines are present and none of
the prior lines remain (tests/test_closets.py:L354-L370).

It packs lines into multiple closet documents bounded by `CLOSET_CHAR_LIMIT`
without ever splitting an individual line across documents. Four 600-char lines
pack into 2 closet documents; the call returns the number of documents written
(here `2`); every line inside each document retains its full length (no
truncation) and each document's total length is `<= CLOSET_CHAR_LIMIT`
(tests/test_closets.py:L372-L386).

## purge_file_closets — scoped deletion

`purge_file_closets(collection, source_file)` deletes only closet documents
whose metadata `source_file` matches the given value; closets for other source
files remain (tests/test_closets.py:L393-L406).

## Project miner — closet rebuild end-to-end

A project is mined via `mine(project_path, palace_path, wing_override=...,
agent=...)`. The project root contains a `mempalace.yaml` declaring the wing and
rooms (tests/test_closets.py:L414-L425).

Re-mining a file fully replaces its closets. After a first mine produces many
numbered closets and the file is shrunk (with bumped mtime so the change is
detected), a second mine produces closets reflecting only the new content; no
stale topic text from the first mine survives, and numbered closet ids that
existed only in the larger first run are deleted (no orphans)
(tests/test_closets.py:L413-L454). Closets are retrieved via
`get_closets_collection(palace).get(where={"source_file": <path>})`, returning
`ids` and `documents` (tests/test_closets.py:L427-L431).

The production miner emits 4-segment Tier 6a pointers with content-date. Mining
a file whose name carries a date (e.g. `2024-11-08-conversation.md`) produces at
least one closet document containing `2024-11-08:L`, and every closet line that
contains both a date locator and a `→` arrow splits into exactly 4 pipe segments
(tests/test_closets.py:L456-L503).

## _extract_drawer_ids_from_closet — pointer parsing

Parses drawer ids from closet document text. A single pointer
`topic|;|→drawer_x` yields `["drawer_x"]` (tests/test_closets.py:L510-L511). A
comma-separated pointer `→drawer_a,drawer_b,drawer_c` yields all three in order
(tests/test_closets.py:L513-L515). Ids are de-duplicated across multiple lines
while preserving first-seen order (tests/test_closets.py:L517-L519). Empty input
or input with no arrows yields `[]` (tests/test_closets.py:L521-L523).

## search_memories — hybrid drawer + closet path

`search_memories(query, palace_path, max_distance=...)` returns a dict with a
`results` list; each hit is a dict (tests/test_closets.py:L533-L539).

Direct drawer search is always the floor: a palace with no closets still returns
drawer hits, and every such hit has `matched_via == "drawer"`,
`closet_boost == 0.0`, and no `closet_preview` key
(tests/test_closets.py:L530-L538).

When a closet agrees with direct search on `source_file`, the matching drawer's
`matched_via` becomes `"drawer+closet"`, `closet_boost` is `> 0`, and
`closet_preview` exposes the hydrated index line (containing the `→drawer_...`
pointer). The boosted matching drawer surfaces (e.g. its `text` contains "JWT")
(tests/test_closets.py:L540-L568).

`max_distance` filters hybrid hits: with `max_distance=0.001`, every returned
hit has `distance <= 0.001` (tests/test_closets.py:L570-L583).

## Entity metadata extraction

`_extract_entities_for_metadata(text)` returns a `;`-separated string of
extracted proper-noun entities. Capitalized names repeated in text are extracted
(e.g. "Ben", "Igor") (tests/test_closets.py:L590-L594). Text with no proper
nouns returns the empty string (tests/test_closets.py:L596-L598). Multiple
entities are joined with `;` (tests/test_closets.py:L600-L603).

The same stoplist applies: capitalized sentence-starters ("When", "After",
"The") are excluded even when repeated (tests/test_closets.py:L605-L618).

The output list is capped before joining so a name is never truncated mid-token:
given many distinct names, every extracted token is a whole name from the input
set (tests/test_closets.py:L620-L662).

## Known-entity registry caching

`_load_known_entities()` reads the registry JSON file (path
`miner._ENTITY_REGISTRY_PATH`) of shape `{"people": [...]}` and returns the
names. It caches by file mtime: the registry file is not re-read on a second
call when its mtime is unchanged; bumping the mtime invalidates the cache and a
subsequent call reflects the updated file contents. The cache state is held in
`miner._ENTITY_REGISTRY_CACHE` with keys `mtime` and `names`
(tests/test_closets.py:L664-L696).

## BM25 scoring (real IDF over candidate corpus)

`_bm25_scores(query, candidate_docs)` returns one score per candidate. A doc
matching the query scores `> 0`; a non-matching doc scores `0.0`
(tests/test_closets.py:L703-L709). A query with no term overlap yields all
`0.0` (tests/test_closets.py:L711-L712). IDF down-weights query terms present in
every candidate and up-weights rare terms, so the doc containing the rare query
term wins (tests/test_closets.py:L715-L726). Edge cases: empty query over one
doc yields `[0.0]`; any query over an empty candidate list yields `[]`; a query
over a single empty doc yields `[0.0]` (tests/test_closets.py:L728-L731).

`_hybrid_rank(results, query)` reranks vector results by combining vector
distance with keyword (BM25) signal. A keyword-rich result outranks a
closer-vector but keyword-irrelevant one; the returned hits expose a
`bm25_score` field for debugging and must not expose the internal
`_hybrid_score` key (tests/test_closets.py:L733-L744). Ranking uses absolute
`(1 - distance)` normalization, not relative `distance / max_distance`: adding a
much-worse candidate to the set does not reshuffle the top two
(tests/test_closets.py:L746-L758).

## Diary ingest

`ingest_diaries(diary_dir, palace_dir, wing=..., force=...)` (from
`mempalace.diary_ingest`) returns a dict with `days_updated`. With `force=True`
on a diary file it creates drawers (palace collection `count() >= 1`) and
`days_updated >= 1` (tests/test_closets.py:L765-L777).

A second run with unchanged content skips: `days_updated == 0`
(tests/test_closets.py:L779-L791).

Change detection uses content hash, not byte length. An in-place edit that
preserves total length (e.g. "Teh"→"The") still triggers re-ingest
(`days_updated == 1`); the drawer is updated to hold the corrected text, and the
closet (search index) is rebuilt to reflect the edit (no stale pre-edit content
remains in either) (tests/test_closets.py:L793-L828).

Legacy state backfill: a state entry lacking `content_hash` (carrying only
`size`, `entry_count`, `ingested_at`) skips on a size-match run
(`days_updated == 0`) but records the backfilled `content_hash` =
SHA-256 hex of the UTF-8-encoded file content, so the strict check engages on
later runs (tests/test_closets.py:L830-L869).

State file location: the ingest state file lives outside the user's diary
directory. No file with `diary_ingest` in its name appears in the diary dir.
The state file resolved by `_state_file_for(palace_path, diary_dir_resolved)`
exists under `~/.mempalace/state/` — its parent directory is named `state` and
its grandparent is named `.mempalace` (tests/test_closets.py:L871-L899).

Drawer ids are wing-prefixed to prevent cross-diary collisions. Two diaries
sharing a date but ingested under different wings (`personal`, `work`) produce
distinct drawers; `_diary_drawer_id_entry(wing, date, entry_idx, chunk_idx)`
yields different ids for `personal` vs `work`, and each drawer holds its
wing-specific content (tests/test_closets.py:L901-L937).

### Per-entry drawers and chunking (#1539)

Each `##` entry in a diary becomes its own drawer rather than one
file-level drawer: a file with 3 `##` entries yields exactly 3 drawers
(tests/test_closets.py:L941-L960).

When a single `##` entry exceeds the chunk size, that entry is split into
multiple bounded drawers: no drawer document exceeds 800 characters
(`CHUNK_SIZE=800`), and a file with 3 entries where the middle is oversized
yields `>= 4` drawers (tests/test_closets.py:L962-L995).

Incremental ingest adds only the delta: appending one new `##` entry to a
2-entry file (already at 2 drawers) yields exactly 3 drawers after re-ingest
(tests/test_closets.py:L997-L1022).

The persisted `entry_count` watermark equals the number of entries split from
the file by `_split_entries(text)` (and thus the drawer count). The state key is
`"diary|<filename>"` (tests/test_closets.py:L1024-L1049).

`chunk_index` is a file-global contiguous counter `0..N-1` across all entries
(not per-entry); all drawers from one file share a single `source_file`. This
lets neighbor expansion query by `source_file` + `chunk_index` range
(tests/test_closets.py:L1051-L1086).

Shrinking a diary purges orphan drawers: a file shrunk from 3 entries to 1
yields exactly 1 drawer after re-ingest (the prior pass's trailing 2 drawers are
purged via full rebuild on content change) (tests/test_closets.py:L1088-L1121).

A `##` header-only entry (no body) still produces a drawer carrying the header
text: two header-only entries yield 2 drawers and the header text appears in the
drawer documents (tests/test_closets.py:L1123-L1147).

### Atomic batched upsert on failure

Diary ingest accumulates all of a file's drawers into a single batched upsert
per file. If the upsert raises mid-pass (simulated embedding failure), the
`RuntimeError` propagates, exactly one upsert call is attempted per file (not one
per entry), and the real collection is left with zero drawers for that source
file — no partial write (tests/test_closets.py:L1149-L1215).

## Cross-wing tunnels

Tunnels are explicit, undirected cross-wing connections persisted as JSON. Tests
redirect the resolver `palace_graph._get_tunnel_file` to a temp `tunnels.json`
and neutralize endpoint-existence validation via `_get_collection`
(tests/test_closets.py:L1221-L1249).

`create_tunnel(src_wing, src_room, tgt_wing, tgt_room, label=...)` returns a
record with non-empty `id`, nested `source` `{wing, room}`, nested `target`
`{wing, room}`, and `label` (tests/test_closets.py:L1251-L1258).

`list_tunnels(wing=None)` returns all tunnels, or only those touching the given
wing on either endpoint; a wing on neither endpoint yields zero
(tests/test_closets.py:L1260-L1267).

`delete_tunnel(id)` removes the tunnel; afterward `list_tunnels()` is empty
(tests/test_closets.py:L1269-L1272).

Dedup by endpoints updates the label: creating a tunnel with the same endpoints
again does not add a second record — it updates the existing label, leaving one
tunnel (tests/test_closets.py:L1274-L1279).

`follow_tunnels(wing, room)` returns connected endpoints. For an endpoint with
two tunnels, two connections are returned, exposing `connected_wing` for each;
unrelated tunnels do not surface (tests/test_closets.py:L1281-L1290).

Tunnels are symmetric/undirected: `create(A, B)` and `create(B, A)` resolve to
the same canonical id and dedupe into one record (the second call updates the
label) (tests/test_closets.py:L1294-L1302). `follow_tunnels` works from either
endpoint, and both surfaces carry the same `label`; following from the source
yields the target's wing as `connected_wing` and vice versa
(tests/test_closets.py:L1304-L1315).

`create_tunnel` rejects empty or whitespace-only strings on any of the four
endpoint fields with `ValueError` (tests/test_closets.py:L1317-L1330).

A corrupt/truncated `tunnels.json` is treated as empty (`list_tunnels()`
returns `[]` without raising), and a subsequent `create_tunnel` persists cleanly
over it (tests/test_closets.py:L1332-L1348).

Writes are atomic (write-then-replace): after a successful create, the
`tunnels.json` exists and no leftover `tunnels.json.tmp` remains
(tests/test_closets.py:L1350-L1357).

Concurrent creates preserve all tunnels: five threads each creating a distinct
tunnel (released from a barrier) raise no errors and result in exactly 5
persisted tunnels (no write race drops any) (tests/test_closets.py:L1359-L1383).

`created_at` is timezone-aware UTC: the ISO string ends with `+00:00` or `Z`
(tests/test_closets.py:L1385-L1389).

## Drawer-grep neighbor expansion

`_expand_with_neighbors(collection, matched_doc, matched_meta, radius=1)`
returns a dict with keys `text`, `drawer_index`, `total_drawers`. It fetches
sibling chunks of the matched chunk's source file and joins matched ±radius
neighbors in `chunk_index` order (tests/test_closets.py:L1402-L1438).

For a 5-chunk file with matched `chunk_index=2`, output has `drawer_index == 2`,
`total_drawers == 5`, and text joins chunks 1, 2, 3 in order while excluding 0
and 4 (tests/test_closets.py:L1421-L1438).

At the start of a file (`chunk_index=0`) only the next neighbor is included
(chunk_0 and chunk_1; never a nonexistent chunk_-1)
(tests/test_closets.py:L1440-L1452). At the end only the previous neighbor is
included (tests/test_closets.py:L1454-L1466). A single-drawer file returns just
the matched chunk text, `drawer_index == 0`, `total_drawers == 1`
(tests/test_closets.py:L1468-L1477).

When metadata lacks `source_file`/`chunk_index`, it degrades gracefully:
`text` is the matched doc, and `drawer_index` / `total_drawers` are both
`None` (tests/test_closets.py:L1479-L1485).

### End-to-end enrichment in search_memories

When a closet boosts a source with many drawers, the hit gains `total_drawers`
(the source's chunk count) and an integer `drawer_index`, and `text` includes the
grep-best chunk plus neighbors (tests/test_closets.py:L1487-L1527).

### Isolation by parent_drawer_id (#1580)

When two logical drawer groups share the same `source_file` but have different
`parent_drawer_id` values, expansion scopes neighbors to the matched group's
`parent_drawer_id`. Group A's chunks are returned (in `chunk_index` order) and
group B's chunks do not leak in; `total_drawers` is the matched group's chunk
count (e.g. 2), not the file-global row count (e.g. 4)
(tests/test_closets.py:L1529-L1610).

Backward compatibility: drawers without a `parent_drawer_id` use the 2-clause
`source_file + chunk_index` filter, so file-global neighbor expansion is
unchanged (a 5-chunk file returns `total_drawers == 5` with chunks 1/2/3 around
matched index 2) (tests/test_closets.py:L1612-L1633).

The end-to-end `search_memories` enrichment honors `parent_drawer_id` isolation:
with two groups sharing a `source_file` and a closet boosting group A, the
boosted hit's `text` contains only group A content ("alpha"), never group B
("bravo"), and `total_drawers == 2` (the matched group)
(tests/test_closets.py:L1635-L1732). Internal scoring keys
`_parent_drawer_id`, `_source_file_full`, `_chunk_index`, and `_sort_key` are
scrubbed from every returned result (tests/test_closets.py:L1737-L1741).

Asymmetric groups: group A with 1 chunk and group B with 3 chunks under a shared
`source_file`; expanding around group A's single chunk returns only that chunk
text with `total_drawers == 1` (not the file-global 4)
(tests/test_closets.py:L1743-L1803).

An empty-string `parent_drawer_id` is treated as absent, degrading to the
file-global 2-clause filter (mirroring the empty `source_file` handling); a
3-chunk file returns `total_drawers == 3` with all chunks present
(tests/test_closets.py:L1805-L1828).
