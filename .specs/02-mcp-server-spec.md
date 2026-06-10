# Spec 02 — MCP Server + `/hivemind-push` Slash Command (Eng 2)

**Owner:** Eng 2.
**Stack:** Python (FastMCP / MCP SDK) + a Claude Code slash command (a markdown prompt file).
**Depends on:** Backend `/memories` and `/search` (Eng 1). You can build against the contract before the backend is live by stubbing the HTTP calls.

> Read `00-hivemind-overview-prd.md` §4.1–§4.3 carefully — the slash command *is* the compression logic, and getting the extraction prompt right is the single biggest lever on this whole project's quality.

## Goal

Two thin things:
1. An **MCP server** exposing `push_memory` and `search_memory` — the manual front door to the backend.
2. A **`/hivemind-push` slash command** whose body is the extraction prompt that turns the live session + referenced PRs/docs into atomic memories and calls `push_memory`.

The MCP server holds no logic beyond shaping requests to the backend. The intelligence lives in the slash command prompt.

## What you build

### MCP server (`hivemind` server)
```
push_memory(service, type, title, body, refs)   -> calls POST /memories, returns id
search_memory(query, service=None, k=5)          -> calls GET /search, returns results
```
- `author` should be auto-filled (machine user / env var) rather than asked of the model each time — keep the tool surface minimal.
- Validate `type` against the enum so junk doesn't reach the store.
- Keep it stateless; it's a proxy.

### `/hivemind-push` slash command (`.claude/commands/hivemind-push.md`)
This is the heart of the write path. The prompt should instruct Claude to:
1. Read any PRs / docs / files the developer named as arguments (the agent has the tools; let it fetch them).
2. From the current session **plus** those references, extract **up to ~5 distinct, durable, self-contained facts a future developer on this service would want** — architecture, conventions, gotchas, where things live, why a decision was made, common failure modes. **Not** the transient detail of today's task.
3. For each fact, choose a `type` from `{architecture, gotcha, how-to, decision, failure-mode}`, write a short `title` and a self-contained `body`, and collect any `refs`.
4. Determine `service` (from the `.hivemind` file in the repo root — coordinate the convention with Eng 3 — or ask once).
5. Call `push_memory` once per fact.
6. Report back a short summary of what was stored.

Bias the prompt toward **fewer, higher-quality, atomic** memories over many shallow ones. Each fact must stand alone — readable with zero surrounding context, since it'll be retrieved in isolation.

## Acceptance criteria
- [ ] `push_memory` from the MCP server creates a memory retrievable via `search_memory`.
- [ ] `/hivemind-push` in a real session produces **multiple atomic** memories, not one blob.
- [ ] Each stored memory is self-contained and reads as durable service knowledge, not task chatter.
- [ ] `service` is set correctly without manual fiddling in the common case.

## Gotchas
- Don't let the extraction emit one giant compressed paragraph — that kills retrieval (see PRD §4.2).
- Don't build any summarization in the server; the agent does it. The server just forwards structured fields.
- Confirm MCP tool registration details against the current MCP SDK docs.

## Coordinate with
- **Eng 1** on the exact `/memories` body and the `type` enum.
- **Eng 3** on the `.hivemind` service-detection convention (shared).
- **Eng 1/Eng 4** so seeded memories and `/hivemind-push` output have the same shape and quality bar.
