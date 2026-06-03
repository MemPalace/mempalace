# MemPalace × Hermes integration

Plug MemPalace into [Hermes](https://github.com/NousResearch/hermes-agent)
as a first-class memory provider. Every conversation turn is filed into the
palace automatically — verbatim, semantic-search ready, available in the next
session.

## One-command install

```bash
pip install mempalace
mempalace hermes install
```

This installs the plugin into `~/.hermes/plugins/mempalace/`, points
`memory.provider: mempalace` in `~/.hermes/config.yaml`, and (optionally)
backfills your existing Hermes sessions.

After the next Hermes restart, the AI uses MemPalace for memory.

## What it does

| Hook | Behavior |
|------|----------|
| `initialize()` | Loads wing config + identity from `~/.mempalace/`, opens the ChromaDB collection through `mempalace.backends.chroma.ChromaBackend` (so the canonical embedding function is bound), starts the background filing worker, warms the AAAK wake-up cache. |
| `system_prompt_block()` | Injects identity (L0) + AAAK wake-up at session start, when available. |
| `prefetch()` | Semantic search before each turn; top-N drawer snippets injected as context. |
| `sync_turn()` | Files the (user, assistant) pair via a bounded background queue — the agent loop never blocks. |
| `on_session_end()` | Mines the full session and regenerates the AAAK critical-facts layer for the next session. |
| `on_session_switch()` | Repoints subsequent writes at the new `session_id` on `/resume`, `/branch`, `/reset`, `/new`, and after context compression. |
| `on_pre_compress()` | Files messages about to be compressed and tells the summarizer the verbatim copy stays searchable via `mempalace_search`. |
| `on_memory_write()` | Mirrors explicit `add user` writes into the knowledge graph as `user asserted <content>`. |
| `on_delegation()` | Records subagent (task, result) pairs as synthetic turns so parent-session recall surfaces delegated work. |

The plugin is inactive when `agent_context in {"cron", "flush"}` or
`platform == "cron"` — system-generated turns must not corrupt the user
representation.

## 8 tools exposed

| Tool | What |
|------|------|
| `mempalace_search` | Semantic search across the palace, optionally scoped by wing/room |
| `mempalace_status` | Palace overview: total drawers, per-wing counts |
| `mempalace_list_wings` | All wings with drawer counts |
| `mempalace_list_rooms` | Rooms (and counts) within a wing |
| `mempalace_kg_query` | Knowledge-graph relationships for an entity, with optional `since` filter |
| `mempalace_kg_add` | Add a `(subject, predicate, object)` fact to the knowledge graph |
| `mempalace_diary_write` | Append an AAAK diary entry |
| `mempalace_diary_read` | Read recent diary entries |

## Manual install

If you'd rather not run `mempalace hermes install`:

1. Copy `mempalace/integrations/hermes/__init__.py` → `~/.hermes/plugins/mempalace/__init__.py`
2. Copy `mempalace/integrations/hermes/backfill.py` → `~/.hermes/plugins/mempalace/backfill.py`
3. Write `~/.hermes/plugins/mempalace/plugin.yaml`:

   ```yaml
   name: mempalace
   version: 1.0.0
   description: "MemPalace memory provider — verbatim, local, semantic recall across sessions."
   pip_dependencies:
     - mempalace>=3.0.0
   hooks:
     - on_session_end
     - on_session_switch
     - on_pre_compress
     - on_memory_write
     - on_delegation
   ```

4. In `~/.hermes/config.yaml`, set `memory.provider: mempalace`
5. Restart Hermes.

## Backfill existing sessions

`mempalace hermes install` offers backfill interactively. To run it standalone
later:

```bash
python ~/.hermes/plugins/mempalace/backfill.py \
    --sessions-dir ~/.hermes/sessions \
    --palace-path ~/.mempalace/palace
```

## Configuration

The provider reads from `$HERMES_HOME/mempalace.json` first; non-empty
env vars then override individual keys; anything still missing falls
back to defaults. An empty env var is treated as unset, not as an
empty-string override.

Supported config keys (also exposed via `MempalaceProvider.get_config_schema()`):

| Key | Env var | Default |
|---|---|---|
| `palace_path` | `MEMPALACE_PALACE_PATH` | `~/.mempalace/palace` |
| `identity_path` | `MEMPALACE_IDENTITY_PATH` | `~/.mempalace/identity.txt` |
| `wing` | `MEMPALACE_WING` | auto-classify via `wing_config.json` |
| `n_prefetch` | — | `3` (clamped to 1-20) |

`collection_name` is intentionally **not** user-configurable on this
side. Writes go through this provider's collection; reads from
`prefetch` / `_tool_search` go through `search_memories`, which reads
its own collection name from `~/.mempalace/config.json`. Letting users
set the name in two places would silently let the two diverge — set
it once in mempalace's own config.

Underlying mempalace state lives in `~/.mempalace/`:

- `identity.txt` — L0 wake-up text loaded every session
- `wing_config.json` — keyword → wing routing for auto-classification
- `palace/` — ChromaDB collection (`mempalace_drawers` by default)
- `knowledge_graph.sqlite3` — temporal KG
- `diary.jsonl` — AAAK diary entries

Generate them with `mempalace init <project-dir>`.

## Why this plugin won't break existing palaces

Earlier in-tree Hermes PRs (#5671, #12203, #9761) all called
`chromadb.PersistentClient.get_or_create_collection(...)` directly, without
passing `embedding_function=`. ChromaDB then bound its default 384-dim
embedding function to the collection. Users with existing palaces built on
`bge-m3` (1024-dim) or `embeddinggemma-300m` would hit a hard dimension
mismatch on the next write.

This plugin instead goes through `mempalace.backends.chroma.ChromaBackend`,
which delegates to `mempalace.embedding.get_embedding_function()` and binds
the *same* embedding function the rest of mempalace uses. Existing palaces
import without rebuild.
