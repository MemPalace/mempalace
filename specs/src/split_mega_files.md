# Specification: split_mega_files

Splits concatenated Claude Code transcript "mega-files" into per-session `.txt` files. Each mega-file is a `.txt` containing multiple Claude Code sessions identified by `Claude Code v` headers; the tool detects true session starts, slices the file at those boundaries, and writes one file per session named with date/time, detected people, and a subject derived from the first user prompt (mempalace/split_mega_files.py:L1-L23).

## Configuration & Environment

The default source directory is taken from the environment variable `MEMPALACE_SOURCE_DIR`; if unset, it defaults to `<HOME>/Desktop/transcripts` (mempalace/split_mega_files.py:L31-L32).

An optional known-names config is read from `<HOME>/.mempalace/known_names.json`. If the file exists and parses as JSON it is loaded and cached; if it does not exist or fails to parse (invalid JSON or read error), the config is treated as absent (cached as none) (mempalace/split_mega_files.py:L36-L59).

Known people are derived from the config: if the parsed config is a JSON array, that array is used; if it is a JSON object, the value of its `names` key is used (default empty list); otherwise (no config) a fallback list of generic names `["Alice", "Ben", "Riley", "Max", "Sam", "Devon", "Jordan"]` is used (mempalace/split_mega_files.py:L37-L72).

A username-to-name mapping is derived from the config object's `username_map` key (an object), defaulting to empty when the config is absent or is an array (mempalace/split_mega_files.py:L75-L80).

## Public Functions

### is_true_session_start(lines, idx) -> bool

Given the list of file lines and an index, returns true when the 6-line window starting at `idx` (lines `idx`..`idx+5` joined) contains neither the substring `Ctrl+E` nor `previous messages`. Those substrings mark a mid-session context restore rather than a genuine new session (mempalace/split_mega_files.py:L83-L89).

### find_session_boundaries(lines) -> list of int

Returns, in ascending order, the indices of every line that both contains the substring `Claude Code v` and satisfies `is_true_session_start` at that index (mempalace/split_mega_files.py:L92-L98).

### extract_timestamp(lines) -> (human_str | null, iso_str | null)

Scans only the first 50 lines for the first line matching the pattern `⏺ <H:MM AM|PM> <Weekday>, <Month> <DD>, <YYYY>` (time is 1-2 digit hour, 2-digit minute, `AM` or `PM`) (mempalace/split_mega_files.py:L101-L122). On a match it maps the full English month name to a 2-digit number (unknown month maps to `00`), zero-pads the day to 2 digits, and strips colons and spaces from the time. It returns two strings: an ISO date `YYYY-MM-DD` and a human/filename form `YYYY-MM-DD_<HHMMAM>` (time with `:` and spaces removed). If no line in the first 50 matches, returns `(null, null)` (mempalace/split_mega_files.py:L106-L131).

### extract_people(lines) -> sorted list of str

Examines the joined text of the first 100 lines. For each known person name, if a whole-word case-insensitive match is found in the text, the name is added to the result set (mempalace/split_mega_files.py:L134-L145). Additionally, the first occurrence of a path of the form `/Users/<username>/` is extracted; if that username is present in the configured `username_map`, the mapped name is added (mempalace/split_mega_files.py:L147-L155). The result is returned as a sorted list of unique names (mempalace/split_mega_files.py:L139-L157).

### extract_subject(lines) -> str

Finds the first line beginning with `> ` (a user prompt). The prompt text after `> ` is stripped; it is accepted only if non-empty, longer than 5 characters, and not matching the skip pattern for shell-style commands (lines starting with any of `./`, `cd `, `ls `, `python`, `bash`, `git `, `cat `, `source `, `export `, `claude`, `./activate`) (mempalace/split_mega_files.py:L160-L171). An accepted prompt is cleaned for filenames by removing all characters that are not word characters, whitespace, or hyphens, collapsing whitespace runs into single hyphens, and truncating to 60 characters (mempalace/split_mega_files.py:L172-L175). If no acceptable prompt is found, returns the literal string `session` (mempalace/split_mega_files.py:L176-L176).

### split_file(filepath, output_dir, dry_run=False) -> list of output paths

Refuses to process files larger than 500 MB: prints a SKIP message and returns an empty list (mempalace/split_mega_files.py:L184-L188). Reads the file with undecodable bytes replaced, preserving line endings (mempalace/split_mega_files.py:L189-L189).

Computes session boundaries. If fewer than 2 true session starts exist, the file is not treated as a mega-file and an empty list is returned (mempalace/split_mega_files.py:L191-L193). A sentinel boundary equal to the total line count is appended so the last session extends to end of file (mempalace/split_mega_files.py:L195-L196).

The output directory is the supplied `output_dir`, or the source file's parent directory when none is given (mempalace/split_mega_files.py:L198-L198).

For each consecutive boundary pair, the line range is sliced into a chunk. Chunks with fewer than 10 lines are skipped as tiny fragments (mempalace/split_mega_files.py:L201-L204). For each retained chunk, the timestamp, people, and subject are extracted from that chunk (mempalace/split_mega_files.py:L206-L208).

#### Output filename contract

The filename is built as `<src_stem>__<ts_part>_<people_part>_<subject>.txt` where: `src_stem` is the source file stem with every non-word/non-hyphen character replaced by `_` and truncated to 40 characters; `ts_part` is the human timestamp string, or `part<NN>` (1-based, zero-padded to 2 digits) when no timestamp was found; `people_part` is up to the first 3 detected names joined with `-`, or `unknown` when none; `subject` is the extracted subject (mempalace/split_mega_files.py:L210-L216). The full name is then sanitized: every character that is not a word character, dot, or hyphen is replaced with `_`, and runs of `_` are collapsed to a single `_` (mempalace/split_mega_files.py:L217-L219). The source-stem prefix exists to prevent collisions when multiple mega-files yield sessions with identical timestamp/people/subject (mempalace/split_mega_files.py:L210-L212).

In dry-run mode, a line is printed describing the would-be file and chunk size; no file is written (mempalace/split_mega_files.py:L223-L224). Otherwise the chunk's joined text is written to the output path as UTF-8 and a `+` line is printed (mempalace/split_mega_files.py:L225-L227). The output path is appended to the returned list in either case (mempalace/split_mega_files.py:L229-L231).

## CLI Behavior (main)

Command-line arguments (mempalace/split_mega_files.py:L234-L262):
- `--source` — source directory; defaults to `MEMPALACE_SOURCE_DIR` / `~/Desktop/transcripts`. When given, it is user-expanded and resolved to an absolute path (mempalace/split_mega_files.py:L238-L243, L264-L264).
- `--output-dir` — output directory; default is same directory as each source file (mempalace/split_mega_files.py:L244-L246, L265-L265).
- `--min-sessions` — integer, default 2; only files with at least N session boundaries are processed (mempalace/split_mega_files.py:L247-L252, L280-L281).
- `--dry-run` — flag; show actions without writing (mempalace/split_mega_files.py:L253-L255).
- `--file` — process a single specified file instead of scanning the directory (mempalace/split_mega_files.py:L256-L261, L267-L268).

When not given a single `--file`, the candidate files are all `*.txt` in the source directory, sorted by name (mempalace/split_mega_files.py:L269-L270).

### Mega-file detection pass

Each candidate file larger than 500 MB is skipped with a printed message (mempalace/split_mega_files.py:L273-L277). Other files are read (undecodable bytes replaced) and their session boundaries counted; a file qualifies as a mega-file when its boundary count is at least `min-sessions` (mempalace/split_mega_files.py:L278-L281). If no qualifying files are found, a message is printed and the program returns without processing (mempalace/split_mega_files.py:L283-L285).

A header block is printed showing whether this is a DRY RUN or SPLITTING, the source, the output target, and the number of mega-files (mempalace/split_mega_files.py:L287-L293).

### Splitting pass and backup contract

Each mega-file is processed via `split_file`. After processing, if not a dry run and at least one output file was written, the original source file is renamed to the same path with extension replaced by `.mega_backup` (the original is never deleted) (mempalace/split_mega_files.py:L296-L306). A closing summary line reports the number of files that would be / were created and the number of mega-files (mempalace/split_mega_files.py:L308-L313).

## Side Effects & Invariants

- Filesystem reads: source `.txt` files and the optional `~/.mempalace/known_names.json` (mempalace/split_mega_files.py:L51-L53, L278-L278).
- Filesystem writes (non-dry-run only): per-session `.txt` output files, and renaming of each split source to `*.mega_backup` (mempalace/split_mega_files.py:L226-L226, L302-L303).
- Environment read: `MEMPALACE_SOURCE_DIR` (mempalace/split_mega_files.py:L32-L32).
- Standard output: progress/header/summary text is printed throughout (mempalace/split_mega_files.py:L287-L313).
- No network access and no telemetry occur.
- Ordering: session boundaries and resulting output files follow the order of sessions in the source file; directory scan order is lexicographic by filename (mempalace/split_mega_files.py:L95-L98, L270-L270).
- Safety invariant: files exceeding 500 MB are never read or split, and originals are preserved (renamed, not deleted) (mempalace/split_mega_files.py:L185-L188, L302-L303).
