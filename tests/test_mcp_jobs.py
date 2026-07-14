import os
import subprocess

from mempalace import mcp_jobs


class FakeClient:
    def __init__(self, submitted=None, jobs=None, listed=None):
        self.submitted = submitted or {"id": "job-1", "state": "queued"}
        self.jobs = list(jobs or [])
        self.listed = listed or []
        self.submit_calls = []
        self.list_calls = []

    def submit(self, kind, payload, dedupe_key=None, priority=0):
        self.submit_calls.append((kind, payload, dedupe_key, priority))
        return dict(self.submitted)

    def get_job(self, job_id):
        if self.jobs:
            return dict(self.jobs.pop(0))
        return {**self.submitted, "id": job_id}

    def list_jobs(self, limit=20, state=None, kind=None):
        self.list_calls.append((limit, state, kind))
        return list(self.listed)


def _init_repo(path):
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("fixture\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "fixture"],
        check=True,
        capture_output=True,
    )


def test_mine_dedupe_key_is_stable_for_symlinked_source(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source)

    direct = mcp_jobs.mine_dedupe_key(
        "/palace", {"source": str(source), "mode": "projects", "wing": "se-code"}
    )
    symlinked = mcp_jobs.mine_dedupe_key(
        "/palace", {"source": str(alias), "mode": "projects", "wing": "se-code"}
    )

    assert direct == symlinked


def test_mine_dedupe_key_changes_with_project_config(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    config = source / "mempalace.yaml"
    config.write_text("wing: first\n")
    first = mcp_jobs.mine_dedupe_key("/palace", {"source": str(source), "mode": "projects"})
    config.write_text("wing: second\n")
    second = mcp_jobs.mine_dedupe_key("/palace", {"source": str(source), "mode": "projects"})
    assert first != second


def test_detect_linked_worktree_returns_primary_checkout(tmp_path):
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    primary.mkdir()
    _init_repo(primary)
    subprocess.run(
        ["git", "-C", str(primary), "worktree", "add", str(linked)],
        check=True,
        capture_output=True,
    )

    is_linked, canonical = mcp_jobs.detect_linked_worktree(str(linked))

    assert is_linked is True
    assert canonical == str(primary.resolve())
    assert mcp_jobs.detect_linked_worktree(str(primary)) == (False, str(primary.resolve()))


def test_submit_mine_returns_accepted_job_and_deduplication(monkeypatch, tmp_path):
    source = tmp_path / "repo"
    source.mkdir()

    def fake_submit(kind, payload, **kwargs):
        assert kind == "mine"
        assert kwargs["wait"] is False
        assert kwargs["auto_start"] is False
        assert kwargs["dedupe_key"] == mcp_jobs.mine_dedupe_key("/palace", payload)
        return {
            "id": "job-1",
            "state": "queued",
            "created_at": "2026-07-14T00:00:00+00:00",
            "deduplicated": True,
        }

    monkeypatch.setattr(mcp_jobs, "submit_job", fake_submit)
    result = mcp_jobs.submit_mine(
        "/palace",
        {"source": str(source), "mode": "projects", "wing": "se-code", "priority": 0},
    )

    assert result == {
        "success": True,
        "accepted": True,
        "job_id": "job-1",
        "state": "queued",
        "deduplicated": True,
        "source": os.path.realpath(source),
        "mode": "projects",
        "wing": "se-code",
        "submitted_at": "2026-07-14T00:00:00+00:00",
    }


def test_submit_mine_rejects_linked_worktree_by_default(monkeypatch, tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    monkeypatch.setattr(
        mcp_jobs, "detect_linked_worktree", lambda _source: (True, "/canonical/repo")
    )

    result = mcp_jobs.submit_mine(
        "/palace",
        {"source": str(source), "mode": "projects", "allow_linked_worktree": False},
    )

    assert result["success"] is False
    assert result["error_class"] == "LinkedWorktreeRejected"
    assert result["canonical_root"] == "/canonical/repo"


def test_submit_mine_fails_fast_when_daemon_is_unavailable(monkeypatch, tmp_path):
    source = tmp_path / "repo"
    source.mkdir()

    def unavailable(*args, **kwargs):
        raise mcp_jobs.DaemonError("offline")

    monkeypatch.setattr(mcp_jobs, "submit_job", unavailable)
    result = mcp_jobs.submit_mine(
        "/palace",
        {"source": str(source), "mode": "projects"},
    )

    assert result["success"] is False
    assert result["error_class"] == "DaemonUnavailable"
    assert "daemon start" in result["hint"]


def test_daemon_write_returns_completed_result_on_fast_path(monkeypatch):
    client = FakeClient(
        submitted={"id": "j1", "state": "queued"},
        jobs=[
            {
                "id": "j1",
                "state": "succeeded",
                "result": {"success": True, "drawer_id": "d1", "exit_code": 0},
            }
        ],
    )
    monkeypatch.setattr(mcp_jobs, "get_client_if_running", lambda *a, **k: client)

    result = mcp_jobs.dispatch_daemon_write(
        "mempalace_add_drawer",
        {"wing": "se", "room": "decisions", "content": "verbatim"},
        palace_path="/palace",
        fast_wait_ms=250,
    )

    assert result == {
        "success": True,
        "drawer_id": "d1",
        "exit_code": 0,
        "job_id": "j1",
        "delivery": "completed",
    }
    assert client.submit_calls == [
        (
            "mcp_tool",
            {
                "name": "mempalace_add_drawer",
                "arguments": {"wing": "se", "room": "decisions", "content": "verbatim"},
            },
            None,
            0,
        )
    ]


def test_daemon_write_returns_accepted_when_not_done(monkeypatch):
    client = FakeClient(
        submitted={"id": "j1", "state": "queued"},
        jobs=[{"id": "j1", "state": "running"}],
    )
    monkeypatch.setattr(mcp_jobs, "get_client_if_running", lambda *a, **k: client)
    monkeypatch.setattr(mcp_jobs.time, "sleep", lambda _seconds: None)
    ticks = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(mcp_jobs.time, "monotonic", lambda: next(ticks, 1.0))

    result = mcp_jobs.dispatch_daemon_write(
        "mempalace_checkpoint",
        {"items": []},
        palace_path="/palace",
        fast_wait_ms=1,
    )

    assert result == {
        "success": True,
        "accepted": True,
        "job_id": "j1",
        "state": "running",
        "delivery": "durable_queue",
    }


def test_job_status_and_list_use_daemon_without_exposing_payload(monkeypatch):
    client = FakeClient(
        jobs=[{"id": "j1", "state": "running", "progress": {"phase": "mining"}}],
        listed=[{"id": "j1", "kind": "mine", "state": "running"}],
    )
    monkeypatch.setattr(mcp_jobs, "get_client_if_running", lambda *a, **k: client)

    status = mcp_jobs.tool_job_status("j1", palace_path="/palace")
    listed = mcp_jobs.tool_list_jobs(
        limit=5,
        state="running",
        kind="mine",
        palace_path="/palace",
    )

    assert status == {
        "success": True,
        "job": {"id": "j1", "state": "running", "progress": {"phase": "mining"}},
    }
    assert listed == {
        "success": True,
        "jobs": [{"id": "j1", "kind": "mine", "state": "running"}],
    }
    assert client.list_calls == [(5, "running", "mine")]


def test_job_tools_fail_closed_when_daemon_is_unavailable(monkeypatch):
    monkeypatch.setattr(mcp_jobs, "get_client_if_running", lambda *a, **k: None)

    result = mcp_jobs.tool_job_status("j1", palace_path="/palace")

    assert result["success"] is False
    assert result["error_class"] == "DaemonUnavailable"
