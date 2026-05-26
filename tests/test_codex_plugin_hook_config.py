"""Schema tests for Codex plugin hook config."""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_CONFIG = REPO_ROOT / ".codex-plugin" / "hooks.json"

# Per-event timeout bounds (seconds): (floor, ceiling).
# - SessionStart is lightweight and should complete quickly.
# - Stop may do synchronous diary save before detached mine spawn.
# - PreCompact runs synchronous mining and needs a longer bound.
EVENT_TIMEOUT_BOUNDS: dict[str, tuple[int, int]] = {
    "SessionStart": (5, 30),
    "Stop": (10, 30),
    "PreCompact": (60, 90),
}


@pytest.fixture(scope="module")
def hook_config() -> dict:
    return json.loads(HOOK_CONFIG.read_text(encoding="utf-8"))


@pytest.mark.parametrize("event", sorted(EVENT_TIMEOUT_BOUNDS))
def test_codex_plugin_hook_timeout_within_bounds(hook_config: dict, event: str) -> None:
    floor, ceiling = EVENT_TIMEOUT_BOUNDS[event]
    assert event in hook_config.get("hooks", {}), f"missing event {event!r} in hook config"
    entries = hook_config["hooks"][event]
    assert isinstance(entries, list) and entries, f"no entries declared for {event}"
    assert len(entries) == 1, (
        f"{event} expected exactly one entry, found {len(entries)}; "
        "duplicate entries would double-fire the hook"
    )
    for entry in entries:
        sub_hooks = entry.get("hooks")
        assert isinstance(sub_hooks, list) and sub_hooks, (
            f"{event} entry missing non-empty 'hooks' array"
        )
        assert len(sub_hooks) == 1, (
            f"{event} entry expected exactly one hook command, found {len(sub_hooks)}"
        )
        for hook in sub_hooks:
            assert hook.get("type") == "command", (
                f"unexpected hook type for {event}: {hook.get('type')!r}"
            )
            assert "timeout" in hook, f"{event} hook missing 'timeout' key"
            timeout = hook["timeout"]
            is_real_int = isinstance(timeout, int) and not isinstance(timeout, bool)
            assert is_real_int and floor <= timeout <= ceiling, (
                f"{event} hook timeout must be an int in [{floor}, {ceiling}]s; got {timeout!r}"
            )


def test_codex_plugin_hook_command_uses_plugin_root_env(hook_config: dict) -> None:
    """Codex passes PLUGIN_ROOT/CLAUDE_PLUGIN_ROOT for plugin-bundled hooks."""
    for event, entries in hook_config.get("hooks", {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                if hook.get("type") != "command":
                    continue
                command = hook.get("command", "")
                has_plugin_root = "${PLUGIN_ROOT}" in command or "${CLAUDE_PLUGIN_ROOT}" in command
                assert has_plugin_root, (
                    f"{event} command should reference PLUGIN_ROOT or CLAUDE_PLUGIN_ROOT: {command!r}"
                )


def test_codex_plugin_hook_no_unbounded_events(hook_config: dict) -> None:
    declared_events = set(hook_config.get("hooks", {}).keys())
    bounded_events = set(EVENT_TIMEOUT_BOUNDS)
    unbounded = declared_events - bounded_events
    assert not unbounded, (
        f"plugin hook events without timeout bounds: {sorted(unbounded)}. "
        "Add a (floor, ceiling) entry to EVENT_TIMEOUT_BOUNDS in this test "
        "after deciding the event's acceptable max runtime."
    )
