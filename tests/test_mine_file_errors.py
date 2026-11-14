"""A file the mine could not file is an error, not a skip.

The stale-drawer purge that follows every (re-)mine of a file fails that file
when it raises (#23 / #105): its new drawers stay marked unfinished. The mine
used to count those files as "skipped (already filed or other)" and finish as
a success, so a mine that filed nothing past its first file after a backend
failure reported the same as a clean one: the MCP tool answered
``success: true`` and the CLI exited 0.
"""

import os
import sys

import pytest

BAD = ("b.md", "d.md")


def _project(tmp_path):
    src = tmp_path / "project"
    src.mkdir()
    for name in ("a.md", "b.md", "c.md", "d.md"):
        body = " ".join(f"{name} word{j} palace drawer" for j in range(80))
        (src / name).write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
    return src


def _fail_purge_for(monkeypatch, names):
    """The stale-drawer purge raises for sources named in ``names``."""
    from mempalace import palace

    real = palace._source_file_delete_ids

    def delete_ids(collection, source_file, extract_mode, pass_contents=None):
        if os.path.basename(source_file) in names:
            raise RuntimeError("simulated closed backend handle")
        return real(collection, source_file, extract_mode, pass_contents=pass_contents)

    monkeypatch.setattr(palace, "_source_file_delete_ids", delete_ids)


def _filed(palace_path):
    """Sources whose drawers are all there: a failed purge leaves them unfinished."""
    from mempalace.palace import file_already_mined, get_collection

    col = get_collection(palace_path, create=False)
    sources = {m["source_file"] for m in col.get(include=["metadatas"])["metadatas"]}
    return {os.path.basename(s) for s in sources if file_already_mined(col, s)}


def test_project_mine_counts_purge_failures_as_errors_and_fails(
    monkeypatch, palace_path, tmp_path, capsys
):
    from mempalace import miner

    src = _project(tmp_path)
    _fail_purge_for(monkeypatch, BAD)

    with pytest.raises(Exception) as excinfo:
        miner.mine(str(src), palace_path, wing_override="w")
    out = capsys.readouterr()

    assert type(excinfo.value).__name__ == "MineFileErrors"
    assert sorted(os.path.basename(f) for f in excinfo.value.failed_files) == sorted(BAD)
    assert excinfo.value.total_files == 4
    assert "Files processed: 2" in out.out
    assert "Files failed: 2" in out.out
    assert "Files skipped (already filed or other): 0" in out.out
    assert "[error]" in out.err and "stale-drawer purge failed" in out.err
    assert _filed(palace_path) == {"a.md", "c.md"}


def test_mcp_mine_reports_success_false_with_the_failure_count(monkeypatch, config, tmp_path):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    src = _project(tmp_path)
    _fail_purge_for(monkeypatch, BAD)

    result = mcp_server.tool_mine(source=str(src), mode="projects", wing="w")

    assert result["success"] is False, result
    assert result["error_class"] == "MineFileErrors"
    assert result["files_failed"] == 2
    assert sorted(os.path.basename(f) for f in result["failed_files"]) == sorted(BAD)
    assert "2 of 4 file(s) failed" in result["error"]
    # Per-file lines go to stderr, which `output` does not capture.
    assert "the server's stderr over MCP" in result["error"]
    # The summary the mine printed still reaches the caller.
    assert "Files failed: 2" in result["output"] and "Done." in result["output"]
    assert "errors above" not in result["output"]
    assert "per-file errors went to stderr" in result["output"]


def test_mcp_mine_without_failures_still_succeeds(monkeypatch, config, tmp_path):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    src = _project(tmp_path)

    result = mcp_server.tool_mine(source=str(src), mode="projects", wing="w")

    assert result["success"] is True, result
    assert "error" not in result and "files_failed" not in result


def test_cli_mine_exits_nonzero_when_files_failed(monkeypatch, palace_path, tmp_path, capsys):
    import mempalace.cli as cli

    src = _project(tmp_path)
    _fail_purge_for(monkeypatch, BAD)
    monkeypatch.setenv("MEMPALACE_HUB_FORWARD", "0")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mempalace",
            "--palace",
            palace_path,
            "mine",
            str(src),
            "--mode",
            "projects",
            "--wing",
            "w",
        ],
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    err = capsys.readouterr().err

    assert exit_info.value.code == 1
    assert "2 of 4 file(s) failed to mine" in err


def test_convo_mine_counts_purge_failures_as_errors_and_fails(
    monkeypatch, palace_path, tmp_path, capsys
):
    from mempalace import convo_miner

    src = tmp_path / "convos"
    src.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (src / name).write_text(
            f"> What is {name}?\n{name} is a transcript long enough to chunk here.\n\n"
            f"> Why {name}?\nBecause {name} must be filed as at least one drawer.\n",
            encoding="utf-8",
        )
    _fail_purge_for(monkeypatch, ("b.txt",))

    with pytest.raises(Exception) as excinfo:
        convo_miner.mine_convos(str(src), palace_path, wing="w")
    out = capsys.readouterr().out

    assert type(excinfo.value).__name__ == "MineFileErrors"
    assert [os.path.basename(f) for f in excinfo.value.failed_files] == ["b.txt"]
    assert "Files failed: 1" in out
    assert "Files skipped (already filed): 0" in out
    assert _filed(palace_path) == {"a.txt", "c.txt"}


def test_daemon_mine_job_reports_the_failure(monkeypatch, palace_path, tmp_path):
    from mempalace import service

    src = _project(tmp_path)
    _fail_purge_for(monkeypatch, BAD)

    result = service.run_mine(
        {"source": str(src), "palace_path": palace_path, "mode": "projects", "wing": "w"}
    )

    assert result["success"] is False, result
    assert result["exit_code"] == 1
    assert "2 of 4 file(s) failed" in result["error"]
