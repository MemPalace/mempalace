# Spec: exporter

Exports a palace as a browsable folder tree of Markdown files: an `index.md` table of contents at the root, and one Markdown file per room nested under one directory per wing (`output_dir/wing_name/room_name.md`) (mempalace/exporter.py:L1-L12). Drawers are streamed in paginated batches so peak memory stays bounded regardless of palace size (mempalace/exporter.py:L10-L11).

## Public surface

### `export_palace(palace_path, output_dir, format="markdown") -> dict`

Exports all palace drawers as Markdown files organized by wing/room (mempalace/exporter.py:L68-L82).

Inputs:
- `palace_path` (string): path to the palace storage directory; opened via `get_collection` (mempalace/exporter.py:L76-L83).
- `output_dir` (string): destination directory for the exported Markdown tree (mempalace/exporter.py:L77-L77).
- `format` (string, default `"markdown"`): currently only `"markdown"` is meaningful; the parameter is accepted but not branched on (mempalace/exporter.py:L78-L78).

Output: a stats dict with integer fields `{"wings": N, "rooms": N, "drawers": N}` (mempalace/exporter.py:L80-L81, L199-L208).

### Helper behaviors (internal, but define observable contracts)

- `_safe_path_component(name)`: produces a filesystem-safe path component by replacing each of the characters `/ \ : * ? " < > |` with `_`, then stripping leading/trailing `.` and space characters; if the result is empty it becomes the literal `"unknown"` (mempalace/exporter.py:L23-L27).
- `_quote_content(text)`: formats text for a Markdown blockquote. Trailing newlines are stripped, the text is split on newlines, and lines are joined with `"\n> "` so each subsequent line is also prefixed as a blockquote continuation (mempalace/exporter.py:L211-L214).

## Empty-palace behavior

If the collection count is `0`, the function prints `  Palace is empty — nothing to export.` and returns `{"wings": 0, "rooms": 0, "drawers": 0}` without creating any output directory or files (mempalace/exporter.py:L84-L88).

## Streaming and ordering

The total drawer count is read once before streaming (mempalace/exporter.py:L84-L84). Drawers are fetched in batches of up to 1000 using an increasing `offset`, requesting documents and metadata; iteration stops when a batch returns no ids (mempalace/exporter.py:L106-L110, L173). `offset` advances by the number of ids actually returned in each batch (mempalace/exporter.py:L173).

Within each batch, records are grouped by `(wing, room)` and each room file is written once per batch in a single open/write cycle (mempalace/exporter.py:L112-L128, L140-L172). Because drawers are processed in batch/offset order, drawer sections appear in each room file in the order the storage backend returns them.

Per-drawer fields come from metadata with these fallbacks: `wing` defaults to `"unknown"`, `room` defaults to `"general"`, `source_file` defaults to empty string, `filed_at` defaults to empty string, `added_by` defaults to empty string (mempalace/exporter.py:L114-L125).

## On-disk output format (contract)

### Directory layout
- The root output directory is created (idempotently) and chmoded to `0o700` (best-effort; permission errors and unsupported-operation errors are ignored) (mempalace/exporter.py:L90-L95).
- Each distinct wing produces a subdirectory named by `_safe_path_component(wing)`, created once and chmoded `0o700` best-effort (mempalace/exporter.py:L128-L138).
- Each distinct room produces a file `<safe_room>.md` inside its wing directory, where `safe_room = _safe_path_component(room)` (mempalace/exporter.py:L140-L142).

### Room file format
The first time a room file is written (within this export run), it is opened in truncate/overwrite mode and begins with a header line `# {wing} / {room}` followed by a blank line; the room key is then marked as opened so subsequent batches append rather than overwrite (mempalace/exporter.py:L143-L149).

For each drawer, the following block is appended (mempalace/exporter.py:L151-L168):
- `## {drawer id}`
- blank line
- `> {quoted content}` (multiline content uses `> ` continuation per `_quote_content`)
- blank line
- a Markdown table with header `| Field | Value |` / `|-------|-------|` and rows `| Source | {source} |`, `| Filed | {filed} |`, `| Added by | {added_by} |`
- blank line
- a horizontal rule `---` followed by a blank line

For these three fields, empty/falsy values are rendered as the literal `"unknown"`: `source` from `source_file`, `filed` from `filed_at`, `added_by` from `added_by` (mempalace/exporter.py:L152-L165).

### index.md format
After streaming, `index.md` is written at the output root in truncate mode (mempalace/exporter.py:L195-L197). Its contents are (mempalace/exporter.py:L184-L197):
- Header `# Palace Export — {YYYY-MM-DD}` using the current local date (mempalace/exporter.py:L184-L186).
- a blank line, then a table header `| Wing | Rooms | Drawers |` / `|------|-------|---------|`.
- one row per wing, in ascending sorted order of wing name, formatted `| [{wing}]({wing}/) | {room_count} | {drawer_count} |` where the wing link target uses the raw (unsanitized) wing name with a trailing slash (mempalace/exporter.py:L176-L192).
- a trailing empty line. Lines are joined with `\n` (no trailing newline beyond the joined empty line) (mempalace/exporter.py:L193-L197).

Note: `index.md` wing rows use the raw wing name for both label and link, while the actual directory on disk uses the sanitized component; these can differ if the wing name contains sanitized characters (mempalace/exporter.py:L129-L130, L191-L192).

## Return value semantics

- `wings` = number of distinct wings that received at least one drawer (mempalace/exporter.py:L199-L200).
- `rooms` = total number of distinct rooms across all wings (sum of per-wing room counts) (mempalace/exporter.py:L201-L202, L176-L181).
- `drawers` = total number of drawers written across all room files (mempalace/exporter.py:L170-L171, L199-L203).

## Console side effects

The function prints progress to stdout: a `Streaming {total} drawers...` line before the loop (mempalace/exporter.py:L105-L105), a per-wing summary line `  {wing}: {N} rooms, {M} drawers` during stats building (mempalace/exporter.py:L177-L181), a final summary `Exported {drawers} drawers across {wings} wings, {rooms} rooms`, and `Output: {output_dir}` (mempalace/exporter.py:L204-L207).

## Security / symlink protection (contract)

- Before creating the output directory, if `output_dir` is itself a symbolic link, the function raises an error refusing to export and does not write (mempalace/exporter.py:L30-L41, L90-L90).
- Before creating each wing directory, the same symlink rejection is applied to the wing directory path (mempalace/exporter.py:L131-L132).
- All file writes (room files and `index.md`) refuse to follow a symlink at the target path. On platforms supporting `O_NOFOLLOW`, the open is performed atomically with no-follow semantics and a `0o600` creation mode; a symlink target causes an error refusing to write (closing the check/open race window). On platforms without `O_NOFOLLOW`, a pre-open symlink check is performed instead (mempalace/exporter.py:L44-L65, L146-L146, L196-L196). Write mode is append when the room was already opened, otherwise truncate (mempalace/exporter.py:L146-L146); append maps to `O_APPEND`, truncate to `O_TRUNC` in the no-follow path (mempalace/exporter.py:L54-L55).

## Error / edge cases

- Symlinked output dir, wing dir, or any write target raises a refusal error and aborts (mempalace/exporter.py:L37-L41, L59-L64).
- `chmod` failures during directory hardening are silently ignored (best-effort) (mempalace/exporter.py:L92-L95, L134-L137).
- A batch with no ids terminates streaming early (mempalace/exporter.py:L109-L110).
- Missing metadata fields fall back to defaults rather than failing (mempalace/exporter.py:L114-L124).
