# Spec: convo_miner

Mines conversation files (chat exports, transcripts) into the palace. Conversations
are normalized, chunked by exchange pair (one user turn plus its response = one unit)
or by a general memory extractor, and filed as drawers in the same palace as project
mining but with a distinct ingest strategy (mempalace/convo_miner.py:L1-L9).

## Constants and observable contract values

- Recognized conversation file extensions are `.txt`, `.md`, `.json`, `.jsonl`
  (case-insensitive on suffix) (mempalace/convo_miner.py:L58-L63, L363).
- `MIN_CHUNK_SIZE` default floor is 30 characters; `CHUNK_SIZE` default is 800
  characters per drawer (mempalace/convo_miner.py:L65-L66).
- Files larger than `MAX_FILE_SIZE` = 500 MB are skipped during scanning
  (mempalace/convo_miner.py:L70-L77, L373-L374).
- Drawer upserts are batched in groups of 1000 (mempalace/convo_miner.py:L69, L419).
- Default extraction mode is `"exchange"`; the alternative is `"general"`
  (mempalace/convo_miner.py:L522, L526-L528).

## Hall detection: `_detect_hall_cached(content) -> str`

Routes content to a hall by scoring the first 3000 characters (lowercased) against
configured hall keywords, counting how many keywords appear, and returning the
highest-scoring hall; returns `"general"` if no keyword matches
(mempalace/convo_miner.py:L41-L54). Hall keywords are loaded once from config and
cached for the process lifetime (mempalace/convo_miner.py:L37-L47).

## Room detection: `detect_convo_room(content) -> str`

Scores the first 3000 characters (lowercased) against five fixed topic buckets:
`technical`, `architecture`, `planning`, `decisions`, `problems`, each defined by a
fixed keyword list. Returns the highest-scoring room, or `"general"` when nothing
matches (mempalace/convo_miner.py:L258-L335).

## Chunking: `chunk_exchanges(content, chunk_size=None, min_chunk_size=None) -> list`

Returns a list of chunk objects, each shaped `{"content": <str>, "chunk_index": <int>}`
with `chunk_index` ascending from 0 in emission order (mempalace/convo_miner.py:L139-L172, L231-L232).

Defaults: when `chunk_size`/`min_chunk_size` are not supplied, the module defaults
`CHUNK_SIZE` (800) and `MIN_CHUNK_SIZE` (30) are used (mempalace/convo_miner.py:L156-L159).

Validation: raises an error (`ValueError`) if `chunk_size <= 0`, or if
`min_chunk_size < 0` (mempalace/convo_miner.py:L161-L164).

Strategy selection: counts lines whose stripped form starts with `>`. If there are
at least 3 such quote lines, exchange-pair chunking is used; otherwise paragraph
chunking is used (mempalace/convo_miner.py:L166-L172).

### Exchange-pair chunking

A user turn is a line whose stripped form starts with `>`. After a user turn, all
following lines are accumulated as the AI response verbatim until a line whose
stripped form starts with `>` (next turn) or with `---` (separator) is reached
(mempalace/convo_miner.py:L185-L199). Response lines are joined with newlines (not
spaces) so blank lines and indentation survive; only trailing newlines are trimmed
(mempalace/convo_miner.py:L201-L204). The emitted unit is `"<user_turn>\n<ai_response>"`,
or just the user turn when there is no response (mempalace/convo_miner.py:L205). Lines
before the first `>` turn are skipped (mempalace/convo_miner.py:L208-L209).

### Paragraph chunking (fallback)

Splits on blank-line boundaries (`\n\n`), trimming each paragraph and dropping empty
ones (mempalace/convo_miner.py:L238). If there is at most one paragraph but the content
has more than 20 newlines, it falls back to grouping lines into groups of 25 lines each
(mempalace/convo_miner.py:L67-L68, L241-L246). Otherwise each paragraph is emitted
(mempalace/convo_miner.py:L248-L249).

### Bounded emission invariant

For each input unit, if its stripped length is at or below `min_chunk_size` the unit is
dropped entirely as noise; otherwise the unit is sliced into consecutive pieces of at
most `chunk_size` characters each, and every slice (including a small trailing remainder)
is emitted verbatim. `chunk_index` equals the count of chunks already emitted at the time
each slice is appended (mempalace/convo_miner.py:L214-L232).

## Scanning: `scan_convos(convo_dir) -> list`

Walks the expanded, resolved directory tree (mempalace/convo_miner.py:L355-L357). Directory
names in `SKIP_DIRS` are pruned from the walk (mempalace/convo_miner.py:L358). Files ending
in `.meta.json` are ignored (mempalace/convo_miner.py:L360-L361). Only files whose lowercased
suffix is a recognized conversation extension are considered (mempalace/convo_miner.py:L363).
Symlinks are skipped, and each skipped symlink prints a line `  SKIP: <relative-posix-path>
(symlink)` to standard error (errors writing that line are swallowed)
(mempalace/convo_miner.py:L365-L371). Files whose size exceeds 500 MB are skipped; a stat
error also skips the file (mempalace/convo_miner.py:L372-L376). Returns the list of qualifying
file paths (mempalace/convo_miner.py:L377-L378).

## Wing resolution: `_resolve_wing(convo_path, wing) -> str`

Precedence, first match wins: (1) an explicit non-empty `wing` argument is returned as-is;
(2) if the path is inside a known AI-tool storage dir, returns `"wing_api"`; (3) otherwise the
directory basename is normalized via config's `normalize_wing_name` (lowercase, spaces/hyphens
collapsed to underscores) (mempalace/convo_miner.py:L490-L512).

### AI-tool path detection: `_is_ai_tool_path(path) -> bool`

Returns true (exact path-segment matches, not substrings) when any resolved path segment equals
`.codex`, or any segment equals `.gemini`, or two consecutive segments are `.claude` then
`projects`. A bare `.claude` segment does not match. Resolution errors yield false
(mempalace/convo_miner.py:L461-L487).

## Mining entry point: `mine_convos(convo_dir, palace_path, wing=None, agent="mempalace", limit=0, dry_run=False, extract_mode="exchange")`

`extract_mode` selects `"exchange"` (Q+A pair chunking) or `"general"` (memory extractor:
decisions, preferences, milestones, problems, emotions) (mempalace/convo_miner.py:L515-L528).

Concurrency: a non-dry-run mine acquires a per-palace lock around the work; if another mine
holds it, `MineAlreadyRunning` propagates (CLI renders a holder message and exits non-zero).
Dry-run skips the lock and never writes (mempalace/convo_miner.py:L530-L566).

### Implementation behavior (`_mine_convos_impl`)

Chunk parameters come from config: `chunk_size` from config; `min_chunk_size` from config only
when explicitly set, otherwise the convo floor of 30 (more permissive than the project default)
(mempalace/convo_miner.py:L578-L591). Wing is resolved as above
(mempalace/convo_miner.py:L593-L594). Files are discovered via `scan_convos`
(mempalace/convo_miner.py:L596).

A header banner is printed to standard output reporting wing, source path, file count (with an
optional ` (limit: N new)` suffix when `limit > 0`), palace path, and a `DRY RUN` notice when
applicable (mempalace/convo_miner.py:L598-L608).

For non-dry-run, the already-mined set is bulk-prefetched in one pass keyed by `extract_mode`;
the per-file loop then does an O(1) membership check to skip files already filed at the current
normalize version (mempalace/convo_miner.py:L610-L634).

Per file: content is produced by `normalize(filepath)`. On an OS or value error during
normalization, a registry sentinel is written (non-dry-run) and the file is skipped
(mempalace/convo_miner.py:L636-L642). If normalized content is empty or its stripped length is
below the effective min chunk size, a sentinel is written and the file is skipped
(mempalace/convo_miner.py:L644-L647).

Chunking: in `general` mode chunks come from the general extractor (each chunk carries a
`memory_type`); otherwise from `chunk_exchanges` (mempalace/convo_miner.py:L649-L660). If no
chunks result, a sentinel is written and the file is skipped (mempalace/convo_miner.py:L662-L665).
In non-general mode the room is detected from content; in general mode the room is set per chunk
from `memory_type` (mempalace/convo_miner.py:L667-L671).

Dry-run output: for general mode prints `[DRY RUN] <name> → <N> memories (<type:count>, ...)`;
otherwise `[DRY RUN] <name> → room:<room> (<N> drawers)`. Room/type counts and drawer totals are
accumulated, files-mined is incremented, and processing stops once `limit > 0` files have been
mined (mempalace/convo_miner.py:L673-L692).

For non-dry-run, chunks are filed via the locked file routine; skipped files increment the skip
counter, room deltas are merged, drawers and files-mined counters advance, a progress line
`  + [<i>/<total>] <name(<=50 chars)> +<drawers_added>` is printed, and the loop stops at `limit`
(mempalace/convo_miner.py:L694-L712).

After processing (non-dry-run) the palace FTS5 index is validated
(mempalace/convo_miner.py:L714-L715). A summary banner prints files processed (processed minus
skipped), files skipped, drawers filed, a by-room breakdown sorted by descending count, and a
next-step hint (mempalace/convo_miner.py:L717-L727).

### Locked per-file filing (`_file_chunks_locked`)

Holds a per-source-file lock. After acquiring the lock it re-checks `file_already_mined`; if true,
returns `(0, {}, True)` signaling skip (mempalace/convo_miner.py:L386-L402). It then purges stale
drawers for that source file and extract mode (best-effort; failures are logged at debug and
ignored) so a normalize-version bump does not leave mixed old/new drawers
(mempalace/convo_miner.py:L404-L412). One `filed_at` timestamp is shared by all drawers of the
file (mempalace/convo_miner.py:L416-L418).

Chunks are upserted in batches of `DRAWER_UPSERT_BATCH_SIZE` (1000). For each chunk the room is
the chunk's `memory_type` in general mode (incrementing the room-count delta), else the file-level
room (mempalace/convo_miner.py:L419-L426). A drawer id is computed per chunk
(mempalace/convo_miner.py:L427-L429). Each drawer's metadata contains: `wing`, `room`, `hall` (from
cached hall detection on the chunk content), `source_file`, `chunk_index`, `added_by` (agent),
`filed_at`, `ingest_mode` = `"convos"`, `extract_mode`, `normalize_version`, and `id_recipe`
(mempalace/convo_miner.py:L432-L446). Before upsert, id/metadata collisions are asserted absent
(mempalace/convo_miner.py:L447). On upsert, an "already exists" error is swallowed (idempotent
re-file); any other error is re-raised (mempalace/convo_miner.py:L448-L457). Returns
`(drawers_added, room_counts_delta, False)` (mempalace/convo_miner.py:L458).

### Registry sentinel (`_register_file`)

Writes one sentinel record so files that normalize to nothing or yield zero chunks are not
re-read on every run. The sentinel document is `"[registry] <source_file>"`, with metadata: `wing`,
`room` = `"_registry"`, `source_file`, `added_by`, `filed_at` (current time), `ingest_mode` =
`"registry"`, `extract_mode`, `normalize_version`, `id_recipe`
(mempalace/convo_miner.py:L80-L104).

### Stale-drawer id collection (`_source_file_delete_ids`)

Pages through all drawers matching `source_file` in batches of 1000 and collects ids whose metadata
matches the requested extract mode. Legacy drawers lacking an explicit `extract_mode` are treated as
exchange-mode so schema rebuilds can clean them without deleting newer general-mode drawers
(mempalace/convo_miner.py:L107-L131).

## CLI / module entry point

When run as a script with no argument, prints
`Usage: python convo_miner.py <convo_dir> [--palace PATH] [--limit N] [--dry-run]` and exits with
status 1 (mempalace/convo_miner.py:L730-L733). With an argument, it mines the given directory using
the configured palace path (mempalace/convo_miner.py:L734-L736).

## Side effects summary

- Filesystem: reads files under the conversation directory; writes drawer and sentinel records into
  the palace collection via upsert/delete (mempalace/convo_miner.py:L596-L712, L80-L104, L404-L412).
- Standard output: header/progress/summary banners (mempalace/convo_miner.py:L598-L727).
- Standard error: per-symlink SKIP lines (mempalace/convo_miner.py:L368).
- Process exit: status 1 on the no-argument CLI usage path (mempalace/convo_miner.py:L731-L733).
- Locking: per-palace lock on non-dry-run mines and per-source-file lock during filing
  (mempalace/convo_miner.py:L557-L566, L397).
