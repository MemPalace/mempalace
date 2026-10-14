"""The MCP server is a package; the public import path is unchanged."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS = (
    "_logging",
    "_guards",
    "_session",
    "tools_read",
    "tools_write",
    "tools_kg",
    "tools_diary",
    "tools_coord",
    "schemas",
    "protocol",
    "http",
    "runtime",
)


def test_mcp_server_is_a_package():
    import mempalace.mcp_server as mcp

    assert hasattr(mcp, "__path__")
    assert callable(mcp.main)
    assert callable(mcp.handle_request)
    assert "mempalace_status" in mcp.TOOLS
    assert callable(mcp.TOOLS["mempalace_status"]["handler"])


@pytest.mark.parametrize("name", FRAGMENTS)
def test_fragments_refuse_direct_import(name):
    with pytest.raises(ImportError, match="implementation fragment"):
        importlib.import_module(f"mempalace.mcp_server.{name}")


def test_python_m_mcp_server_still_runs():
    """``python -m mempalace.mcp_server`` must still be a valid entry."""
    proc = subprocess.run(
        [sys.executable, "-m", "mempalace.mcp_server"],
        input=b"",
        capture_output=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    assert proc.stdout == b""


def test_fragment_files_exist():
    pkg = REPO_ROOT / "mempalace" / "mcp_server"
    missing = [name for name in FRAGMENTS if not (pkg / f"{name}.py").is_file()]
    assert missing == []
    assert not (REPO_ROOT / "mempalace" / "mcp_server.py").exists()


def test_stdio_protection_preserves_handles_across_reload():
    """Reloading mempalace.mcp_server must not clobber _REAL_STDOUT or leak fds."""
    code = (
        "import importlib, sys, os\n"
        "import mempalace.mcp_server as mcp\n"
        "orig_stdout = mcp._REAL_STDOUT\n"
        "orig_fd = mcp._REAL_STDOUT_FD\n"
        "# Reload while redirected\n"
        "importlib.reload(mcp)\n"
        "assert mcp._REAL_STDOUT is orig_stdout\n"
        "assert mcp._REAL_STDOUT_FD == orig_fd\n"
        "# Restore stdout and verify reload re-protects cleanly\n"
        "mcp._restore_stdout()\n"
        "assert mcp._REAL_STDOUT_FD is None\n"
        "assert sys.stdout is orig_stdout\n"
        "importlib.reload(mcp)\n"
        "assert mcp._REAL_STDOUT is orig_stdout\n"
        "assert mcp._REAL_STDOUT_FD is not None\n"
        "mcp._restore_stdout()\n"
        "assert sys.stdout is orig_stdout\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr


_SERVER_SETTINGS_ENV = (
    "MEMPALACE_PALACE_PATH",
    "MEMPALACE_PALACE",
    "MEMPAL_PALACE_PATH",
    "MEMPALACE_BACKEND",
    "MEMPALACE_BACKEND_EXPLICIT",
    "MEMPALACE_MCP_READ_ONLY",
)


@pytest.mark.parametrize(
    "argv",
    [
        ["--palace", "/somewhere/else/palace"],
        ["--backend", "no-such-backend"],
        ["--port", "not-a-port"],
        ["--read-only"],
        ["--help"],
    ],
)
def test_importing_the_server_leaves_the_importers_command_line_alone(argv):
    """Importing the server parses no flags from the importing program's argv (#2528).

    On develop a host program's own ``--palace`` redirected MemPalace, a ``--port``
    value that is not a number ended the host, and ``--help`` printed this
    server's options instead of the host's.
    """
    code = (
        "import os, sys\n"
        "import mempalace.mcp_server as mcp\n"
        "state = (os.environ.get('MEMPALACE_PALACE_PATH'), os.environ.get('MEMPALACE_BACKEND'),"
        " mcp._READ_ONLY, mcp._palace_flag_given)\n"
        "print('state after import:', state, file=sys.stderr)\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in _SERVER_SETTINGS_ENV}
    proc = subprocess.run(
        [sys.executable, "-c", code, *argv],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "state after import: (None, None, False, False)" in proc.stderr
    assert "usage:" not in proc.stdout + proc.stderr


def _serve_nothing(monkeypatch, served):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_install_shutdown_signal_handlers", lambda: None)
    monkeypatch.setattr("mempalace.update_awareness.schedule_update_check", lambda: None)
    monkeypatch.setattr(mcp_server, "_run_stdio_loop", lambda: served.append("stdio"))
    monkeypatch.setattr(
        mcp_server,
        "_run_http_loop",
        lambda: served.append(
            (
                "http",
                mcp_server._args.port,
                mcp_server._READ_ONLY,
                mcp_server._config.palace_path,
                mcp_server._resolve_kg_path(),
            )
        ),
    )
    # main() drops PYTHONPATH for its children; give the session's back afterwards.
    monkeypatch.delenv("PYTHONPATH", raising=False)


def test_main_applies_its_own_flags_before_serving(monkeypatch, tmp_path):
    from _mcp_server_helpers import _keep_server_command_line_state
    from mempalace import mcp_server

    _keep_server_command_line_state(monkeypatch)
    served = []
    _serve_nothing(monkeypatch, served)
    palace = tmp_path / "palace"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mempalace-mcp",
            "--palace",
            str(palace),
            "--read-only",
            "--transport",
            "http",
            "--port",
            "8799",
        ],
    )

    mcp_server.main()

    assert served == [
        ("http", 8799, True, str(palace), str(palace / "knowledge_graph.sqlite3")),
    ]


@pytest.mark.parametrize(
    ("watched_at_import", "backend_flag", "watched_after"),
    [
        (("mempalace", "chromadb"), "sqlite_exact", ("mempalace",)),
        (("mempalace",), "chroma", ("mempalace", "chromadb")),
    ],
)
def test_main_backend_flag_sets_the_stale_library_watch_list(
    monkeypatch, tmp_path, watched_at_import, backend_flag, watched_after
):
    """With no backend in config.json (the case here), ``--backend`` decides which
    installed libraries the stale-library gate watches, and every watched one
    needs a startup baseline or drift in it is never detected. Watching chromadb
    for a server that does not store in it refuses that server's writes after an
    upgrade of chromadb alone."""
    from _mcp_server_helpers import _keep_server_command_line_state
    from mempalace import mcp_server
    from mempalace.config import MempalaceConfig

    _keep_server_command_line_state(monkeypatch)
    # A config.json that names a backend would decide the watch list instead.
    monkeypatch.setattr(mcp_server, "_config", MempalaceConfig(config_dir=str(tmp_path)))
    baseline = {dist: "0" for dist in watched_at_import}
    monkeypatch.setattr(mcp_server, "_STALE_LIBRARY_WATCHED_DISTS", watched_at_import)
    monkeypatch.setattr(mcp_server, "_STARTUP_DIST_VERSIONS", baseline)
    monkeypatch.setattr(mcp_server, "_STARTUP_DIST_ERRORS", {})
    monkeypatch.setattr(mcp_server, "_STARTUP_DIST_STATE", (baseline, {}))
    served = []
    _serve_nothing(monkeypatch, served)
    monkeypatch.setattr(sys, "argv", ["mempalace-mcp", "--backend", backend_flag])

    mcp_server.main()

    assert served == ["stdio"]
    assert mcp_server._STALE_LIBRARY_WATCHED_DISTS == watched_after
    assert set(mcp_server._STARTUP_DIST_VERSIONS) == set(watched_after)


def test_main_refuses_an_unknown_backend_flag_before_serving(monkeypatch):
    from _mcp_server_helpers import _keep_server_command_line_state
    from mempalace import mcp_server
    from mempalace.backends.registry import BackendUnavailableError

    _keep_server_command_line_state(monkeypatch)
    served = []
    _serve_nothing(monkeypatch, served)
    monkeypatch.setattr(sys, "argv", ["mempalace-mcp", "--backend", "no-such-backend"])

    with pytest.raises(BackendUnavailableError):
        mcp_server.main()

    assert served == []
    assert "MEMPALACE_BACKEND" not in os.environ


def test_flags_main_applied_survive_a_reload():
    """A reload keeps the flags main() applied, as __init__.py keeps its stdio
    handles and protocol.py its startup baseline."""
    code = (
        "import importlib, sys\n"
        "sys.argv = ['mempalace-mcp', '--read-only', '--palace', '/x/p', '--port', '8799']\n"
        "import mempalace.mcp_server as mcp\n"
        "import mempalace.update_awareness as ua\n"
        "mcp._install_shutdown_signal_handlers = lambda: None\n"
        "ua.schedule_update_check = lambda: None\n"
        "mcp._run_stdio_loop = lambda: None\n"
        "mcp.main()\n"
        "importlib.reload(mcp)\n"
        "print('after reload:', (mcp._READ_ONLY, mcp._palace_flag_given, mcp._args.port),"
        " file=sys.stderr)\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in _SERVER_SETTINGS_ENV}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "after reload: (True, True, 8799)" in proc.stderr


def test_flags_without_a_backend_leave_the_stale_library_baseline_alone(monkeypatch, tmp_path):
    """The daemon applies its palace on every mcp_tool job, long after startup.
    Re-reading the baseline then would take whatever is installed by that time
    as the code being served, which is how the gate would miss an upgrade."""
    from _mcp_server_helpers import _keep_server_command_line_state
    from mempalace import mcp_server

    _keep_server_command_line_state(monkeypatch)
    baseline = {"mempalace": "0", "chromadb": "0"}
    monkeypatch.setattr(mcp_server, "_STALE_LIBRARY_WATCHED_DISTS", ("mempalace", "chromadb"))
    monkeypatch.setattr(mcp_server, "_STARTUP_DIST_VERSIONS", baseline)
    monkeypatch.setattr(mcp_server, "_STARTUP_DIST_ERRORS", {})
    monkeypatch.setattr(mcp_server, "_STARTUP_DIST_STATE", (baseline, {}))
    # The environment moved on after import; only --backend may act on that.
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")

    mcp_server._apply_server_flags(palace=str(tmp_path / "palace"))

    assert mcp_server._STALE_LIBRARY_WATCHED_DISTS == ("mempalace", "chromadb")
    assert mcp_server._STARTUP_DIST_VERSIONS is baseline
