"""CLI --include-subagents must persist through the hub that owns the palace."""

import os
import subprocess
import sys
import threading
from pathlib import Path

from mempalace import server_registry


def _exchanges(topic):
    return (
        f"> What did the {topic} find?\n"
        f"The {topic} found the configuration loader in the settings module.\n\n"
        f"> Where does the {topic} report go?\n"
        f"The {topic} report goes into the parent session as a short summary.\n\n"
        f"> Should the {topic} keep the original wording?\n"
        f"Yes, the {topic} keeps the exact words the user wrote.\n"
    )


def test_cli_mines_subagent_transcripts_while_http_hub_holds_writer_lease(
    tmp_path, monkeypatch, config
):
    from mempalace import mcp_server, palace

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", config.palace_path)
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "test-hub-token")
    monkeypatch.setenv("MEMPALACE_CLI_WRITE_ROUTING", "direct")
    monkeypatch.delenv("MEMPALACE_HUB_FORWARD", raising=False)
    session = tmp_path / "convos" / "session-a"
    (session / "subagents").mkdir(parents=True)
    parent = session / "parent.txt"
    parent.write_text(_exchanges("parent session"), encoding="utf-8")
    subagent = session / "subagents" / "agent-explore.txt"
    subagent.write_text(_exchanges("explore subagent"), encoding="utf-8")

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
        capabilities=["mine_include_subagents"],
    )
    try:
        # A separate process cannot borrow the hub's process-local lock credit,
        # so this mine must run inside the hub. Before the fix the hub ran it
        # without the flag and the subagents/ folder was skipped.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mempalace.cli",
                "mine",
                str(tmp_path / "convos"),
                "--mode",
                "convos",
                "--include-subagents",
                "--agent",
                "test-agent",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "forwarding mine to palace hub" in result.stdout
        assert mcp_server._MCP_WRITER_LOCK_CM is not None
        collection = palace.get_collection(config.palace_path)
        stored = collection.get(include=["metadatas"])
        sources = {meta["source_file"] for meta in stored["metadatas"]}
        assert sources == {str(parent), str(subagent)}, sources
        assert {meta["added_by"] for meta in stored["metadatas"]} == {"test-agent"}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        server_registry.clear_serverinfo(config.palace_path)
        mcp_server._release_mcp_writer_lock()
