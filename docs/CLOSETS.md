# Closets — The Searchable Index Layer

## What closets are

Drawers hold your verbatim content. Closets are the index — compact pointers that tell the searcher which drawers to open.

```
CLOSET: "built auth system|Ben;Igor|→drawer_api_auth_a1b2c3"
         ↑ topic           ↑ entities  ↑ points to this drawer
```

An agent searching "who built the auth?" hits the closet first (fast scan of short text), then opens the referenced drawer to get the full verbatim content.

## Lifecycle

### When are closets created?

Closets are created during `mempalace mine`. For each file mined:
1. Content is chunked into drawers (verbatim, ~800 chars each)
2. Topics, entities, and quotes are extracted from the content
3. A closet is created with pointer lines to those drawers

### What's inside a closet?

Each line is one atomic topic pointer:
```
topic description|entity1;entity2|→drawer_id_1,drawer_id_2
"verbatim quote from the content"|entity1|→drawer_id_3
```

Topics are never split across closets. If adding a topic would exceed 1,500 characters, a new closet is created.

### When do closets update?

When a file is re-mined (content changed, or `NORMALIZE_VERSION` was bumped), the miner first deletes every closet for that source file (`purge_file_closets`) and then writes a fresh set. Stale topics from the prior mine are gone — closets are always a snapshot of the current content, never an accumulation across runs.

### What about stale topics?

There are no stale topics: each re-mine is a clean rebuild for that source file. If a file gets larger and produces fewer or more closets than last time, the leftover numbered closets from the larger run are still purged because the delete is done by `source_file`, not by ID.

### Do closets survive palace rebuilds?

Closets are stored in the `mempalace_closets` ChromaDB collection alongside `mempalace_drawers`. If you delete and rebuild the palace, closets are recreated during the next `mempalace mine`.

## How search uses closets

```
Query → search mempalace_closets (fast, small documents)
         ↓
    top closet hits → parse `→drawer_id_a,drawer_id_b` pointers
         ↓
    fetch exactly those drawers from mempalace_drawers (verbatim content)
         ↓
    apply max_distance filter
         ↓
    return chunk-level results (same shape as direct search)
```

Hits carry `matched_via: "closet"` (or `"drawer"` for the fallback path) plus a `closet_preview` field showing the line that surfaced them.

If no closets exist (palace created before this feature) — or all closet hits get filtered out by `max_distance` — search falls back to direct drawer search. Closets are created on next mine.

> **BM25 hybrid re-rank** is on the roadmap (deferred to a follow-up PR alongside generic `LLM_*` env-var support); the current closet search ranks purely by ChromaDB cosine distance against the closet text.

## Limits

| Setting | Value | Reason |
|---------|-------|--------|
| Max closet size | 1,500 chars (`CLOSET_CHAR_LIMIT`) | Leaves buffer under ChromaDB's working limit |
| Source content scanned | 5,000 chars (`CLOSET_EXTRACT_WINDOW`) | Caps regex extraction cost on long files; back-of-file content is currently invisible to closet extraction (tracked for follow-up) |
| Max topics per file | 12 | Keeps closets focused |
| Max quotes per file | 3 | Most relevant only |
| Max entities per pointer | 5 | Top names by frequency, after stoplist filtering |

## For developers

Closet functions live in `mempalace/palace.py`:
- `get_closets_collection()` — get the closets ChromaDB collection
- `build_closet_lines()` — extract topics/entities/quotes into pointer lines
- `upsert_closet_lines()` — write lines to closets respecting the char limit (overwrites existing IDs; does not append — call `purge_file_closets` first when re-mining)
- `purge_file_closets()` — delete every closet for a given source file before rebuild
- `CLOSET_CHAR_LIMIT` / `CLOSET_EXTRACT_WINDOW` — size constants

The closet-first search path lives in `mempalace/searcher.py`:
- `_extract_drawer_ids_from_closet()` — parse `→drawer_a,drawer_b` pointers out of a closet document
- `_closet_first_hits()` — query closets, parse pointers, hydrate matching drawers, return chunk-level hits or `None` to fall back

Note: only the project miner (`miner.py::process_file`) builds closets today. Conversation-mined wings (Claude Code JSONL, ChatGPT export, etc.) will keep using direct drawer search via the searcher fallback until the convo-closet PR lands.
