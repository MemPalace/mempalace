import os
import subprocess

from mempalace import mcp_jobs


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
