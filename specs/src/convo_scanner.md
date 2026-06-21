# Spec: convo_scanner

Parses Claude Code conversation directories into `ProjectInfo` records. Claude Code stores sessions under `~/.claude/projects/<slug>/<id>.jsonl`, where `<slug>` is the original working directory with `/` replaced by `-`; that encoding is lossy, so this scanner reads the true path from a `cwd` field inside the JSONL records (mempalace/convo_scanner.py:L1-L20). Output uses the same `ProjectInfo` shape produced by the project scanner so multiple discovery sources can be combined (mempalace/convo_scanner.py:L14-L19).

## Data type: ProjectInfo

A record with fields: `name` (string), `repo_root` (filesystem path), `manifest` (string or null), `has_git` (bool), `total_commits` (int), `user_commits` (int), `is_mine` (bool) (mempalace/project_scanner.py:L68-L75). This scanner repurposes `total_commits` and `user_commits` to carry the conversation session count, not git commit counts (mempalace/convo_scanner.py:L122-L125).

## Public surface

### is_claude_projects_root(path) -> bool

Input: a filesystem path. Returns `false` if the path is not a directory (mempalace/convo_scanner.py:L40-L41). Returns `false` if listing the directory raises a filesystem error (mempalace/convo_scanner.py:L42-L45). Otherwise returns `true` if at least one immediate child directory whose name begins with `-` contains at least one regular file with a `.jsonl` suffix; if listing a candidate child raises a filesystem error that child is skipped (mempalace/convo_scanner.py:L46-L53). Returns `false` if no qualifying child is found (mempalace/convo_scanner.py:L54-L54).

### scan_claude_projects(path) -> list[ProjectInfo]

Input: a path string or path. The path is expanded (user home `~`) and resolved to an absolute canonical path before use (mempalace/convo_scanner.py:L126-L126). If the resolved path does not satisfy `is_claude_projects_root`, an empty list is returned (mempalace/convo_scanner.py:L127-L128).

Iterates immediate children in sorted order (mempalace/convo_scanner.py:L131-L131). A child is processed only if it is a directory and its name starts with `-`; others are skipped (mempalace/convo_scanner.py:L132-L133). For each processed child, the set of session files is the regular files with a `.jsonl` suffix directly inside it; if listing that child raises a filesystem error the child is skipped (mempalace/convo_scanner.py:L134-L137). A child with zero session files is skipped (mempalace/convo_scanner.py:L138-L139).

For each remaining child a `ProjectInfo` is built with: `name` resolved as described below; `repo_root` set to the child directory path; `manifest` null; `has_git` false; `total_commits` and `user_commits` both set to the session count (number of `.jsonl` files); `is_mine` true (mempalace/convo_scanner.py:L141-L152).

Results are deduplicated by resolved name: when two child directories resolve to the same name, the one with the larger session count is kept; ties keep the first one encountered (the existing entry is replaced only when the new session count is strictly greater) (mempalace/convo_scanner.py:L130-L155).

Ordering of the returned list: sorted by session count descending, then by name ascending (mempalace/convo_scanner.py:L157-L160).

## Project name resolution

For a project directory, session files are the `.jsonl` regular files inside it, sorted by file modification time descending (newest first) (mempalace/convo_scanner.py:L107-L111). Sessions are examined in that order; for the first session whose `cwd` can be read, the project name is the final path component of that `cwd`, or the full `cwd` string if it has no final component (mempalace/convo_scanner.py:L112-L115). If no session yields a `cwd`, the name falls back to slug-decoding the directory name (mempalace/convo_scanner.py:L116-L116).

### cwd extraction from a session file

A session file is read line by line, examining at most the first 20 lines (mempalace/convo_scanner.py:L31-L31, mempalace/convo_scanner.py:L62-L66). Each line is trimmed; blank lines are skipped (mempalace/convo_scanner.py:L67-L69). Each non-blank line is parsed as a JSON object; lines that are not valid JSON are skipped (mempalace/convo_scanner.py:L70-L73). The first record whose `cwd` field is a non-empty string returns that string value (mempalace/convo_scanner.py:L74-L76). If the file cannot be opened/read (filesystem error), `null` is returned (mempalace/convo_scanner.py:L77-L78). If no record within the scanned lines has a usable `cwd`, `null` is returned (mempalace/convo_scanner.py:L79-L79). Reading uses UTF-8 decoding with malformed bytes replaced rather than aborting (mempalace/convo_scanner.py:L63-L63).

### slug fallback decoding

The directory name (slug) has all leading `-` characters removed, then is split on `-` into non-empty segments; the last segment is returned. If there are no non-empty segments, the original slug is returned unchanged (mempalace/convo_scanner.py:L89-L91).

### modification-time helper

When sorting sessions, a file whose modification time cannot be obtained (filesystem error) is treated as having modification time `0.0`, sorting it oldest (mempalace/convo_scanner.py:L94-L99).

## Side effects and contracts

Read-only with respect to the filesystem: the scanner lists directories, stats files, and reads at most the first 20 lines of session JSONL files; it never writes (mempalace/convo_scanner.py:L40-L160). Input on-disk contract consumed: a `.claude/projects/` directory of `<slug>` subdirectories (names starting with `-`) each containing `<id>.jsonl` session files, where session records are newline-delimited JSON objects optionally carrying a string `cwd` field (mempalace/convo_scanner.py:L1-L12, mempalace/convo_scanner.py:L74-L76). No network, process, or environment-variable side effects.
