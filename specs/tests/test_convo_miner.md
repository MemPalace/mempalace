# Behavior Spec: Conversation Miner (`convo_miner`)

This spec is derived from the test suite `tests/test_convo_miner.py`, which exercises the
public surface of the conversation miner. It describes observable behavior of three public
entry points: `mine_convos`, `_is_ai_tool_path`, and `_resolve_wing`, plus the on-disk
drawer store contract those functions produce.

## Storage Contract (drawer collection)

Mining writes into a persistent palace store rooted at a `palace_path` directory. Drawers
are stored in a collection named `mempalace_drawers` (tests/test_convo_miner.py:L27-L29).
Each stored drawer is addressable by a string `id` and carries a document body plus a
metadata record. Drawer metadata includes at least the fields `wing`, `room`,
`source_file`, `chunk_index`, `normalize_version`, and `extract_mode`
(tests/test_convo_miner.py:L108-L111, L162-L173). The store supports retrieval filtered by
metadata, e.g. fetching all drawers whose `source_file` equals a given resolved path
(tests/test_convo_miner.py:L108-L109, L142-L143), and supports a semantic/text query that
returns matching documents (tests/test_convo_miner.py:L32-L33).

The `source_file` metadata value is the fully resolved (canonical, symlink-collapsed)
absolute path of the input file, not the path as supplied (tests/test_convo_miner.py:L52-L56,
L107-L108). On platforms where temp paths are symlinks (e.g. macOS `/var` -> `/private/var`)
the stored value reflects the resolved target (tests/test_convo_miner.py:L52-L53).

## `mine_convos(input_dir, palace_path, wing=..., extract_mode=..., dry_run=..., limit=...)`

### Inputs
- `input_dir`: directory containing conversation transcript files (`.txt`) to mine
  (tests/test_convo_miner.py:L18-L25).
- `palace_path`: directory where the drawer store is created/opened
  (tests/test_convo_miner.py:L24-L27).
- `wing` (string): the wing under which produced drawers are filed; written into each
  drawer's `wing` metadata (tests/test_convo_miner.py:L25, L162-L171).
- `extract_mode` (string, optional): `"exchange"` (default behavior) or `"general"`,
  recorded per drawer in `extract_mode` metadata (tests/test_convo_miner.py:L98-L110).
- `dry_run` (boolean, optional): when true, performs no writes to the palace
  (tests/test_convo_miner.py:L292).
- `limit` (integer, optional): caps the number of files processed in this run
  (tests/test_convo_miner.py:L445).

### Core mining behavior
Given transcript files with exchange markers (lines beginning with `> `), mining produces
one or more drawers and persists them to the `mempalace_drawers` collection; a typical
three-exchange transcript yields at least two drawers (tests/test_convo_miner.py:L19-L29).
After mining, a text query against the stored documents returns at least one matching
document (tests/test_convo_miner.py:L32-L33).

Every produced drawer is stamped with the current `normalize_version`
(tests/test_convo_miner.py:L146-L147), and (when `extract_mode` is supplied) with that
extract mode (tests/test_convo_miner.py:L108-L110).

### Console output contract
`mine_convos` prints a summary to standard output. Observed summary lines include:
- `Files skipped (already filed): N` reporting how many files were skipped because already
  mined (tests/test_convo_miner.py:L61, L80, L103, L180).
- `Files processed: N` reporting how many files were newly mined this run
  (tests/test_convo_miner.py:L448).
- `Drawers filed: N` reporting the count of drawers written this run
  (tests/test_convo_miner.py:L449-L453).

### Idempotency / sentinel behavior
A file too short to produce any chunks (below the minimum chunk size, e.g. content `"hi"`)
is still recorded as mined via a sentinel, so it is skipped on a subsequent run
(tests/test_convo_miner.py:L38-L61). After the first run, `file_already_mined(col,
resolved_file)` returns true for such a file (tests/test_convo_miner.py:L52-L56). A second
run reports `Files skipped (already filed): 1` (tests/test_convo_miner.py:L58-L61).

A file whose content is long enough to pass the minimum chunk size but contains no exchange
markers (no `> ` lines), so chunking yields zero exchange chunks, is also marked with a
sentinel and skipped on re-run, reporting `Files skipped (already filed): 1`
(tests/test_convo_miner.py:L66-L80).

### Mode coexistence (exchange then general)
A transcript already mined with `extract_mode="exchange"` is NOT treated as already-filed
when re-mined with `extract_mode="general"`; the second run reports
`Files skipped (already filed): 0` and adds general-mode drawers alongside the existing
exchange-mode drawers (tests/test_convo_miner.py:L85-L103). After both runs, the set of
`extract_mode` metadata values for that source file is a superset of `{"exchange",
"general"}` (tests/test_convo_miner.py:L108-L110). General-mode extraction can classify and
name drawers by detected topic; at least one drawer id begins with the pattern
`drawer_<wing>_decision_` (e.g. `drawer_test_decision_`) (tests/test_convo_miner.py:L111).

### Stale-version rebuild (schema gate)
Each mine stamps drawers with the current `normalize_version`
(tests/test_convo_miner.py:L146-L147). On a later mine, if the stored drawers for a source
file carry an older `normalize_version` than the current one, the miner does NOT skip the
file; it purges the stale drawers and refiles fresh ones — a rebuild, reported as
`Files skipped (already filed): 0` (tests/test_convo_miner.py:L149-L182). The rebuild:
- removes any pre-existing drawer rows for that source file, including orphan drawers whose
  `chunk_index` no longer corresponds to current output (an orphan with id `orphan_drawer`
  is gone after rebuild) (tests/test_convo_miner.py:L161-L188);
- ensures no stale document content survives (e.g. `"STALE NOISE"` and `"OLD ORPHAN"` bodies
  are absent from rebuilt documents) (tests/test_convo_miner.py:L156-L191);
- stamps all rebuilt drawers with the current `normalize_version`
  (tests/test_convo_miner.py:L192-L194).

This is the mechanism by which a normalization/noise-stripping upgrade is applied to existing
corpora automatically on the next mine, without manual erasure
(tests/test_convo_miner.py:L117-L123).

### Concurrency contract
Mining holds a per-palace lock (`mine_palace_lock`) keyed by `palace_path`
(tests/test_convo_miner.py:L211-L213). If another process/thread currently holds that lock
for the same palace, a `mine_convos` call that would write must immediately raise
`MineAlreadyRunning` rather than blocking/queuing as a waiter
(tests/test_convo_miner.py:L221-L254). The lock is intentionally re-entrant within a single
thread (so collection write methods can compose with mining without self-deadlock); the
concurrency guarantee is enforced across separate processes/threads
(tests/test_convo_miner.py:L204-L207).

A `dry_run=True` call bypasses the palace lock entirely: it must NOT raise
`MineAlreadyRunning` even while another process holds the lock, because it performs no writes
(tests/test_convo_miner.py:L263-L292).

### `limit` semantics
`limit=N` counts only newly-mined files, not already-mined skips. Given 4 already-mined files
and 3 new files, a run with `limit=2` reports `Files processed: 2` and `Drawers filed:` with
a count greater than zero — i.e. the limit budget is spent on new work, not consumed by
skipping the 4 pre-existing files (tests/test_convo_miner.py:L421-L453).

## `_is_ai_tool_path(path) -> bool`

Returns whether a directory path lies within a known AI-tool conversation storage location.
It returns true for:
- any subdirectory inside `~/.claude/projects/` (tests/test_convo_miner.py:L314-L318);
- the `~/.claude/projects/` directory itself (tests/test_convo_miner.py:L321-L325);
- the `~/.codex` root (tests/test_convo_miner.py:L328-L331) and any subpath such as
  `~/.codex/sessions/YYYY/MM/DD/` (tests/test_convo_miner.py:L334-L338);
- the `~/.gemini` root (tests/test_convo_miner.py:L341-L344) and any subpath such as
  `~/.gemini/tmp/<hash>/chats/` (tests/test_convo_miner.py:L347-L351).

It returns false for:
- a bare `~/.claude` directory without a `/projects` segment (the settings dir is not a
  conversation source) (tests/test_convo_miner.py:L354-L359);
- arbitrary unrelated directories (tests/test_convo_miner.py:L362-L365);
- directories whose names merely resemble the markers as substrings, e.g. `.gemini-backup`
  or `.codex-archive` — matching is by exact path segment, not substring
  (tests/test_convo_miner.py:L368-L376).

## `_resolve_wing(path, wing) -> str`

Determines the effective wing for a mined directory.

- An explicit, non-empty `wing` always wins, even when `path` is an AI-tool path; the value
  is returned unchanged (tests/test_convo_miner.py:L379-L383).
- When `wing` is `None` or an empty string (both treated as "no wing"), and `path` is an
  AI-tool path (Claude projects, Codex, or Gemini), the resolved wing is the literal string
  `"wing_api"`, grouping API-sourced conversations under one dedicated wing
  (tests/test_convo_miner.py:L386-L401, L413-L418).
- When `wing` is absent and `path` is unrelated, the wing is the sanitized directory
  basename: spaces and hyphens become underscores and the result is lowercased — e.g.
  `"MyProject Folder"` -> `"myproject_folder"` (tests/test_convo_miner.py:L404-L410).

## Side Effects & Environment
- Mining reads transcript files from `input_dir` and writes a persistent store under
  `palace_path` (tests/test_convo_miner.py:L18-L27).
- Mining acquires a filesystem lock scoped to the palace (tests/test_convo_miner.py:L211-L213,
  L253-L254).
- Wing auto-routing depends on the user home location (`HOME`), against which AI-tool paths
  like `~/.claude/projects` are matched (tests/test_convo_miner.py:L230, L314-L351).
