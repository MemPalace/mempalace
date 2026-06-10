# Spec 01 — Backend Service (Eng 1)

**Owner:** Eng 1 — also the **API contract owner**. If the contract changes, you announce it.
**Stack:** Python, FastAPI, Chroma (local), local embedding model.
**Depends on:** nothing — you unblock everyone else. Ship `/search` and the seed script first.

> Read `00-hivemind-overview-prd.md` §4 (decisions), §5 (schema), §6 (contract) before starting.

## Goal

A dumb, fast service that ingests already-distilled memories, embeds them locally, stores them in Chroma, and serves filtered vector search + aggregate stats. No summarization happens here — the agent does that upstream.

## What you build

### Embeddings (pin the model)
Pick **one** local model and hardcode it everywhere:
- `sentence-transformers` with `bge-small-en-v1.5` or `all-MiniLM-L6-v2` (CPU-fine, ~100–400 MB), **or**
- Ollama `nomic-embed-text`.

Expose a single `embed(text) -> list[float]`. Write-time and query-time **must** call this same function. This is the #1 footgun: mismatched models = garbage results.

### Chroma storage
One collection, e.g. `hivemind`. Per record:
- `ids` = `id`
- `embeddings` = `embed(title + " " + body)` — **pass explicitly**, don't rely on Chroma's default embedder
- `documents` = `title + " " + body`
- `metadatas` = `{ service, author, type, title, created_at, refs }` where `refs = json.dumps(refs_list)` and `created_at = int(time.time())`

Use a **persistent** client (writes survive restarts during the demo). Verify the constructor + `add`/`upsert` signature against the **current Chroma docs** — the API has shifted across versions.

### Endpoints (canonical — §6 of the PRD)

```
POST /memories      { service, author, type, title, body, refs[] }  -> { id }
GET  /search?q=&service=&k=5                                          -> { results: [...] }
GET  /stats                                                          -> { by_author, by_service, total }
```

- `/search`: `embed(q)` → Chroma query with `where={"service": service}` when provided → top-k. Return `score`, and `json.loads` the `refs` back into a list in the response.
- `/stats`: read all metadatas; aggregate `by_author` (count) and `by_service` (count + max `created_at` as `last_updated`). For hackathon scale, in-memory aggregation over a `.get()` is fine — don't over-engineer.

### Seed script (high priority — the demo depends on it)
`seed.py` that inserts **hand-curated** atomic memories for 2–3 demo services (e.g. `notary`, `billing`, `auth`). 5–10 good memories per service across the `type` enum. These are what make retrieval look magic — write them as if you were the most helpful senior engineer onboarding someone. Coordinate content with Eng 4 (demo) and Eng 2 (so the extraction prompt's output shape matches).

## Acceptance criteria
- [ ] `POST /memories` then `GET /search` returns the new memory ranked sensibly.
- [ ] `service` filter actually scopes results (a `notary` query doesn't return `billing` memories).
- [ ] `refs[]` round-trips (list in → list out).
- [ ] `seed.py` populates the demo services idempotently-ish (safe to re-run).
- [ ] `/stats` returns real counts that match what's stored.

## Gotchas
- Pinned embedding model — same function both paths.
- Chroma metadata must be scalars; `refs` → `json.dumps`.
- `created_at` as int epoch for range filters.
- Don't add auth/dedup/versioning — explicitly out of scope.

## If you have spare time (could)
A sessionless push path: `POST /distill { pr_url | text }` that calls the **Claude API** to extract atomic memories server-side, then stores them — the one thing the slash command can't do (works outside a live session). Strictly optional.
