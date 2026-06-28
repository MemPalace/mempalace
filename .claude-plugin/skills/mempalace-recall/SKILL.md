---
name: mempalace-recall
description: Recall protocol for MemPalace — search the palace before answering about past work, prior decisions, people, or projects. Use when the user asks what was decided, what happened before, who someone is, what was discussed last time, or anything that may already be filed in their memory palace.
allowed-tools: Bash
---

# MemPalace Recall

Search-before-answer protocol for MemPalace. Read the user's memory
palace before answering anything that may already be filed there,
instead of guessing from model memory. This complements the `mempalace`
skill (install / mine / status); this one covers recall only.

## Step 0 — Verify MemPalace is available

```bash
mempalace --version
```

If the `mempalace_*` MCP tools are not available, tell the user the
server is not connected and point them at the `mempalace` skill or
`/init`. Do not silently fall back to answering from model memory.

## When to recall

Search the palace **before answering** whenever the user asks about
something that may be filed:

- Past work or prior decisions — "what did we decide / try / do?"
- A person, project, or entity — "who is …", "what is …"
- An earlier session — "remember when …", "last time …"
- A preference, fact, or relationship that could have changed over time

Skip recall for pure greenfield work with no memory relevance (renaming
a variable, fixing a typo). Recall is question-driven, not reflexive.

## Protocol

1. Before responding about people / projects / past events / prior
   decisions: call `mempalace_search` first. Use `mempalace_kg_query`
   for relational or time-bound facts.
2. If unsure about a fact: say "let me check the palace" and query.
3. Return the drawer's **verbatim** text — never summarize or paraphrase
   stored content.
4. After a substantive session, record continuity with
   `mempalace_diary_write` (skip if a background hook already saved).
5. When a fact changes: `mempalace_kg_invalidate` the old fact, then
   `mempalace_kg_add` the new one.

## Unhappy paths

- **Empty results** — say the palace has nothing on this; do not invent
  an answer. Offer to widen the search or file the new information.
- **MCP error / server down** — surface the error, suggest `mempalace
  status` or re-running `/init`; never fall back to guessing.
- **Palace index corrupt / compactor error** — if the server reports an
  HNSW segment-writer error, a ChromaDB compaction failure, or stays
  "Not connected" after a write, the index is out of sync with
  `chroma.sqlite3` but the rows are intact. Tell the user to stop the
  server and rebuild from SQLite (`mempalace repair --mode from-sqlite
  --archive-existing --yes`), not re-mine, which drops MCP-added drawers
  and diary entries (#1843). Do not repair in-process.
- **Conflicting facts** — trust the knowledge graph's time-valid answer;
  invalidate-then-add rather than overwriting silently.

The canonical protocol, shared across all MemPalace integrations, lives
in `integrations/shared/recall-protocol.md`.
