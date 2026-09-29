"""Contract tests for ``hooks/cursor/install.sh``.

The installer's job is to merge MemPalace hook entries into a Cursor
``hooks.json`` file without:

- modifying unrelated hook entries already in the file,
- duplicating MemPalace entries when re-run,
- writing to disk when ``--dry-run`` is passed,
- leaving stale MemPalace entries behind on ``--uninstall``.

These four contracts are what protects a user's existing Cursor
configuration. Tests use ``--scope project --target <tmp_path>`` so
the test never touches the real user `~/.cursor/`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "hooks" / "cursor" / "install.sh"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="install.sh is POSIX-only")


# ── helpers ─────────────────────────────────────────────────────────


def _run_install(
    *args: str,
    target: Path,
    home: Path | None = None,
    expected_rc: int = 0,
) -> tuple[str, str]:
    """Invoke install.sh with --scope project --target <target>.

    Forces ``MEMPAL_PYTHON=sys.executable`` for the same reason the
    shell-hook tests do — ensures the JSON merge runs even if PATH on
    a GUI-launched CI runner is missing python3. Forces a clean HOME
    so the default --install-dir (under ~/.mempalace/hooks/cursor/)
    lands in a sandboxed tmp tree rather than the developer's real
    home.
    """
    env = {
        "HOME": str(home) if home else "/tmp/mempal-install-test-home",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "MEMPAL_PYTHON": sys.executable,
    }
    cmd = [
        "bash",
        str(INSTALL_SH),
        "--scope",
        "project",
        "--target",
        str(target),
        *args,
    ]
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert p.returncode == expected_rc, (
        f"install.sh exited {p.returncode} (expected {expected_rc}); "
        f"stderr={p.stderr!r}; stdout={p.stdout!r}; argv={cmd}"
    )
    return p.stdout, p.stderr


def _hooks_file(target: Path) -> Path:
    return target / ".cursor" / "hooks.json"


def _seed(target: Path, payload: dict) -> Path:
    cursor_dir = target / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    hf = cursor_dir / "hooks.json"
    hf.write_text(json.dumps(payload, indent=2))
    return hf


# ── --help and bash syntax ──────────────────────────────────────────


def test_bash_syntax_clean():
    p = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, f"install.sh syntax error: {p.stderr}"


def test_help_describes_all_flags():
    p = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        env={
            "HOME": "/tmp/mempal-help",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
        timeout=10,
    )
    assert p.returncode == 0
    for flag in ("--scope", "--target", "--variant", "--dry-run", "--uninstall"):
        assert flag in p.stdout, f"--help must describe {flag}"


def test_unknown_flag_exits_nonzero():
    p = subprocess.run(
        ["bash", str(INSTALL_SH), "--bogus-flag"],
        capture_output=True,
        text=True,
        env={
            "HOME": "/tmp/mempal-bogus",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
        timeout=10,
    )
    assert p.returncode != 0


# ── --dry-run contract ─────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_does_not_write_target_file(self, tmp_path):
        stdout, _ = _run_install("--dry-run", target=tmp_path, home=tmp_path)
        assert not _hooks_file(tmp_path).exists(), "--dry-run must not write the target file"
        # But it must still print valid JSON to stdout for the user to
        # review.
        parsed = json.loads(stdout)
        assert parsed["version"] == 1
        assert "hooks" in parsed

    def test_dry_run_does_not_copy_scripts(self, tmp_path):
        install_dir = tmp_path / "install-dest"
        _run_install(
            "--dry-run",
            "--install-dir",
            str(install_dir),
            target=tmp_path,
            home=tmp_path,
        )
        assert not install_dir.exists(), (
            "--dry-run must not copy any hook scripts to the install dir"
        )

    def test_dry_run_emits_full_variant_by_default(self, tmp_path):
        stdout, _ = _run_install("--dry-run", target=tmp_path, home=tmp_path)
        cfg = json.loads(stdout)
        assert set(cfg["hooks"].keys()) >= {"sessionStart", "stop", "preCompact"}

    def test_dry_run_minimal_variant_only_wires_stop(self, tmp_path):
        stdout, _ = _run_install(
            "--dry-run",
            "--variant",
            "minimal",
            target=tmp_path,
            home=tmp_path,
        )
        cfg = json.loads(stdout)
        # `stop` is the only event we touch. Anything else (including
        # sessionStart / preCompact) should not be present unless the
        # seed file already had it.
        assert "stop" in cfg["hooks"]
        assert "sessionStart" not in cfg["hooks"]
        assert "preCompact" not in cfg["hooks"]


# ── merge-preservation contract ────────────────────────────────────


class TestMergePreservation:
    def test_preserves_unrelated_hook_events(self, tmp_path):
        _seed(
            tmp_path,
            {
                "version": 1,
                "hooks": {
                    "afterFileEdit": [
                        {"command": "/usr/local/bin/my-formatter.sh"},
                    ],
                    "beforeShellExecution": [
                        {"command": "/usr/local/bin/audit-shell.sh"},
                    ],
                },
            },
        )
        _run_install(target=tmp_path, home=tmp_path)
        result = json.loads(_hooks_file(tmp_path).read_text())
        assert result["hooks"]["afterFileEdit"] == [
            {"command": "/usr/local/bin/my-formatter.sh"},
        ], "unrelated afterFileEdit entry must survive merge"
        assert result["hooks"]["beforeShellExecution"] == [
            {"command": "/usr/local/bin/audit-shell.sh"},
        ], "unrelated beforeShellExecution entry must survive merge"

    def test_preserves_other_entries_on_same_event(self, tmp_path):
        # User has their own `stop` hook. We must add MemPalace's
        # entry alongside, not replace.
        _seed(
            tmp_path,
            {
                "version": 1,
                "hooks": {
                    "stop": [
                        {"command": "/usr/local/bin/my-stop-hook.sh"},
                    ],
                },
            },
        )
        _run_install(target=tmp_path, home=tmp_path)
        result = json.loads(_hooks_file(tmp_path).read_text())
        stop_entries = result["hooks"]["stop"]
        assert len(stop_entries) == 2, (
            f"expected user entry + MemPalace entry; got {stop_entries!r}"
        )
        commands = {e["command"] for e in stop_entries}
        assert "/usr/local/bin/my-stop-hook.sh" in commands
        assert any("mempal_save_hook_cursor.sh" in c for c in commands)

    def test_creates_target_dir_when_missing(self, tmp_path):
        # No .cursor/ exists yet; install must create both the
        # directory and the file.
        assert not (tmp_path / ".cursor").exists()
        _run_install(target=tmp_path, home=tmp_path)
        assert _hooks_file(tmp_path).exists()
        cfg = json.loads(_hooks_file(tmp_path).read_text())
        assert "stop" in cfg["hooks"]

    def test_refuses_to_overwrite_malformed_existing_json(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir(parents=True)
        (cursor_dir / "hooks.json").write_text("{ this is not json")
        # The merge step should fail with a non-zero exit; the file
        # must remain untouched so the user can fix it.
        _, stderr = _run_install(target=tmp_path, home=tmp_path, expected_rc=2)
        assert "not valid JSON" in stderr or "Refusing to overwrite" in stderr
        # File should be unchanged.
        assert (cursor_dir / "hooks.json").read_text() == "{ this is not json"


# ── idempotency contract ───────────────────────────────────────────


class TestIdempotency:
    def test_running_install_twice_does_not_duplicate(self, tmp_path):
        _run_install(target=tmp_path, home=tmp_path)
        first = json.loads(_hooks_file(tmp_path).read_text())
        _run_install(target=tmp_path, home=tmp_path)
        second = json.loads(_hooks_file(tmp_path).read_text())
        assert first == second, (
            "re-running install.sh must produce an identical config "
            f"(idempotency); first={first!r} second={second!r}"
        )
        # And specifically no duplicate MemPalace entry on `stop`.
        stop = second["hooks"]["stop"]
        mempal_entries = [e for e in stop if "mempal_save_hook_cursor.sh" in e["command"]]
        assert len(mempal_entries) == 1, (
            f"re-running install must not duplicate the stop entry; got {stop!r}"
        )


# ── --uninstall contract ───────────────────────────────────────────


class TestUninstall:
    def test_uninstall_removes_only_mempalace_entries(self, tmp_path):
        # Seed: user has their own stop hook AND an unrelated event.
        _seed(
            tmp_path,
            {
                "version": 1,
                "hooks": {
                    "stop": [
                        {"command": "/usr/local/bin/my-stop-hook.sh"},
                    ],
                    "afterFileEdit": [
                        {"command": "/usr/local/bin/my-formatter.sh"},
                    ],
                },
            },
        )
        _run_install(target=tmp_path, home=tmp_path)
        # MemPalace is now wired alongside the user's entries.
        _run_install("--uninstall", target=tmp_path, home=tmp_path)
        cfg = json.loads(_hooks_file(tmp_path).read_text())
        # User's stop hook must remain; MemPalace's must be gone.
        commands = {e["command"] for e in cfg["hooks"].get("stop", [])}
        assert commands == {"/usr/local/bin/my-stop-hook.sh"}
        # Unrelated event untouched.
        assert cfg["hooks"]["afterFileEdit"] == [
            {"command": "/usr/local/bin/my-formatter.sh"},
        ]
        # sessionStart / preCompact (which were ONLY ever wired by us)
        # must be removed entirely since they would otherwise dangle
        # as empty lists.
        assert "sessionStart" not in cfg["hooks"]
        assert "preCompact" not in cfg["hooks"]

    def test_uninstall_removes_empty_file_when_no_user_hooks_remain(self, tmp_path):
        # No pre-existing hooks; install then uninstall should leave
        # an effectively-empty config -> file removed entirely.
        _run_install(target=tmp_path, home=tmp_path)
        assert _hooks_file(tmp_path).exists()
        _run_install("--uninstall", target=tmp_path, home=tmp_path)
        assert not _hooks_file(tmp_path).exists(), (
            "fully-empty hooks.json after uninstall must be removed, "
            'not left as `{"version": 1, "hooks": {}}`'
        )

    def test_uninstall_is_safe_when_file_missing(self, tmp_path):
        # User never installed; running uninstall must not crash.
        assert not _hooks_file(tmp_path).exists()
        _run_install("--uninstall", target=tmp_path, home=tmp_path)
        # File should still not exist (and definitely should not have
        # been created with an empty config).
        assert not _hooks_file(tmp_path).exists()

    def test_uninstall_dry_run_does_not_modify_file(self, tmp_path):
        _seed(
            tmp_path,
            {
                "version": 1,
                "hooks": {
                    "stop": [
                        {"command": "/usr/local/bin/my-stop-hook.sh"},
                        {
                            "command": (
                                "/Users/anon/.mempalace/hooks/cursor/mempal_save_hook_cursor.sh"
                            ),
                            "loop_limit": 1,
                        },
                    ],
                },
            },
        )
        before = _hooks_file(tmp_path).read_text()
        _run_install("--uninstall", "--dry-run", target=tmp_path, home=tmp_path)
        after = _hooks_file(tmp_path).read_text()
        assert before == after, "--uninstall --dry-run must not mutate the target file"


# ── scope handling ──────────────────────────────────────────────────


def test_project_scope_writes_to_project_dir(tmp_path):
    # Sanity check that --scope project + --target lands the file at
    # <target>/.cursor/hooks.json and nowhere else.
    home = tmp_path / "fake-home"
    home.mkdir()
    project = tmp_path / "fake-project"
    project.mkdir()
    _run_install(target=project, home=home)
    assert (project / ".cursor" / "hooks.json").exists()
    assert not (home / ".cursor" / "hooks.json").exists(), (
        "--scope project must not write into HOME"
    )


def test_invalid_scope_rejected(tmp_path):
    p = subprocess.run(
        ["bash", str(INSTALL_SH), "--scope", "bogus"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
        timeout=10,
    )
    assert p.returncode != 0
    assert "scope" in p.stderr.lower()


def test_invalid_variant_rejected(tmp_path):
    p = subprocess.run(
        ["bash", str(INSTALL_SH), "--variant", "bogus"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
        timeout=10,
    )
    assert p.returncode != 0
    assert "variant" in p.stderr.lower()


# ── --install-dir path resolution ───────────────────────────────────


class TestInstallDirAbsolutePath:
    """Regression for gh-PR review: a relative ``--install-dir`` must
    be resolved to an absolute path BEFORE it is written into
    ``hooks.json``. Cursor invokes hook commands from its own working
    directory (typically the project root), so a relative command path
    would silently fail to launch the hook.
    """

    def test_relative_install_dir_is_absolutized_in_hooks_json(self, tmp_path):
        # Run install from a known cwd with a relative --install-dir.
        # The resulting hooks.json must reference an absolute path.
        cwd = tmp_path / "run-from-here"
        cwd.mkdir()
        relative_install_dir = "rel-install"
        # NOTE: we deliberately do NOT pre-create the directory — the
        # installer itself creates it. The test asserts on the path
        # baked into hooks.json, not on filesystem state.
        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "MEMPAL_PYTHON": sys.executable,
        }
        p = subprocess.run(
            [
                "bash",
                str(INSTALL_SH),
                "--scope",
                "project",
                "--target",
                str(tmp_path),
                "--install-dir",
                relative_install_dir,
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd),
            timeout=30,
        )
        assert p.returncode == 0, f"install failed: {p.stderr!r}"
        cfg = json.loads(_hooks_file(tmp_path).read_text())
        expected_abs = str(cwd / relative_install_dir)
        stop_cmd = cfg["hooks"]["stop"][0]["command"]
        assert stop_cmd.startswith("/"), (
            f"hook command must be absolute path, not relative; got {stop_cmd!r}"
        )
        assert stop_cmd.startswith(expected_abs), (
            f"hook command must be resolved against cwd={cwd!s}; got {stop_cmd!r}"
        )

    def test_absolute_install_dir_is_preserved_verbatim(self, tmp_path):
        """The relative-to-absolute resolution must not mangle paths
        that were already absolute."""
        abs_install_dir = tmp_path / "abs-install"
        _run_install(
            "--install-dir",
            str(abs_install_dir),
            target=tmp_path,
            home=tmp_path,
        )
        cfg = json.loads(_hooks_file(tmp_path).read_text())
        stop_cmd = cfg["hooks"]["stop"][0]["command"]
        assert stop_cmd.startswith(str(abs_install_dir)), (
            f"absolute --install-dir must be preserved verbatim; "
            f"got {stop_cmd!r} for input {abs_install_dir!s}"
        )
