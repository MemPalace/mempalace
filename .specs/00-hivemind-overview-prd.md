# Hivemind — Project Overview & PRD

> A shared **memory layer for AI-assisted development**. Developers push distilled, durable knowledge about a service; that knowledge is pulled back automatically into future coding sessions so anyone can work in an unfamiliar service with full context.

**Audience:** the 4 engineers building Hivemind during the 2-day hackathon.
**Status:** scoping locked. Read this whole doc first, then your component spec (`01`–`04`).

---

## 1. The problem

Every developer in the org uses Claude (Claude Code) day to day. But when someone jumps into a service they don't normally own, they lack the context to be effective — architecture, conventions, gotchas, where things live, why decisions were made. Cross-service features make this worse: one change touches several services, each with its own tribal knowledge.

A shared memory layer fixes this. Knowledge captured once, by whoever has it in-session, becomes retrievable context for everyone else's agent later.

## 2. What we're building (one paragraph)

A backend service stores **atomic memories** (small, self-contained facts) about each service in a vector DB. Developers **write** memories with a manual trigger inside Claude Code — a slash command that distills the current session plus any referenced PRs/docs into a few durable facts and pushes them. Memories are **read** two ways: automatically (a Claude Code hook injects relevant memories into every prompt, scoped to the current service) and manually (an MCP tool for ad-hoc search). A dashboard reports contribution and coverage from the memories' metadata.

## 3. Architecture

```
WRITE PATH
  /hivemind-push  →  Claude (has 200K session context + reads referenced PRs/docs)
                  →  distills into up to ~5 atomic memories
                  →  push_memory()  [MCP tool]
                  →  FastAPI POST /memories  →  embed (local model)  →  Chroma

READ PATH (auto)
  UserPromptSubmit hook  →  reads current service from .hivemind file
                         →  FastAPI GET /search?q=<prompt>&service=<svc>
                         →  injects top memories as context before Claude sees the prompt

READ PATH (manual)
  search_memory()  [MCP tool]  →  FastAPI GET /search

DASHBOARD
  FastAPI GET /stats  →  GROUP BY author / service over memory metadata
```

**One backend, two front doors.** The MCP server and the hook both talk to the same FastAPI service. The hook talks to the backend *directly* (HTTP/CLI), not through MCP. This single API contract is what lets four people build in parallel — agree on it in hour 0 and don't drift from it.

## 4. Key design decisions (and why)

These are the decisions that de-risk the two days. Understand the *why*, not just the *what*.

### 4.1 The in-session agent is the compressor — we do not build a summarization pipeline
The hard part of "memory for AI" is never the storage; it's getting **high-signal** memories in and **relevant** memories out. We were tempted to build a step that exports a 200K-token session and compresses it with a separate model. We are **not** doing that. The thing already holding the session context, with tools to read the referenced PRs and docs, is Claude itself. So extraction happens *inline*, in-session, via a slash command whose body is an extraction prompt. The raw 200K never leaves the machine — only the distilled memories go over the wire. This removes our single riskiest component and keeps the backend dumb (ingest, embed, store).

### 4.2 Memories are atomic, not one compressed blob
The extraction prompt emits **several small, self-contained facts** ("up to ~5 distinct durable facts a future dev on this service would want"), each stored as its own vector. Atomic memories retrieve cleanly — a query matches the one relevant fact instead of a blob diluted by four irrelevant ones — and they give the dashboard honest counts for free.

### 4.3 "Relevant" has a target: durable knowledge for the next developer
Compression without a target is just summarization. The target is **durable knowledge useful to the next developer on this service**: architecture, conventions, gotchas, where things live, why a decision was made, common failure modes — *not* the transient detail of today's task. This sentence belongs verbatim in the extraction prompt.

### 4.4 Hooks pull; humans push. No auto-write.
Auto-writing every prompt/diff floods the store with noise and makes retrieval *worse* — and we can't tune that in two days. Writes are always the deliberate `/hivemind-push`. Hooks are for the *automatic pull* only.

### 4.5 Filter by service before/with the vector search
Semantic search is bad at exact symbols — developers query things like a service name or `CreateSessionV1_1`, which embed poorly. We rescue this by storing `service` as metadata and filtering on it before/alongside the vector search. The same metadata powers the dashboard.

### 4.6 Embeddings stay local — even though we have a Claude API key
Two distinct "AI" jobs live in this stack: **extraction** (handled by the in-session agent, §4.1) and **embeddings** (text → vector, at write and query time). The Claude API key only touches the second — and **Anthropic has no embeddings endpoint** (Claude is a generation model, not an embedding model). So the key is *not* a drop-in embedder. A local embedding model (e.g. `bge-small` / `all-MiniLM` via sentence-transformers, or `nomic-embed-text` via Ollama) is tiny (hundreds of MB, CPU-fine), removes a network hop from the hot read path, and keeps the pinned-model discipline trivial. **Decision: embeddings local.** The Claude API key's natural home is an optional *sessionless* push path (a CLI that distills a PR without a live session) — a "could," not a "must."

### 4.7 Seed the demo store by hand
Retrieval looks magic only if the inputs are good. We curate memories for 2–3 demo services by hand and demo retrieval against *those*. Do not demo retrieval against an auto-populated store.

## 5. The memory schema (canonical)

```
id          string      unique id
service     string      e.g. "notary", "billing"  — filterable
author      string      who pushed it             — filterable, dashboard
type        enum        architecture | gotcha | how-to | decision | failure-mode
title       string      short headline
body        string      the fact, self-contained
refs[]      string[]    PR links, doc paths        — display only
created_at  int         epoch seconds              — range-filterable
embedding   float[]     vector of (title + " " + body)
```

**Embed `title + body`.** Keep `service` / `author` / `type` / `created_at` as filterable metadata.

### Storing this in Chroma
Chroma stores all of it across four parallel arrays per record: `ids`, `embeddings`, `documents` (put `title + body` here), `metadatas` (everything else). Two rules:
- **Metadata values must be scalars** (`str`/`int`/`float`/`bool`). `refs[]` is a list, so serialize it: `json.dumps(refs)` on write, `json.loads` on read.
- **`created_at` as int epoch**, not an ISO string — Chroma range-filters numbers (`{"created_at": {"$gte": ...}}`) but not date strings.
- **Pass embeddings explicitly** with the pinned local model rather than relying on Chroma's default embedder — write-time and query-time *must* use the identical model or the vectors are nonsense.

## 6. The API contract (shared — do not drift)

All components code against this. Owner: Eng 1 (Backend).

```
POST /memories
  body:  { service, author, type, title, body, refs[] }
  action: embed(title+body) → store
  returns: { id }

GET /search?q=<text>&service=<svc>&k=<int default 5>
  action: embed(q) → vector search filtered by service → top-k
  returns: { results: [ { id, service, author, type, title, body, refs[], created_at, score } ] }

GET /stats
  returns: {
    by_author:  [ { author, count } ],
    by_service: [ { service, count, last_updated } ],
    total
  }
```

`service` on `/search` is optional but strongly recommended by callers; the hook always sends it.

## 7. Scope (MoSCoW)

- **Must:** backend `/memories` + `/search` + local embeddings + Chroma; MCP `push_memory`/`search_memory`; `/hivemind-push` slash command; `UserPromptSubmit` auto-pull hook; hand-seeded memories for 2–3 services; single-service onboarding demo.
- **Should:** `/stats` + dashboard with **real** contributor & coverage counts; cross-service feature demo.
- **Could:** sessionless push CLI using the Claude API key; LLM-generated titles / service summaries for polish; "retrieval hit rate" metric.
- **Won't (this time):** auth, dedup, edit/versioning of memories, reranking, auto-write hooks (showcase only, never populating the demo store).

## 8. Honest caveats to frame in the demo

"More memory = healthier service" is a vanity metric — it rewards spam and ignores whether memories are actually *used*. Frame the dashboard number as **coverage**, not health. If there's spare time, **retrieval hit rate** (how often a search returns something that gets used) is a far more defensible signal — but it's a "could."

## 9. Two-day plan & ownership

**Hour 0 (everyone, ~30 min):** lock the schema (§5) and API contract (§6). This is what unblocks parallel work.

**Day 1 — end-to-end skeleton:**
- Eng 1: FastAPI `/memories` + `/search`, local embeddings, Chroma, **seed script**.
- Eng 2: MCP `push_memory` + `search_memory` calling the backend; draft `hivemind-push.md`.
- Eng 3: stub the hook against `/search` with a fixed service string.
- Eng 4: scaffold dashboard against `/stats` (mock shape first, real data when Eng 1 ships it).
- **Day-1 milestone (fight for this):** `/hivemind-push` writes a memory → the hook pulls it back into a fresh session. Once that loop closes, the rest is polish.

**Day 2 — magic & polish:**
- Eng 3: real service detection via `.hivemind` file; injection formatting.
- Eng 2: tune the extraction prompt to produce atomic, durable memories.
- Eng 4: real `/stats` views; own end-to-end glue; rehearse demos.
- Eng 1: support, seed more curated memories, optional sessionless CLI if time.
- Leave a buffer block for breakage.

| Eng | Component | Spec |
|-----|-----------|------|
| 1 | Backend (FastAPI + embeddings + Chroma + seed) — **contract owner** | `01-backend-spec.md` |
| 2 | MCP server + `/hivemind-push` slash command | `02-mcp-server-spec.md` |
| 3 | `UserPromptSubmit` auto-pull hook | `03-hook-spec.md` |
| 4 | Dashboard + integration + demo | `04-dashboard-demo-spec.md` |

## 10. Demo script (Eng 4 owns rehearsal)

1. **Cold onboarding:** open a fresh session in a service the "developer" has never touched; ask a normal question; the hook silently injects seeded memories; Claude answers with service-specific context it otherwise wouldn't have.
2. **Cross-service feature:** a task spanning two services; memories from both are pulled; show the dashboard's contributor/coverage view as the closer.

## 11. Things to verify against live docs (don't trust memory)

- The exact Claude Code **hook event name** and payload for prompt-time injection (Eng 3).
- The current **Chroma client API** — the persistent-client constructor and `add`/`upsert` signatures have shifted across versions (Eng 1). The id/embedding/document/metadata model itself is stable.
