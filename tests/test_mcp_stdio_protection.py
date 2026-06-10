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
    assert proc.stdout == b"", (
        f"stdout must be empty before the first JSON-RPC response, but got: {proc.stdout!r}"
    )


def test_main_reconfigures_all_streams_to_utf8_strict():
    """Verify that main() reconfigures stdio with per-stream error policies:
    stdin uses surrogateescape (malformed bytes survive the read loop);
    stdout and stderr use strict (server-controlled JSON-RPC, failures are bugs)."""
    code = textwrap.dedent(
        """
        import sys
        from unittest.mock import Mock, patch, MagicMock

        reconfigure_calls = []

        def make_reconfigure(stream_name):
            def reconfigure(**kwargs):
                reconfigure_calls.append((stream_name, kwargs))
            return reconfigure

        # Create fake stream objects with reconfigure methods
        fake_stdin = MagicMock()
        fake_stdout = MagicMock()
        fake_stderr = MagicMock()

        fake_stdin.reconfigure = make_reconfigure('stdin')
        fake_stdout.reconfigure = make_reconfigure('stdout')
        fake_stderr.reconfigure = make_reconfigure('stderr')

        # Mock readline to return empty on first call (which causes main loop to exit)
        fake_stdin.readline = Mock(return_value='')

        from mempalace import mcp_server

        # Patch _REAL_STDOUT so that _restore_stdout() will restore to our fake
        with patch.object(mcp_server, '_REAL_STDOUT', fake_stdout), \
             patch.object(mcp_server.sys, 'stdin', fake_stdin), \
             patch.object(mcp_server.sys, 'stdout', fake_stdout), \
             patch.object(mcp_server.sys, 'stderr', fake_stderr), \
             patch.object(mcp_server, '_start_idle_exit_watchdog', return_value=None), \
             patch.object(mcp_server, '_maybe_eager_warmup_embedder', return_value=None), \
             patch.object(mcp_server, '_refresh_vector_disabled_flag', return_value=None):
            mcp_server.main()

        # Verify reconfigure was called on all three streams with correct args
        assert len(reconfigure_calls) == 3, (
            f"Expected exactly 3 reconfigure calls, got {len(reconfigure_calls)}: {reconfigure_calls}"
        )

        stream_names = {call[0] for call in reconfigure_calls}
        assert stream_names == {'stdin', 'stdout', 'stderr'}, (
            f"Expected all three streams, got: {stream_names}"
        )

        # Per-stream error policies: stdin uses surrogateescape so malformed
        # bytes survive the read loop; stdout/stderr use strict because
        # they carry server-controlled JSON-RPC and encode failures are bugs.
        expected_per_stream = {
            'stdin':  {'encoding': 'utf-8', 'errors': 'surrogateescape'},
            'stdout': {'encoding': 'utf-8', 'errors': 'strict'},
            'stderr': {'encoding': 'utf-8', 'errors': 'strict'},
        }
        for stream_name, kwargs in reconfigure_calls:
            expected = expected_per_stream[stream_name]
            assert kwargs == expected, (
                f"Stream {stream_name}: expected {expected}, got {kwargs}"
            )

        print("OK", file=sys.stderr)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Test failed. stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
