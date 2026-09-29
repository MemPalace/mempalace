"""
Integration tests for the legacy ``.sh`` hook scripts.

The shell hooks do their own Python resolution (unlike the Python
``hooks_cli.py`` which uses ``sys.executable`` — trivially correct).
GUI-launched harnesses on macOS provide a minimal PATH that often lacks
the Python where ``mempalace`` is installed, so the shell path needs to:

  1. honour ``$MEMPAL_PYTHON`` as an explicit user override;
  2. fall back to ``$(command -v python3)`` / bare ``python3``;
  3. *never* crash the hook when the resolved interpreter can't import
     mempalace — log and skip the auto-ingest instead, so Claude Code
     doesn't see a non-zero exit from its Stop hook.

These regressions matter because every failure mode they catch produced
silent breakage in production — the user's hook appeared to "not fire"
but was actually crashing deep in a PATH-resolution edge case.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVE_HOOK = REPO_ROOT / "hooks" / "mempal_save_hook.sh"
PRECOMPACT_HOOK = REPO_ROOT / "hooks" / "mempal_precompact_hook.sh"
SESSION_END_HOOK = REPO_ROOT / "hooks" / "mempal_session_end_hook.sh"
PLUGIN_SESSION_END_HOOK = REPO_ROOT / ".claude-plugin" / "hooks" / "mempal-session-end-hook.sh"


pytestmark = pytest.mark.skipif(os.name == "nt", reason="bash hook scripts are POSIX-only")


# ── helpers ───────────────────────────────────────────────────────────────


def _write_fake_python(
    path: Path, *, can_import_mempalace: bool = False, marker_file: Path | None = None
) -> Path:
    """Create a python3 shim that proxies to the real interpreter so
    the hook's JSON-parsing calls still work, but fails ``-c 'import
    mempalace'`` / ``-m mempalace`` when ``can_import_mempalace`` is
    False.

    Every invocation appends the shim name to ``marker_file`` so tests
    can prove which interpreter the hook invoked — using a file because
    the hook pipes some python calls to ``2>/dev/null``, so stderr
    markers are unreliable."""
    real_python = sys.executable
    marker = str(marker_file) if marker_file is not None else ""
    shim_src = f"""#!/bin/bash
# Fake python3 shim: proxy to the real interpreter, drop a marker,
# and simulate a missing mempalace install when configured that way.
MARKER_FILE="{marker}"
if [ -n "$MARKER_FILE" ]; then
    echo "{path.name}" >> "$MARKER_FILE"
fi
CAN_IMPORT={"1" if can_import_mempalace else "0"}
# Simulate the "mempalace is not installed in this interpreter" case.
if [ "$CAN_IMPORT" = "0" ]; then
    if [ "$1" = "-c" ] && echo "$2" | grep -q "import mempalace"; then
        exit 1
    fi
    if [ "$1" = "-m" ] && [ "$2" = "mempalace" ]; then
        exit 1
    fi
fi
# Everything else — JSON parsing, heredoc stdin, etc — delegate to real python.
exec "{real_python}" "$@"
"""
    path.write_text(shim_src)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run_hook(
    script: Path,
    stdin_json: dict,
    *,
    env_overrides: dict | None = None,
    path_prefix: list[Path] | None = None,
) -> subprocess.CompletedProcess:
    """Invoke a shell hook with a minimal controlled environment."""
    env = {
        # Give the hook a clean slate — no inherited MEMPAL_* vars.
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    if path_prefix:
        env["PATH"] = os.pathsep.join(str(p) for p in path_prefix) + os.pathsep + env["PATH"]
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(script)],
        input=json.dumps(stdin_json),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# ── MEMPAL_PYTHON resolution contract ────────────────────────────────────


class TestMempalPythonOverride:
    def test_explicit_override_wins_over_path(self, tmp_path):
        """If MEMPAL_PYTHON is set and executable, the hook must use it
        in preference to whatever is on PATH."""
        marker = tmp_path / "markers.log"
        fake = _write_fake_python(
            tmp_path / "override_python",
            can_import_mempalace=True,
            marker_file=marker,
        )
        result = _run_hook(
            SAVE_HOOK,
            {"session_id": "abc", "stop_hook_active": False, "transcript_path": ""},
            env_overrides={"MEMPAL_PYTHON": str(fake), "HOME": str(tmp_path)},
        )
        assert result.returncode == 0, (
            f"hook exited non-zero: stderr={result.stderr!r} stdout={result.stdout!r}"
        )
        invocations = marker.read_text().splitlines() if marker.exists() else []
        assert "override_python" in invocations, (
            f"MEMPAL_PYTHON override was not used. Marker log: {invocations!r}"
        )

    def test_ignores_override_when_not_executable(self, tmp_path):
        """If MEMPAL_PYTHON is set but the file isn't executable, the
        hook must fall back to PATH rather than blow up with a
        'permission denied'."""
        bogus = tmp_path / "not_executable"
        bogus.write_text("# not a python")
        # Do NOT chmod +x — the hook should notice and skip.
        result = _run_hook(
            SAVE_HOOK,
            {"session_id": "abc", "stop_hook_active": False, "transcript_path": ""},
            env_overrides={"MEMPAL_PYTHON": str(bogus), "HOME": str(tmp_path)},
        )
        assert result.returncode == 0, (
            f"hook crashed on non-executable MEMPAL_PYTHON: {result.stderr!r}"
        )

    def test_falls_back_to_path_when_unset(self, tmp_path):
        """With MEMPAL_PYTHON unset, the hook uses whatever ``python3``
        is found on PATH. Prove this by putting a marker-emitting shim
        first on PATH."""
        marker = tmp_path / "markers.log"
        fake = _write_fake_python(
            tmp_path / "python3",
            can_import_mempalace=True,
            marker_file=marker,
        )
        result = _run_hook(
            SAVE_HOOK,
            {"session_id": "abc", "stop_hook_active": False, "transcript_path": ""},
            env_overrides={"MEMPAL_PYTHON": "", "HOME": str(tmp_path)},
            path_prefix=[fake.parent],
        )
        assert result.returncode == 0
        invocations = marker.read_text().splitlines() if marker.exists() else []
        assert "python3" in invocations, (
            f"fallback-to-PATH did not use the shimmed python3. Marker log: {invocations!r}"
        )


# ── session-end wrapper: must background so the foreground beats the budget ──


def _write_recording_mempalace(
    path: Path, args_file: Path, *, sleep_secs: float = 0.0, done_file: Path | None = None
) -> Path:
    """A fake ``mempalace`` that consumes stdin, optionally sleeps, then records
    its argv to ``args_file`` (and touches ``done_file``). Lets a test observe a
    *backgrounded* dispatch after the wrapper's foreground has already returned.
    """
    done_line = f'printf done > "{done_file}"' if done_file is not None else ":"
    src = f"""#!/bin/bash
cat >/dev/null
sleep {sleep_secs}
printf '%s' "$*" > "{args_file}"
{done_line}
printf '{{}}'
"""
    path.write_text(src)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _wait_for(path: Path, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return True
        time.sleep(0.05)
    return False


class TestSessionEndWrapper:
    def test_foreground_returns_before_worker_finishes(self, tmp_path):
        """Budget contract: the foreground must return well before the (slow)
        worker completes, otherwise SessionEnd's ~1.5s budget would kill the
        mine. Proven with a worker that sleeps 2s before recording."""
        args_file = tmp_path / "args.log"
        done_file = tmp_path / "worker.done"
        fake = _write_recording_mempalace(
            tmp_path / "mempalace", args_file, sleep_secs=2.0, done_file=done_file
        )
        t0 = time.monotonic()
        result = _run_hook(
            SESSION_END_HOOK,
            {"session_id": "abc", "transcript_path": ""},
            env_overrides={"HOME": str(tmp_path)},
            path_prefix=[fake.parent],
        )
        elapsed = time.monotonic() - t0
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert result.stdout == "{}"
        assert elapsed < 1.5, f"foreground blocked {elapsed:.2f}s; the budget would kill it"
        assert not done_file.exists(), (
            "worker finished before the foreground returned — wrapper is not backgrounding"
        )
        assert _wait_for(done_file), "detached worker never completed"
        assert args_file.read_text() == "hook run --hook session-end --harness claude-code"

    def test_dispatches_via_mempal_python_override(self, tmp_path):
        args_file = tmp_path / "args.log"
        shim = tmp_path / "python3"
        shim.write_text(
            f"""#!/bin/bash
if [ "$1" = "-c" ]; then exit 0; fi
if [ "$1" = "-m" ] && [ "$2" = "mempalace" ]; then
  shift 2
  cat >/dev/null
  printf '%s' "$*" > "{args_file}"
  printf '{{}}'
  exit 0
fi
exit 1
""",
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        result = _run_hook(
            SESSION_END_HOOK,
            {"session_id": "abc", "transcript_path": ""},
            env_overrides={
                "HOME": str(tmp_path),
                "PATH": "/usr/bin:/bin",
                "MEMPAL_PYTHON": str(shim),
            },
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert result.stdout == "{}"
        assert _wait_for(args_file), "backgrounded worker never ran"
        assert args_file.read_text() == "hook run --hook session-end --harness claude-code"

    def test_harness_override_is_forwarded(self, tmp_path):
        args_file = tmp_path / "args.log"
        fake = _write_recording_mempalace(tmp_path / "mempalace", args_file)
        result = _run_hook(
            SESSION_END_HOOK,
            {"session_id": "abc", "transcript_path": ""},
            env_overrides={"HOME": str(tmp_path), "MEMPALACE_HOOK_HARNESS": "codex"},
            path_prefix=[fake.parent],
        )
        assert result.returncode == 0
        assert _wait_for(args_file)
        assert args_file.read_text() == "hook run --hook session-end --harness codex"


class TestPluginSessionEndWrapper:
    def test_foreground_returns_before_worker_finishes(self, tmp_path):
        args_file = tmp_path / "args.log"
        done_file = tmp_path / "worker.done"
        fake = _write_recording_mempalace(
            tmp_path / "mempalace", args_file, sleep_secs=2.0, done_file=done_file
        )
        t0 = time.monotonic()
        result = _run_hook(
            PLUGIN_SESSION_END_HOOK,
            {"session_id": "abc", "transcript_path": ""},
            env_overrides={"HOME": str(tmp_path)},
            path_prefix=[fake.parent],
        )
        elapsed = time.monotonic() - t0
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert result.stdout == "{}"
        assert elapsed < 1.5, f"plugin foreground blocked {elapsed:.2f}s"
        assert not done_file.exists()
        assert _wait_for(done_file), "detached plugin worker never completed"
        assert args_file.read_text() == "hook run --hook session-end --harness claude-code"
