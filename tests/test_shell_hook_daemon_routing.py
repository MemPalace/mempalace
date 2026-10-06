"""Legacy hooks probe once, keep Stop detached, and preserve both mine targets."""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mempalace import hook_shell

ROOT = Path(__file__).resolve().parents[1]
HOOKS = [
    "mempal_save_hook.sh",
    "mempal_precompact_hook.sh",
    "cursor/mempal_save_hook_cursor.sh",
    "cursor/mempal_precompact_hook_cursor.sh",
    "antigravity/mempal_save_hook_antigravity.sh",
]


@pytest.mark.parametrize("available", [True, False])
def test_shell_probe_uses_single_bounded_health_check(monkeypatch, available):
    from mempalace import daemon

    calls = []

    def probe(palace, *, health_timeout):
        calls.append(health_timeout)
        return object() if available else None

    monkeypatch.setattr(daemon, "get_client_if_running", probe)
    assert hook_shell.main(["daemon-available"]) == (0 if available else 1)
    assert calls == [daemon.HOOK_PROBE_TIMEOUT]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_console_hook_probe_dispatches_without_reading_stdin(monkeypatch, exit_code):
    from mempalace import cli

    calls = []

    def probe(argv):
        calls.append(argv)
        return exit_code

    monkeypatch.setattr(hook_shell, "main", probe)
    monkeypatch.setattr(sys, "argv", ["mempalace", "hook", "daemon-available"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == exit_code
    assert calls == [["daemon-available"]]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell hooks")
@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize("available", [True, False])
def test_shell_hooks_probe_once_and_route_mines(tmp_path, hook, available):
    home = tmp_path / "home"
    (home / ".mempalace").mkdir(parents=True)
    (home / ".mempalace/config.json").write_text("{}")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        (json.dumps({"message": {"role": "user", "content": "remember"}}) + "\n") * 15
    )
    project = tmp_path / "project"
    project.mkdir()
    calls = tmp_path / "calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # These shims intercept the routing protocol, while real Python handles
    # payload parsing and counters. Blocking the probe on a gate detects a Stop
    # regression without depending on network timing or installed packages.
    shim = r"""#!/bin/sh
case "$*" in
    *daemon-available*)
        echo probe >> "$ROUTING_CALLS"
        while [ ! -f "$PROBE_GATE" ]; do sleep 0.05; done
        exit "$PROBE_RC" ;;
    *daemon\ status*)
        echo unbounded-probe >> "$ROUTING_CALLS"
        while [ ! -f "$PROBE_GATE" ]; do sleep 0.05; done
        exit "$PROBE_RC" ;;
    *mine*) echo "mine $*" >> "$ROUTING_CALLS"; exit 0 ;;
    *--version*) exit 0 ;;
esac
exec REAL_PYTHON "$@"
""".replace("REAL_PYTHON", shlex.quote(sys.executable))
    for name in ("python", "mempalace"):
        script = bin_dir / name
        script.write_text(shim)
        script.chmod(0o755)
    payload = {
        "session_id": "routing",
        "conversation_id": "routing",
        "conversationId": "routing",
        "transcript_path": str(transcript),
        "transcriptPath": str(transcript),
        "workspacePaths": [str(project)],
        "fullyIdle": True,
        "terminationReason": "completed",
    }
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "MEMPAL_PYTHON": str(bin_dir / "python"),
        "MEMPAL_SAVE_INTERVAL": "1",
        "MEMPAL_DIR": str(project),
        "ROUTING_CALLS": str(calls),
        "PROBE_RC": "0" if available else "1",
        "PROBE_GATE": str(tmp_path / "probe-gate"),
    }
    hook_path = ROOT / "hooks" / hook
    if "/" not in hook:
        # Claude's project target is configured by editing the script.
        hook_path = tmp_path / Path(hook).name
        hook_path.write_text(
            (ROOT / "hooks" / hook)
            .read_text()
            .replace('MEMPAL_DIR=""', "MEMPAL_DIR=" + shlex.quote(str(project)))
        )
    gate = Path(env["PROBE_GATE"])
    if "precompact" in hook:
        gate.touch()
    try:
        result = subprocess.run(
            ["bash", str(hook_path)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            cwd=ROOT,
            timeout=3,
        )
    finally:
        gate.touch()
    assert result.returncode == 0, result.stderr
    expected_mines = 1 if "antigravity" in hook else 2
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        lines = calls.read_text().splitlines() if calls.exists() else []
        mines = [line for line in lines if line.startswith("mine ")]
        if len(mines) == expected_mines:
            break
        time.sleep(0.02)
    assert lines.count("probe") == 1
    assert "unbounded-probe" not in lines
    assert len(mines) == expected_mines
    assert all(("--daemon" in mine.split()) == available for mine in mines)
