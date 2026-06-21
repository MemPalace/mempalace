# Behavior Spec: `mempalace.convo_scanner`

Derived from the test suite `tests/test_convo_scanner.py`, which exercises the
public and internal surface of the `convo_scanner` module: detecting a Claude
projects root, extracting a working directory from session transcripts, decoding
encoded directory slugs, reading file modification times safely, resolving
project names, and scanning a directory tree for discoverable projects
(tests/test_convo_scanner.py:L6-L13).

## Domain context

The module operates over a "Claude projects root" directory. Such a root
contains subdirectories whose names are path-encoded slugs (e.g.
`-home-user-dev-foo`), each holding one or more JSONL session transcript files
(tests/test_convo_scanner.py:L20-L23). Each JSONL file is a newline-delimited
sequence of JSON objects ("records"), one per line
(tests/test_convo_scanner.py:L53-L57).

## `is_claude_projects_root(dir) -> bool`

Returns true when the given directory is a Claude projects root, defined as:
it contains at least one subdirectory whose name begins with `-` (dash prefix)
and that subdirectory contains at least one `.jsonl` file
(tests/test_convo_scanner.py:L19-L23).

Returns false when:
- A subdirectory exists with a `.jsonl` file but its name does NOT start with a
  dash prefix (e.g. `normal-folder`) (tests/test_convo_scanner.py:L26-L30).
- A dash-prefixed subdirectory exists but contains no `.jsonl` file (only other
  file types such as `.txt`) (tests/test_convo_scanner.py:L33-L37).
- The directory is empty (tests/test_convo_scanner.py:L40-L41).
- The directory does not exist on disk (tests/test_convo_scanner.py:L44-L45).

## `_extract_cwd_from_session(file) -> str | None`

Reads a JSONL session file and returns the value of the `cwd` field from the
first record that contains it (tests/test_convo_scanner.py:L51-L58). Records
that lack a `cwd` field (e.g. a leading `file-history-snapshot` record) are
skipped, and scanning continues to subsequent records
(tests/test_convo_scanner.py:L53-L58).

Lines that are not valid JSON are skipped without aborting; scanning continues
to later valid records, and the `cwd` from a later well-formed record is still
returned (tests/test_convo_scanner.py:L61-L66).

Returns `None` when no record in the file contains a `cwd` field
(tests/test_convo_scanner.py:L69-L72), and returns `None` when the file does not
exist (tests/test_convo_scanner.py:L75-L76).

## `_decode_slug_fallback(slug) -> str`

Decodes an encoded directory slug to a project name by returning the last
path segment. The slug is a dash-delimited string; the final non-empty segment
is returned (e.g. `-home-user-dev-foo` -> `foo`)
(tests/test_convo_scanner.py:L82-L83). A doubled dash that produces an empty
segment is treated as a separator only, so the trailing real segment is still
returned (e.g. `-home-user--bentokit` -> `bentokit`)
(tests/test_convo_scanner.py:L86-L87).

Edge cases:
- An empty input string returns an empty string
  (tests/test_convo_scanner.py:L90-L91).
- An input consisting solely of dashes (e.g. `---`) returns the input string
  unchanged (tests/test_convo_scanner.py:L94-L95).

## `_safe_mtime(file) -> float`

Returns the modification time of the file as a floating-point value. If reading
the file's stat metadata raises an OS-level error (e.g. permission denied), the
function returns `0.0` rather than propagating the error
(tests/test_convo_scanner.py:L101-L112).

## `_resolve_project_name(project_dir) -> str`

Resolves a human-meaningful project name for an encoded project directory. The
preferred source is the `cwd` recorded inside the directory's session files: the
last path segment of that `cwd` becomes the project name (e.g. a `cwd` of
`/home/user/dev/cool-proj-real` yields `cool-proj-real`, even though the encoded
directory name decodes differently) (tests/test_convo_scanner.py:L118-L123).

When none of the directory's session files contain a `cwd`, the name falls back
to decoding the directory's own slug via the last-segment rule (e.g.
`-home-user-dev-foo` -> `foo`) (tests/test_convo_scanner.py:L126-L130).

When multiple session files exist with differing `cwd` values, the `cwd` from
the session file with the newest modification time wins. This handles a project
directory that was renamed between sessions: the most recent session's `cwd`
determines the resolved name (tests/test_convo_scanner.py:L133-L149).

## `scan_claude_projects(dir) -> list[Project]`

Scans a candidate projects-root directory and returns a list of discovered
project descriptors.

Returns an empty list when the directory is empty
(tests/test_convo_scanner.py:L155-L156), and when the directory does not look
like a Claude projects root (no dash-prefixed subdirectory containing `.jsonl`
files) (tests/test_convo_scanner.py:L159-L163). Dash-prefixed subdirectories
that contain no `.jsonl` file (e.g. only a `.md` file) are ignored and do not
produce a result entry (tests/test_convo_scanner.py:L187-L191).

For each qualifying project directory, the result entry carries at least:
- `name`: the resolved project name (tests/test_convo_scanner.py:L176-L179).
- `user_commits`: an integer count equal to the number of session files
  (`.jsonl`) belonging to that project. A directory with 2 session files reports
  `user_commits == 2`; one with a single session file reports `1`
  (tests/test_convo_scanner.py:L166-L184).
- `is_mine`: a boolean flag set to `true` for discovered projects
  (tests/test_convo_scanner.py:L194-L200).

Ordering / ranking: results are ranked such that a project with more session
files ranks higher than one with fewer (the test asserts alpha with 2 sessions
ranks above beta with 1) (tests/test_convo_scanner.py:L180-L184).

Deduplication by name: when two distinct encoded directories resolve to the same
project name, they collapse into a single result entry. The surviving entry is
the one with the greater session count, and its `user_commits` reflects that
larger count (two dirs both decoding to `proj`, one with 2 sessions and one with
1, collapse to a single `proj` entry with `user_commits == 2`)
(tests/test_convo_scanner.py:L203-L218).

## Observable contracts

- Session files are `.jsonl`: newline-delimited JSON, one record per line; the
  reader tolerates malformed (non-JSON) lines by skipping them
  (tests/test_convo_scanner.py:L61-L66).
- The `cwd` record field is a filesystem path string; project names are derived
  from its final path segment (tests/test_convo_scanner.py:L51-L58,L118-L123).
- Project descriptors expose the fields `name`, `user_commits`, and `is_mine`
  (tests/test_convo_scanner.py:L177-L200).
