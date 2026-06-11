# MemPalace Recall Protocol

The canonical "search before answering" protocol shared across every
MemPalace integration (Cursor, Antigravity, Claude Code, Codex,
OpenClaw). This file is the single source of truth — skills and rules
should link here rather than restating the protocol, so the rule never
drifts from the skill.

The protocol exists to honour MemPalace's foundational promise:
**100% recall, verbatim, never guess.** When the palace might hold the
answer, the agent must read the palace before answering from model
memory.

## When to recall

Search the palace **before answering** whenever the user asks about
anything that may already be filed:

- Past work, prior decisions, or "what did we do / decide / try?"
- A person, project, or entity ("who is …", "what is …")
- Something that happened in an earlier session ("remember when …",
  "last time …", "the thing we discussed")
- A preference, fact, or relationship that could have changed over time

If the question is pure greenfield work with no memory relevance (e.g.
"rename this variable", "fix this typo"), do not search — recall is
question-driven, not reflexive.

## The protocol

1. **On wake-up** (if a session-start hook injected context, honour its
   wing scoping): scope recall to the wing inferred from the workspace,
   then continue.
2. **Before responding** about people, projects, past events, or prior
   decisions: call `mempalace_search` first. For relational or temporal
   facts ("who reported to whom in March", "what was true then"), call
   `mempalace_kg_query` instead or as well.
3. **If unsure** about a fact (name, age, relationship, preference): say
   "let me check the palace" and query. Wrong is worse than slow.
4. **Return verbatim.** Quote the drawer's exact stored words. Never
   summarize, paraphrase, or lossy-compress what the palace returns —
   that is the whole point of the system.
5. **After a substantive session**, record continuity with
   `mempalace_diary_write` (background hooks may already do this — do not
   double-file).
6. **When a fact changes**, call `mempalace_kg_invalidate` on the old
   fact, then `mempalace_kg_add` for the new one.

## Tool selection

| You need | Tool |
|---|---|
| Find any memory by meaning | `mempalace_search` (start here) |
| Relational / time-bound facts about an entity | `mempalace_kg_query` |
| The chronological story of an entity | `mempalace_kg_timeline` |
| Recent session continuity | `mempalace_diary_read` |
| Which wings / rooms exist (when scope unknown) | `mempalace_list_wings`, `mempalace_list_rooms` |
| Record this session | `mempalace_diary_write` |

`mempalace_search` takes a short natural-language `query` (keywords or a
question — not a system prompt or pasted conversation) plus optional
`wing` / `room` filters and `limit` (default 5).

## Unhappy paths

- **Empty results.** Say the palace has nothing on this; do not invent an
  answer to fill the gap. Offer to widen the search (drop the wing
  filter) or to file the new information.
- **MCP unavailable / tool error.** Surface the error plainly and suggest
  the user verify the server (`mempalace status`, or re-run install).
  Do not silently fall back to guessing from model memory.
- **Stale or conflicting facts.** Prefer the knowledge graph's
  time-valid answer; if a fact has changed, invalidate the old one and
  add the new one rather than overwriting context silently.

## Anti-patterns

- Answering about past work, people, or decisions from model memory when
  the palace might know — search first.
- Paraphrasing or summarizing stored content instead of quoting it
  verbatim.
- Searching reflexively on every turn, including pure greenfield coding
  with no memory relevance.
- Pasting the full conversation or a system prompt into the `query`
  argument — keep queries short and keyword-driven.

## See also

- [`integrations/openclaw/SKILL.md`](../openclaw/SKILL.md) — the original
  full-protocol skill this is distilled from.
- MemPalace design principles (verbatim, local-first, never summarize):
  <https://github.com/MemPalace/mempalace>
