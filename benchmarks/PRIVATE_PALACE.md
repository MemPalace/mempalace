# Private Palace search benchmark

This harness compares retrieval algorithms against a real Palace without
copying drawer text into benchmark reports. Queries and judgments stay in a
local JSONL dataset; reports contain case IDs, ranked logical drawer IDs,
metrics, timings, and non-content corpus state only.

Keep datasets and reports outside the repository, for example under:

```text
~/.mempalace/benchmarks/search/my-suite/
```

## 1. Create the query set

```powershell
uv run python benchmarks/private_palace_bench.py init `
  --dataset "$HOME/.mempalace/benchmarks/search/my-suite/cases.jsonl"
```

Each JSONL row has this shape:

```json
{"id":"q-001","query":"Which database did we choose?","judgments":{},"filters":{"wing":"project"},"tags":["dev","decision"],"expect_no_results":false}
```

Use at least 50 development questions and 100 held-out questions. Include
exact names, paraphrases, dates, preferences, cross-session questions,
Portuguese and English, ambiguity, and expected misses. Add `dev` or `test`
to `tags`; do not tune on the held-out set. For an expected miss, set
`"expect_no_results": true` and keep every judgment at grade 0.

## 2. Build a blinded judgment pool

```powershell
uv run python benchmarks/private_palace_bench.py pool `
  --dataset "$HOME/.mempalace/benchmarks/search/my-suite/cases.jsonl" `
  --out "$HOME/.mempalace/benchmarks/search/my-suite/pool.json" `
  --tag dev `
  --pool-depth 20
```

The pool is the shuffled union of every algorithm's top candidates. It hides
which algorithm retrieved each drawer and does not contain drawer text. Open
each candidate locally with `mempalace_get_drawer`, then add its grade to the
case's `judgments` object:

- `0`: irrelevant
- `1`: related context, but not useful evidence
- `2`: useful answer evidence
- `3`: exact or direct evidence

Re-grade a hidden 10% sample to check judgment consistency. Because pooled
judgments are not necessarily exhaustive, the report calls its recall metric
`pooled_recall@k` rather than claiming corpus-wide recall.

## 3. Run the benchmark

Start with a small development smoke run:

```powershell
uv run python benchmarks/private_palace_bench.py run `
  --dataset "$HOME/.mempalace/benchmarks/search/my-suite/cases.jsonl" `
  --out "$HOME/.mempalace/benchmarks/search/my-suite/dev-report.json" `
  --tag dev `
  --limit 10 `
  --ks 1,5,10 `
  --warmups 1 `
  --repeats 2
```

Use at least seven repeats for a real run. Algorithm/query order is seeded and
interleaved. Reported latency includes query sanitization, embeddings where
needed, retrieval, fusion, and final ranking. Report serialization is outside
the timed section. The `current` profile additionally includes local MCP HTTP
transport and JSON serialization; its end-to-end latency is reported in the
separate `product_boundary` scope and must not be compared directly with the
in-process algorithms. If `current` is explicitly allowed to fall back to a
direct call because no hub is available, the report labels it
`direct_product_path` instead.

The default matrix is:

- `vector`: raw vector-distance order
- `bm25`: backend lexical order
- `current`: exact MCP product replay, including the live safety probe and retry
- `rrf`: equal Reciprocal Rank Fusion over vector and BM25 rankings
- `weighted_rrf`: weighted RRF, defaulting to 60% vector and 40% BM25

`union` is also implemented, but MCP does not expose it. Run that profile only
in a write-quiescent window with the hub stopped and both flags explicit:

```powershell
uv run python benchmarks/private_palace_bench.py run `
  --dataset "$HOME/.mempalace/benchmarks/search/my-suite/cases.jsonl" `
  --out "$HOME/.mempalace/benchmarks/search/my-suite/union-dev.json" `
  --tag dev `
  --algorithms union `
  --max-distance 0 `
  --allow-direct-product-path
```

RRF uses `sum(weight / (rank_constant + one_based_rank))`, with a default
rank constant of 60. It never compares backend score scales.

For the final held-out run:

```powershell
uv run python benchmarks/private_palace_bench.py run `
  --dataset "$HOME/.mempalace/benchmarks/search/my-suite/cases.jsonl" `
  --out "$HOME/.mempalace/benchmarks/search/my-suite/test-report.json" `
  --tag test `
  --warmups 1 `
  --repeats 7
```

## Safety and interpretation

The controlled vector and BM25 paths currently require the `sqlite_exact`
backend, the only built-in backend with an enforced read-only open. They call
`get_collection(..., create=False, read_only=True)`. The `current` profile
calls the live local MCP hub, so it exercises the product boundary without
opening another write-capable handle. Direct current/union replay is disabled
unless `--allow-direct-product-path` is explicit. The report reopens the
read-only collection and compares public backend maintenance state, including
SQLite's non-content consistency token, before and after a run. It marks a
changed corpus invalid and exits nonzero. For a publishable comparison, still
use a write-quiescent window; never raw-copy an active SQLite database without
its WAL.

Primary quality metric: macro `nDCG@10`. Secondary metrics are hit rate, MRR,
pooled recall, judgment coverage, and expected-no-result accuracy at each
configured cutoff. Relevance grades 2 and 3 count as positive by default.
Every scored run requires `--tag dev` or `--tag test`, and a case cannot carry
both tags. Compare latency only within the same `latency_scope` and after the
warmup because `sqlite_exact` caches its vector matrix after the first scan.

Adopt a new algorithm only if held-out known-target regressions are explained
or eliminated. Quality comes first; latency chooses among candidates on the
quality Pareto frontier.
