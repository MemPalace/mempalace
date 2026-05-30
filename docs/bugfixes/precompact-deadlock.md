# PreCompact Hook Deadlock

**Status:** Fixed in [#863](https://github.com/MemPalace/mempalace/pull/863) on `develop`
**Affects:** All MemPalace versions through v3.3.0 when used with the Claude Code harness
**Related issues:** [#856](https://github.com/MemPalace/mempalace/issues/856), [#858](https://github.com/MemPalace/mempalace/issues/858), [#872](https://github.com/MemPalace/mempalace/issues/872)
**Symptom:** Session appears frozen near the context limit. `/compact` has no effect. `~/.mempalace/hook_state/hook.log` shows repeated `PRE-COMPACT triggered for session …` entries, many per minute, with no intervening `Stop` hook activity.

## TL;DR

`hook_precompact()` in `mempalace/hooks_cli.py` unconditionally returned `{"decision": "block", "reason": PRECOMPACT_BLOCK_REASON}`. In the Claude Code harness, `decision: block` on a PreCompact hook **cancels the compaction** and feeds the `reason` string back to the model as an instruction. The model then tries to save memory, the response ends, and Claude Code notices the context is still over the limit — so it fires PreCompact again. The hook blocks again. The loop never terminates on its own, and manual `/compact` was also blocked because the hook ignored the `trigger` field.

The fix (PR #863) removes the block entirely. `hook_precompact()` now mines the transcript synchronously (so memory lands before compaction proceeds) and returns `{}` — the documented no-block pass-through in Claude Code. No state files, no trigger-field special-casing, no re-fire cycle. This also aligns `hooks_cli.py` with the standalone bash hooks under `hooks/`, which had already switched to "allow + background mine" a while back, and with the CLAUDE.md principle of "background everything — zero tokens spent on bookkeeping in the chat window."

## How the deadlock was observed

One affected session in the wild (`~/.mempalace/hook_state/hook.log`):

```
[15:27:02] Session 082d4cc3-…: 173 exchanges, 14 since last save
[16:13:45] PRE-COMPACT triggered for session 082d4cc3-…
[16:16:31] PRE-COMPACT triggered for session 082d4cc3-…
[16:18:56] PRE-COMPACT triggered for session 082d4cc3-…
[16:21:29] PRE-COMPACT triggered for session 082d4cc3-…
[16:22:05] PRE-COMPACT triggered for session 082d4cc3-…
[16:23:57] PRE-COMPACT triggered for session 082d4cc3-…
[16:28:05] PRE-COMPACT triggered for session 082d4cc3-…
```

Eight PreCompact fires in 15 minutes. Zero `Stop` hook entries in between, because the session never got control back to a clean "response done" state — every time the model finished writing memory, Claude Code immediately re-attempted compaction, which immediately re-fired the hook, which immediately re-blocked.

Invoking `/compact` manually did not help: Claude Code sends the same `PreCompact` event for manual compactions (just with `trigger: "manual"` in the payload), and the old code didn't read that field.

## Root cause in the old code

```python
# mempalace/hooks_cli.py  (before the fix)
def hook_precompact(data: dict, harness: str):
    """Precompact hook: always block with comprehensive save instruction."""
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]

    _log(f"PRE-COMPACT triggered for session {session_id}")

    # ... optional auto-ingest ...

    # Always block -- compaction = save everything
    _output({"decision": "block", "reason": PRECOMPACT_BLOCK_REASON})
```

Compare to the Stop hook, which already had a loop guard (`stop_hook_active`, read from the harness payload) so that a save cycle triggered by a previous block would pass through instead of re-blocking. The PreCompact event has no equivalent `precompact_hook_active` flag, so *any* fix that kept the block would need to maintain its own cross-invocation state. #863 sidesteps this entirely by removing the block.

### An important aside: `"allow"` is not a valid decision value

One earlier iteration (see [#872](https://github.com/MemPalace/mempalace/issues/872)) returned `{"decision": "allow"}` to mean "don't block." That **happens to work, but by accident** — `"block"` is the only top-level `decision` value Claude Code recognizes on this hook. Anything else (`"allow"`, `"pass"`, an unknown string, a missing key) is treated as a no-op pass-through. The documented way to not block is to return `{}`. The same correction applies to any older bash hooks that may still be using `"allow"`.

## The fix (#863)

```python
def hook_precompact(data: dict, harness: str):
    """Precompact hook: mine transcript synchronously, then allow compaction."""
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    transcript_path = parsed["transcript_path"]

    _log(f"PRE-COMPACT triggered for session {session_id}")

    # Mine synchronously so data lands before compaction proceeds
    _mine_sync(transcript_path)

    _output({})
```

With supporting helpers that also enable mining when `MEMPAL_DIR` isn't set:

```python
def _get_mine_dir(transcript_path: str = "") -> str:
    """Determine directory to mine from MEMPAL_DIR or transcript path."""
    mempal_dir = os.environ.get("MEMPAL_DIR", "")
    if mempal_dir and os.path.isdir(mempal_dir):
        return mempal_dir
    if transcript_path:
        path = Path(transcript_path).expanduser()
        if path.is_file():
            return str(path.parent)
    return ""


def _mine_sync(transcript_path: str = ""):
    """Run mempalace mine synchronously (for precompact -- data must land first)."""
    mine_dir = _get_mine_dir(transcript_path)
    if not mine_dir:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_DIR / "hook.log"
        with open(log_path, "a") as log_f:
            subprocess.run(
                [sys.executable, "-m", "mempalace", "mine", mine_dir],
                stdout=log_f,
                stderr=log_f,
                timeout=60,
            )
    except (OSError, subprocess.TimeoutExpired):
        pass
```

### Behavior after the fix

| Scenario                                                  | Before             | After                |
| --------------------------------------------------------- | ------------------ | -------------------- |
| User runs `/compact` (`trigger="manual"`)                 | blocked → deadlock | passes through (`{}`) |
| Auto PreCompact at context threshold                      | blocked → loop     | passes through, mines synchronously first |
| Re-fire of PreCompact seconds later                       | blocks again → loop | already mined, passes through again |
| `MEMPAL_DIR` unset                                        | no mining at all   | mines from transcript parent dir |

## Escape hatch for a frozen session (pre-fix)

If you are currently stuck in the loop on an unpatched MemPalace, the only reliable workaround is to exit the Claude Code session and start a new one with `claude --continue`. No amount of `/compact` will break the loop until `hooks_cli.py` is replaced — manual and auto compactions fire the same hook and both get blocked.

If you had a prior deadlock-guard variant installed (some downstream patches wrote `~/.mempalace/hook_state/{session_id}_precompact_blocked_at` sentinel files as a workaround), those files are harmless but no longer needed and can be removed:

```bash
rm -f ~/.mempalace/hook_state/*_precompact_blocked_at
```

## Verifying you have the fix

```bash
# Should NOT contain `"decision": "block"` in hook_precompact
grep -A 20 'def hook_precompact' "$(python3 -c 'import mempalace.hooks_cli as m; print(m.__file__)')" \
  | grep -q '"decision": "block"' && echo "NOT patched" || echo "patched"
```

Or run the smoke test directly:

```bash
echo '{"session_id":"t","transcript_path":"/nonexistent","trigger":"auto"}' \
  | python3 -m mempalace hook run --hook precompact --harness claude-code
# Expect: {}

echo '{"session_id":"t","transcript_path":"/nonexistent","trigger":"manual"}' \
  | python3 -m mempalace hook run --hook precompact --harness claude-code
# Expect: {}
```

## Tests

Unit coverage in `tests/test_hooks_cli.py` (from #863):

- `test_precompact_allows` — precompact returns `{}`, not a block.
- `test_get_mine_dir_*` — coverage for the MEMPAL_DIR → transcript-parent fallback.
- `test_mine_sync_*` — synchronous mining is invoked on precompact.

## Why not keep the PreCompact block and just guard against the loop?

An earlier proposal ([#867](https://github.com/MemPalace/mempalace/pull/867)) kept the block and added a stateful deadlock guard: a per-session sentinel file that tracked the human-message count at which the last block fired, plus a `trigger == "manual"` bypass. It works, but it layers a workaround on top of a premise that doesn't hold:

1. The Stop hook already runs the same save logic every `SAVE_INTERVAL` human messages. By the time PreCompact fires, the session is typically already caught up to within a few turns of its last save.
2. Blocking PreCompact burns a whole model turn on save instructions that the Stop hook would have issued anyway on the next response boundary.
3. CLAUDE.md explicitly calls for "background everything — zero tokens spent on bookkeeping in the chat window."
4. The standalone bash hooks in `hooks/` had already moved to "allow + background mine" some time ago; `hooks_cli.py` was drift.

Removing the block resolves all four points at once and is strictly simpler: no state files, no trigger-field special-casing, no per-session counter, no subtle re-arm logic. The synchronous mine in #863 preserves the "memory lands before compaction" guarantee that the block was reaching for, without any of the failure modes.
