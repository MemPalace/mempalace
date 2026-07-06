# Local backlog — deferred ideas

Ideas investigated and scoped, deliberately not implemented yet. Each
entry has enough context to pick up cold, without re-deriving the
investigation. Durable notes about the community-wide value of an idea
are worth turning into a GitHub issue when picked up; this file is just
"don't lose this," not a public commitment.

## Per-file wing resolution for `mine_convos` on multi-project sweeps

**Status:** designed, not implemented. Deferred in favor of the
incremental-mining work (see the design doc from that session — ask if
it needs re-locating).

**Problem:** Mining `~/.claude/projects` (or any directory containing
sessions from more than one underlying project) in a single
`mempalace mine <dir> --mode convos` call puts every session into the
*same* wing, regardless of which project it actually came from. A
Claude Code session about `deductiv/export-everything` and one about
`forktail/forktail-app` end up in one undifferentiated bucket.

**Root cause** (`mempalace/convo_miner.py`):
- `_resolve_wing(convo_path, wing)` is called **once per `mine_convos()`
  invocation**, using the top-level directory passed in — not once per
  file.
- `scan_convos()` recursively walks that whole tree (`os.walk`),
  collecting every `.jsonl` across every project subdirectory in one
  pass.
- `_is_ai_tool_path(path)` matches any path containing the consecutive
  segments `.claude/projects` (or `.codex`, or `.gemini`), and when it
  matches, `_resolve_wing` defaults to a single hardcoded wing:
  `"wing_api"`. This check *always* wins over the basename fallback for
  anything under those roots, so mining a specific project's session
  subdirectory instead of the whole tree doesn't help either — the path
  still contains `.claude/projects`.
- Net effect: without an explicit `--wing` override, every AI-tool
  session mined in one call collapses into `wing_api`, with no
  automatic per-project split.

**The fix already half-exists in the codebase.** `mempalace/convo_scanner.py`
solves the harder part of this problem already — recovering the *real*
project identity, not the lossy directory-name encoding Claude Code uses
(`-Users-jrmurray-Code-deductiv-export-everything` conflates `/` and `-`,
so `foo-bar` in a slug could be one segment or two). Its
`_extract_cwd_from_session(session_file)` reads the first ~20 lines of
a session's JSONL looking for a `cwd` field — every message record
carries the true, unambiguous path. This is currently used *only* by
`project_scanner.py` for entity discovery during `mempalace init` —
confirmed via `grep -rln "convo_scanner\|scan_claude_projects" mempalace/*.py`,
zero references from `convo_miner.py`.

**Proposed fix:**
1. Import `_extract_cwd_from_session` from `convo_scanner.py` into
   `convo_miner.py` (no circular-import risk: `convo_scanner.py` only
   imports `project_scanner.py`).
2. Change wing resolution to run **per file** rather than once per
   `mine_convos()` call, when no explicit `--wing` is given and the file
   lives under an AI-tool path: extract that file's own `cwd`, run it
   through the existing `normalize_wing_name` (same slugging already
   used everywhere else — `cmd_init`, `room_detector_local`,
   `miner.load_config`), and use that as the per-file wing instead of
   the blanket `wing_api`.
3. Fall back to `wing_api` only when `_extract_cwd_from_session` returns
   `None` (malformed/empty JSONL, or genuinely no `cwd` field present —
   e.g. Codex/Gemini sessions may not carry the same field; verify their
   actual schema before assuming this generalizes past Claude Code
   JSONL specifically).
4. Explicit `--wing` argument still wins outright, unchanged — this only
   changes the *default* behavior for an undifferentiated multi-project
   sweep.

**Lower risk than incremental mining:** this wires together two
already-existing, already-tested pieces (`_extract_cwd_from_session`,
`normalize_wing_name`) rather than inventing new mechanics. Main design
work still needed: confirm `cwd` availability across all formats
`_is_ai_tool_path` currently covers (Claude Code confirmed; Codex/Gemini
not yet checked), decide the exact fallback chain, and test that
existing drawers already filed under `wing_api` aren't retroactively
touched (this only changes wing assignment for content mined *after* the
change — a NORMALIZE_VERSION-style consideration, though probably not an
actual version bump since it's a metadata/config-shaped change, not a
normalization-algorithm change).

**Test plan sketch:**
- Mine a synthetic multi-project tree (2+ fake project subdirs, each
  with a session JSONL carrying a distinct `cwd`) with no explicit
  `--wing`; assert each project's drawers land in a distinct,
  correctly-named wing.
- Malformed/empty JSONL with no readable `cwd` → falls back to
  `wing_api`, not a crash.
- Explicit `--wing` argument still overrides everything, unchanged.
- Non-Claude-Code AI-tool path (`.codex`, `.gemini`) — verify their real
  `cwd` availability before assuming the same per-file resolution
  applies; may need a per-format allowlist mirroring the incremental-
  mining design's format gate.

## `MempalaceConfig` crashes on a malformed non-dict config-section value

**Status:** found, not fixed. Found during adversarial review of the
incremental-mining rollout's final PR (default-flip for
`incremental_mining_enabled`), but it's pre-existing and unrelated to
that change — flagging here so it doesn't get lost.

**Problem:** several `MempalaceConfig` properties use the pattern
`self._file_config.get("<section>", {}).get("<key>", <default>)` —
e.g. `incremental_mining_enabled` (`config.py`), and the same shape for
`duplicate_detection_enabled`/`duplicate_detection_threshold`, and
likely `hooks` and others. If a user's `config.json` has that top-level
key set to something other than a dict/object — e.g.
`{"incremental_mining": false}` or `{"incremental_mining":
"disabled"}` (an easy typo, since the *env var* form of these flags
IS a bare string like `"false"`) — the inner `.get("enabled", ...)`
call raises `AttributeError: 'bool'/'str' object has no attribute
'get'`, crashing config load entirely rather than falling back to the
documented default the way a missing/malformed *value* does elsewhere
in this file.

**Fix sketch:** a small shared helper, e.g. `_get_section(self,
name: str) -> dict`, returning `self._file_config.get(name, {})` but
coercing a non-dict value to `{}` (with a stderr warning, matching the
`_dynamic_noise_patterns()` convention already used in `normalize.py`
for a bad `noise_patterns_file` line) instead of crashing. Every
property using the two-level `.get().get()` pattern would call this
instead of indexing `_file_config` directly.

**Test plan sketch:** for each affected property, a `config.json`
fixture with the section set to a bare bool, a bare string, and a
list, asserting the property falls back to its documented default (or,
if a warning-on-stderr convention is chosen, asserting the warning
fires) rather than raising.
