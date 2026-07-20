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


def _wal_at(tmp_path, monkeypatch, full_payload):
    """Point the WAL at tmp_path with an explicit payload mode.

    The payload mode is a process-lifetime cache, so it is set through
    monkeypatch to keep it from leaking into other tests in the session.
    """
    from mempalace import wal

    wal_file = tmp_path / "wal" / "write_log.jsonl"
    monkeypatch.setattr(wal, "_WAL_FILE", wal_file)
    monkeypatch.setattr(wal, "_WAL_INITIALIZED_DIR", None)
    monkeypatch.setattr(wal, "_WAL_FULL_PAYLOAD", full_payload)
    return wal, wal_file


def test_wal_stores_full_payload_when_enabled(tmp_path, monkeypatch):
    """With the flag on, content-bearing params are logged verbatim.

    This is the property that makes a silent storage-layer drop recoverable:
    the 2026-04-30 MemPalace data loss was unrecoverable precisely because the
    WAL held "[REDACTED 200 chars]" instead of the text of each lost write.
    """
    import json

    wal, wal_file = _wal_at(tmp_path, monkeypatch, True)

    secret = "the exact diary text that must survive a dropped write"
    wal._wal_log("diary_write", {"entry": secret, "query": "find me", "safe": "ok"})

    entry = json.loads(wal_file.read_text().strip())
    assert entry["params"]["entry"] == secret
    assert not entry["params"]["entry"].startswith("[REDACTED")
    assert entry["params"]["query"] == "find me"
    assert entry["params"]["safe"] == "ok"


def test_wal_redacts_when_flag_disabled(tmp_path, monkeypatch):
    """Backward compatibility: flag off reproduces the historical behaviour."""
    import json

    wal, wal_file = _wal_at(tmp_path, monkeypatch, False)

    wal._wal_log("diary_write", {"entry": "secret diary text", "safe": "ok"})

    entry = json.loads(wal_file.read_text().strip())
    assert entry["params"]["entry"] == "[REDACTED 17 chars]"
    assert entry["params"]["safe"] == "ok"


def test_wal_full_payload_reads_env(tmp_path, monkeypatch):
    """The setting resolves from the environment and caches for the process."""
    from mempalace import wal

    monkeypatch.setattr(wal, "_WAL_FULL_PAYLOAD", None)
    monkeypatch.setenv("MEMPALACE_WAL_STORE_FULL_PAYLOAD", "true")
    assert wal._store_full_payload() is True

    # Cached: flipping the env afterwards must not change the resolved value.
    monkeypatch.setenv("MEMPALACE_WAL_STORE_FULL_PAYLOAD", "false")
    assert wal._store_full_payload() is True


def test_wal_full_payload_defaults_off_and_fails_closed(tmp_path, monkeypatch):
    """Unset env redacts; a broken config lookup redacts rather than leaking."""
    from mempalace import wal

    monkeypatch.setattr(wal, "_WAL_FULL_PAYLOAD", None)
    monkeypatch.delenv("MEMPALACE_WAL_STORE_FULL_PAYLOAD", raising=False)
    monkeypatch.setattr(wal, "_WAL_FILE", tmp_path / "wal" / "write_log.jsonl")
    assert wal._store_full_payload() is False

    monkeypatch.setattr(wal, "_WAL_FULL_PAYLOAD", None)
    import mempalace.config as config_mod

    def _boom(*args, **kwargs):
        raise OSError("config unreadable")

    monkeypatch.setattr(config_mod, "MempalaceConfig", _boom)
    assert wal._store_full_payload() is False


def test_diary_write_payload_recoverable_from_wal(tmp_path, monkeypatch, config, palace_path, kg):
    """End-to-end: a real diary_write leaves a reconstructable WAL record.

    Exercises the actual tool rather than _wal_log directly, so the redact-key
    name used by tool_diary_write ("entry_preview") stays covered — that key is
    what was redacted during the 2.5-month silent-drop window.
    """
    import json

    from tests.test_mcp_server import _patch_mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    wal, wal_file = _wal_at(tmp_path, monkeypatch, True)

    from mempalace.mcp_server import tool_diary_write

    secret = "SESSION:2026-07-19|payload that must be recoverable|★★★"
    result = tool_diary_write(agent_name="tester", entry=secret, topic="wal")
    assert result["success"] is True

    entries = [json.loads(line) for line in wal_file.read_text().splitlines() if line.strip()]
    diary = [e for e in entries if e["operation"] == "diary_write"]
    assert diary, f"no diary_write WAL record in {entries}"
    previews = [e["params"]["entry_preview"] for e in diary]
    assert not any(p.startswith("[REDACTED") for p in previews), previews
    assert any(secret in p for p in previews), previews
