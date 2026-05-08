"""Regression tests for issue #225 — MCP stdio protection.

The MCP protocol multiplexes JSON-RPC over stdio. Stdout MUST carry only
valid JSON-RPC messages. Several transitive deps (chromadb → onnxruntime,
posthog telemetry) print banners and warnings to stdout — sometimes at
the C level — which broke Claude Desktop's JSON parser on Windows.

The fix in mcp_server.py redirects stdout → stderr at both the Python
and file-descriptor level during module import, then restores the real
stdout in main() before entering the protocol loop.
"""

import subprocess
import sys
import textwrap


def test_module_import_redirects_stdout_to_stderr():
    """At import time, sys.stdout must point at sys.stderr so any stray
    print() from a transitive dependency is sent to stderr."""
    code = textwrap.dedent(
        """
        import sys
        original_stdout = sys.stdout
        from mempalace import mcp_server
        assert sys.stdout is sys.stderr, (
            f"Expected sys.stdout to be redirected to sys.stderr, "
            f"got: {sys.stdout!r}"
        )
        assert mcp_server._REAL_STDOUT is original_stdout, (
            "mcp_server._REAL_STDOUT must hold the original stdout"
        )
        print("OK", file=sys.stderr)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"


def test_restore_stdout_returns_real_stdout():
    """_restore_stdout() must reassign sys.stdout to the original handle
    so main() can write JSON-RPC responses to the real stdout."""
    code = textwrap.dedent(
        """
        import sys
        original_stdout = sys.stdout
        from mempalace import mcp_server
        assert sys.stdout is sys.stderr
        mcp_server._restore_stdout()
        assert sys.stdout is original_stdout, (
            f"After _restore_stdout(), sys.stdout must be the original; "
            f"got: {sys.stdout!r}"
        )
        mcp_server._restore_stdout()  # idempotent
        print("OK", file=sys.stderr)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"


def test_mcp_server_no_stdout_noise_on_clean_exit():
    """`python -m mempalace.mcp_server` with empty stdin must produce
    nothing on stdout. Empty input → readline() returns '' → main()
    breaks out cleanly. Any stdout content here would corrupt the
    JSON-RPC stream in real use."""
    proc = subprocess.run(
        [sys.executable, "-m", "mempalace.mcp_server"],
        input=b"",
        capture_output=True,
        timeout=60,
    )
    assert (
        proc.stdout == b""
    ), f"stdout must be empty before the first JSON-RPC response, but got: {proc.stdout!r}"
