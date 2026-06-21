# Behavior Spec: `tests/test_save_hook_mines.py`

This is a test module that asserts contracts on two shell hook scripts located in
the repository's `hooks/` directory: `mempal_save_hook.sh` and
`mempal_precompact_hook.sh`. The tests read those scripts as text and assert that
specific contractual substrings are present, and additionally exercise an embedded
shell validator function (tests/test_save_hook_mines.py:L1-L9).

## Hook discovery contract

Both hook files are located relative to this test file: the parent of the test
file's directory, then `hooks/`, then the hook filename
(tests/test_save_hook_mines.py:L24-L28, L46-L50, L77-L80). The test repository
must therefore contain `hooks/mempal_save_hook.sh` and
`hooks/mempal_precompact_hook.sh` as readable text files.

## Save hook auto-mining contract (`mempal_save_hook.sh`)

The save hook's source text MUST contain the token `TRANSCRIPT_PATH`, indicating
it reads the transcript path supplied by the host (Claude Code)
(tests/test_save_hook_mines.py:L34). It MUST contain `mempalace mine`, i.e. it
invokes the mine command (tests/test_save_hook_mines.py:L35). It MUST contain the
literal shell expression `dirname "$TRANSCRIPT_PATH"`, so mining targets the
transcript's parent directory (tests/test_save_hook_mines.py:L36-L38). It MUST
contain `--mode convos`, ensuring the conversation miner runs rather than the
projects miner (tests/test_save_hook_mines.py:L39-L41).

## MEMPAL_DIR default contract

The hook must not silently disable mining via an empty `MEMPAL_DIR`. If the source
contains the literal `MEMPAL_DIR=""` (empty default), then an alternative mining
path must exist: either `mempalace mine` appears more than once in the source, or
the token `TRANSCRIPT_PATH` appears in the portion of the source preceding the
first `mempalace mine` occurrence (tests/test_save_hook_mines.py:L54-L66). If
`MEMPAL_DIR=""` is not present, no constraint is imposed by this case
(tests/test_save_hook_mines.py:L57).

## Transcript-path validator definition contract (both hooks)

Before asserting on validator presence, the test strips all lines whose first
non-whitespace character is `#` (comment lines), so the required tokens must appear
in non-comment code (tests/test_save_hook_mines.py:L83-L84).

Each hook's non-comment source MUST define a shell function via the literal
`is_valid_transcript_path() {` (tests/test_save_hook_mines.py:L88, L95). Each hook
MUST invoke that validator against the transcript path via the literal
`is_valid_transcript_path "$TRANSCRIPT_PATH"`, and this invocation must occur
before mining (tests/test_save_hook_mines.py:L89-L91, L96-L98). This applies to
both `mempal_save_hook.sh` and `mempal_precompact_hook.sh`
(tests/test_save_hook_mines.py:L86-L98).

## Validator runtime behavior contract (both hooks)

This behavior is verified by extracting the validator function from each hook and
running it under `bash`. The extraction takes the substring from the first
occurrence of `is_valid_transcript_path() {` up to and including the first `\n}\n`
sequence after that point (tests/test_save_hook_mines.py:L109-L113). The extracted
function is written to a temporary script that calls
`is_valid_transcript_path "$1"` and prints `OK` on success (exit 0) or `NO` on
failure (non-zero exit) (tests/test_save_hook_mines.py:L114-L123).

The validator MUST accept these paths (print `OK`):
- `/tmp/sessions/abc.jsonl` — absolute, `.jsonl` extension, no traversal (tests/test_save_hook_mines.py:L125)
- `/tmp/sessions/abc.json` — absolute, `.json` extension, no traversal (tests/test_save_hook_mines.py:L126)

The validator MUST reject these paths (print `NO`):
- `""` — empty argument (tests/test_save_hook_mines.py:L127)
- `/tmp/notes.txt` — disallowed extension `.txt` (tests/test_save_hook_mines.py:L128)
- `../etc/passwd.jsonl` — relative path with `..` traversal segment (tests/test_save_hook_mines.py:L129)
- `/tmp/../etc/t.jsonl` — absolute path containing a `..` traversal segment (tests/test_save_hook_mines.py:L130)

The implied contract: only paths ending in `.jsonl` or `.json` with no `..`
traversal segments and a non-empty value are valid
(tests/test_save_hook_mines.py:L125-L130).

## Platform and environment notes

The runtime-validator test is skipped on Windows (`sys.platform == "win32"`)
because the shell hooks are POSIX-only and Windows bash maps to `wsl.exe` with no
distro (tests/test_save_hook_mines.py:L100-L103). The other tests perform only text
assertions and run on all platforms. The validator test spawns a `bash` subprocess
per hook, writing a temporary script into a per-test temporary directory
(tests/test_save_hook_mines.py:L104-L123).

## Externally observable contracts summary

- Files `hooks/mempal_save_hook.sh` and `hooks/mempal_precompact_hook.sh` must exist and be readable (tests/test_save_hook_mines.py:L24-L28, L79-L80).
- Save hook text must include `TRANSCRIPT_PATH`, `mempalace mine`, `dirname "$TRANSCRIPT_PATH"`, and `--mode convos` (tests/test_save_hook_mines.py:L34-L41).
- Both hooks must define `is_valid_transcript_path() {` and call `is_valid_transcript_path "$TRANSCRIPT_PATH"` in non-comment code (tests/test_save_hook_mines.py:L86-L98).
- The validator exits 0 for valid `.json`/`.jsonl` non-traversal paths and non-zero otherwise (tests/test_save_hook_mines.py:L125-L130).
