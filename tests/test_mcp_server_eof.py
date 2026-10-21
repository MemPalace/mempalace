"""Self-exit on stdin EOF / broken stdout pipe — orphaned stdio servers.

A stdio MCP server launched over SSH (``ssh <host> python3 -m
mempalace.mcp_server``) is orphaned when the SSH session drops. If it
does not exit when its stdio channel dies, it sleeps forever on the dead
pipe and keeps holding whatever palace locks it acquired — the
``mine_palace`` flock held idle for ~4 h caused the 2026-07-10 palace
write-path outage.

These tests spawn the real server as a subprocess and kill its stdio
channel from the outside. Palace isolation comes from conftest.py, which
redirects HOME to a throwaway temp dir before any test runs; the
subprocess inherits that environment, so ``~/.mempalace`` resolves inside
the temp dir (same mechanism the test_mcp_stdio_protection.py subprocess
tests rely on).
"""

import json
import subprocess
import sys

import pytest

# Server startup imports chromadb — slow on a cold cache — so the exit
# deadline is generous. The requirement is "exits at all", not "exits fast".
_EXIT_TIMEOUT = 60


def _spawn_server() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "mempalace.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_or_fail(proc: subprocess.Popen, why: str) -> int:
    try:
        return proc.wait(timeout=_EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail(f"server did not exit within {_EXIT_TIMEOUT}s after {why}")


def test_stdin_eof_exits_zero_and_logs():
    """Closing stdin must make the server log to stderr and exit 0."""
    proc = _spawn_server()
    proc.stdin.close()
    returncode = _wait_or_fail(proc, "stdin EOF")
    stderr = proc.stderr.read().decode("utf-8", errors="replace")
    assert returncode == 0, f"expected exit 0, got {returncode}\nstderr: {stderr}"
    assert "stdin EOF" in stderr, f"missing EOF shutdown log line\nstderr: {stderr}"
    assert proc.stdout.read() == b"", "stdout must stay a clean JSON-RPC channel"


def test_stdout_broken_pipe_exits_zero():
    """A dead stdout reader must terminate the server, not be swallowed.

    Deterministic: once the parent closes the pipe's only read end, the
    server's next stdout write fails with EPIPE immediately (Python
    ignores SIGPIPE, so it surfaces as BrokenPipeError, never a signal
    death). stdin stays open the whole time, so an exit can only come
    from the write path.
    """
    proc = _spawn_server()
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode() + b"\n"

    # Prove the loop is up: one request, one response line.
    proc.stdin.write(request)
    proc.stdin.flush()
    first_response = proc.stdout.readline()
    assert first_response.startswith(b"{"), f"no JSON-RPC response: {first_response!r}"

    # Kill the read end, then force another response write.
    proc.stdout.close()
    proc.stdin.write(request)
    proc.stdin.flush()

    returncode = _wait_or_fail(proc, "stdout broken pipe")
    stderr = proc.stderr.read().decode("utf-8", errors="replace")
    assert returncode == 0, f"expected exit 0, got {returncode}\nstderr: {stderr}"
    assert "client disconnected" in stderr, (
        f"missing broken-pipe shutdown log line\nstderr: {stderr}"
    )
