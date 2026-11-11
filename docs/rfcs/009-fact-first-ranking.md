# Fact-first search

## Goal

Return matching, readable facts before verbatim drawers. MCP and hub search add
a `facts` list; CLI search prints Facts above drawers. Facts carry their original
thread/range or drawer pointer, origin, approval, lifecycle and successor ID.
The existing drawer search still runs with the same limits, filters and order.

## Scoring

Reuse the query sanitizer and lexical tokenizer, treating predicate underscores
as word separators. Ignore English question/predicate glue and configured locale
stop words. Preserve capitalized names mid-sentence, standalone names and all-caps
acronyms such as `Will` and `IT`; sentence-initial capitalization alone does not
preserve question glue. Lowercase `who` and `can` remain fillers, including when
the graph contains `WHO` or `Can`. A filler-only query returns no facts; every
returned fact must match at least one remaining whole word.

Ignore query terms absent from the fact vocabulary. If any remaining query term
occurs in at most half the facts, use those terms to seed the lookup; otherwise
keep all remaining meaningful terms. Unknown words cannot suppress this fallback.
This avoids scanning the corpus for universal words when a selective term is
available. Known query terms contribute to final IDF relevance.

Select at most 50 scoped subject/predicate seed keys in SQL. Selective queries
start with 50 BM25 matches; if duplicate keys leave slots empty, fill from a
bounded prefix of up to 1,000 recent scoped matches (`ORDER BY rowid DESC`).
All-common queries use that bounded prefix directly, avoiding a full BM25 sort.
Scope and as-of filters run inside
both seed lookups, before their limits. Empty scopes and selective prefixes with
distinct keys skip the fallback lookup entirely. Group the prefix by key using
each key's best BM25 score, then expand selected keys to retain their full visible
history and current successors. Final lexical relevance is the fraction of query
IDF weight matched:

`idf(term) = ln(1 + (N - df(term) + 0.5) / (df(term) + 0.5))`

`lexical_relevance = sum(idf(matched_terms)) / sum(idf(query_terms))`

`score = lexical_relevance * status_weight * decay`

All ranking constants live in `mempalace/searcher/facts.py`:

| Status | Weight |
| --- | ---: |
| Current, approved | 1.0 |
| Current, proposal | 0.85 |
| Current, unknown | 0.75 |
| Ended, superseded or invalidated | 0.25 |

Decay is `max(0.5, 2 ** (-age_days / 180))`. Age starts at the latest
`fact_sources.added_at`, falling back to `valid_from`, then creation time.
Approved facts have decay 1.0. A future observation has age zero. Date-only
validity bounds retain KG semantics. Historical facts remain visible, but every
matching current fact precedes ended facts with the same subject and predicate.
An explicit as-of read limits candidates to the interval valid then.

## Scope and cost

Use the existing KG wing/sensitivity filters and gateway caller policy, including
permission to read KG facts. Never widen a scoped search. A local SQLite FTS
index supplies candidates; one indexed query also reads their term frequencies
and observed sources after the seed cap. FTS maps to stable triple IDs through
an INTEGER PRIMARY KEY mapping, so VACUUM cannot misalign facts and search hits.
The gateway performs one fact lookup across allowed wings, independently of its
existing drawer fan-out and uses the existing KG policy for scope enumeration.
The separate KG import/provenance work owns cheap wing listing. Callers that
discard facts explicitly disable their lookup. Missing graphs return no facts
and do not create a palace file. Any fact-side exception returns an empty facts
list and a generic `facts_error` marker; drawer search remains independent. Index
build failure also leaves ordinary KG reads and writes available: roll back the
build, disable derived write hooks, and retry on the next graph open. Readiness
checks include all index tables, the key index and all four maintenance triggers.
Search-mode KG queries use the same query sanitizer as drawer search, rather
than the entity-name length limit. CLI capture omits an empty facts list unless
there is a fact-side error marker, matching direct MCP search.

Wide scopes check each FTS hit against the triple primary key instead of
materializing every visible triple ID. Empty-scope checks reuse the capped
visibility count, so an empty scope runs that probe once. With the default seed
limit, all-common queries count at most 5,001 visible rows and use bounded
document IDs for scopes of at most 5,000 rows. Selective queries retain at most
1,001 probe rows and 1,000 document IDs; broad selective queries stop the probe
after 51, and rare terms can stop it earlier. Both small paths require fewer
visible rows than the estimated matching population; other queries follow FTS
hits with primary-key checks.
The small-scope path bounds the FTS rowid range and checks membership in that
small ID set, then sorts its matches. This avoids recomputing BM25's global term
counts for every individual rowid. Both paths enforce the same scope and as-of
filters before limiting seeds and when expanding selected keys' history.

## Known limits

Dutch stop words are out of scope; there is currently no Dutch locale. Without
an applicable configured locale, non-English glue can still produce weak matches.
All-common queries and duplicate-key top-ups consider only a bounded scoped
prefix. Keys outside that prefix may be omitted, and a prefix dominated by one
key may return fewer than 50 distinct keys. History is retained in full for
selected keys, so very large histories still add lookup cost. Drawer retrieval
and recall remain independent of these fact candidate limits.

All-common queries use the small-scope path up to 5,000 visible rows; selective
queries retain their narrower bound. The small path's cost follows the scope's
docid span, rather than its row count: scattered rows can span most of the index.
All-common queries on scopes at the oldest docids have an accepted regression
band above that bound. At 200k facts, scopes of 5,001-20,000 visible rows can run
roughly 10-30% slower than develop: 5,001 rows take 281-305 ms versus 229-232 ms,
and 20,000 rows take 265-277 ms versus 240-243 ms. At 50k facts, 5,001 rows take
about 67-78 ms versus 58-60 ms, exceeding the 50 ms target. The path is chosen by
row count rather than docid spread, so a compact old scope just above the bound
walks most FTS hits newest-first instead of using its short docid range. A bounded
choice based on docid spread is the follow-up; the current bounds stay unchanged.

## Non-goals

No extraction, embeddings, AAAK, external services, source replication or import
repair. No fact-driven truncation or reordering of drawers; no source-drawer
boost in this step. Facts remain index entries pointing to the verbatim source.

## Eval gates

- Fact-first `fact_recall_bench.run(retriever=...)`: every positive bar 1.0,
  stale rate 0, private leaks 0, including a successor and matching stale value.
- Compare plain drawer search and fact-first search on the same corpus; no bar
  regresses. Report the exact numbers and the adapter's source mapping.
- Fact provenance benchmark passes unchanged.
- Existing searcher tests pass unchanged; a cheap local drawer benchmark keeps
  its recall. Tests cover weights, decay, boundaries, whole words, provenance,
  one indexed lookup, unchanged drawers and real gateway privacy/scope.
- Deterministic gates cover filler/noise rejection, rare-term ranking, the SQL
  seed cap with key history and diversity, unknown words in tiny graphs,
  capitalized names/acronyms, long queries across gateway and local MCP, optional
  FTS index setup, source ordering, generic error isolation, and VACUUM.
- The seeded latency probe reports before/after added lookup time at 5k, 20k,
  50k and 200k facts. Broad, selective and all-common queries run unscoped,
  within one wing, with gateway wing/sensitivity filters, within a small
  non-empty scope, and with all matches hidden, targeting at most 50 ms at
  50k outside the accepted old-scope band above. The old_5k/old_5001 pair tracks
  both sides of the small-scope boundary.
- Run lint, format and the requested broad suite without changing budget tests.
