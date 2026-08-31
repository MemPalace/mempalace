"""stdio MCP writer marker.

The stdio MCP replicas an agent connects through contend for the per-palace
single-writer lease via ``flock`` but are not HTTP hubs, so they are invisible
to the hub-facing ``serverinfo.json``. A read-only peer would report an opaque
"Peer MCP writer active" with no way to say *which* replica holds the lease.
These tests cover the marker that names the current stdio writer so the
refusal can be diagnostic, and that it stays conservative (never claims a
dead writer, never clears another process's record).
"""

import os

import pytest

from mempalace import mcp_server, server_registry


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MEMPALACE_HUB_FORWARD", raising=False)
    return tmp_path


class TestStdioWriterRegistry:
    def test_write_then_read_roundtrip(self, isolated_home):
        palace = str(isolated_home / "palace")
        server_registry.write_stdio_writer(palace)
        info = server_registry.read_stdio_writer(palace)
        assert info is not None
        assert info["pid"] == os.getpid()
        assert info["role"] == "writer"
        assert info["palace_path"].replace("\\", "/") == palace.replace("\\", "/")

    def test_read_none_without_marker(self, isolated_home):
        palace = str(isolated_home / "palace")
        assert server_registry.read_stdio_writer(palace) is None

    def test_clear_only_removes_own_pid(self, isolated_home):
        palace = str(isolated_home / "palace")
        server_registry.write_stdio_writer(palace)
        # Simulate a newer writer's record by rewriting with a different pid.
        path = server_registry.stdio_writer_path(palace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"pid": 99999999, "role": "writer", "started_at": "x", "palace_path": %r}\n' % palace,
            encoding="utf-8",
        )
        server_registry.clear_stdio_writer(palace)
        # clear must not unlink a record that belongs to another pid -- the
        # file stays on disk (its pid is stale so read ignores it, but the
        # marker is not actively deleted by a foreign process).
        assert path.exists()

    def test_clear_removes_live_own_pid(self, isolated_home):
        palace = str(isolated_home / "palace")
        server_registry.write_stdio_writer(palace)
        assert server_registry.read_stdio_writer(palace) is not None
        server_registry.clear_stdio_writer(palace)
        assert server_registry.read_stdio_writer(palace) is None

    def test_dead_pid_is_ignored(self, isolated_home):
        palace = str(isolated_home / "palace")
        path = server_registry.stdio_writer_path(palace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"pid": 99999999, "role": "writer", "started_at": "x", "palace_path": %r}\n' % palace,
            encoding="utf-8",
        )
        # A pid that is almost certainly not alive should be treated as stale.
        assert server_registry.read_stdio_writer(palace) is None


class TestRefusalNamesWriter:
    def test_refusal_includes_writer_info(self, isolated_home, monkeypatch):
        palace = str(isolated_home / "palace")
        server_registry.write_stdio_writer(palace)
        monkeypatch.setattr(mcp_server, "_config", type("C", (), {"palace_path": palace})())

        def denied(*_a, **_k):
            return (False, "another mempalace writer already holds the palace lock")

        monkeypatch.setattr(mcp_server, "_acquire_mcp_writer_lock", denied)
        result = mcp_server._mcp_peer_writer_refusal(1, "mempalace_add_drawer")
        assert result is not None
        err = result["error"]
        assert "Peer MCP writer active" in err["message"]
        assert str(os.getpid()) in err["message"]
        assert err["data"]["writer"]["pid"] == os.getpid()

    def test_refusal_without_writer_marker(self, isolated_home, monkeypatch):
        palace = str(isolated_home / "palace")

        def denied(*_a, **_k):
            return (False, "blocked")

        monkeypatch.setattr(mcp_server, "_config", type("C", (), {"palace_path": palace})())
        monkeypatch.setattr(mcp_server, "_acquire_mcp_writer_lock", denied)
        result = mcp_server._mcp_peer_writer_refusal(1, "mempalace_add_drawer")
        assert result is not None
        assert result["error"]["data"]["writer"] is None
