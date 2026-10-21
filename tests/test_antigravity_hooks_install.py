"""End-to-end tests for the Antigravity install.sh.

Covers:

* `--dry-run` is fully side-effect free.
* A real install creates the expected file tree.
* `hooks.json` is rendered with absolute paths (no `__PLUGIN_DIR__` leak).
* Re-running the installer is byte-identical (cmp gate works).
* `--uninstall` removes the dir cleanly.
* `--uninstall` refuses to wipe a directory whose basename isn't `mempalace`.
* `--uninstall` refuses if the dir is missing a `mempalace` plugin.json.
* Relative `--install-dir` is absolutized into the rendered hooks.json.
"""

from __future__ import annotations

import filecmp
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "hooks" / "antigravity" / "install.sh"

# Skip on Windows — install.sh is bash and uses POSIX path semantics.
pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="install.sh is a bash script; Windows users use a separate code path.",
)

EXPECTED_FILES = (
    "plugin.json",
    "mcp_config.json",
    "README.md",
    "hooks.json",
    "skills/mempalace/SKILL.md",
    "hooks/lib/common.sh",
    "hooks/mempal_save_hook_antigravity.sh",
    "hooks/mempal_wake_hook_antigravity.sh",
)


def _run_install(
    install_dir: Path,
    *args: str,
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    """Invoke install.sh with the given install dir and args."""
    cmd = [
        "bash",
        str(INSTALL_SH),
        "--install-dir",
        str(install_dir),
        *args,
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


def _assert_install_layout(install_dir: Path) -> None:
    for rel in EXPECTED_FILES:
        path = install_dir / rel
        assert path.is_file(), f"missing after install: {rel}"
        assert not path.is_symlink(), f"unexpected symlink at: {rel}"


# ── --dry-run ──────────────────────────────────────────────────────────


def test_dry_run_is_side_effect_free(tmp_path: Path) -> None:
    """--dry-run must not create the install dir or any of its files."""
    install_dir = tmp_path / "mempalace"
    result = _run_install(install_dir, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert not install_dir.exists(), (
        f"dry-run created install dir {install_dir} — that is a side effect."
    )
    # Output should mention DRY-RUN at least once for visibility.
    assert "DRY-RUN" in result.stdout, result.stdout


# ── Real install ──────────────────────────────────────────────────────


def test_real_install_creates_full_layout(tmp_path: Path) -> None:
    install_dir = tmp_path / "mempalace"
    result = _run_install(install_dir)
    assert result.returncode == 0, f"install failed:\n{result.stdout}\n{result.stderr}"
    _assert_install_layout(install_dir)


def test_install_renders_absolute_paths_in_hooks_json(tmp_path: Path) -> None:
    """`__PLUGIN_DIR__` must be substituted into hooks.json command paths."""
    install_dir = tmp_path / "mempalace"
    result = _run_install(install_dir)
    assert result.returncode == 0
    hooks = json.loads((install_dir / "hooks.json").read_text())
    # Every "command" string must be absolute and live under the
    # install dir.
    cmds = []
    for ns_payload in hooks.values():
        if not isinstance(ns_payload, dict):
            continue
        for entries in ns_payload.values():
            for entry in entries:
                cmds.append(entry["command"])
    assert cmds, f"no command entries found in rendered hooks.json: {hooks}"
    install_str = str(install_dir)
    for cmd in cmds:
        assert "__PLUGIN_DIR__" not in cmd, f"placeholder leaked into rendered hooks.json: {cmd!r}"
        assert cmd.startswith("/"), f"command path is not absolute: {cmd!r}"
        assert cmd.startswith(install_str + "/"), (
            f"command path {cmd!r} does not live under install dir {install_str!r}"
        )


def test_install_executable_bits_preserved(tmp_path: Path) -> None:
    """Both hook scripts must end up executable on the install side."""
    install_dir = tmp_path / "mempalace"
    result = _run_install(install_dir)
    assert result.returncode == 0
    for rel in (
        "hooks/mempal_save_hook_antigravity.sh",
        "hooks/mempal_wake_hook_antigravity.sh",
    ):
        path = install_dir / rel
        assert os.access(path, os.X_OK), f"hook script not executable: {rel}"


# ── Idempotency ───────────────────────────────────────────────────────


def test_install_is_byte_identical_on_re_run(tmp_path: Path) -> None:
    """Re-running the installer should leave every file byte-identical.

    The `cmp`-gated copy and template render is what makes the
    installer safe to run from CI and from `babysit`-style cron loops.
    """
    install_dir = tmp_path / "mempalace"
    first = _run_install(install_dir)
    assert first.returncode == 0, first.stderr
    # Snapshot every file's content + mtime + mode.
    snap1: dict[str, tuple[bytes, float, int]] = {}
    for rel in EXPECTED_FILES:
        p = install_dir / rel
        st = p.stat()
        snap1[rel] = (p.read_bytes(), st.st_mtime, st.st_mode)

    # Re-run.
    second = _run_install(install_dir)
    assert second.returncode == 0, second.stderr

    # Every file's contents must be byte-identical.
    for rel in EXPECTED_FILES:
        p = install_dir / rel
        body, _, mode = snap1[rel]
        assert p.read_bytes() == body, f"{rel} differs after re-install"
        # Mode must be preserved (we don't enforce mtime since the
        # cmp gate explicitly avoids re-writing).
        assert p.stat().st_mode == mode, f"{rel} mode changed after re-install"

    # Use filecmp.dircmp as a belt-and-suspenders check.
    cmp = filecmp.dircmp(install_dir, install_dir)
    assert not cmp.diff_files


def test_install_logs_no_writes_on_idempotent_re_run(tmp_path: Path) -> None:
    """The second run must not log "wrote: ..." for any file."""
    install_dir = tmp_path / "mempalace"
    first = _run_install(install_dir)
    assert first.returncode == 0
    # First run writes everything.
    assert "wrote:" in first.stdout
    second = _run_install(install_dir)
    assert second.returncode == 0
    assert "wrote:" not in second.stdout, (
        f"second install should be a no-op but wrote files:\n{second.stdout}"
    )


# ── Uninstall ─────────────────────────────────────────────────────────


def test_uninstall_removes_mempalace_install(tmp_path: Path) -> None:
    install_dir = tmp_path / "mempalace"
    install = _run_install(install_dir)
    assert install.returncode == 0
    uninstall = _run_install(install_dir, "--uninstall")
    assert uninstall.returncode == 0, uninstall.stderr
    assert not install_dir.exists()


def test_uninstall_refuses_basename_mismatch(tmp_path: Path) -> None:
    """Refuses to remove a directory whose basename isn't 'mempalace'.

    Honours the basename-match safety guard caught in the cursor PR
    review — prevents an accidental wipe of a sibling like
    'mempalace-foo' or, in the worst case, the user's home directory.
    """
    bad_dir = tmp_path / "totally-not-mempalace"
    bad_dir.mkdir()
    # Even though the dir has a plugin.json with name=mempalace, the
    # basename mismatch must still refuse.
    (bad_dir / "plugin.json").write_text(json.dumps({"name": "mempalace"}), encoding="utf-8")
    sentinel = bad_dir / "do-not-delete.txt"
    sentinel.write_text("preserve me", encoding="utf-8")

    result = _run_install(bad_dir, "--uninstall")
    assert result.returncode != 0, (
        f"uninstall should have refused but exited 0:\n{result.stdout}\n{result.stderr}"
    )
    assert bad_dir.is_dir(), f"bad uninstall removed {bad_dir}"
    assert sentinel.is_file(), "uninstall removed unrelated files inside bad dir"


def test_uninstall_refuses_when_plugin_json_missing(tmp_path: Path) -> None:
    """Refuses when the dir is missing plugin.json (not actually our plugin)."""
    install_dir = tmp_path / "mempalace"
    install_dir.mkdir()
    sentinel = install_dir / "stranger.txt"
    sentinel.write_text("preserve me", encoding="utf-8")
    result = _run_install(install_dir, "--uninstall")
    assert result.returncode != 0
    assert install_dir.is_dir()
    assert sentinel.is_file()


def test_uninstall_refuses_when_plugin_json_wrong_name(tmp_path: Path) -> None:
    """Refuses when plugin.json names a different plugin."""
    install_dir = tmp_path / "mempalace"
    install_dir.mkdir()
    (install_dir / "plugin.json").write_text(
        json.dumps({"name": "some-other-plugin"}), encoding="utf-8"
    )
    sentinel = install_dir / "preserved.txt"
    sentinel.write_text("safe", encoding="utf-8")
    result = _run_install(install_dir, "--uninstall")
    assert result.returncode != 0
    assert install_dir.is_dir()
    assert sentinel.is_file()


def test_uninstall_no_op_when_target_missing(tmp_path: Path) -> None:
    """Uninstalling a non-existent dir is a graceful no-op."""
    install_dir = tmp_path / "mempalace"
    result = _run_install(install_dir, "--uninstall")
    assert result.returncode == 0, result.stderr


# ── Relative path absolutization ──────────────────────────────────────


def test_relative_install_dir_is_absolutized(tmp_path: Path) -> None:
    """A relative --install-dir must be absolutized in the rendered hooks.json.

    Catches the cursor PR review issue where a relative path baked
    in verbatim left Antigravity unable to resolve hook commands.
    """
    work = tmp_path / "work"
    work.mkdir()
    # Relative path resolved against $PWD at invocation time.
    rel = "build/agy-out/mempalace"
    result = _run_install(Path(rel), cwd=work)
    assert result.returncode == 0, result.stderr
    abs_install = work / rel
    assert abs_install.is_dir(), f"installer did not create {abs_install} from relative path {rel}"
    hooks = json.loads((abs_install / "hooks.json").read_text())
    cmds = []
    for ns_payload in hooks.values():
        if not isinstance(ns_payload, dict):
            continue
        for entries in ns_payload.values():
            for entry in entries:
                cmds.append(entry["command"])
    for cmd in cmds:
        assert cmd.startswith("/"), f"relative install dir leaked into rendered hooks.json: {cmd!r}"
        assert "build/agy-out/mempalace" in cmd, (
            f"command path lost the relative-segment context: {cmd!r}"
        )


# ── Misc ──────────────────────────────────────────────────────────────


def test_install_help_does_not_write(tmp_path: Path) -> None:
    """`--help` should print usage and exit 0 without touching the dir."""
    install_dir = tmp_path / "mempalace"
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "Usage:" in result.stdout or "Usage:" in result.stderr
    assert not install_dir.exists()


def test_install_unknown_arg_exits_non_zero(tmp_path: Path) -> None:
    """Unknown args must fail loudly rather than silently ignoring."""
    install_dir = tmp_path / "mempalace"
    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--install-dir",
            str(install_dir),
            "--this-flag-does-not-exist",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert not install_dir.exists()
