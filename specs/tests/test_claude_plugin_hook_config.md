# Spec: `tests/test_claude_plugin_hook_config.py`

Schema-validation test suite asserting that the Claude plugin hook configuration file declares positive, bounded timeouts for every hook event (tests/test_claude_plugin_hook_config.py:L1-L1).

## Target artifact under test

The tests validate an on-disk JSON file located at `<repo-root>/.claude-plugin/hooks/hooks.json`, where `<repo-root>` is the parent directory two levels above this test file (tests/test_claude_plugin_hook_config.py:L8-L9). The file is read as UTF-8 text and parsed as JSON; its top-level value is a JSON object (tests/test_claude_plugin_hook_config.py:L28-L30).

## Hook config contract (the observable on-disk format being asserted)

The `hooks.json` object MUST contain a top-level key `"hooks"` whose value is an object keyed by event name (tests/test_claude_plugin_hook_config.py:L42-L43, L81). For each registered event the value is a non-empty array of "entry" objects (tests/test_claude_plugin_hook_config.py:L43-L44). Each event MUST declare exactly one entry; more than one is a contract violation because duplicate entries would double-fire the hook (tests/test_claude_plugin_hook_config.py:L45-L51). Each entry object MUST contain a key `"hooks"` whose value is a non-empty array of hook-command objects, and that array MUST contain exactly one element (tests/test_claude_plugin_hook_config.py:L52-L59). Each hook-command object MUST have `"type"` equal to the string `"command"` and MUST contain a `"timeout"` key (tests/test_claude_plugin_hook_config.py:L60-L64).

## Timeout bounds (per event)

The test fixes a mapping of event name to an inclusive integer timeout range in seconds: `"Stop"` -> `[10, 30]` and `"PreCompact"` -> `[60, 90]` (tests/test_claude_plugin_hook_config.py:L22-L25). For a given event, the `"timeout"` value MUST be a genuine integer within that inclusive range; values equal to the floor or ceiling are accepted (tests/test_claude_plugin_hook_config.py:L41, L65-L70). Boolean values are explicitly rejected even though `true` would otherwise compare equal to `1` — the timeout must be a real integer, not a boolean (tests/test_claude_plugin_hook_config.py:L66-L70).

The rationale recorded for these bounds (not itself asserted, but documents the contract intent): `Stop` is fire-and-forget for the mine subprocess yet synchronously saves the diary touching the storage backend, so `10..30s` is generous without allowing runaway hangs; `PreCompact` runs a synchronous mine with an inner per-target subprocess timeout of 60s, so the hook-level floor of 60 prevents truncating that inner bound and the ceiling of 90 caps the worst case (tests/test_claude_plugin_hook_config.py:L11-L21).

## Test cases

### `test_plugin_hook_timeout_within_bounds`
Parameterized once per event name, in sorted order of the bounds-mapping keys (tests/test_claude_plugin_hook_config.py:L33-L34). For each event it asserts: the event key is present under `"hooks"`; its entry list is a non-empty list of length exactly 1; the single entry's `"hooks"` array is non-empty and of length exactly 1; the hook's `"type"` is `"command"`; a `"timeout"` key exists; and the timeout is a non-boolean integer within `[floor, ceiling]` for that event (tests/test_claude_plugin_hook_config.py:L34-L70). Failure messages identify the offending event and the violated condition (tests/test_claude_plugin_hook_config.py:L42-L70).

### `test_no_unbounded_events_in_plugin_config`
Asserts that the set of event keys declared under `"hooks"` in the config is a subset of the events that have registered bounds in the in-test mapping; i.e. there must be no declared event lacking a `(floor, ceiling)` bounds entry (tests/test_claude_plugin_hook_config.py:L73-L88). Any extra declared event causes failure, with a message listing the unbounded event names in sorted order and instructing that a bounds entry be added (tests/test_claude_plugin_hook_config.py:L83-L88).

## Invariants / ordering guarantees
- Exactly-one cardinality is enforced at both the entry level and the hook-command level per event (tests/test_claude_plugin_hook_config.py:L48-L59).
- Event parameterization is deterministic via sorted key order (tests/test_claude_plugin_hook_config.py:L33).
- The set of config events and the set of bounded events must match such that no config event is unbounded (tests/test_claude_plugin_hook_config.py:L81-L84).

## Side effects
Read-only: the suite reads `<repo-root>/.claude-plugin/hooks/hooks.json` from the filesystem (module-scoped, read once per module) and performs no writes, network, or process actions (tests/test_claude_plugin_hook_config.py:L28-L30).

## Error / edge-case behavior
- Missing `"hooks"` key or missing event key fails with a "missing event" message (tests/test_claude_plugin_hook_config.py:L42).
- Empty or non-list entry/hook arrays fail (tests/test_claude_plugin_hook_config.py:L44, L54).
- Wrong hook `"type"` fails (tests/test_claude_plugin_hook_config.py:L61-L63).
- Absent `"timeout"` fails (tests/test_claude_plugin_hook_config.py:L64).
- Boolean timeout, non-integer timeout, or integer timeout outside the inclusive range fails (tests/test_claude_plugin_hook_config.py:L66-L70).
