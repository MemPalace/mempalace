"""End-to-end diagnostics for MCP transport availability.

These tests exercise the CLI and a real child process over stdio. They cover
the operator path agents need when the in-thread MCP handle is already closed:
run a shell command, classify the failure as transport-level, and use CLI
fallback guidance instead of treating the palace as empty.
"""

import json
import shlex
import subprocess
import sys
from pathlib import Path


def _run_mcp_health(server_command: str, tmp_path: Path):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mempalace",
            "--palace",
            str(tmp_path / "palace"),
            "mcp-health",
            "--json",
            "--timeout",
            "5",
            "--server-command",
            server_command,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_mcp_health_reports_ok_for_real_stdio_server(tmp_path):
    server_command = " ".join(
        shlex.quote(part)
        for part in [
            sys.executable,
            "-m",
            "mempalace.mcp_server",
            "--backend",
            "sqlite_exact",
            "--palace",
            str(tmp_path / "palace"),
        ]
    )

    result = _run_mcp_health(server_command, tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mcp_transport"] == "ok"
    assert payload["server"]["name"] == "mempalace"
    assert payload["tools"]["has_status"] is True
    assert payload["fallback"]["needed"] is False


def test_mcp_health_distinguishes_transport_closed_from_empty_palace(tmp_path):
    server_command = " ".join(
        shlex.quote(part)
        for part in [
            sys.executable,
            "-c",
            "import sys; sys.exit(42)",
        ]
    )

    result = _run_mcp_health(server_command, tmp_path)

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["mcp_transport"] == "transport_unavailable"
    assert payload["failure_mode"] == "mcp_transport_closed"
    assert payload["fallback"]["needed"] is True
    assert payload["fallback"]["classification"] == "MCP transport unavailable"
    assert payload["fallback"]["cli_status_command"].startswith("mempalace ")
    assert payload["fallback"]["cli_status_command"].endswith(" status")
    assert "not_empty_palace" in payload["agent_guidance"]
