"""
test_mcp_mine.py — Tests for the ``mempalace_mine`` MCP tool (#1662).

Mining was previously CLI-only (``mempalace mine``); non-Claude-Code MCP clients
(Desktop Commander, LM Studio, Aionui) had no MCP-callable mine. ``tool_mine``
wraps the same in-process miners the CLI uses — projects / convos / extract —
synchronously, mirroring the ``tool_sync`` contract.

The miners print progress + a summary to stdout, which in the MCP server is the
JSON-RPC channel. ``tool_mine`` therefore redirects stdout at the file-descriptor
level around the miner and returns the text as an opaque ``output`` field rather
than letting it corrupt the protocol. These tests assert the dispatch/return
contract, that convos mining actually files drawers (the #1662 gap), and that the
stdout isolation holds.
"""

import os

import chromadb


def _patch(monkeypatch, config):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ── Registration ─────────────────────────────────────────────────────────


def test_registered_in_tools():
    from mempalace import mcp_server

    assert "mempalace_mine" in mcp_server.TOOLS
    entry = mcp_server.TOOLS["mempalace_mine"]
    assert entry["handler"] is mcp_server.tool_mine
    assert entry["input_schema"]["required"] == ["source"]


# ── Guard rails ──────────────────────────────────────────────────────────


def test_no_palace_returns_structured_error(monkeypatch):
    from mempalace import mcp_server

    class _EmptyConfig:
        palace_path = ""
        collection_name = "mempalace_drawers"

    monkeypatch.setattr(mcp_server, "_config", _EmptyConfig())
    result = mcp_server.tool_mine(source="/tmp")
    assert result["success"] is False
    assert "error" in result


def test_invalid_mode_returns_structured_error(monkeypatch, config, tmp_dir):
    from mempalace import mcp_server

    _patch(monkeypatch, config)
    src = os.path.join(tmp_dir, "src")
    os.makedirs(src)
    result = mcp_server.tool_mine(source=src, mode="bogus")
    assert result["success"] is False
    assert "invalid mode" in result["error"].lower()


def test_missing_source_dir_returns_structured_error(monkeypatch, config):
    from mempalace import mcp_server

    _patch(monkeypatch, config)
    result = mcp_server.tool_mine(source="/nonexistent/path/xyz")
    assert result["success"] is False
    assert "source" in result["error"].lower()


# ── Dispatch + return contract ───────────────────────────────────────────


def test_dry_run_projects_returns_success_and_output(monkeypatch, config, tmp_dir):
    from mempalace import mcp_server

    _patch(monkeypatch, config)
    src = os.path.join(tmp_dir, "proj")
    os.makedirs(src)
    _write(os.path.join(src, "notes.md"), "# Title\n\n" + ("Some real content. " * 40))

    result = mcp_server.tool_mine(source=src, mode="projects", dry_run=True)
    assert result["success"] is True
    assert result["mode"] == "projects"
    assert result["dry_run"] is True
    assert isinstance(result["output"], str) and result["output"]


def test_convos_mode_files_drawers(monkeypatch, config, tmp_dir):
    """The #1662 core ask: mine conversation transcripts via MCP.

    Proves the tool eliminates the gap rather than masking it — after a real
    convos mine the palace collection actually holds the drawers.
    """
    from mempalace import mcp_server

    _patch(monkeypatch, config)
    src = os.path.join(tmp_dir, "convos")
    os.makedirs(src)
    _write(
        os.path.join(src, "chat.txt"),
        "> What is memory?\nMemory is persistence.\n\n"
        "> Why does it matter?\nIt enables continuity across sessions.\n\n"
        "> How do we build it?\nWith structured verbatim storage.\n",
    )

    result = mcp_server.tool_mine(source=src, mode="convos", wing="test_convos")
    assert result["success"] is True
    assert result["mode"] == "convos"
    assert result["dry_run"] is False

    client = chromadb.PersistentClient(path=config.palace_path)
    try:
        col = client.get_collection("mempalace_drawers")
        assert col.count() >= 2
    finally:
        del client


def test_stdout_captured_not_leaked_to_fd(monkeypatch, config, tmp_dir, capfd):
    """Miner stdout must land in ``output``, never on the real fd-1 JSON-RPC
    channel. ``tool_mine`` redirects fd 1 around the in-process miner."""
    from mempalace import mcp_server

    _patch(monkeypatch, config)
    src = os.path.join(tmp_dir, "convos")
    os.makedirs(src)
    _write(
        os.path.join(src, "chat.txt"),
        "> Q one?\nAnswer one is reasonably long so it forms a chunk here.\n\n"
        "> Q two?\nAnswer two is also long enough to be filed as a drawer here.\n",
    )

    result = mcp_server.tool_mine(source=src, mode="convos", wing="cap", dry_run=True)
    captured = capfd.readouterr()
    assert "Done." in result["output"]
    assert "Done." not in captured.out


def test_mine_already_running_surfaces_structured_error(monkeypatch, config, tmp_dir):
    """A held palace lock (MineAlreadyRunning) surfaces as a structured
    already-running error, mirroring tool_sync."""
    from mempalace import mcp_server
    from mempalace.palace import MineAlreadyRunning

    _patch(monkeypatch, config)
    src = os.path.join(tmp_dir, "proj")
    os.makedirs(src)
    _write(os.path.join(src, "a.md"), "content " * 50)

    def _boom(*args, **kwargs):
        raise MineAlreadyRunning("held by pid 999")

    monkeypatch.setattr("mempalace.miner.mine", _boom)
    result = mcp_server.tool_mine(source=src, mode="projects")
    assert result["success"] is False
    assert result.get("error_class") == "LockHeldByOtherProcess"


def test_large_output_is_tail_truncated(monkeypatch, config, tmp_dir):
    """A very large miner summary is tail-trimmed (and flagged, never silently)
    so the MCP response stays bounded."""
    from mempalace import mcp_server

    _patch(monkeypatch, config)
    src = os.path.join(tmp_dir, "proj")
    os.makedirs(src)

    def _chatty(*args, **kwargs):
        print("X" * 5000)
        return None

    monkeypatch.setattr("mempalace.miner.mine", _chatty)
    result = mcp_server.tool_mine(source=src, mode="projects")
    assert result["success"] is True
    assert result["output_truncated"] is True
    assert len(result["output"]) == 4000


def test_import_error_outside_extract_is_not_mislabeled(monkeypatch, config, tmp_dir):
    """An ImportError outside extract mode is a real bug, not a missing extra —
    it must not be labelled MissingDependency."""
    from mempalace import mcp_server

    _patch(monkeypatch, config)
    src = os.path.join(tmp_dir, "proj")
    os.makedirs(src)

    def _broken(*args, **kwargs):
        raise ImportError("no module named 'totally_internal'")

    monkeypatch.setattr("mempalace.miner.mine", _broken)
    result = mcp_server.tool_mine(source=src, mode="projects")
    assert result["success"] is False
    assert result.get("error_class") == "ImportError"
    assert "mine failed" in result["error"]


def test_extract_missing_dependency_is_named(monkeypatch, config, tmp_dir):
    """extract mode surfaces a MissingDependency error pointing at the extra."""
    from mempalace import mcp_server

    _patch(monkeypatch, config)
    src = os.path.join(tmp_dir, "docs")
    os.makedirs(src)

    def _no_extra(*args, **kwargs):
        raise ImportError("No module named 'markitdown'")

    monkeypatch.setattr("mempalace.format_miner.mine_formats", _no_extra)
    result = mcp_server.tool_mine(source=src, mode="extract")
    assert result["success"] is False
    assert result.get("error_class") == "MissingDependency"
    assert "mempalace[extract]" in result["error"]


def test_system_exit_from_miner_does_not_kill_server(monkeypatch, config, tmp_dir):
    """miner.mine turns Ctrl-C into sys.exit(130); in-process that SystemExit
    would escape the protocol loop (which only catches Exception) and kill the
    server. tool_mine converts it to a structured error instead."""
    from mempalace import mcp_server

    _patch(monkeypatch, config)
    src = os.path.join(tmp_dir, "proj")
    os.makedirs(src)

    def _exit(*args, **kwargs):
        raise SystemExit(130)

    monkeypatch.setattr("mempalace.miner.mine", _exit)
    result = mcp_server.tool_mine(source=src, mode="projects")
    assert result["success"] is False
    assert result.get("error_class") == "Interrupted"


def test_generic_exception_carries_error_class(monkeypatch, config, tmp_dir):
    """An unexpected miner failure is surfaced with its exception type so the
    caller can distinguish error kinds."""
    from mempalace import mcp_server

    _patch(monkeypatch, config)
    src = os.path.join(tmp_dir, "proj")
    os.makedirs(src)

    def _boom(*args, **kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr("mempalace.miner.mine", _boom)
    result = mcp_server.tool_mine(source=src, mode="projects")
    assert result["success"] is False
    assert "mine failed" in result["error"]
    assert result.get("error_class") == "RuntimeError"
