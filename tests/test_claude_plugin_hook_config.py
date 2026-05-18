"""Schema tests for Claude plugin hook config: timeout must be bounded."""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_CONFIG = REPO_ROOT / ".claude-plugin" / "hooks" / "hooks.json"

# Per-event hook-level timeout bounds (seconds): (floor, ceiling).
#
# Stop is fire-and-forget for the mine subprocess (_spawn_mine returns
# after a detached Popen), but the handler also calls _save_diary_direct
# synchronously, which touches chromadb. 10..30s is generous for that
# work without leaving room for runaway hangs to freeze the session.
#
# PreCompact runs _mine_sync synchronously with a per-target subprocess
# timeout of 60s in mempalace/hooks_cli.py. The hook-level floor of 60
# keeps the inner bound from being truncated, and the ceiling of 90
# bounds the worst case at ~30s above that.
EVENT_TIMEOUT_BOUNDS: dict[str, tuple[int, int]] = {
    "Stop": (10, 30),
    "PreCompact": (60, 90),
}


@pytest.fixture(scope="module")
def hook_config() -> dict:
    return json.loads(HOOK_CONFIG.read_text(encoding="utf-8"))


@pytest.mark.parametrize("event", sorted(EVENT_TIMEOUT_BOUNDS))
def test_plugin_hook_timeout_within_bounds(hook_config: dict, event: str) -> None:
    """Each declared plugin hook must declare a positive bounded timeout (#1465).

    Without ``timeout``, Claude Code falls back to the 600s command default
    and a hung ``mempalace hook run`` freezes the interactive session for
    up to ten minutes before being canceled.
    """
    floor, ceiling = EVENT_TIMEOUT_BOUNDS[event]
    assert event in hook_config.get("hooks", {}), f"missing event {event!r} in hook config"
    entries = hook_config["hooks"][event]
    assert isinstance(entries, list) and entries, f"no entries declared for {event}"
    # Pin cardinality: the plugin config intentionally declares exactly one
    # command per event. A duplicate entry would silently double-fire the
    # hook and pass per-hook bounds, so cardinality drift must fail loudly.
    assert len(entries) == 1, (
        f"{event} expected exactly one entry, found {len(entries)}; "
        "duplicate entries would double-fire the hook"
    )
    for entry in entries:
        sub_hooks = entry.get("hooks")
        assert (
            isinstance(sub_hooks, list) and sub_hooks
        ), f"{event} entry missing non-empty 'hooks' array"
        assert (
            len(sub_hooks) == 1
        ), f"{event} entry expected exactly one hook command, found {len(sub_hooks)}"
        for hook in sub_hooks:
            assert (
                hook.get("type") == "command"
            ), f"unexpected hook type for {event}: {hook.get('type')!r}"
            assert "timeout" in hook, f"{event} hook missing 'timeout' key"
            timeout = hook["timeout"]
            # bool subclasses int, so reject it explicitly: True == 1 must fail.
            is_real_int = isinstance(timeout, int) and not isinstance(timeout, bool)
            assert (
                is_real_int and floor <= timeout <= ceiling
            ), f"{event} hook timeout must be an int in [{floor}, {ceiling}]s; got {timeout!r}"


def test_no_unbounded_events_in_plugin_config(hook_config: dict) -> None:
    """No plugin hook event may ship without an explicit bounds entry.

    Adding a new event (SessionStart, PreToolUse, etc.) to
    ``.claude-plugin/hooks/hooks.json`` without registering bounds in
    ``EVENT_TIMEOUT_BOUNDS`` would silently fall back to the 600s
    Claude Code command default and re-introduce the regression.
    """
    declared_events = set(hook_config.get("hooks", {}).keys())
    bounded_events = set(EVENT_TIMEOUT_BOUNDS)
    unbounded = declared_events - bounded_events
    assert not unbounded, (
        f"plugin hook events without timeout bounds: {sorted(unbounded)}. "
        "Add a (floor, ceiling) entry to EVENT_TIMEOUT_BOUNDS in this test "
        "after deciding the worst-case freeze the event can tolerate."
    )
