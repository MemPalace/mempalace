"""CLI include overrides must persist through the hub that owns the palace."""

import os
import subprocess
import sys
import threading
from pathlib import Path

from mempalace import server_registry


def test_cli_mines_ignored_note_while_http_hub_holds_writer_lease(tmp_path, monkeypatch, config):
    from mempalace import mcp_server, palace

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", config.palace_path)
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "test-hub-token")
    monkeypatch.setenv("MEMPALACE_CLI_WRITE_ROUTING", "direct")
    monkeypatch.delenv("MEMPALACE_HUB_FORWARD", raising=False)
    project = tmp_path / "project"
    notes = project / ".agents" / "handoffs"
    notes.mkdir(parents=True)
    (project / ".gitignore").write_text(".agents/\n", encoding="utf-8")
    target = notes / "note.md"
    content = (
        "The checkpoint records a successful deployment of the local service. "
        "The next review must retain the original request and its exact source. "
        "The operator confirmed that the saved note remains readable after the client exits.\n"
    )
    target.write_text(content, encoding="utf-8")
    (notes / "unrelated.md").write_text("This ignored file must remain excluded.\n")

    ok, reason = mcp_server._acquire_mcp_writer_lock()
    assert ok, reason
    httpd = mcp_server._build_http_server("127.0.0.1", 0)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    server_registry.write_serverinfo(
        config.palace_path,
        host="127.0.0.1",
        port=httpd.server_address[1],
        scheme="http",
        read_only=False,
        capabilities=["mine_include_ignored"],
    )
    try:
        # A separate process cannot borrow the hub's process-local lock credit.
        # Before the fix, this exact command refuses the held writer lease.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mempalace.cli",
                "mine",
                str(project),
                "--mode",
                "projects",
                "--include-ignored",
                ".agents/handoffs/note.md",
                "--limit",
                "0",
                "--agent",
                "test-agent",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "forwarding mine to palace hub" in result.stdout
        assert mcp_server._MCP_WRITER_LOCK_CM is not None
        collection = palace.get_collection(config.palace_path)
        stored = collection.get(include=["documents", "metadatas"])
        assert len(stored["ids"]) == 1, stored
        assert stored["documents"] == [content.strip()]
        assert stored["metadatas"][0]["source_file"] == str(target)
        assert stored["metadatas"][0]["added_by"] == "test-agent"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        server_registry.clear_serverinfo(config.palace_path)
        mcp_server._release_mcp_writer_lock()
