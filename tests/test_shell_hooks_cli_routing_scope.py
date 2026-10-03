"""Shell hooks must select the hooks write-routing scope.

The portable shell hooks shell out to ``mempalace mine`` instead of writing
through the in-process ``mempalace.hooks_cli`` path, so they must export
``MEMPALACE_CLI_ROUTING_SCOPE=hooks`` before every ``mine`` invocation,
otherwise ``resolve_cli_write_routing`` resolves the ``cli`` write-routing
scope for a write that a user configured under ``write_routing.hooks``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVE_HOOK = REPO_ROOT / "hooks" / "mempal_save_hook.sh"
PRECOMPACT_HOOK = REPO_ROOT / "hooks" / "mempal_precompact_hook.sh"
CURSOR_SAVE_HOOK = REPO_ROOT / "hooks" / "cursor" / "mempal_save_hook_cursor.sh"
CURSOR_PRECOMPACT_HOOK = REPO_ROOT / "hooks" / "cursor" / "mempal_precompact_hook_cursor.sh"
ANTIGRAVITY_SAVE_HOOK = REPO_ROOT / "hooks" / "antigravity" / "mempal_save_hook_antigravity.sh"

_MINE_INVOCATION_SITES = {
    SAVE_HOOK: 2,
    PRECOMPACT_HOOK: 2,
    CURSOR_SAVE_HOOK: 2,
    CURSOR_PRECOMPACT_HOOK: 2,
    ANTIGRAVITY_SAVE_HOOK: 1,
}

pytestmark = pytest.mark.skipif(os.name == "nt", reason="bash hook scripts are POSIX-only")


def _write_fake_python(path: Path, *, marker_file: Path) -> Path:
    """A python3 shim that records the routing scope seen by ``mine`` and
    proxies everything else (JSON parsing helpers) to the real interpreter."""
    real_python = sys.executable
    shim_src = f"""#!/bin/bash
if [ "$1" = "-m" ] && [ "$2" = "mempalace.hook_shell" ] && [ "$3" = "count-human-messages" ]; then
    echo 999
    exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "mempalace" ] && [ "$3" = "mine" ]; then
    echo "${{MEMPALACE_CLI_ROUTING_SCOPE:-<unset>}}" >> "{marker_file}"
    exit 0
fi
exec "{real_python}" "$@"
"""
    path.write_text(shim_src)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _write_fake_mempalace(path: Path, *, marker_file: Path) -> Path:
    """A bare ``mempalace`` shim for the cursor hooks, which invoke the
    console script directly rather than ``$MEMPAL_PYTHON_BIN -m mempalace``."""
    shim_src = f"""#!/bin/bash
if [ "$1" = "mine" ]; then
    echo "${{MEMPALACE_CLI_ROUTING_SCOPE:-<unset>}}" >> "{marker_file}"
    exit 0
fi
exit 0
"""
    path.write_text(shim_src)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class TestSaveHookExportsHooksScope:
    def test_save_hook_exports_hooks_scope_for_transcript_mine(self, tmp_path):
        marker = tmp_path / "scope.log"
        fake_python = _write_fake_python(tmp_path / "python3", marker_file=marker)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        result = subprocess.run(
            ["bash", str(SAVE_HOOK)],
            input=json.dumps(
                {
                    "session_id": "abc",
                    "stop_hook_active": False,
                    "transcript_path": str(transcript),
                }
            ),
            capture_output=True,
            text=True,
            env={
                "HOME": str(tmp_path),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "MEMPAL_PYTHON": str(fake_python),
            },
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

        # The save hook backgrounds the mine with ``&``; give it a moment
        # to write the marker before asserting on it.
        subprocess.run(["bash", "-c", "wait"], env={}, timeout=5)
        import time

        for _ in range(50):
            if marker.exists() and marker.read_text().strip():
                break
            time.sleep(0.1)

        assert marker.exists(), f"mine was never invoked: stderr={result.stderr!r}"
        assert marker.read_text().splitlines() == ["hooks"], (
            "shell-hook-triggered mine must resolve the 'hooks' write-routing scope, not 'cli'"
        )


class TestPrecompactHookExportsHooksScope:
    def test_precompact_hook_exports_hooks_scope_for_transcript_mine(self, tmp_path):
        marker = tmp_path / "scope.log"
        fake_python = _write_fake_python(tmp_path / "python3", marker_file=marker)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        result = subprocess.run(
            ["bash", str(PRECOMPACT_HOOK)],
            input=json.dumps(
                {
                    "session_id": "abc",
                    "transcript_path": str(transcript),
                }
            ),
            capture_output=True,
            text=True,
            env={
                "HOME": str(tmp_path),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "MEMPAL_PYTHON": str(fake_python),
            },
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert marker.exists(), f"mine was never invoked: stderr={result.stderr!r}"
        assert marker.read_text().splitlines() == ["hooks"], (
            "shell-hook-triggered mine must resolve the 'hooks' write-routing scope, not 'cli'"
        )


class TestCursorHooksExportHooksScope:
    def test_cursor_precompact_hook_exports_hooks_scope(self, tmp_path):
        marker = tmp_path / "scope.log"
        _write_fake_mempalace(tmp_path / "mempalace", marker_file=marker)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        result = subprocess.run(
            ["bash", str(CURSOR_PRECOMPACT_HOOK)],
            input=json.dumps(
                {
                    "conversation_id": "abc",
                    "transcript_path": str(transcript),
                }
            ),
            capture_output=True,
            text=True,
            env={
                "HOME": str(tmp_path),
                "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            },
            timeout=30,
        )
        assert marker.exists(), (
            f"mine was never invoked: rc={result.returncode} stderr={result.stderr!r}"
        )
        assert marker.read_text().splitlines() == ["hooks"], (
            "shell-hook-triggered mine must resolve the 'hooks' write-routing scope, not 'cli'"
        )


class TestEveryMineInvocationSiteIsHookScoped:
    """Cheap source-level backstop across every portable shell hook: every
    ``mempalace mine`` call site must be prefixed with
    ``MEMPALACE_CLI_ROUTING_SCOPE=hooks``, so a future hook variant (or a
    new call site added to an existing one) cannot silently regress back to
    the unscoped ``cli`` default."""

    @pytest.mark.parametrize(
        "hook_path,expected_sites",
        _MINE_INVOCATION_SITES.items(),
        ids=lambda p: getattr(p, "name", p),
    )
    def test_every_mine_call_is_hook_scoped(self, hook_path, expected_sites):
        src = hook_path.read_text()
        lines = [line for line in src.splitlines() if not line.lstrip().startswith("#")]
        mine_lines = [line for line in lines if 'mempalace mine "' in line]
        assert len(mine_lines) == expected_sites, (
            f"{hook_path.name}: expected {expected_sites} `mempalace mine` call site(s), "
            f"found {len(mine_lines)}: {mine_lines!r}"
        )
        for line in mine_lines:
            assert "MEMPALACE_CLI_ROUTING_SCOPE=hooks" in line, (
                f"{hook_path.name}: mine call site is missing "
                f"MEMPALACE_CLI_ROUTING_SCOPE=hooks: {line!r}"
            )
