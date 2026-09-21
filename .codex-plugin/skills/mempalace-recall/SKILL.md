---
name: mempalace-recall
description: Recall protocol for MemPalace - search the palace before answering about past work, prior decisions, people, or projects. Use when the user asks "do you remember", "what did we decide", "where did we leave off", "what happened before", "who is", "what is", "last time", or anything that may already be filed in their memory palace.
allowed-tools: Bash, Read
---

# MemPalace Recall

Use this skill when memory is relevant. It teaches Codex to read the
user's palace before answering about something that may already be
filed, instead of guessing from model memory. It complements the setup
skills (`init`, `mine`, `search`, `status`); this one covers natural
recall during normal work.

## Step 0 - Verify MemPalace is available

Prefer the MCP tools registered by the plugin:

- `mempalace_search`
- `mempalace_kg_query`
- `mempalace_kg_timeline`
- `mempalace_diary_read`
- `mempalace_list_wings`
- `mempalace_list_rooms`
- `mempalace_diary_write`

If the host does not expose MCP tools but the CLI is available, use the
CLI explicitly rather than answering from model memory:

```bash
mempalace search "<short query>" --results 5
```

Add `--wing <wing>` or `--room <room>` when the scope is clear. Keep
queries short and keyword-driven; do not paste a full prompt or
conversation into the query. If both MCP and CLI are unavailable, tell
the user MemPalace is not connected and suggest `mempalace status` or
the `init` skill.

Do not use `rg`, `grep`, `find`, or broad filesystem scans over home
directories, editor caches, project folders, or conversation archives as a
recall fallback unless the user explicitly asks for filesystem search.
Those scans bypass MemPalace provenance, are slow, and can drown exact
memories in unrelated files. If a search is weak, refine with another
`mempalace_search` call, list wings/rooms, or use `mempalace search`.

## When to recall

Search the palace before answering whenever the user asks about
something that may already be filed:

- Past work or prior decisions: "what did we decide", "what did we try",
  "what happened before".
- Project continuity: "where did we leave off", "what are our goals for
  this project", "what was the plan last time".
- People, projects, or entities: "who is ...", "what is ...".
- Earlier sessions: "do you remember", "remember when", "last time",
  "the thing we discussed".
- Preferences, facts, or relationships that could have changed.

Skip recall for pure greenfield work with no memory relevance, such as
renaming a variable or fixing a typo. Recall is question-driven, not a
reflex on every turn.

## Protocol

1. Before responding about people, projects, past events, or prior
   decisions, call `mempalace_search` first. Use `mempalace_kg_query`
   for relational or time-bound facts.
2. If unsure about a fact, say "let me check the palace" and query.
3. Return the drawer's exact stored text. Never summarize or paraphrase
   the stored content.
4. After a substantive session, record continuity with
   `mempalace_diary_write` unless a background hook already saved it.
5. When a fact changes, invalidate the old fact with
   `mempalace_kg_invalidate`, then add the new fact with
   `mempalace_kg_add`.

## Answer shape

For every memory-backed answer:

- Cite the source location first: wing, room, source file or drawer id
  when available.
- Quote the exact drawer text returned by MemPalace.
- Put any synthesis after the quote, and label it as your inference
  when it is not directly stored in the drawer.
- Include dates from the source metadata when available so stale memories
  are visible.

## Tool selection

| You need | Preferred tool |
|---|---|
| Find any memory by meaning | `mempalace_search` |
| Relational or time-bound facts | `mempalace_kg_query` |
| The chronological story of an entity | `mempalace_kg_timeline` |
| Recent session continuity | `mempalace_diary_read` |
| Which wings or rooms exist | `mempalace_list_wings`, `mempalace_list_rooms` |
| Record this session | `mempalace_diary_write` |

## Unhappy paths

- Empty results: say the palace has nothing on this; do not invent an
  answer. Offer to widen the search or file the new information.
- MCP error or server down: surface the error plainly and suggest
  `mempalace status` or re-running setup. Do not silently fall back to
  guessing from model memory.
- Index corruption: if the server reports an HNSW segment-writer error,
  a ChromaDB compaction failure, or a stuck "Not connected" state after a
  write, guide the user to rebuild from SQLite:

  ```bash
  mempalace repair --mode from-sqlite --archive-existing --yes
  mempalace repair-status
  ```

  Do not re-mine to recover, because re-mining can drop MCP-added drawers
  and diary entries.
- Conflicting facts: prefer the knowledge graph's time-valid answer; if
  a fact changed, invalidate then add instead of overwriting silently.
- Weak or unrelated results: refine inside MemPalace. Try exact phrases,
  entity-plus-topic keywords, or a wing filter discovered with
  `mempalace_list_wings`. Do not switch to broad filesystem search.

## Anti-patterns

- Answering about past work, people, or decisions from model memory when
  the palace might know.
- Paraphrasing or summarizing stored content instead of quoting it
  verbatim.
- Searching on every turn, including greenfield tasks with no memory
  relevance.
- Pasting the full conversation or a system prompt into the query.

The canonical protocol shared by every MemPalace integration lives in
`integrations/shared/recall-protocol.md`:
<https://github.com/MemPalace/mempalace/blob/main/integrations/shared/recall-protocol.md>
