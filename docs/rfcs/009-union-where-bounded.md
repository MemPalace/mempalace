# RFC 009: Union search where lexical search is bounded

Status: proposed, 31 Oct 2026. Redesign of the union default (PR #38). Revised after design review.

## Why

`union` (vector + lexical candidates) lets default search find text that is not embedded, or embeds badly: ids, paths, error strings, keyword-only records (RFC 008 B1c). PR #38 made it the global default. Its last round counted term frequencies over the whole collection, before the wing filter, and returned a data-dependent skip reason. A connection that may read only some wings could then tell whether a word exists elsewhere. The review also found that FTS5 `bm25()` ranks with whole-table statistics, so lexical ranking (closet boosts included) already shifts when an unreadable wing changes. Two more gaps: pgvector and Qdrant score lexical candidates in Python over every row in scope, and long questions that start with a capitalised word bypassed the long-prose rule.

## Privacy property

For a search scoped to wings W, everything the response contains (hits, scores, order, stats, reasons) is a function of the query and the content of W only. Timing is out of scope: response time already depends on unrelated load. What is in scope is anything that lets one principal observe another's queries. So caches are never shared across principals.

Known exceptions (pre-existing, follow-up): fact ranking uses graph-wide statistics, and Chroma’s vector-disabled fallback uses whole-index FTS rank above 20,000 scoped drawers. The drawer/closet differential tests exclude facts and that fallback.

## Rules

1. **Bounded per call.** Union-mode lexical retrieval runs only when the `where` filter and a LIMIT can both go inside the index query: Chroma's SQLite FTS path, sqlite_exact, compact, for filters on wing/room/source that push down. Otherwise the search resolves to `vector`, with no post-filter, recency or scan fallback. Backends with no bounded path (pgvector, Qdrant, Milvus) always resolve to `vector`, and a test pins it.
2. **Default.** `candidate_strategy` defaults to `None`, resolved per call by rule 1: `union` when bounded, else `vector`. An explicit `union` follows rule 1 too. An explicit `vector` is honoured.
3. **Scope-local ranking.** Per selected term: `MATCH '"<term>"' AND <filter> ORDER BY rowid DESC LIMIT cutoff+1`.
   - The fetch gives the term's in-scope document frequency.
   - Drawer candidates are first ranked by the sum of in-scope IDF of matched terms using rowid sets and indexed length metadata only. Equal weights favour shorter documents (unicode61 FTS5 docsize; Chroma SQL text length), then newer rowids. At most max(100, 10 × requested lexical results) candidates receive full BM25 scoring; this is approximate within equally covered terms. BM25 uses candidate-pool N and average length, both entirely in scope. The top k are hydrated. The final hybrid rerank scores the full query with index-normalised BM25 over its in-scope candidate pool; selected-term index matches only add the match floor. Closets retain their full bounded match pool.
   - There is no separate probe query and no probe cache.
   - The same scope-local ranking replaces `bm25()` in the closet lexical path, so closet boosts no longer depend on other wings. Closets use full-query scope-local BM25 without the union term selection or DF rejection: the cutoff bounds drawer candidate pools, while closet pointer lines must preserve develop's boosts. Closet queries retain the 256-token/64-character caps and quoted terms. Each term fetch has a hard LIMIT of 5,000 matched closets; reaching that LIMIT disables all closet boosts for that search instead of scanning or ranking an incomplete pool. Statistics come only from in-scope closets, so removing the old table-wide influence of drawers and other collections can reorder boosted closets; closets never gate drawer recall.
4. **Term selection** (shared, deterministic):
   - Normalise terms with the index tokenizer. Quoted spans require at least one non-stop index word meeting the per-token minimum; empty, whitespace-only and stop-word-only spans are dropped, while valid spans retain their phrase semantics.
   - A literal stays one term; where the index tokenizer splits it, it is fetched and scored as a phrase.
   - Cap the query at 256 tokens and each index term at 64 characters. Over-length literals use a prefix fetch, but drawer tf counts exact untruncated occurrences and prefix-only membership supplies no match floor. Drop terms shorter than the index minimum (Chroma trigram: 3). Quote every term, doubling any `"`.
   - Classes in priority order: literals (a digit, `_ / . :`, a quoted span, or mixed case inside a word such as `getUser`; not hyphens alone, not all-caps words alone), then proper names (capitalised words that are not the first word of a sentence: the query start or after `. ! ?`), then other words.
   - Take the first 8 terms in priority order whose in-scope df is between 1 and the cutoff (5% of the scope, bounded to 1,000–2,000). At most 16 term fetches per search.
   - Count the query's tokens after stop words are removed. A query with more than 12 tokens uses literals and names only. With none of either it skips lexical. A long question with an incidental date can therefore miss a lexical-only answer containing only ordinary topic words; topic-word-next-to-literal recall is required for queries of at most 12 post-stopword tokens.
5. **Multi-wing search.** The gateway searches all wings the token may read in one worker call: a `wings` list becomes one `$in` filter, giving one term selection, one ranking and no merged re-rank. `wings` is a public search argument. The gateway always intersects it with the token's readable wings (private wings need `private:read`) before calling the worker.
6. **Responses carry only query-shape reasons:** `long_query_no_literal` and `no_terms_after_stopwords`, computed without touching data. Anything that depends on stored content (which terms were dropped and why, fallback causes such as `candidate_strategy_fallback`) stays internal (debug log, no content). The exception is a reason that depends only on the backend and the filter shape: `lexical_unbounded_for_filter`.
7. **Backends' public `lexical_search`** keeps its develop signature. Union drawer retrieval uses the bounded term selection of rule 4. Closets use the full-query scope-local ranking of rule 3, without union term selection or DF rejection, with the same query caps and quoting; reaching the per-term 5,000-match LIMIT disables closet boosts for the search.

## Gates

- **Privacy property tests**, on random corpora, for Chroma (HNSW stubbed, because filtered ANN results depend on vectors outside the scope), sqlite_exact and compact. For a search scoped to W, the chosen terms, the lexical ids and the full response are each asserted equal whether or not other wings contain the query words. Cases:
  - df crossing the cutoff;
  - scopes with more matches than the LIMIT;
  - closets;
  - two principals sharing one worker;
  - filters that can't be pushed down falling back to vector;
  - the gateway multi-wing path.
- **Latency:** union p95 ≤ max(vector p95 + 25%, vector p95 + 50 ms) on both backends, warm and cold, on:
  - 60k and 200k drawers;
  - the 123-token log and a 50-word prompt (all wings and one wing);
  - a 12-token everyday question and 8 common words;
  - long questions starting with a capital word, and long prose with one literal;
  - a small wing whose terms are common in other wings;
  - CJK text (at most one term fetch per run);
  - the gateway multi-wing search with each of these.
- **Recall:**
  - the PR #39 gate through default search;
  - the needle shapes from PR #38 (ids, paths, ports, quoted phrase, surname, short traceback id, topic word next to a rare literal) on both backends;
  - recall@10 on the existing benchmarks not lower than develop.
- **Closets:** boosts on a single-wing palace equal to develop's.
