---
inclusion: always
---

# MemPalace — persistent AI memory

You have a persistent, searchable memory of past work and conversations,
exposed through the `mempalace` MCP server. Treat it as your long-term
memory: consult it before guessing, and record what matters after doing
non-trivial work.

## Recall (read) — do this proactively
Before answering questions about a person, project, past decision, or prior
work — and before starting non-trivial work on a feature/file you have not
discussed this session — search your memory first:

- `mempalace_search` — semantic search across everything you've stored.
  Pass ONLY keywords in `query`; put background in `context`.
- `mempalace_status` / `mempalace_list_wings` / `mempalace_get_taxonomy` —
  see what's stored (wings = people/projects, rooms = days/topics).
- `mempalace_kg_query` / `mempalace_kg_timeline` — look up facts and how
  they changed over time.
- `mempalace_get_drawer` / `mempalace_list_drawers` — open verbatim content.

Triggers: "like we discussed", "as we decided", "remember when", "how did
we fix", "what's my usual approach", or any reference to prior context.

## Record (write) — after meaningful work
- `mempalace_diary_write` — a short session summary of what happened, what
  was decided, and what was learned.
- `mempalace_add_drawer` — file verbatim quotes, decisions, and code into
  the right wing/room. Call `mempalace_check_duplicate` first to avoid
  re-filing.
- `mempalace_kg_add` / `mempalace_kg_invalidate` — when facts are
  established or change.

## Principles
- Verbatim is sacred — store exact words, never paraphrase user content.
- Be quiet about bookkeeping: don't narrate memory lookups or saves unless
  asked. If a search finds nothing useful, just continue.
- Everything stays local — MemPalace never sends your data anywhere.

## Backfilling history
MemPalace also reads Kiro's own session transcripts. To import past Kiro
chats into the palace, run `mempalace kiro sync` (or
`mempalace mine <kiro-sessions-dir> --mode convos`).
