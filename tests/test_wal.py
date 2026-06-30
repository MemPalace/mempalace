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


def test_wal_ensure_is_idempotent_and_cached(tmp_path, monkeypatch):
    """_ensure_wal hardens the dir once, then short-circuits on the cached path.

    Covers the cache-hit early return (wal.py:58): once _WAL_INITIALIZED_DIR
    matches the WAL dir, a second call must not touch the filesystem again. This
    is what stops a persistent chmod failure on a restricted FS from being
    retried on every single write.
    """
    from pathlib import Path

    from mempalace import wal

    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    monkeypatch.setattr(wal, "_WAL_FILE", wal_dir / "write_log.jsonl")
    monkeypatch.setattr(wal, "_WAL_INITIALIZED_DIR", None)

    wal._ensure_wal()
    assert wal._WAL_INITIALIZED_DIR == wal_dir

    # After caching, a second call must return before reaching any chmod/mkdir.
    def _boom(self, *args, **kwargs):
        raise AssertionError("filesystem touched again after dir was cached")

    monkeypatch.setattr(Path, "chmod", _boom)
    monkeypatch.setattr(Path, "mkdir", _boom)
    wal._ensure_wal()  # must hit the cached early-return, not raise


def test_wal_log_never_raises_when_write_fails(tmp_path, monkeypatch, caplog):
    """A WAL write failure is logged and swallowed, never crashing the caller.

    Covers wal.py:96-97 — the module docstring and _wal_log both promise that
    any WAL failure is non-fatal, so a tool call is never broken by audit-log
    I/O (e.g. a full disk or a read-only filesystem).
    """
    import logging

    from mempalace import wal

    monkeypatch.setattr(wal, "_WAL_FILE", tmp_path / "wal" / "write_log.jsonl")
    monkeypatch.setattr(wal, "_WAL_INITIALIZED_DIR", None)

    def _boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(wal.os, "open", _boom)

    with caplog.at_level(logging.ERROR, logger="mempalace.wal"):
        wal._wal_log("add_drawer", {"safe": "ok"})  # must not raise

    assert any("WAL write failed" in r.getMessage() for r in caplog.records)


def test_wal_ensure_swallows_chmod_failure_on_existing_dir(tmp_path, monkeypatch):
    """A denied chmod on an existing dir is swallowed and the dir still caches.

    Covers wal.py:67-68 — the WAL dir already exists (so no FileNotFoundError),
    but chmod is denied (restricted FS). _ensure_wal must not raise and must
    cache the dir so the failing chmod is not retried on every write.
    """
    from pathlib import Path

    from mempalace import wal

    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    monkeypatch.setattr(wal, "_WAL_FILE", wal_dir / "write_log.jsonl")
    monkeypatch.setattr(wal, "_WAL_INITIALIZED_DIR", None)

    def _denied(self, *args, **kwargs):
        raise OSError("operation not permitted")

    monkeypatch.setattr(Path, "chmod", _denied)

    wal._ensure_wal()  # must not raise
    assert wal._WAL_INITIALIZED_DIR == wal_dir


def test_wal_ensure_swallows_mkdir_failure(tmp_path, monkeypatch):
    """A failed fallback mkdir is swallowed and the dir still caches.

    Covers wal.py:65-66 — chmod raises FileNotFoundError (dir absent), the
    fallback mkdir then fails too (read-only parent). _ensure_wal must not raise.
    """
    from pathlib import Path

    from mempalace import wal

    wal_dir = tmp_path / "missing" / "wal"
    monkeypatch.setattr(wal, "_WAL_FILE", wal_dir / "write_log.jsonl")
    monkeypatch.setattr(wal, "_WAL_INITIALIZED_DIR", None)

    def _not_found(self, *args, **kwargs):
        raise FileNotFoundError

    def _mkdir_denied(self, *args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "chmod", _not_found)
    monkeypatch.setattr(Path, "mkdir", _mkdir_denied)

    wal._ensure_wal()  # must not raise
    assert wal._WAL_INITIALIZED_DIR == wal_dir


def test_wal_log_redacts_non_string_values(tmp_path, monkeypatch):
    """Non-string values under a redact key use the plain [REDACTED] marker.

    Covers the else-branch of the redaction ternary (wal.py:80): only str values
    get the "[REDACTED N chars]" form; any other type is fully redacted without
    calling len() on it.
    """
    import json

    from mempalace import wal

    wal_file = tmp_path / "wal" / "write_log.jsonl"
    monkeypatch.setattr(wal, "_WAL_FILE", wal_file)
    monkeypatch.setattr(wal, "_WAL_INITIALIZED_DIR", None)

    wal._wal_log("kg_add", {"document": [1, 2, 3], "safe": "ok"})

    entry = json.loads(wal_file.read_text().strip())
    assert entry["params"]["document"] == "[REDACTED]"
    assert entry["params"]["safe"] == "ok"
