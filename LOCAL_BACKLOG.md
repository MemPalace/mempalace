# Local backlog — deferred ideas

Ideas investigated and scoped, deliberately not implemented yet. Each
entry has enough context to pick up cold, without re-deriving the
investigation. Durable notes about the community-wide value of an idea
are worth turning into a GitHub issue when picked up; this file is just
"don't lose this," not a public commitment.

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
