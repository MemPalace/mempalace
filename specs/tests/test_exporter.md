# Spec: tests/test_exporter.py

This file is a behavioral test suite for `export_palace`, the palace-to-Markdown
exporter. The tests define the observable contract of `export_palace(palace_path,
output_dir)`: its return value, the on-disk directory/file structure it produces,
the Markdown format of those files, and its symlink-safety guarantees. Each test
first builds a populated palace from project source directories via `mine`, then
exercises the exporter (tests/test_exporter.py:L8-L9).

## Test fixture: palace construction

A reusable setup helper builds a small palace with content spanning two wings
(tests/test_exporter.py:L17-L55). It creates two project directories. Project A
declares wing `alpha` with two rooms `backend` and `frontend`, configured via a
`mempalace.yaml` file containing keys `wing` (string) and `rooms` (list of objects
each with `name` and `description`) (tests/test_exporter.py:L23-L38). Project A
places a file `backend/server.py` and `frontend/app.js`, each containing a short
code snippet repeated 20 times to exceed minimum drawer size
(tests/test_exporter.py:L24-L27). Project B declares wing `beta` with one room
`docs` and a file `docs/guide.md` likewise repeated 20 times
(tests/test_exporter.py:L40-L50). The helper mines both projects into a single
shared palace path, calling `mine(project_dir, palace_path)` once per project, and
returns the palace path (tests/test_exporter.py:L52-L55). The `mempalace.yaml`
config is YAML with the structure shown above; rooms are an ordered list of
`{name, description}` objects (tests/test_exporter.py:L28-L50).

A helper `write_file(path, content)` creates any missing parent directories and
writes UTF-8 text (tests/test_exporter.py:L12-L14).

## Return value contract (statistics)

`export_palace` returns a statistics object (dictionary) with integer counts under
keys `wings`, `rooms`, and `drawers` (tests/test_exporter.py:L64-L69). For the
two-wing fixture, `wings` is exactly 2, `rooms` is at least 2, and `drawers` is at
least 3 (tests/test_exporter.py:L67-L69). When the palace path does not exist /
contains no data (empty palace), the return value is exactly `{"wings": 0,
"rooms": 0, "drawers": 0}` and no error is raised (tests/test_exporter.py:L126-L134).

## On-disk output structure

`export_palace(palace_path, output_dir)` writes its output rooted at `output_dir`
(tests/test_exporter.py:L64). At the root it creates a file `index.md`
(tests/test_exporter.py:L72). For each wing it creates a subdirectory named after
the wing — `alpha/` and `beta/` (tests/test_exporter.py:L73-L74). Within each wing
directory it creates one Markdown file per room, named `<room>.md`: for wing
`alpha` the files `backend.md` and `frontend.md`, and for wing `beta` the file
`docs.md` (tests/test_exporter.py:L77-L79).

## Room Markdown file format

Each room file begins with a top-level heading `# <wing> / <room>` followed by a
newline — e.g. the `alpha/backend.md` file starts with the literal text
`# alpha / backend\n` (tests/test_exporter.py:L96). Each drawer in the room is
rendered as a second-level heading whose text begins with the prefix `drawer_`
(`## drawer_`) (tests/test_exporter.py:L97). Each drawer includes a metadata table
introduced by the header row `| Field | Value |`, containing at least the rows
`| Source |`, `| Filed |`, and `| Added by |` (tests/test_exporter.py:L98-L101).
The file contains horizontal-rule separators rendered as `---`
(tests/test_exporter.py:L102).

## Index Markdown file format

The root `index.md` file contains a top-level title text `# Palace Export`
(tests/test_exporter.py:L118). It contains a summary table introduced by the
header row `| Wing | Rooms | Drawers |` (tests/test_exporter.py:L119). Each wing is
listed as a Markdown link whose link text is the wing name and whose target is the
wing directory with a trailing slash — `[alpha](alpha/)` and `[beta](beta/)`
(tests/test_exporter.py:L120-L121).

## Symlink-safety contract (defense-in-depth)

The exporter must never follow a symbolic link at any path it would write to;
encountering one is a hard error. In every such case `export_palace` raises an
error whose message matches `symbolic link` (a `ValueError`), and the symlink's
target is left completely untouched (tests/test_exporter.py:L155-L237).

- If `output_dir` itself is a symbolic link, export refuses with the
  `symbolic link` error, and the decoy directory the link points at remains empty
  (nothing was written through it) (tests/test_exporter.py:L155-L171).
- If a wing subdirectory (e.g. `output_dir/alpha`) is pre-placed as a symbolic
  link, export refuses with the same error and the decoy target stays empty
  (tests/test_exporter.py:L176-L192).
- If a room file path (e.g. `output_dir/alpha/backend.md`) is a symbolic link to
  an existing file, export refuses and the linked file's contents are unchanged —
  the decoy file still reads exactly `untouched\n`
  (tests/test_exporter.py:L197-L214).
- If the root `output_dir/index.md` is a symbolic link to an existing file, export
  refuses and the linked file's contents remain exactly `untouched\n`
  (tests/test_exporter.py:L219-L235).

These symlink checks apply at each level (output root, wing directory, room file,
index file) independently, so a malicious or pre-existing symlink at any one level
aborts the entire export before any write that would follow it
(tests/test_exporter.py:L155-L237).

## Environment notes (not part of the contract)

Symlink creation is unsupported on some runtimes (Windows without Developer
Mode/admin, restricted CI sandboxes); the test helper skips the symlink tests when
symlink creation raises an OS-level or not-implemented error, rather than failing
(tests/test_exporter.py:L139-L152). This is a test-harness accommodation, not an
exporter behavior.
