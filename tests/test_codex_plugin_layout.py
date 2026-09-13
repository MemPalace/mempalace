"""Contract tests for the repository-root Codex plugin snapshot.

Codex automatically discovers a plugin hook definition at ``hooks/hooks.json``.
These tests use the raw repository layout so a locally flattened marketplace
cannot hide a packaging regression.
"""

from __future__ import annotations

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
HOOK_CONFIG_PATH = REPO_ROOT / "hooks" / "hooks.json"
RUNNER_PATH = REPO_ROOT / "hooks" / "codex" / "mempal-hook.sh"
LEGACY_HOOK_CONFIG_PATH = REPO_ROOT / ".codex-plugin" / "hooks.json"
LEGACY_RUNNER_PATH = REPO_ROOT / ".codex-plugin" / "hooks" / "mempal-hook.sh"
PLUGIN_ROOT_VARIABLE = "${PLUGIN_ROOT}"
UNSUPPORTED_ROOT_VARIABLES = ("${CODEX_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}")
SUPPORTED_EVENTS = {
    "SessionStart": "session-start",
    "Stop": "stop",
    "PreCompact": "precompact",
}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_plugin_reference(reference: object, field: str, plugin_root: Path) -> Path:
    assert isinstance(reference, str) and reference.startswith("./"), (
        f"{field} must be a ./-prefixed path relative to the installed plugin root; "
        f"got {reference!r}"
    )

    resolved_root = plugin_root.resolve()
    resolved = (resolved_root / reference.removeprefix("./")).resolve()
    assert resolved.is_relative_to(resolved_root), (
        f"{field} escapes the installed plugin root: {reference!r}"
    )
    return resolved


def _require_plugin_component(
    reference: object, field: str, expected_kind: str, plugin_root: Path
) -> Path:
    resolved = _resolve_plugin_reference(reference, field, plugin_root)

    if expected_kind == "directory":
        assert resolved.is_dir(), f"{field} resolves to missing directory {resolved}"
    else:
        assert resolved.is_file(), f"{field} resolves to missing file {resolved}"
    return resolved


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


def _parse_event_command(
    command: str, event: str, expected_hook_name: str, plugin_root: Path
) -> tuple[Path, str]:
    assert command.count(PLUGIN_ROOT_VARIABLE) == 1, (
        f"{event} must use exactly one {PLUGIN_ROOT_VARIABLE}; got {command!r}"
    )
    for unsupported_variable in UNSUPPORTED_ROOT_VARIABLES:
        assert unsupported_variable not in command, (
            f"{event} uses unsupported root variable {unsupported_variable}"
        )

    argv = shlex.split(command.replace(PLUGIN_ROOT_VARIABLE, str(plugin_root.resolve())))
    assert len(argv) == 2, f"{event} command must contain runner and hook name: {command!r}"
    assert argv[1] == expected_hook_name, (
        f"{event} must pass {expected_hook_name!r}; got {argv[1]!r}"
    )

    resolved_root = plugin_root.resolve()
    runner = Path(argv[0]).resolve()
    assert runner.is_relative_to(resolved_root), f"{event} runner escapes the plugin root: {runner}"
    return runner, argv[1]


@pytest.fixture(scope="module")
def manifest() -> dict:
    return _load_json(MANIFEST_PATH)


@pytest.fixture(scope="module")
def hook_config(manifest: dict) -> dict:
    assert "hooks" not in manifest, "default discovery must not use a manifest hooks override"
    assert HOOK_CONFIG_PATH.is_file(), (
        f"missing default Codex hook definition at {HOOK_CONFIG_PATH}"
    )
    return _load_json(HOOK_CONFIG_PATH)


@pytest.mark.parametrize(
    ("reference", "expected_kind"),
    [
        ("./skills/", "directory"),
        ("./.mcp.json", "file"),
    ],
)
def test_manifest_components_resolve_from_plugin_root(
    manifest: dict, reference: str, expected_kind: str
) -> None:
    """Declared root-level components must exist in the raw repository snapshot."""
    field = "skills" if expected_kind == "directory" else "mcpServers"
    assert manifest[field] == reference
    _require_plugin_component(reference, field, expected_kind, REPO_ROOT)


def test_default_hook_definition_uses_canonical_layout(manifest: dict) -> None:
    assert "hooks" not in manifest
    assert HOOK_CONFIG_PATH.is_file(), f"missing default hook definition: {HOOK_CONFIG_PATH}"
    assert not LEGACY_HOOK_CONFIG_PATH.exists(), (
        f"legacy hook definition remains: {LEGACY_HOOK_CONFIG_PATH}"
    )
    assert not LEGACY_RUNNER_PATH.exists(), f"legacy runner remains: {LEGACY_RUNNER_PATH}"


@pytest.mark.parametrize(
    ("reference", "expected_kind"),
    [
        ("./missing-hooks.json", "file"),
        ("./missing-skills", "directory"),
    ],
)
def test_component_validation_rejects_missing_paths(reference: str, expected_kind: str) -> None:
    with pytest.raises(AssertionError, match="resolves to missing"):
        _require_plugin_component(reference, "fixture", expected_kind, REPO_ROOT)


def test_component_validation_rejects_out_of_root_reference() -> None:
    with pytest.raises(AssertionError, match="escapes the installed plugin root"):
        _require_plugin_component("./../outside", "fixture", "file", REPO_ROOT)


@pytest.mark.parametrize(
    ("command", "match"),
    [
        ('"${CODEX_PLUGIN_ROOT}/hooks/codex/mempal-hook.sh" stop', "PLUGIN_ROOT"),
        ('"${CLAUDE_PLUGIN_ROOT}/hooks/codex/mempal-hook.sh" stop', "PLUGIN_ROOT"),
        ('"/tmp/mempal-hook.sh" stop', "PLUGIN_ROOT"),
        ('"${PLUGIN_ROOT}/../outside/mempal-hook.sh" stop', "escapes the plugin root"),
    ],
)
def test_hook_command_validation_rejects_invalid_roots(command: str, match: str) -> None:
    with pytest.raises(AssertionError, match=match):
        _parse_event_command(command, "Stop", "stop", REPO_ROOT)


def test_hook_command_validation_rejects_wrong_action() -> None:
    command = '"${PLUGIN_ROOT}/hooks/codex/mempal-hook.sh" unexpected'

    with pytest.raises(AssertionError, match="must pass 'stop'"):
        _parse_event_command(command, "Stop", "stop", REPO_ROOT)


def test_hook_commands_use_documented_installed_root(hook_config: dict) -> None:
    resolved_runners = set()

    for event, expected_hook_name in SUPPORTED_EVENTS.items():
        command = _event_command(hook_config, event)
        runner, action = _parse_event_command(command, event, expected_hook_name, REPO_ROOT)
        assert action == expected_hook_name
        assert runner.is_file(), f"{event} runner is missing from the plugin snapshot: {runner}"
        resolved_runners.add(runner)

    assert resolved_runners == {RUNNER_PATH.resolve()}


def test_hook_commands_preserve_plugin_root_with_spaces_and_unicode(hook_config: dict) -> None:
    fake_root = Path("/tmp/installed MemPalace 插件")

    for event, expected_hook_name in SUPPORTED_EVENTS.items():
        command = _event_command(hook_config, event)
        runner, action = _parse_event_command(command, event, expected_hook_name, fake_root)
        assert action == expected_hook_name
        assert runner == (fake_root / "hooks" / "codex" / "mempal-hook.sh").resolve()


def test_hook_commands_execute_from_installed_root_with_spaces_and_unicode(
    hook_config: dict, tmp_path: Path
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash required for Codex plugin hook execution test")

    plugin_root = tmp_path / "installed MemPalace 插件"
    installed_hook_config = plugin_root / "hooks" / "hooks.json"
    installed_runner = plugin_root / "hooks" / "codex" / "mempal-hook.sh"
    installed_hook_config.parent.mkdir(parents=True)
    installed_runner.parent.mkdir(parents=True)
    shutil.copy2(HOOK_CONFIG_PATH, installed_hook_config)
    shutil.copy2(RUNNER_PATH, installed_runner)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_mempalace = fake_bin / "mempalace"
    fake_mempalace.write_text(
        """#!/bin/sh
cat >/dev/null
printf '%s\\n' "$*"
""",
        encoding="utf-8",
    )
    fake_mempalace.chmod(0o755)

    env = os.environ.copy()
    env["PLUGIN_ROOT"] = str(plugin_root)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

    for event, expected_hook_name in SUPPORTED_EVENTS.items():
        command = _event_command(hook_config, event)
        result = subprocess.run(
            [bash, "-c", command],
            input='{"session_id":"isolated"}',
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env=env,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == f"hook run --hook {expected_hook_name} --harness codex\n"


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI required for marketplace test")
def test_codex_installs_canonical_snapshot_in_isolated_home(tmp_path: Path) -> None:
    codex = shutil.which("codex")
    assert codex is not None

    isolated_codex_home = tmp_path / "isolated Codex 插件"
    isolated_codex_home.mkdir()
    env = os.environ.copy()
    env["CODEX_HOME"] = str(isolated_codex_home)

    def run_codex(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [codex, *args],
            capture_output=True,
            check=False,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            timeout=30,
        )

    marketplace_result = run_codex("plugin", "marketplace", "add", str(REPO_ROOT), "--json")
    assert marketplace_result.returncode == 0, marketplace_result.stderr
    marketplace = json.loads(marketplace_result.stdout)
    assert marketplace["marketplaceName"] == "mempalace"
    assert Path(marketplace["installedRoot"]).resolve() == REPO_ROOT.resolve()

    available_result = run_codex("plugin", "list", "--available", "--json")
    assert available_result.returncode == 0, available_result.stderr
    available_plugins = json.loads(available_result.stdout)["available"]
    assert any(plugin["pluginId"] == "mempalace@mempalace" for plugin in available_plugins)

    install_result = run_codex("plugin", "add", "mempalace@mempalace", "--json")
    assert install_result.returncode == 0, install_result.stderr
    installed_root = Path(json.loads(install_result.stdout)["installedPath"]).resolve()
    assert installed_root.is_relative_to(isolated_codex_home.resolve())

    installed_manifest = _load_json(installed_root / ".codex-plugin" / "plugin.json")
    assert installed_manifest["mcpServers"] == "./.mcp.json"
    assert "hooks" not in installed_manifest
    assert (installed_root / ".mcp.json").is_file()
    assert (installed_root / "hooks" / "hooks.json").is_file()

    installed_runner = installed_root / "hooks" / "codex" / "mempal-hook.sh"
    assert installed_runner.is_file()
    assert installed_runner.stat().st_mode & stat.S_IXUSR

    isolated_config = (isolated_codex_home / "config.toml").read_text(encoding="utf-8")
    assert "trust" not in isolated_config.lower()
    assert not list(isolated_codex_home.rglob("*trust*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode is not portable to Windows")
def test_packaged_hook_runner_is_executable(hook_config: dict) -> None:
    mode = RUNNER_PATH.stat().st_mode
    assert mode & stat.S_IXUSR, f"plugin hook runner is not executable: {RUNNER_PATH}"
