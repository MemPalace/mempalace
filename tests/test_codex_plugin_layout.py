"""Contract tests for the published Codex plugin snapshot layout.

Codex resolves manifest component paths from the installed plugin root, which
is the directory containing ``.codex-plugin/``. These tests intentionally use
the repository root as that snapshot root so a locally flattened marketplace
cannot hide packaging regressions.
"""

import json
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / ".codex-plugin" / "plugin.json"
SUPPORTED_EVENTS = {
    "SessionStart": "session-start",
    "Stop": "stop",
    "PreCompact": "precompact",
}
SUPPORTED_ROOT_VARIABLES = ("${PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_plugin_reference(reference: object, field: str) -> Path:
    assert isinstance(reference, str) and reference.startswith("./"), (
        f"{field} must be a ./-prefixed path relative to the installed plugin root; "
        f"got {reference!r}"
    )

    plugin_root = REPO_ROOT.resolve()
    resolved = (plugin_root / reference.removeprefix("./")).resolve()
    assert resolved.is_relative_to(plugin_root), (
        f"{field} escapes the installed plugin root: {reference!r}"
    )
    return resolved


def _require_plugin_component(reference: object, field: str, expected_kind: str) -> Path:
    resolved = _resolve_plugin_reference(reference, field)

    if expected_kind == "directory":
        assert resolved.is_dir(), f"{field} resolves to missing directory {resolved}"
    else:
        assert resolved.is_file(), f"{field} resolves to missing file {resolved}"
    return resolved


@pytest.fixture(scope="module")
def manifest() -> dict:
    return _load_json(MANIFEST_PATH)


@pytest.fixture(scope="module")
def hook_config(manifest: dict) -> dict:
    hook_path = _resolve_plugin_reference(manifest.get("hooks"), "hooks")
    assert hook_path.is_file(), (
        f"hooks resolves to missing file {hook_path}; "
        "the repository-layout marketplace snapshot cannot load it"
    )
    return _load_json(hook_path)


def _event_command(hook_config: dict, event: str) -> str:
    entries = hook_config.get("hooks", {}).get(event)
    assert isinstance(entries, list) and len(entries) == 1, (
        f"{event} must declare exactly one hook entry"
    )
    commands = [
        hook.get("command") for hook in entries[0].get("hooks", []) if hook.get("type") == "command"
    ]
    assert len(commands) == 1 and isinstance(commands[0], str), (
        f"{event} must declare exactly one command hook"
    )
    return commands[0]


@pytest.mark.parametrize(
    ("field", "expected_kind"),
    [
        ("skills", "directory"),
        ("hooks", "file"),
    ],
)
def test_manifest_components_resolve_from_plugin_root(
    manifest: dict, field: str, expected_kind: str
) -> None:
    """Every declared local component must exist in the raw repository snapshot."""
    _require_plugin_component(manifest.get(field), field, expected_kind)


@pytest.mark.parametrize(
    ("reference", "expected_kind"),
    [
        ("./missing-hooks.json", "file"),
        ("./missing-skills", "directory"),
    ],
)
def test_component_validation_rejects_missing_paths(reference: str, expected_kind: str) -> None:
    """The regression guard reports absent files and directories."""
    with pytest.raises(AssertionError, match="resolves to missing"):
        _require_plugin_component(reference, "fixture", expected_kind)


def test_component_validation_rejects_out_of_root_reference() -> None:
    """The regression guard rejects traversal outside the installed snapshot."""
    with pytest.raises(AssertionError, match="escapes the installed plugin root"):
        _require_plugin_component("./../outside", "fixture", "file")


def test_hook_commands_use_documented_installed_root(
    hook_config: dict,
) -> None:
    """Every lifecycle command must resolve one packaged runner from the plugin root."""
    resolved_runners = set()

    for event, expected_hook_name in SUPPORTED_EVENTS.items():
        command = _event_command(hook_config, event)
        used_root_variables = [
            variable for variable in SUPPORTED_ROOT_VARIABLES if variable in command
        ]
        assert len(used_root_variables) == 1, (
            f"{event} must use exactly one documented installed-root variable; got {command!r}"
        )
        assert "${CODEX_PLUGIN_ROOT}" not in command, f"{event} uses undocumented CODEX_PLUGIN_ROOT"

        root_variable = used_root_variables[0]
        argv = shlex.split(command.replace(root_variable, str(REPO_ROOT.resolve())))
        assert len(argv) == 2, f"{event} command must contain runner and hook name: {command!r}"
        assert argv[1] == expected_hook_name

        runner = Path(argv[0]).resolve()
        try:
            runner.relative_to(REPO_ROOT.resolve())
        except ValueError:
            pytest.fail(f"{event} runner escapes the installed plugin root: {runner}")
        assert runner.is_file(), f"{event} runner is missing from the plugin snapshot: {runner}"
        resolved_runners.add(runner)

    assert len(resolved_runners) == 1, (
        f"all Codex lifecycle events must use one packaged runner; got {sorted(resolved_runners)}"
    )


def test_hook_command_preserves_plugin_root_with_spaces(hook_config: dict) -> None:
    """Shell parsing must keep an installed root containing spaces intact."""
    fake_root = Path("/tmp/mempalace plugin root")

    for event in SUPPORTED_EVENTS:
        command = _event_command(hook_config, event)
        root_variable = next(
            (variable for variable in SUPPORTED_ROOT_VARIABLES if variable in command),
            None,
        )
        assert root_variable is not None
        argv = shlex.split(command.replace(root_variable, str(fake_root)))
        assert len(argv) == 2
        assert Path(argv[0]).is_relative_to(fake_root)


def test_hook_commands_execute_from_installed_root_with_spaces(
    hook_config: dict, tmp_path: Path
) -> None:
    """Lifecycle commands execute the packaged runner from a nontrivial install path."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash required for Codex plugin hook execution test")

    plugin_root = tmp_path / "installed MemPalace 插件"
    runner = plugin_root / ".codex-plugin" / "hooks" / "mempal-hook.sh"
    runner.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / ".codex-plugin" / "hooks" / "mempal-hook.sh", runner)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_mempalace = fake_bin / "mempalace"
    fake_mempalace.write_text(
        "#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    fake_mempalace.chmod(0o755)

    env = os.environ.copy()
    env["PLUGIN_ROOT"] = str(plugin_root)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

    for event, expected_hook_name in SUPPORTED_EVENTS.items():
        result = subprocess.run(
            [bash, "-c", _event_command(hook_config, event)],
            input='{"session_id":"isolated"}',
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env=env,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == f"hook run --hook {expected_hook_name} --harness codex\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode is not portable to Windows")
def test_packaged_hook_runner_is_executable(hook_config: dict) -> None:
    """Directly invoked plugin hook runners must retain an executable mode."""
    command = _event_command(hook_config, "Stop")
    root_variable = next(variable for variable in SUPPORTED_ROOT_VARIABLES if variable in command)
    runner = Path(shlex.split(command.replace(root_variable, str(REPO_ROOT.resolve())))[0])

    mode = runner.stat().st_mode
    assert mode & stat.S_IXUSR, f"plugin hook runner is not executable: {runner}"
