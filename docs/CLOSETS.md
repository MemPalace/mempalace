# Closets — The Searchable Index Layer

## What closets are

Drawers hold your verbatim content. Closets are the index — compact pointers that tell the searcher which drawers to open.

```
CLOSET: "built auth system|Ben;Igor|→drawer_api_auth_a1b2c3"
         ↑ topic           ↑ entities  ↑ points to this drawer
```

Search never depends on closets: every query runs against the drawers directly, and a closet match only moves the drawers of its source file up the ranking (see [How search uses closets](#how-search-uses-closets)).

## Lifecycle

### When are closets created?

Closets are created by the project miner (`mempalace mine` in projects mode) and by diary ingest. For each file:
1. Content is chunked into drawers (verbatim, ~800 chars each)
2. Topics, entities, and quotes are extracted from the first 5,000 characters of the content
3. A closet is created with pointer lines to at most three drawers (for a project file, its first three; for a diary entry, the entry's first chunk)

Conversation mining (`mempalace mine --mode convos`) does not build closets. Those wings are searched through their drawers only.

### What's inside a closet?

Each line is one atomic topic pointer:
```
topic description|entity1;entity2|→drawer_id_1,drawer_id_2
"verbatim quote from the content"|entity1|→drawer_id_3
```

When the drawers carry line and date metadata, a fourth segment locates the span: `topic|entities|YYYY-MM-DD:Lstart-Lend|→drawer_ids`.

Topics are never split across closets. If adding a topic would exceed 1,500 characters, a new closet is created.

### When do closets update?

When a file is re-mined (content changed, or `NORMALIZE_VERSION` was bumped), the miner first deletes every closet for that source file (`purge_file_closets`) and then writes a fresh set. Stale topics from the prior mine are gone — closets are always a snapshot of the current content, never an accumulation across runs.

### What about stale topics?

There are no stale topics: each re-mine is a clean rebuild for that source file. If a file gets larger and produces fewer or more closets than last time, the leftover numbered closets from the larger run are still purged because the delete is done by `source_file`, not by ID.

### Do closets survive palace rebuilds?

Closets are stored in the `mempalace_closets` ChromaDB collection alongside `mempalace_drawers`. If you delete and rebuild the palace, closets are recreated during the next `mempalace mine`.

## How search uses closets

Closets are a ranking signal, never a gate. An earlier design searched closets first and fell back to drawers; it was replaced because weak closets (regex extraction over narrative text) hid drawers that direct search would have found.

```
Query → vector search over mempalace_drawers (the floor; see the fallback below)
         ↓
    closet search over mempalace_closets → best closet rank per source file
         ↓
    drawers whose source file matched a closet get a rank-based boost
    (0.40 / 0.25 / 0.15 / 0.08 / 0.04 off the distance for the top five
     closet ranks; closets with cosine distance > 1.5 give no boost)
         ↓
    boosted hits are re-rendered from their source file: the drawer with the
    most query-term overlap, plus its neighbours on each side
         ↓
    BM25 hybrid re-rank (vector 0.6 / BM25 0.4 by default; configurable)
         ↓
    return chunk-level results
```

The boost is keyed by **source file**, not by the drawer IDs in the pointer lines. Search does not follow the `→drawer_id` pointers.

Every hit carries `matched_via`: `"drawer"` for a plain vector hit, `"drawer+closet"` when a closet boosted it. Boosted hits also carry `closet_boost` and a `closet_preview` field showing the closet line that matched. `similarity` is always the raw vector score; the boost changes only the ordering (`effective_distance`).

With no closets (palace created before this feature, or a conversation-only palace), search is plain drawer search plus the BM25 re-rank. Closets are created on the next project mine.

The pipeline above is the normal path. When the vector index is disabled (the capacity probe found the HNSW segment diverged from `chroma.sqlite3`, #1222), search never opens either collection: it runs a BM25-only search straight from `chroma.sqlite3`'s full-text index, with no vector step and no closet boost.

## Limits

| Setting | Value | Reason |
|---------|-------|--------|
| Max closet size | 1,500 chars (`CLOSET_CHAR_LIMIT`) | Leaves buffer under ChromaDB's working limit |
| Source content scanned | 5,000 chars (`CLOSET_EXTRACT_WINDOW`) | Caps regex extraction cost on long files; back-of-file content is currently invisible to closet extraction (tracked for follow-up) |
| Max topics per file | 12 | Keeps closets focused |
| Max quotes per file | 3 | Most relevant only |
| Max entities per pointer | 5 | Top names by frequency, after stoplist filtering |

## For developers

Closet functions live in `mempalace.palace` (`mempalace/palace/collection.py` and `mempalace/palace/closets.py`):
- `get_closets_collection()` — get the closets ChromaDB collection
- `build_closet_lines()` — extract topics/entities/quotes into pointer lines
- `upsert_closet_lines()` — write lines to closets respecting the char limit (overwrites existing IDs; does not append — call `purge_file_closets` first when re-mining)
- `purge_file_closets()` — delete every closet for a given source file before rebuild
- `CLOSET_CHAR_LIMIT` / `CLOSET_EXTRACT_WINDOW` — size constants

The closet boost lives in `mempalace.searcher` (`mempalace/searcher/`):
- `_closet_boosts()` (`query.py`) — query closets, keep the best rank per source file
- `_enrich_closet_hits()` (`candidates.py`) — re-render boosted hits from their source file's drawers
- `_extract_drawer_ids_from_closet()` (`filters.py`) — parse `→drawer_a,drawer_b` pointers out of a closet document (search does not currently use it)

Note: only the project miner (`miner.py::process_file`) and diary ingest (`diary_ingest.py`) build closets today. Conversation-mined wings (Claude Code JSONL, ChatGPT export, etc.) get no closet boost and are searched through their drawers only.
