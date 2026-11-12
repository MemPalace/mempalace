---
trigger: model_decision
description: Recall protocol for MemPalace — search the palace before answering about past work, people, projects, or prior decisions.
---

# MemPalace Recall Rule

Before answering a question about past work, a person, a project, or a prior decision, search the user's memory palace via the `mempalace` MCP server (`palace_query` with `FIND` for semantic search, `palace_query` with `KG` for entity/temporal facts) and quote any matching drawer verbatim rather than guessing.

 If `palace_query` is not loaded, fall back to `mempalace_search` / `mempalace_kg_query` / `mempalace_check_duplicate`.

Follow the full protocol in the `mempalace-recall` skill — single source of truth at [`integrations/shared/recall-protocol.md`](https://github.com/MemPalace/mempalace/blob/main/integrations/shared/recall-protocol.md).
