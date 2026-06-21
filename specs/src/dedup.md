# Spec: dedup — Near-duplicate drawer detection and removal

## Purpose

This module finds drawers (verbatim text chunks) that originate from the same source file and are too similar to one another, keeps the longest/richest version, and deletes the rest. It operates against the configured storage backend's similarity search; with local backends no external calls occur (mempalace/dedup.py:L1-L13).

## Constants and observable defaults

- The collection operated on is named `mempalace_drawers` (mempalace/dedup.py:L35-L35).
- The default similarity threshold is a cosine **distance** of `0.15` (lower = stricter; `0.15` corresponds to roughly 85% cosine similarity). For looser dedup of paraphrased content, values of `0.3`–`0.4` are intended (mempalace/dedup.py:L36-L39).
- Only source groups with at least `5` drawers are considered for dedup or stats (`MIN_DRAWERS_TO_CHECK = 5`) (mempalace/dedup.py:L40-L40).

## Palace path resolution

When no palace path is provided, the palace path is resolved from configuration. If configuration cannot be loaded, the fallback path is `~/.mempalace/palace` (the `.mempalace/palace` directory under the user's home directory) (mempalace/dedup.py:L43-L50).

## Public surface

### `get_source_groups(col, min_count=5, source_pattern=None, wing=None) -> dict[str, list[str]]`

Groups all drawer IDs by their `source_file` metadata value and returns only groups containing at least `min_count` drawers (mempalace/dedup.py:L53-L78).

- It reads the total drawer count, then iterates over the collection in batches of `1000`, fetching only metadata, advancing an offset by the number of IDs returned per batch until the offset reaches the total or a batch returns no IDs (mempalace/dedup.py:L59-L76).
- If `wing` is given, only drawers whose `wing` metadata equals that value are fetched (this catches cross-wing duplicates from the same source mined into multiple wings) (mempalace/dedup.py:L54-L57, L66-L67).
- A drawer's group key is its `source_file` metadata; if missing it defaults to the literal string `"unknown"` (mempalace/dedup.py:L72-L72).
- If `source_pattern` is set, a drawer is skipped unless `source_pattern` appears as a case-insensitive substring of its `source_file` (mempalace/dedup.py:L73-L74).
- Output: a map from `source_file` to the list of its drawer IDs, filtered to groups with `len(ids) >= min_count` (mempalace/dedup.py:L78-L78).

### `dedup_source_group(col, drawer_ids, threshold=0.15, dry_run=True) -> (kept_ids, deleted_ids)`

Deduplicates drawers within one source-file group using a greedy longest-first algorithm and returns the tuple `(list of kept IDs, list of deleted IDs)` (mempalace/dedup.py:L81-L129).

Algorithm and ordering guarantees:

- Fetches the documents and metadata for the given IDs, then **sorts items by document length, longest first** (mempalace/dedup.py:L87-L89).
- Any drawer whose document is empty or shorter than `20` characters is unconditionally added to the delete list (never kept) (mempalace/dedup.py:L94-L97).
- The first (longest) valid drawer is always kept (mempalace/dedup.py:L99-L101).
- For each subsequent valid drawer, a similarity query is run using the drawer's own document text as the query, requesting up to `min(number_kept, 5)` nearest neighbors and their distances. The drawer is treated as a duplicate (deleted) if any returned neighbor that is already in the kept set has a distance strictly less than `threshold`; otherwise it is kept (mempalace/dedup.py:L103-L121).
- If the similarity query raises any error, the drawer is kept (fail-safe toward retention, never deletion) (mempalace/dedup.py:L122-L123).

Side effects:

- Deletions only occur when `dry_run` is false; in that case IDs are deleted in batches of up to `500` at a time (mempalace/dedup.py:L125-L127).
- When `dry_run` is true, no deletion side effect occurs, but the returned `deleted_ids` still lists what would be removed (mempalace/dedup.py:L125-L129).

### `show_stats(palace_path=None) -> None`

Prints duplication statistics without modifying any data (mempalace/dedup.py:L132-L149).

Observable output:

- Opens the `mempalace_drawers` collection at the resolved palace path and computes source groups with the default minimum of `5` (mempalace/dedup.py:L134-L137).
- Prints the count of sources with 5+ drawers and the total drawer count across those sources (thousands grouping applied to the total) (mempalace/dedup.py:L139-L141).
- Prints a "Top 15 by drawer count" list, sorted by descending drawer count, showing each count and the source path truncated to 65 characters (mempalace/dedup.py:L143-L146).
- Prints an estimated-duplicates figure computed as the sum over groups with more than 20 drawers of `int(len(ids) * 0.4)` (mempalace/dedup.py:L148-L149).

### `dedup_palace(palace_path=None, threshold=0.15, dry_run=True, source_pattern=None, min_count=5, wing=None) -> None`

Main entry point that deduplicates near-identical drawers across the palace and prints a progress/summary report (mempalace/dedup.py:L152-L210).

Behavior:

- Resolves the palace path, opens the `mempalace_drawers` collection, and prints a header with the palace path, drawer count, threshold, and mode (`DRY RUN` if `dry_run` else `LIVE`) (mempalace/dedup.py:L161-L173).
- If `wing` is set, prints the wing and scopes group selection to that wing (mempalace/dedup.py:L175-L177).
- Computes source groups and **processes them sorted by descending drawer count** (mempalace/dedup.py:L177-L186).
- For each group, calls `dedup_source_group` with the same threshold and dry-run flag, accumulates total kept and deleted counts, and prints a per-group line only when that group had deletions (showing index, source truncated to 50 chars, original count, kept count, and removed count) (mempalace/dedup.py:L186-L196).
- Prints elapsed time, a before→after drawer total with removed count, and the post-run collection count. When `dry_run` is true, additionally prints a notice that no changes were written (mempalace/dedup.py:L198-L210).

## Command-line interface

When invoked as a standalone program (`python -m mempalace.dedup`), the following arguments are accepted (mempalace/dedup.py:L213-L226):

- `--palace PATH` — palace directory (default: config-resolved); the value is tilde-expanded before use (mempalace/dedup.py:L215-L215, L228-L228).
- `--threshold FLOAT` — cosine distance threshold (default `0.15`) (mempalace/dedup.py:L216-L221).
- `--dry-run` — preview without deleting (mempalace/dedup.py:L222-L222).
- `--stats` — show stats only (mempalace/dedup.py:L223-L223).
- `--wing NAME` — scope dedup to a single wing (mempalace/dedup.py:L224-L224).
- `--source PATTERN` — filter by source-file substring pattern (mempalace/dedup.py:L225-L225).

Dispatch: if `--stats` is given, `show_stats` is called and no dedup runs; otherwise `dedup_palace` is called with the parsed threshold, dry-run, source pattern, and wing (mempalace/dedup.py:L230-L239).

## Notes / invariants

- The module never deletes data in `--stats` mode or in dry-run mode (mempalace/dedup.py:L125-L129, L132-L149).
- Deletion always favors retention on ambiguity: too-short documents are removed, but any backend query failure results in keeping the drawer (mempalace/dedup.py:L94-L97, L122-L123).
- The longest document in each source group is always retained as the canonical version (mempalace/dedup.py:L89-L101).
