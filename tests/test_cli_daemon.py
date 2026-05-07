"""
test_cli_daemon.py — CLI subcommand daemon routing.

Mirrors the gate in ``mempalace.hooks_cli`` and ``mempalace.mcp_server``:
when ``PALACE_DAEMON_URL`` is set and ``PALACE_DAEMON_STRICT != "0"``,
``cmd_status``, ``cmd_search``, and ``cmd_mine`` route to the daemon
(via ``/mcp`` JSON-RPC for read paths, ``/mine`` for write) instead of
opening a local chromadb client.

The local-path tests (``tests/test_cli.py``) keep working because
``tests/conftest.py`` scrubs ``PALACE_DAEMON_URL`` for the test
session — these tests opt back in via ``patch.dict``.
"""

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


# ── _daemon_strict ──────────────────────────────────────────────────────


class TestDaemonStrictGate:
    def test_returns_true_when_url_set(self):
        from mempalace.cli import _daemon_strict

        with patch.dict("os.environ", {"PALACE_DAEMON_URL": "http://x:8085"}, clear=True):
            assert _daemon_strict() is True

    def test_returns_false_when_url_unset(self):
        from mempalace.cli import _daemon_strict

        with patch.dict("os.environ", {}, clear=True):
            assert _daemon_strict() is False

    def test_strict_zero_disables(self):
        from mempalace.cli import _daemon_strict

        env = {"PALACE_DAEMON_URL": "http://x:8085", "PALACE_DAEMON_STRICT": "0"}
        with patch.dict("os.environ", env, clear=True):
            assert _daemon_strict() is False


# ── _call_daemon_tool ──────────────────────────────────────────────────


class TestCallDaemonTool:
    def test_posts_jsonrpc_tools_call_with_api_key(self):
        from mempalace.cli import _call_daemon_tool

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["api_key"] = req.get_header("X-api-key")
            return _FakeResp(
                b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\\"total_drawers\\": 7}"}]}}'
            )

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085", "PALACE_API_KEY": "k"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = _call_daemon_tool("mempalace_status", {})

        assert captured["url"] == "http://daemon.example:8085/mcp"
        assert captured["body"]["params"]["name"] == "mempalace_status"
        assert captured["api_key"] == "k"
        assert result == {"total_drawers": 7}

    def test_raises_on_jsonrpc_error(self):
        from mempalace.cli import _call_daemon_tool, DaemonError

        def fake_urlopen(req, timeout=None):
            return _FakeResp(
                b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"Unknown tool"}}'
            )

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(DaemonError):
                    _call_daemon_tool("bogus_tool", {})

    def test_raises_on_network_failure(self):
        from mempalace.cli import _call_daemon_tool, DaemonError

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
                with pytest.raises(DaemonError):
                    _call_daemon_tool("mempalace_status", {})


# ── _post_daemon_mine_cli ──────────────────────────────────────────────


class TestPostDaemonMineCli:
    def test_posts_to_mine_endpoint(self):
        from mempalace.cli import _post_daemon_mine_cli

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(b'{"returncode": 0, "stdout": "mined ok"}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085", "PALACE_API_KEY": "k"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                ok = _post_daemon_mine_cli("/some/dir", wing="w", mode="convos")

        assert ok is True
        assert captured["url"] == "http://daemon.example:8085/mine"
        assert captured["body"] == {"dir": "/some/dir", "wing": "w", "mode": "convos"}

    def test_returns_false_on_failure(self, capsys):
        from mempalace.cli import _post_daemon_mine_cli

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
                ok = _post_daemon_mine_cli("/some/dir", wing="w")

        assert ok is False
        # CLI users get errors on stderr — silent swallow is for hooks only.
        err = capsys.readouterr().err
        assert "boom" in err or "daemon" in err.lower()


# ── cmd_status routing ─────────────────────────────────────────────────


class TestCmdStatusDaemon:
    def test_routes_to_daemon_when_strict(self, capsys):
        """cmd_status must NOT call miner.status when daemon-strict; it
        prints a daemon-sourced summary instead."""
        from mempalace import cli

        def fake_urlopen(req, timeout=None):
            return _FakeResp(
                b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":'
                b'"{\\"total_drawers\\": 42, \\"wings\\": {\\"projects\\": 30, \\"sessions\\": 12}}"}]}}'
            )

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(palace=None)

        mock_miner = MagicMock()
        with patch.dict("os.environ", env, clear=True):
            with patch.dict("sys.modules", {"mempalace.miner": mock_miner}):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    cli.cmd_status(args)

        out = capsys.readouterr().out
        assert "42" in out
        assert "projects" in out
        assert "30" in out
        # Local fallback must not run.
        mock_miner.status.assert_not_called()

    def test_local_path_when_daemon_unset(self):
        """Without the env var, cmd_status delegates to miner.status as before."""
        from mempalace import cli

        with patch.dict("os.environ", {}, clear=True):
            with patch("mempalace.cli.MempalaceConfig") as mock_cfg:
                mock_cfg.return_value.palace_path = "/local/palace"
                args = argparse.Namespace(palace=None)
                mock_miner = MagicMock()
                with patch.dict("sys.modules", {"mempalace.miner": mock_miner}):
                    cli.cmd_status(args)
                    mock_miner.status.assert_called_once_with(palace_path="/local/palace")


# ── cmd_search routing ─────────────────────────────────────────────────


class TestCmdSearchDaemon:
    def test_routes_to_daemon_when_strict(self, capsys):
        from mempalace import cli

        # mempalace_search returns a search-shaped dict; the inner JSON
        # text is exactly what tool_search produces.
        inner = {
            "results": [
                {
                    "wing": "projects",
                    "room": "memorypalace",
                    "source_file": "/path/to/file.md",
                    "similarity": 0.91,
                    "text": "matching content here",
                }
            ],
            "warnings": [],
        }
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
            }
        ).encode()

        def fake_urlopen(req, timeout=None):
            captured_body = json.loads(req.data.decode())
            assert captured_body["params"]["name"] == "mempalace_search"
            assert captured_body["params"]["arguments"]["query"] == "graphql"
            return _FakeResp(body)

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(query="graphql", wing=None, room=None, results=5, palace=None)

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                cli.cmd_search(args)

        out = capsys.readouterr().out
        assert "graphql" in out
        assert "matching content here" in out

    def test_local_path_when_daemon_unset(self):
        from mempalace import cli

        with patch.dict("os.environ", {}, clear=True):
            with patch("mempalace.cli.MempalaceConfig") as mock_cfg:
                mock_cfg.return_value.palace_path = "/local/palace"
                args = argparse.Namespace(
                    query="x", wing=None, room=None, results=5, palace=None
                )
                with patch("mempalace.searcher.search") as mock_search:
                    cli.cmd_search(args)
                    mock_search.assert_called_once()


# ── cmd_mine routing ───────────────────────────────────────────────────


class TestCmdMineDaemon:
    def test_routes_projects_mode_to_daemon(self):
        """cmd_mine in projects mode routes to /mine with mode=projects.

        sys.exit(0) is called on success, so the test must wrap in
        pytest.raises(SystemExit). That double-checks the success
        contract: routing succeeded → exit 0.
        """
        from mempalace import cli

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(b'{"returncode": 0}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(
            dir="/home/u/proj",
            mode="projects",
            wing=None,
            agent=None,
            limit=None,
            dry_run=False,
            no_gitignore=False,
            include_ignored=None,
            redetect_origin=False,
            extract=None,
            palace=None,
        )

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_mine(args)

        assert ex.value.code == 0
        assert captured["body"]["mode"] == "projects"
        assert captured["body"]["dir"].endswith("/home/u/proj")

    def test_routes_convos_mode_to_daemon(self):
        from mempalace import cli

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(b'{"returncode": 0}')

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        args = argparse.Namespace(
            dir="/home/u/.claude/projects/-x",
            mode="convos",
            wing="myproj",
            agent=None,
            limit=None,
            dry_run=False,
            extract=None,
            palace=None,
            redetect_origin=False,
            no_gitignore=False,
            include_ignored=None,
        )

        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_mine(args)

        assert ex.value.code == 0
        assert captured["body"]["mode"] == "convos"
        assert captured["body"]["wing"] == "myproj"
