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

## 27 tools exposed

Surface = mempalace's reference [openclaw skill](../openclaw/SKILL.md)'s
**19 tools** (as of PR #491, April 2026) **+ 8 agent-facing tools mempalace
has added since** that openclaw hasn't caught up to. Excludes admin tools
only.

| Tool | What |
|------|------|
| `mempalace_search` | Semantic search, optionally scoped by wing/room |
| `mempalace_status` | Palace overview: total drawers, per-wing counts, protocol |
| `mempalace_list_wings` | All wings with drawer counts |
| `mempalace_list_rooms` | Rooms (and counts) within a wing |
| `mempalace_get_taxonomy` | Full wing → room → drawer-count tree |
| `mempalace_get_aaak_spec` | The AAAK dialect spec (also in the wake-up block) |
| `mempalace_add_drawer` | File a verbatim drawer (auto-tags `added_by=hermes`) |
| `mempalace_update_drawer` | Edit a drawer in place (mempalace is append-first; prefer superseding) |
| `mempalace_delete_drawer` | Remove a drawer by id |
| `mempalace_list_drawers` | Browse drawers in a wing/room |
| `mempalace_get_drawer` | Fetch full verbatim content by id |
| `mempalace_check_duplicate` | Find the closest existing drawer before filing |
| `mempalace_kg_query` | Knowledge-graph relationships for an entity |
| `mempalace_kg_add` | Add a `(subject, predicate, object)` fact |
| `mempalace_kg_invalidate` | Mark a fact ended (palace-protocol step 5) |
| `mempalace_kg_timeline` | Full temporal timeline for an entity |
| `mempalace_kg_stats` | Graph counts |
| `mempalace_diary_write` | Append a diary entry |
| `mempalace_diary_read` | Read recent diary entries |
| `mempalace_traverse` | Walk the room graph from a starting room |
| `mempalace_graph_stats` | Room / hallway / tunnel counts |
| `mempalace_find_tunnels` | Discover cross-wing tunnels |
| `mempalace_create_tunnel` | Mint a cross-wing tunnel for a bridging entity |
| `mempalace_list_tunnels` | List tunnels (optionally by wing) |
| `mempalace_delete_tunnel` | Remove a tunnel by id |
| `mempalace_follow_tunnels` | Discover rooms connected from a `(wing, room)` |
| `mempalace_memories_filed_away` | Drawers filed during the current session |

**Intentionally omitted** (admin operations not agent-facing):
`mempalace_sync` (mines a project directory into the palace; user-initiated),
`mempalace_hook_settings`, `mempalace_reconnect`.

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

**Invalid wing/room names never drop a turn.** Wing and room names are
validated with the same `sanitize_name` rules the MCP write tools use
(no `/`, `..`, null bytes, over-length names). But unlike the MCP
tools — which return an error to the caller — live filing *falls back*
instead: an invalid wing files under `wing_general`, an invalid room
under `conversations`, and a warning is logged. A config typo in
`MEMPALACE_WING` or `wing_config.json` must not silently lose
conversation turns; misrouted-but-recallable beats gone.

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
