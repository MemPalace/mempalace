# Behavior Specification — `mempalace/searcher.py`

Hybrid memory search. Combines BM25 keyword scoring with vector semantic
similarity. Direct drawer retrieval is always the baseline ("floor"); closet
hits only add a rank-based boost and can never hide a drawer the direct path
would have found (mempalace/searcher.py:L1-L10).

## Errors

A `SearchError` exception type exists and is raised when search cannot proceed,
e.g. no palace found (mempalace/searcher.py:L41-L42).

## Tokenization & BM25

`_tokenize(text)` lowercases input and extracts alphanumeric (Unicode `\w`)
tokens of length ≥ 2. A `None`/empty input yields an empty list
(mempalace/searcher.py:L45-L72).

`_bm25_scores(query, documents, k1=1.5, b=0.75)` computes Okapi-BM25 scores in
the same order as `documents`. IDF is computed over the provided corpus using
the smoothed formula `log((N - df + 0.5) / (df + 0.5) + 1)`, which is always
non-negative. Returns all zeros when the query has no tokens, the corpus is
empty, or all documents are empty. Average document length defaults to 1.0 if it
would be zero. Per-document score sums `idf[term] * (freq*(k1+1)) /
(freq + k1*(1 - b + b*dl/avgdl))` over query terms present; empty documents score
0.0 (mempalace/searcher.py:L75-L131).

## Distance → similarity mapping

`_distance_to_similarity(distance, metric="cosine")` maps a backend distance
(lower = closer) to a bounded similarity. `distance is None` → 0.0. Metric `l2`
→ `1/(1+max(0,d))`. Metric `ip` → `1/(1+e^min(60,d))` (exponent clamped to avoid
overflow). Default/`cosine` → `max(0, 1-d)` (mempalace/searcher.py:L134-L163).

`_metric_for_collection(col)` reads the collection's `distance_metric`
attribute, lowercases it, and returns it only if it is one of
`cosine`/`l2`/`ip`; any failure or unrecognized value falls back to `"cosine"`
(mempalace/searcher.py:L166-L181).

## Hybrid ranking

`_hybrid_rank(results, query, vector_weight=0.6, bm25_weight=0.4,
metric="cosine")` re-ranks a list of result dicts in place. Each result's
`text` field feeds BM25 (corpus-relative IDF over the candidates), and the raw
BM25 scores are min-max normalized within the set. Vector similarity comes from
each result's `distance` via `_distance_to_similarity` in the given metric. Final
score = `vector_weight*vec_sim + bm25_weight*bm25_norm`. Each result gains a
`bm25_score` field (raw BM25 rounded to 3 decimals). Sorted descending by final
score; ties keep stable order. Candidates with `distance=None` score on BM25
alone. Empty input is returned unchanged (mempalace/searcher.py:L184-L226).

## Where-filter construction

`build_where_filter(wing, room)` returns: `{"$and":[{"wing":wing},{"room":room}]}`
when both present; `{"wing":wing}` or `{"room":room}` for one; `{}` when neither
(mempalace/searcher.py:L229-L237).

## Closet pointer parsing

Closet documents contain lines of form `topic|entities|→drawer_id_a,drawer_id_b`
(mempalace/searcher.py:L34-L36). `_extract_drawer_ids_from_closet(doc)` parses
all `→id,id` pointers, splitting on commas, preserving first-seen order and
deduping (mempalace/searcher.py:L240-L251).

## Source-file scoping

`_scoped_source_filter(source_file, parent_drawer_id=None)` returns
`{"$and":[{"source_file":...},{"parent_drawer_id":...}]}` when a parent id is
supplied, otherwise the bare `{"source_file":...}`. This narrows a query to one
logical chunk-group when two unrelated writes share a `source_file`
(mempalace/searcher.py:L254-L276).

## Neighbor expansion

`_expand_with_neighbors(drawers_col, matched_doc, matched_meta, radius=1)`
returns a dict `{text, drawer_index, total_drawers}`. It fetches the matched
chunk's siblings at `chunk_index` offsets `[-radius, +radius]` within the same
`source_file` (further scoped by `parent_drawer_id` when present), orders them by
`chunk_index`, and joins documents with `\n\n`. `total_drawers` is the count of
drawers in the scoped source group (or `None`). If `source_file` or an integer
`chunk_index` is missing, or any backend call fails, it falls back to the matched
drawer alone (`text=matched_doc`, `total_drawers=None`)
(mempalace/searcher.py:L279-L351).

## Legacy-metric warning

`_warn_if_legacy_metric(col)` reads the collection `metadata` dict's
`hnsw:space`. If it equals `"cosine"` (or metadata is not a dict) no warning is
printed. Otherwise (missing or different value) a multi-line NOTICE is printed to
stderr advising `mempalace repair`, including the detail
`hnsw:space=<value>` or `no hnsw:space metadata`
(mempalace/searcher.py:L354-L387).

## CLI search (`search`)

`search(query, palace_path, wing=None, room=None, n_results=5)` opens the drawer
collection. If unavailable, raises `SearchError("No palace found at <path>")`
when the path is not a directory, else `SearchError("No palace database at
<path>")` (mempalace/searcher.py:L390-L399). It emits the legacy-metric warning
(L403), builds the where filter (L405), and queries with
`include=[documents, metadatas, distances]`, adding `where` only if non-empty.
Any query exception prints `"\n  Search error: <e>"` and raises
`SearchError(f"Search error: {e}")` (mempalace/searcher.py:L407-L420).

When no documents are returned, it prints `'\n  No results found for: "<query>"'`
and returns `None` (mempalace/searcher.py:L422-L428). Otherwise it builds hit
dicts `{text, distance(float), metadata}`, hybrid-ranks them, and prints a
formatted block: a header with the query and any wing/room, then per result the
index, `wing / room`, `Source:` (basename of `source_file` or `?`), a match line
`<metric>_sim=<v>  bm25=<b>`, and the verbatim text indented two spaces per line,
with a separator rule between results (mempalace/searcher.py:L438-L471). This is a
human-readable side-effect path (stdout); it returns nothing.

## SQLite BM25-only fallback (`_bm25_only_via_sqlite`)

`_bm25_only_via_sqlite(query, palace_path, wing, room, n_results=5,
max_candidates=500, _include_internal=False, collection_name=None)` reads drawers
directly from `<palace_path>/chroma.sqlite3`, bypassing the vector client. Used
when the HNSW index is diverged/unloadable so a corrupt segment cannot crash the
server (mempalace/searcher.py:L474-L499).

If `chroma.sqlite3` is missing it returns `{"error":"No palace found",
"hint":"Run: mempalace init <dir> && mempalace mine <dir>"}`. When
`collection_name` is None it resolves the configured collection name. A SQLite
open failure returns `{"error":"sqlite open failed: <e>"}`
(mempalace/searcher.py:L500-L539). The DB is opened read-only via
`sqlite_read_uri(db_path)` (mempalace/searcher.py:L537).

Candidate selection: query is tokenized and tokens of length ≥ 3 are kept and
OR-joined for an FTS5 `MATCH` against chromadb's `embedding_fulltext_search`
trigram index, joined through `embeddings`/`segments`/`collections` filtered by
collection name, limited to `max_candidates`. Wing/room filters are applied via a
correlated `EXISTS` over `embedding_metadata` (mempalace/searcher.py:L542-L564,
L511-L534). On FTS5 error it falls back to recency. When no usable token exists
(`use_recency_fallback`) and no candidates were found, it selects the most-recent
`max_candidates` rows `ORDER BY e.created_at DESC`; on schema error it retries
`ORDER BY e.id DESC`; on further error candidates become empty. A clean FTS miss
(tokens present but no match) stays empty rather than falling back to recency
(mempalace/searcher.py:L566-L622).

With no candidates it returns `{query, filters:{wing,room},
total_before_filter:0, results:[], fallback:"bm25_only_via_sqlite"}`
(mempalace/searcher.py:L624-L631).

Otherwise metadata rows are fetched for candidate ids; `chroma:document` keys
become the drawer `text`, other keys become metadata (string value preferred,
else int value) (mempalace/searcher.py:L633-L652). Wing/room filters are
re-applied in Python, building candidate dicts with `text`, `wing`, `room`,
`source_file` (basename or `?`), `created_at` (from `filed_at` or `"unknown"`),
`similarity:None`, `distance:None`, `matched_via:"bm25_sqlite"`, plus internal
`_source_file_full` and `_chunk_index` (mempalace/searcher.py:L654-L683). Local
BM25 ranks candidates (min-max normalized into `_score`), each gets a
`bm25_score`; sorted descending; top `n_results` kept; `_score` is dropped and
internal fields are stripped unless `_include_internal` is set
(mempalace/searcher.py:L685-L701).

Return shape: `{query, filters:{wing,room}, total_before_filter:<candidate count>,
results:[...], fallback:"bm25_only_via_sqlite",
fallback_reason:"vector_search_disabled"}` (mempalace/searcher.py:L703-L710).

## Union candidate merge

`_merge_bm25_union_candidates(hits, drawers_col, query, wing, room, n_results,
max_distance=0.0)` appends backend lexical candidates into `hits` in place. When
`max_distance > 0.0` it returns immediately without adding anything (BM25-only
candidates have no distance and would violate the threshold)
(mempalace/searcher.py:L713-L745). It calls `drawers_col.lexical_search(query,
n_results=n_results*3, where=...)`; an `UnsupportedCapabilityError` propagates,
other exceptions log and return (mempalace/searcher.py:L747-L758). Each lexical
hit becomes a candidate with `distance=None`, `effective_distance=None`,
`closet_boost=0.0`, `matched_via:"bm25_backend"`, `bm25_score` from the hit score,
and internal dedup fields (mempalace/searcher.py:L760-L780). Dedup key is
`(_source_file_full, _chunk_index)` when both present, else basename
`source_file`; candidates with empty/`?`/already-seen keys are skipped
(mempalace/searcher.py:L782-L801).

## Strategy dispatch

Valid `candidate_strategy` values are `"vector"` (no-op default) and `"union"`
(merges lexical candidates) (mempalace/searcher.py:L804-L810).
`_validate_candidate_strategy(strategy)` raises `ValueError` for any other value
(mempalace/searcher.py:L813-L823). `_apply_candidate_strategy` dispatches to the
registered merger; `"vector"` does nothing (mempalace/searcher.py:L826-L843).

`_finalize_candidate_hits(...)` applies the strategy; if it raises
`UnsupportedCapabilityError` it returns an error dict requiring a backend with
`lexical_search` support. Otherwise it runs the final BM25 hybrid re-rank,
truncates to `n_results`, and strips internal fields `_sort_key`,
`_source_file_full`, `_chunk_index`, `_parent_drawer_id`
(mempalace/searcher.py:L846-L881).

## Open-collection error mapping

`_backend_mismatch_result` and `_unknown_backend_result` build fixed error dicts
for backend-mismatch and unknown-backend cases
(mempalace/searcher.py:L884-L897). `_vector_disabled_search` resolves the backend
name; mismatch/unknown produce those error dicts; a non-`chroma` backend returns
a "vector_disabled fallback is Chroma-only" error; otherwise it delegates to the
SQLite BM25-only fallback (mempalace/searcher.py:L900-L929).
`_open_search_collection` opens the collection and maps backend mismatch,
unknown-backend, not-initialized/not-found, generic backend error, and any other
exception each to a distinct error dict (mempalace/searcher.py:L932-L957).

## Filtered-query fallback

`_query_drawers_with_filter_fallback(...)` runs the filtered drawer query; if it
raises and a `where` filter was present, it retries unfiltered (over-fetching
`min(n_results*15, 500)`) and re-applies the wing/room filter in Python, returning
`{documents, metadatas, distances}` with single inner lists. With no filter the
error re-raises (mempalace/searcher.py:L960-L1000).

## Programmatic search (`search_memories`)

`search_memories(query, palace_path, wing=None, room=None, n_results=5,
max_distance=0.0, vector_disabled=False, candidate_strategy="vector",
collection_name=None) -> dict` returns data rather than printing
(mempalace/searcher.py:L1003-L1048). `max_distance` filters out results whose raw
cosine distance exceeds it (0 = identical, 2 = opposite); `0.0` disables
filtering (mempalace/searcher.py:L1024-L1027).

It validates the strategy eagerly (mempalace/searcher.py:L1052). When
`vector_disabled` is set it routes to `_vector_disabled_search`
(mempalace/searcher.py:L1054-L1062). Otherwise it opens the collection (returning
the open error dict on failure), resolves the metric, and builds the where filter
(mempalace/searcher.py:L1064-L1069).

Drawer retrieval always runs as the baseline: query with `n_results*3`
over-fetch, `include=[documents, metadatas, distances]`, where applied if
non-empty, via the filter-fallback helper. Any exception returns
`{"error":"Search error: <e>"}` (mempalace/searcher.py:L1078-L1090).

Closet boost lookup: queries the closets collection (`create=False`) with
`n_results*2`; for each closet hit (in rank order) it records the first occurrence
per `source_file` as `(rank, distance, document[:200])`. Any failure logs and
degrades to drawer-only search (mempalace/searcher.py:L1092-L1118).

Boosts are rank-based: `[0.40, 0.25, 0.15, 0.08, 0.04]` for closet ranks 0–4,
applied only when closet distance ≤ `1.5` and rank is within range
(mempalace/searcher.py:L1119-L1147). For each drawer result: results with
`max_distance>0.0 and dist>max_distance` are skipped (filtered on raw distance
pre-rounding). A boosted hit gets `matched_via="drawer+closet"` and a
`closet_preview`; otherwise `matched_via="drawer"`. `effective_distance` is
`max(0, min(2, dist - boost))` (clamped to valid cosine range). Each entry
carries `text`, `wing` (default `"unknown"`), `room` (default `"unknown"`),
`source_file` (basename or `?`), `created_at` (`filed_at` or `"unknown"`),
`similarity` (3-dec rounded sim of effective distance), `distance` (4-dec raw),
`effective_distance` (4-dec), `closet_boost` (3-dec), `matched_via`, optional
`closet_preview`, and internal `_sort_key`/`_source_file_full`/`_chunk_index`/
`_parent_drawer_id` (mempalace/searcher.py:L1126-L1177).

Entries are sorted ascending by `_sort_key` (effective distance) and truncated to
`n_results` (mempalace/searcher.py:L1179-L1180).

Drawer-grep enrichment: for non-`"drawer"` (closet-boosted) hits with a known
full source, it fetches all drawers in the scoped source group; if more than one
drawer exists, it orders them by `chunk_index`, picks the chunk with the most
query-term hits, and returns that chunk plus its immediate neighbors
(`best_idx-1 .. best_idx+1`) joined by `\n\n`. Output longer than 10000 chars is
truncated with a notice pointing to `mempalace_get_drawer`. The hit gains
`drawer_index` (best index) and `total_drawers`. Backend failures skip the hit
(mempalace/searcher.py:L1182-L1238).

Then the candidate strategy hook runs (`union` merges lexical candidates,
forwarding `max_distance`), followed by the final hybrid re-rank and internal
field stripping; a strategy error dict short-circuits the return
(mempalace/searcher.py:L1240-L1259).

Final return shape: `{query, filters:{wing,room}, total_before_filter:<drawer
document count>, results:[...]}` (mempalace/searcher.py:L1261-L1266).

## Virtual line numbering (pure, no I/O)

Drawers are stored verbatim; a line-number grid is applied only at read time and
source text is never mutated (mempalace/searcher.py:L1269-L1277). A line is
"already numbered" iff it starts with `[<digits>]`
(mempalace/searcher.py:L1281).

`render_with_line_numbers(text, start_line=1)` prefixes each line with `[N] `
starting at `start_line`. Lines already beginning with `[<digits>]` pass through
unchanged but still advance the counter. `None`/empty text returns `""`
(mempalace/searcher.py:L1284-L1301).

`extract_line_range(text, line_start, line_end)` returns the 1-indexed inclusive
slice rendered with line numbers. Empty text or `line_end < line_start` returns
`""`. Bounds are clamped (start to ≥1, end to ≤ line count); if effective start >
effective end it returns `""`. The slice is rendered starting at the effective
start line (mempalace/searcher.py:L1304-L1324).
