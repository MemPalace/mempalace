import subprocess
import sys


def test_wal_import_has_no_mcp_server_side_effect():
    """Importing mempalace.wal must NOT import mempalace.mcp_server.

    mcp_server installs MCP stdio protection at import time (os.dup2(2, 1) and
    sys.stdout = sys.stderr). The CLI sync path and the daemon service layer
    obtain _wal_log from mempalace.wal precisely so they can audit writes
    without triggering that process-global redirect. Run in a fresh subprocess
    so the already-imported mcp_server in this test session can't mask a
    regression.
    """
    code = (
        "import sys\n"
        "import mempalace.wal\n"
        "assert 'mempalace.mcp_server' not in sys.modules, "
        "'importing mempalace.wal pulled in mempalace.mcp_server'\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_wal_log_redacts_and_writes(tmp_path, monkeypatch):
    """_wal_log lives in mempalace.wal now; smoke-test redaction + write there."""
    import json

    from mempalace import wal

    wal_file = tmp_path / "wal" / "write_log.jsonl"
    monkeypatch.setattr(wal, "_WAL_FILE", wal_file)
    monkeypatch.setattr(wal, "_WAL_INITIALIZED_DIR", None)

    wal._wal_log("op", {"entry": "secret diary text", "safe": "ok"})

    entry = json.loads(wal_file.read_text().strip())
    assert entry["operation"] == "op"
    assert entry["params"]["entry"].startswith("[REDACTED")
    assert entry["params"]["safe"] == "ok"
