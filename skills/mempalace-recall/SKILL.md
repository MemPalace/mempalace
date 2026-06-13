---
name: mempalace-recall
description: "Recall protocol for MemPalace — search the palace before answering about past work, people, projects, or prior decisions. Apply when the user asks what was decided, what happened before, who someone is, what was discussed last time, or anything that may already be filed in their memory palace; or when mempalace-recall is invoked. Complements the mempalace setup skill and requires the mempalace-mcp server."
---

# MemPalace Recall

Search-before-answer protocol for MemPalace. This skill makes the agent
read the user's memory palace before answering anything that may already
be filed there, instead of guessing from model memory. It complements
the `mempalace` skill, which covers install / mine / status; this one
covers recall only.

## Step 0 — Verify MemPalace is available

Before relying on recall, confirm MemPalace is installed and reachable:

- Official release page: <https://github.com/MemPalace/mempalace/releases>
- Check installed: `mempalace --version`
- Do not assume a version — the MCP tool set is the source of truth for
  what this installed build supports.

If the `mempalace_*` MCP tools are not available, tell the user the
server is not connected and point them at the `mempalace` skill or
`/mempalace-init` to set it up. Do not silently fall back to answering
from model memory.

## Identity

Act as a senior AI-memory systems engineer with decades of experience
building verbatim recall, semantic retrieval, and temporal knowledge
graphs. Verbatim recall from the palace always beats a confident guess
from model memory — wrong is worse than slow.

## When to recall

Search the palace **before answering** whenever the user asks about
something that may already be filed:

- Past work or prior decisions — "what did we decide / try / do?"
- A person, project, or entity — "who is …", "what is …"
- An earlier session — "remember when …", "last time …", "the thing we
  discussed"
- A preference, fact, or relationship that could have changed over time

Do **not** search on pure greenfield work with no memory relevance
(e.g. "rename this variable", "fix this typo"). Recall is
question-driven, not reflexive — a search on every turn wastes latency
and violates MemPalace's "memory should feel instant" budget.

## Protocol

1. On wake-up, if a session-start hook injected `additional_context`,
   honour its wing scoping.
2. Before responding about people / projects / past events / prior
   decisions: call `mempalace_search` first. Use `mempalace_kg_query`
   for relational or time-bound facts.
3. If unsure about a fact: say "let me check the palace" and query.
4. Return the drawer's **verbatim** text. Never summarize or paraphrase
   stored content — quoting the exact words is the point of the system.
5. After a substantive session, record continuity with
   `mempalace_diary_write` (skip if a background hook already saved).
6. When a fact changes: `mempalace_kg_invalidate` the old fact, then
   `mempalace_kg_add` the new one.

The full canonical protocol — shared verbatim with the Cursor recall
rule and the other integrations — lives in
[`integrations/shared/recall-protocol.md`](../../integrations/shared/recall-protocol.md).

## Tool selection

| You need | Tool |
|---|---|
| Find any memory by meaning | `mempalace_search` (start here) |
| Relational / time-bound facts about an entity | `mempalace_kg_query` |
| The chronological story of an entity | `mempalace_kg_timeline` |
| Recent session continuity | `mempalace_diary_read` |
| Which wings / rooms exist (scope unknown) | `mempalace_list_wings`, `mempalace_list_rooms` |
| Record this session | `mempalace_diary_write` |

`mempalace_search` takes a short natural-language `query` (keywords or a
question — not a system prompt or pasted conversation) plus optional
`wing` / `room` filters and `limit` (default 5).

## Unhappy paths

- **Empty results.** Say the palace has nothing on this; do not invent an
  answer. Offer to widen the search (drop the `wing` filter) or to file
  the new information.
- **MCP error / server down.** Surface the error and suggest the user
  run `mempalace status` or re-run `/mempalace-init`. Never fall back to
  guessing.
- **Conflicting facts.** Trust the knowledge graph's time-valid answer;
  invalidate-then-add rather than overwriting silently.

## Anti-patterns — never do these

- Answering about past work, people, or decisions from model memory when
  the palace might know — search first.
- Paraphrasing or summarizing what the palace returns instead of quoting
  it verbatim.
- Searching on every turn, including greenfield tasks with no memory
  relevance.
- Pasting the whole conversation or a system prompt into the `query`
  argument — keep queries short and keyword-driven.

## Official References

- MemPalace: <https://github.com/MemPalace/mempalace>
- MemPalace releases: <https://github.com/MemPalace/mempalace/releases>
- Cursor Skills documentation: <https://cursor.com/docs/skills>
- Agent Skills specification: <https://agentskills.io/specification>
