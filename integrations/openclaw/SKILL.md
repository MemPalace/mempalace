---
name: mempalace
description: "MemPalace — Local AI memory with 96.6% recall. Semantic search, temporal knowledge graph, palace architecture (wings/rooms/drawers). Free, no cloud, no API keys."
version: 3.8.0
homepage: https://github.com/MemPalace/mempalace
user-invocable: true
metadata:
  openclaw:
    emoji: "🏛"
    os: [darwin, linux, win32]
    requires:
      anyBins: [mempalace, python3]
    install:
      - id: mempalace-pip
        kind: uv
        label: "Install MemPalace (Python, local ChromaDB)"
        package: mempalace
        bins: [mempalace]
---

# MemPalace — Local AI Memory System

Use MemPalace when the user asks about memories, people, projects, past work,
durable facts, session recap, or knowledge graph state. It stores verbatim
drawers and temporal facts locally; no cloud service is required.

## Architecture

- **Wings**: people, projects, or domains.
- **Rooms**: topics within a wing.
- **Drawers**: verbatim memory chunks.
- **Knowledge Graph**: typed facts with temporal validity.
- **Tunnels / hallways**: graph connections between rooms and entities.

## Protocol

1. **Wake-up**: call `mempalace_status` to load the palace overview.
2. **Before answering about people, projects, or past events**: call
   `mempalace_search` or `mempalace_kg_query`. Never guess from memory.
3. **If unsure about a fact**: say you are checking, then query.
4. **After meaningful sessions**: write continuity with `mempalace_diary_write`,
   or use `mempalace_checkpoint` for multi-drawer wrap-ups.
5. **When facts change**:
   - Single-valued replacement (model, employer, owner, address, current status)
     → `mempalace_kg_supersede`.
   - Fact ended with no replacement → `mempalace_kg_invalidate`.
   - Independent/coexisting fact → `mempalace_kg_add`.

Do not hand-roll invalidate + add for single-valued replacements; boundary
queries can briefly show both values. `supersede` is the correct handoff.

## Tool selection

Start with search for context, use KG tools for durable time-valid facts, and
file durable context verbatim.

- Search/browse: `mempalace_search`, `mempalace_get_drawer`, list/taxonomy
  tools, duplicate checks, and `mempalace_get_aaak_spec`.
- Knowledge graph: `mempalace_kg_query`, `mempalace_kg_add`,
  `mempalace_kg_supersede`, `mempalace_kg_invalidate`, timeline/stats.
- Write/diary: `mempalace_add_drawer`, `mempalace_checkpoint`,
  `mempalace_update_drawer`, `mempalace_diary_write`, `mempalace_diary_read`.
- Graph/ingest/system: tunnels/hallways, `mempalace_mine`, cleanup/delete,
  reconnect, hook settings, and silent-checkpoint acknowledgement.

For detailed tool-selection guidance, read `{baseDir}/references/tool-selection.md`.
For install and MCP config examples, read `{baseDir}/references/setup.md`.

## Unhappy paths

- Empty results: say the palace has nothing on this; do not invent an answer.
- MCP unavailable: surface the error and suggest reconnecting/configuring the
  server; do not silently fall back to model memory.
- Conflicting facts: prefer time-valid KG answers and repair the fact history
  with `supersede`, `invalidate`, or `add` as appropriate.

## Anti-patterns

- Answering about past work, people, or decisions without searching first.
- Pasting full conversations or system prompts into `mempalace_search.query`.
- Re-mining to fix index trouble before checking repair/reconnect paths.
- Running bulk ingest, sync, or delete operations without user intent and a
  dry-run/preview when available.

## Tips

- Search is semantic; questions often work better than single keywords.
- Use KG facts when time validity matters.
- Include provenance (`source_file`, source drawer IDs) when filing from files.
- Read AAAK naturally: expand codes mentally and treat markers as context.

## License

[MemPalace](https://github.com/MemPalace/mempalace) is MIT licensed. Created by
Milla Jovovich, Ben Sigman, Igor Lins e Silva, and contributors.
