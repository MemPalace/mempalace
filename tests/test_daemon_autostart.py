"""Hook-side daemon path: a stdlib-only enqueue inside the hook budget."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mempalace import daemon_queue, hooks_cli

HEAVY = ("chromadb", "numpy", "onnxruntime", "mempalace.palace", "mempalace.backends")


def _run_py(code: str, env: dict | None = None, timeout: float = 60) -> str:
    out = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
        check=True,
    )
    return out.stdout.strip()


def test_queue_module_imports_no_storage_stack():
    # A fresh interpreter: this pytest process already has numpy loaded.
    loaded = _run_py(
        f"""
        import sys, json
        import mempalace.daemon_queue, mempalace.hooks_cli
        print(json.dumps([m for m in {HEAVY!r} if m in sys.modules]))
        """
    )
    assert json.loads(loaded) == []


def test_hook_daemon_enqueue_imports_no_storage_stack_and_fits_the_budget(tmp_path):
    """The whole hook-side daemon route -- routing, probe, enqueue -- stays off
    the storage stack and inside the 500 ms hook budget."""
    palace = tmp_path / "palace"
    palace.mkdir()
    state_root = tmp_path / "state"
    # A queue that already exists, as on every hook after the first.
    with patch.dict(os.environ, {daemon_queue.STATE_ROOT_ENV: str(state_root)}):
        daemon_queue.QueueStore(daemon_queue.queue_path(str(palace)))
    out = _run_py(
        f"""
        import json, sys, time
        t0 = time.perf_counter()
        from mempalace import daemon_queue, hooks_cli
        with hooks_cli._hook_trigger("stop"), hooks_cli._hook_write_routing_context() as r:
            job = hooks_cli._submit_daemon_job(
                "mine", {{"source": "/x", "mode": "convos"}}, dedupe_key="k"
            )
        elapsed = time.perf_counter() - t0
        print(json.dumps({{
            "elapsed": elapsed,
            "heavy": [m for m in {HEAVY!r} if m in sys.modules],
            "job": job,
        }}))
        """,
        env={
            "MEMPALACE_PALACE_PATH": str(palace),
            "MEMPALACE_HOOK_WRITE_ROUTING": "prefer",
            daemon_queue.STATE_ROOT_ENV: str(state_root),
            "HOME": str(tmp_path / "home"),
        },
    )
    res = json.loads(out)
    assert res["heavy"] == []
    assert res["job"]["state"] == "queued"
    # Generous on CI runners; the real budget everywhere else.
    budget = 2.0 if os.environ.get("CI") else 0.5
    assert res["elapsed"] < budget, res["elapsed"]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv(daemon_queue.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    return str(palace)


def test_daemon_diary_job_writes_the_hook_ack_only_after_filing(tmp_path, monkeypatch):
    from mempalace import service

    monkeypatch.setattr(hooks_cli, "STATE_DIR", tmp_path)
    ack = {"messages_folded": 4, "ack_ts": "t", "session_id": "s/1", "exchange_count": 9}

    import mempalace.mcp_server as mcp

    monkeypatch.setattr(mcp, "tool_diary_write", lambda **kw: {"success": False, "error": "x"})
    service.run_diary_write({"entry": "e", "hook_ack": ack})
    assert not (tmp_path / "last_checkpoint").exists()

    monkeypatch.setattr(mcp, "tool_diary_write", lambda **kw: {"success": True, "entry_id": "d"})
    service.run_diary_write({"entry": "e", "hook_ack": ack})
    assert json.loads((tmp_path / "last_checkpoint").read_text())["msgs"] == 4
    markers = list(Path(tmp_path).glob("*_last_save"))
    assert [m.read_text() for m in markers] == ["9"]


def test_contended_queue_returns_within_hook_budget(state, monkeypatch):
    import sqlite3

    monkeypatch.setenv("MEMPALACE_PALACE_PATH", state)
    path = daemon_queue.queue_path(state)
    store = daemon_queue.QueueStore(path)
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # A maintenance writer can hold SQLite longer than the hook's budget.
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            hooks_cli._submit_daemon_job("mine", {"source": "s"})
        assert time.monotonic() - t0 < 0.5
