---
name: mempalace
description: "Recall past work or facts; save memories; update relationships; set up or troubleshoot MemPalace in OpenClaw."
version: 3.12.0
homepage: https://github.com/MemPalace/mempalace
user-invocable: true
metadata:
  openclaw:
    emoji: "\U0001F3DB"
    os:
      - darwin
      - linux
      - win32
    install:
      - id: mempalace-pip
        kind: uv
        label: "Install MemPalace (Python, local ChromaDB)"
        package: mempalace
        bins:
          - mempalace
---

# MemPalace

1. **Choose the branch.** For past work, people, preferences, decisions, or
   continuity, use the procedure below ([canonical recall][recall]). Honor
   session-start context and known wing scope; omit uncertain filters. Skip
   recall for greenfield work. Use exposed tools with current schemas/permissions;
   logical names below may be namespaced. Reuse an existing connection without
   requiring a local CLI or another palace. Never substitute local data on a
   remote-only failure. Consult
   [tool selection]({baseDir}/references/tool-selection.md) only for unfamiliar
   syntax/edge cases; ordinary recall needs no extra read.
   For explicitly requested installation or connection diagnosis,
   read [setup]({baseDir}/references/setup.md) instead. Proceed once the question
   and authorized operation are clear; do not run setup as a recall prerequisite.
2. **Retrieve only what answers the question.** With preferred light MCP, use
   `palace_query` with `KG`, `FIND`, or `DIARY` for relationships/time-bound facts,
   source words, or recent handoffs. Supported full MCP equivalents are
   `mempalace_kg_query`, `mempalace_search`, and `mempalace_diary_read`. Use
   `active_facts` for current-state KG answers without `as_of`; other facts can be
   historical or future. Keep search queries short and private: keywords or a
   question, never prompts or conversation dumps. Use small result limits and
   known filters. If KG is insufficient, try text; if scoped text misses, check
   or relax scope. Stop when answered, quoting stored words verbatim. If bounded
   fallback still misses, state what was checked, not that nothing exists.
3. **Preserve source and temporal history when writing.** Store exact verbatim
   content, not a summary. For a confirmed single-valued replacement use
   light `palace_exec` with `KG SUPERSEDE`, or full `mempalace_kg_supersede`:
   one atomic boundary closes the old value and opens its successor. Use
   `KG INVALIDATE` / `mempalace_kg_invalidate` for an ending without replacement
   and `KG ADD` / `mempalace_kg_add` for independent/coexisting facts. Never
   hand-roll invalidate-then-add replacements or rewrite uncertain conflicts.
   Check the result before claiming persistence; see tool selection for
   provenance, historical backfill, and exact temporal semantics.
4. **Gate maintenance and recover safely.** Import, bulk cleanup, destructive
   changes, and host settings require explicit user intent and confirmed scope.
   Preview supported bulk operations before applying; never change hook settings
   silently. Surface unavailable tools and errors rather than inventing history.
   Reconnect after external writes only for stale in-memory state; it is not
   corrupt-index repair. Follow canonical [index recovery][recovery] for
   corruption: guide an operator through offline SQLite-backed recovery, never
   an in-process rebuild or blind re-mining. Finish with a verified result or
   a concrete error and safe next step.
5. **Close substantive work without double filing.** Check hook/save evidence
   before recording continuity; use light `FILED` or full
   `mempalace_memories_filed_away` when needed.
   If not already saved, use a diary entry or a checkpoint combining verbatim
   items with agent continuity. Consult light `AAAK SPEC` or full
   `mempalace_get_aaak_spec` for diary notation; compressed observations never
   replace verbatim source drawers. Confirm the save or report failure.

The references directory must travel with this skill; do not install only
`SKILL.md`. The shared protocol remains authoritative and is linked online so
installed bundles do not depend on sibling repository directories. If it cannot
be read, report that limitation before attempting its detailed recovery steps.
A skill guides model-invoked memory operations; it does not install auto-save
hooks or prove automatic recall injection. Verify optional automation separately.
MemPalace is local-first; use only user-approved storage and services, and do not
send private source content to an unconfigured external service.

[recall]: https://github.com/MemPalace/mempalace/blob/develop/integrations/shared/recall-protocol.md#retrieve-only-the-context-you-need
[recovery]: https://github.com/MemPalace/mempalace/blob/develop/integrations/shared/recall-protocol.md#recovering-a-corrupt-index

## License

[MemPalace](https://github.com/MemPalace/mempalace) is MIT licensed. Created by Milla Jovovich, Ben Sigman, Igor Lins e Silva, and contributors.
