"""The thin stdio front end (mempalace.mcp_proxy).

``mempalace-mcp`` is spawned once per agent session and, whenever a hub is
running, does nothing but forward JSON-RPC over HTTP. Importing the full
server to do that costs ~77 MB (chromadb alone is ~61 MB), so a 50-agent
fleet paid ~3.9 GB to hold proxies that never touch storage. These tests
guard the two properties that make the thin path worth having: it must stay
light, and losing the hub must still leave a working -- and visibly degraded
-- session rather than a broken one.
"""

import http.client
import io
import json
import os
import subprocess
import sys
import urllib.error

import pytest

from mempalace import hub_client, mcp_proxy


class _Hub:
    """A live hub for the proxy to forward to: ``post`` hands each request to ``forward``."""

    base_url = "http://hub"

    def __init__(self, forward=None):
        self._forward = forward

    def post(self, request, *, timeout=None):
        return self._forward(self.base_url, {}, request, "/p")


def _set_hub(monkeypatch, hub):
    monkeypatch.setattr(hub_client.HubClient, "discover", classmethod(lambda cls, palace_path: hub))


class TestInvocationRouting:
    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--transport", "stdio"],
            ["--transport=stdio"],
            ["--palace", "/tmp/p"],
            # The server's parser does not define --collection and skips it, so
            # it never fails without a value.
            ["--palace", "/tmp/p", "--collection"],
            ["--collection", "--palace", "/tmp/p"],
        ],
    )
    def test_plain_stdio_invocations_can_be_proxied(self, argv):
        assert mcp_proxy._is_plain_stdio_invocation(argv) is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["--transport", "http"],
            ["--transport=http"],
            ["--transport"],
            ["--host", "127.0.0.1"],
            ["--port", "8765"],
            ["--read-only"],
            ["--some-future-flag"],
            ["--backend"],
            ["--palace", "/tmp/p", "--backend"],
            ["--palace", "--backend"],
            ["--palace", "/tmp/p", "--collection", "--backend"],
        ],
    )
    def test_non_stdio_or_unknown_invocations_go_to_the_full_server(self, argv):
        """Unknown flags must not be silently dropped by the thin path.

        Guessing wrong this way only costs the old startup weight; guessing
        the other way would run a server the operator did not ask for.
        """
        assert mcp_proxy._is_plain_stdio_invocation(argv) is False

    def test_explicit_palace_flag_wins_over_config(self):
        assert mcp_proxy._palace_path(["--palace", "/tmp/explicit"]) == "/tmp/explicit"
        assert mcp_proxy._palace_path(["--palace=/tmp/eq"]) == "/tmp/eq"


class TestDegradedAnnotation:
    def _tool_response(self):
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": '{"results": []}'}]},
        }

    def test_notice_is_prepended_to_tool_content(self):
        """The agent only ever sees result.content; a log line is not a warning."""
        out = mcp_proxy._annotate_degraded(self._tool_response())
        blocks = out["result"]["content"]
        assert len(blocks) == 2
        assert "WITHOUT its shared hub" in blocks[0]["text"]
        # The real payload must survive untouched, and stay parseable.
        assert json.loads(blocks[1]["text"]) == {"results": []}

    @pytest.mark.parametrize(
        "response",
        [
            None,
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "x"}},
            {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
            "not-a-dict",
        ],
    )
    def test_shapes_without_tool_content_are_left_alone(self, response):
        assert mcp_proxy._annotate_degraded(response) == response


class _FakeServer:
    """Stand-in for the lazily-imported mcp_server."""

    def __init__(self, mutating=False):
        self.calls = []
        self._mutating = mutating

    def _request_is_mutating(self, request):
        return self._mutating

    def handle_request(self, request):
        self.calls.append(request)
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {"content": [{"type": "text", "text": "{}"}]},
        }


class _LoadedLocal:
    def __init__(self, server):
        self.server = server
        self.load_count = 0

    def load(self):
        self.load_count += 1
        return self.server


class _UnimportableLocal:
    """A local server whose import fails, as a broken install makes it."""

    def __init__(self, error=None):
        self.error = error or ImportError("chromadb failed to import")

    def load(self):
        raise self.error


# How a storage stack fails to import: a module missing, or chromadb refusing
# the interpreter's sqlite3, which it raises as a RuntimeError.
_IMPORT_FAILURES = [
    ImportError("chromadb failed to import"),
    RuntimeError("Your system has an unsupported version of sqlite3"),
]


class TestRouting:
    _REQUEST = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "x"}}

    def test_live_hub_is_used_and_the_server_is_never_loaded(self, monkeypatch):
        """The point of the module: a proxied session pays nothing for storage."""
        forwarded = {"jsonrpc": "2.0", "id": 7, "result": {"content": []}}
        _set_hub(monkeypatch, _Hub(lambda *a: forwarded))
        local = _LoadedLocal(_FakeServer())

        assert mcp_proxy._handle(dict(self._REQUEST), "/p", local) is forwarded
        assert local.load_count == 0

    def test_proxied_status_distinguishes_hub_and_local_update_state(self, monkeypatch):
        request = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "mempalace_status", "arguments": {}},
        }
        hub_payload = {
            "total_drawers": 10,
            "updates": {"server": {"enabled": True, "installed": "3.9.0"}},
        }
        forwarded = {
            "jsonrpc": "2.0",
            "id": 8,
            "result": {"content": [{"type": "text", "text": json.dumps(hub_payload)}]},
        }
        _set_hub(monkeypatch, _Hub(lambda *a: forwarded))
        monkeypatch.setattr(
            mcp_proxy,
            "cached_update_status",
            lambda: {"enabled": True, "installed": "3.8.0"},
            raising=False,
        )
        monkeypatch.setattr(mcp_proxy, "schedule_update_check", lambda: False, raising=False)
        local = _LoadedLocal(_FakeServer())

        out = mcp_proxy._handle(request, "/p", local)

        payload = json.loads(out["result"]["content"][0]["text"])
        assert payload["updates"] == {
            "server": {"enabled": True, "installed": "3.9.0"},
            "client": {"enabled": True, "installed": "3.8.0"},
        }
        assert local.load_count == 0

    def test_no_hub_falls_back_locally_and_warns(self, monkeypatch):
        _set_hub(monkeypatch, None)
        server = _FakeServer()
        local = _LoadedLocal(server)

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", local)
        assert server.calls, "request was not served locally"
        assert "WITHOUT its shared hub" in out["result"]["content"][0]["text"]

    def test_unreachable_hub_falls_back_for_a_read(self, monkeypatch):

        def boom(*a):
            raise urllib.error.URLError("connection refused")

        _set_hub(monkeypatch, _Hub(boom))
        server = _FakeServer(mutating=False)
        local = _LoadedLocal(server)

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", local)
        assert server.calls
        assert "WITHOUT its shared hub" in out["result"]["content"][0]["text"]

    def test_mutating_call_that_failed_mid_flight_is_not_replayed(self, monkeypatch):
        """The hub may still be executing it — a local retry could double-write."""

        def boom(*a):
            raise urllib.error.URLError("connection reset")

        _set_hub(monkeypatch, _Hub(boom))
        server = _FakeServer(mutating=True)
        local = _LoadedLocal(server)

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", local)
        assert server.calls == [], "a mutating call was replayed locally"
        assert out["error"]["code"] == -32000

    def test_hub_http_error_is_not_replayed_even_for_a_read(self, monkeypatch):
        """An HTTP status means the hub received it; re-running it here is wrong."""

        def boom(*a):
            raise urllib.error.HTTPError("http://hub/mcp", 500, "boom", {}, None)

        _set_hub(monkeypatch, _Hub(boom))
        server = _FakeServer(mutating=False)
        local = _LoadedLocal(server)

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", local)
        assert server.calls == []
        assert out["error"]["code"] == -32000

    _BROKEN_OFF = [http.client.IncompleteRead(b'{"jsonrpc": '), http.client.BadStatusLine("x")]

    @pytest.mark.parametrize("error", _BROKEN_OFF, ids=["answer-cut-off", "bad-status-line"])
    def test_a_write_whose_hub_answer_broke_off_is_not_replayed(self, monkeypatch, error):
        """The hub got the call and may have run it; only its answer is missing."""

        def broken_off(*a):
            raise error

        _set_hub(monkeypatch, _Hub(broken_off))
        server = _FakeServer(mutating=True)

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", _LoadedLocal(server))

        assert server.calls == [], "a mutating call was replayed locally"
        assert out["error"]["message"].startswith("palace hub proxy failed")

    @pytest.mark.parametrize("error", _BROKEN_OFF, ids=["answer-cut-off", "bad-status-line"])
    def test_a_read_whose_hub_answer_broke_off_is_served_locally(self, monkeypatch, error):

        def broken_off(*a):
            raise error

        _set_hub(monkeypatch, _Hub(broken_off))
        server = _FakeServer(mutating=False)

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", _LoadedLocal(server))

        assert server.calls
        assert "WITHOUT its shared hub" in out["result"]["content"][0]["text"]

    def test_hub_forward_kill_switch_disables_proxying(self, monkeypatch):
        monkeypatch.setenv(hub_client.HUB_FORWARD_ENV, "0")
        assert hub_client.HubClient.discover("/p") is None

    def test_refused_backend_is_answered_once_the_hub_is_gone(self, monkeypatch, refused_local):
        """Without a hub the request has to be served here, and the local server
        refuses to start. The client is told why instead of waiting for an answer."""
        _set_hub(monkeypatch, None)

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", refused_local)

        assert out["id"] == 7
        assert out["error"]["code"] == -32000
        assert "unknown backend 'no-such-backend'" in out["error"]["message"]

    @pytest.mark.parametrize("error", _IMPORT_FAILURES, ids=["ImportError", "RuntimeError"])
    def test_a_local_server_that_cannot_be_imported_is_answered_too(self, monkeypatch, error):
        """Id 0 on purpose: SDK clients number their requests from 0, initialize first."""
        _set_hub(monkeypatch, None)

        out = mcp_proxy._handle({**self._REQUEST, "id": 0}, "/p", _UnimportableLocal(error))

        assert out["id"] == 0
        assert out["error"]["code"] == -32000
        assert str(error) in out["error"]["message"]

    def test_refused_backend_leaves_a_notification_unanswered(self, monkeypatch, refused_local):
        _set_hub(monkeypatch, None)
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}

        assert mcp_proxy._handle(notification, "/p", refused_local) is None

    def test_failed_hub_call_is_reported_when_the_local_server_is_refused(
        self, monkeypatch, refused_local
    ):
        """The hub may still be running a call that failed mid-flight, so the
        answer is the hub's failure, which says so."""

        def boom(*a):
            raise urllib.error.URLError("timed out")

        _set_hub(monkeypatch, _Hub(boom))

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", refused_local)

        assert out["id"] == 7
        assert out["error"]["code"] == -32000
        assert out["error"]["message"] == "palace hub proxy failed: <urlopen error timed out>"
        assert out["error"]["data"]["hub"] == "http://hub"
        assert "unknown backend 'no-such-backend'" in out["error"]["data"]["local_server"]

    @pytest.mark.parametrize("error", _IMPORT_FAILURES, ids=["ImportError", "RuntimeError"])
    def test_failed_hub_call_is_reported_when_the_local_server_cannot_be_imported(
        self, monkeypatch, error
    ):

        def boom(*a):
            raise urllib.error.URLError("timed out")

        _set_hub(monkeypatch, _Hub(boom))

        out = mcp_proxy._handle({**self._REQUEST, "id": 0}, "/p", _UnimportableLocal(error))

        assert out["id"] == 0
        assert out["error"]["message"] == "palace hub proxy failed: <urlopen error timed out>"
        assert str(error) in out["error"]["data"]["local_server"]


@pytest.fixture
def refused_local(monkeypatch):
    """The real local server of a proxy started with a --backend it refuses."""
    from _mcp_server_helpers import _keep_server_command_line_state

    from mempalace import mcp_server

    _keep_server_command_line_state(monkeypatch)
    monkeypatch.setattr(mcp_server, "_restore_stdout", lambda: None)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "argv", ["mempalace-mcp", "--backend", "no-such-backend"])
    return mcp_proxy._LocalServer()


class TestProxyLoop:
    """Every request that carries an id gets an answer when its handling fails."""

    @staticmethod
    def _run(monkeypatch, lines, forward):
        _set_hub(monkeypatch, _Hub(forward))
        # A path that reaches load() must not start the real server and its
        # watchdog threads inside the test session.
        monkeypatch.setattr(mcp_proxy, "_LocalServer", _UnimportableLocal)
        monkeypatch.setattr(sys, "stdin", io.StringIO("".join(line + "\n" for line in lines)))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        mcp_proxy._run_proxy_loop("/p")
        return [json.loads(line) for line in out.getvalue().splitlines()]

    @staticmethod
    def _echo(base_url, headers, request, palace_path):
        return {"jsonrpc": "2.0", "id": request["id"], "result": {}}

    _PING = '{"jsonrpc": "2.0", "id": 2, "method": "ping"}'

    def test_a_stray_byte_from_the_hub_reaches_the_client_as_u_fffd(self, monkeypatch):
        """The hub answers the proxy exactly; the proxy's own client gets
        U+FFFD, because some clients' JSON parsers reject a lone surrogate."""

        def stray(base_url, headers, request, palace_path):
            content = [{"type": "text", "text": "café \udcff"}]
            return {"jsonrpc": "2.0", "id": request["id"], "result": {"content": content}}

        (out,) = self._run(monkeypatch, [self._PING], stray)
        assert out["result"]["content"][0]["text"] == "café �"

    @pytest.mark.parametrize(
        ("line", "rejected_with"),
        [('{"jsonrpc": "2.0", "id": 1, ', json.JSONDecodeError), ("[" * 100000, RecursionError)],
        ids=["invalid-json", "nesting-too-deep-to-parse"],
    )
    def test_a_line_that_does_not_parse_is_answered_with_a_parse_error(
        self, monkeypatch, line, rejected_with
    ):
        with pytest.raises(rejected_with):
            json.loads(line)

        out = self._run(monkeypatch, [line, self._PING], self._echo)

        assert out == [
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
        ]

    @pytest.mark.parametrize("line", ["[1, 2]", "7", '"ping"', "null"])
    def test_json_that_is_not_an_object_is_an_invalid_request(self, monkeypatch, line):
        out = self._run(monkeypatch, [line, self._PING], self._echo)

        assert out == [
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
        ]

    @pytest.mark.parametrize("params", [[1], "x"], ids=["list", "string"])
    def test_the_hub_answer_survives_params_that_are_not_an_object(self, monkeypatch, params):
        """The full server reads such params as none; so does the proxy's own look at them."""
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        out = self._run(monkeypatch, [json.dumps(request)], self._echo)

        assert out == [{"jsonrpc": "2.0", "id": 1, "result": {}}]

    @pytest.mark.parametrize("error", [AttributeError("x"), TypeError("x")])
    def test_a_request_whose_handling_raises_is_answered_and_the_loop_goes_on(
        self, monkeypatch, error
    ):
        """A failure the hub path does not handle, as a bug in it would be."""

        def forward(base_url, headers, request, palace_path):
            if request["id"] == 0:
                raise error
            return self._echo(base_url, headers, request, palace_path)

        request = {"jsonrpc": "2.0", "id": 0, "method": "tools/call", "params": {"name": "x"}}
        out = self._run(monkeypatch, [json.dumps(request), self._PING], forward)

        assert out == [
            {"jsonrpc": "2.0", "id": 0, "error": {"code": -32603, "message": "Internal error"}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
        ]

    def test_a_notification_whose_handling_raises_stays_unanswered(self, monkeypatch):
        def forward(base_url, headers, request, palace_path):
            if "id" not in request:
                raise AttributeError("x")
            return self._echo(base_url, headers, request, palace_path)

        notification = '{"jsonrpc": "2.0", "method": "notifications/initialized"}'
        out = self._run(monkeypatch, [notification, self._PING], forward)

        assert out == [{"jsonrpc": "2.0", "id": 2, "result": {}}]


def test_importing_the_proxy_does_not_import_the_storage_stack():
    """The whole reason this module exists.

    A regression here is invisible in behaviour and only shows up as memory
    across a fleet, so it is asserted directly. Runs in a subprocess because
    the test session has already imported everything.
    """
    code = (
        "import sys; import mempalace.mcp_proxy; "
        "print(','.join(m for m in ('chromadb','numpy','mempalace.mcp_server') "
        "if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"heavy modules imported by the proxy: {out.stdout.strip()}"


def test_a_failed_server_import_leaves_the_proxy_answering_on_stdout():
    """Importing the server moves fd 1 onto stderr before its first import that
    can fail. When one does, the proxy still has to answer where the client reads.

    In a subprocess, because the import has to really run and fail, and it moves
    the process's own fd 1. The control run imports the server the way the proxy
    did before, and shows where the answer went then.
    """
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(mcp_proxy.__file__)))

    def run(before_the_loop):
        code = (
            "import io, sys\n"
            f"sys.path.insert(0, {package_root!r})\n"
            "sys.modules['chromadb'] = None\n"
            "from mempalace import mcp_proxy\n"
            "mcp_proxy.HubClient.discover = classmethod(lambda cls, p: None)\n"
            f"{before_the_loop}"
            'sys.stdin = io.StringIO(\'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\\n\')\n'
            "mcp_proxy._run_proxy_loop('/p')\n"
        )
        out = subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120
        )
        assert out.returncode == 0, out.stderr
        return out

    control = run(
        "def import_server():\n"
        "    from mempalace import mcp_server\n"
        "    return mcp_server\n"
        "mcp_proxy._import_server = import_server\n"
    )
    assert control.stdout == ""
    assert '{"jsonrpc": "2.0", "id": 1, "error": {"code": -32000' in control.stderr

    out = run("")
    answers = [json.loads(line) for line in out.stdout.splitlines()]
    assert [answer["id"] for answer in answers] == [1], out.stderr
    assert "chromadb" in answers[0]["error"]["message"]


def test_local_fallback_serves_the_palace_the_proxy_was_started_for(monkeypatch, tmp_path):
    """When the hub this proxy started with is gone, the proxy serves the session
    in-process through _LocalServer.load(), which never runs the server's main(),
    so the proxy's own flags have to be applied there. Otherwise the session
    silently serves the configured default palace."""
    from _mcp_server_helpers import _keep_server_command_line_state

    from mempalace import mcp_server

    _keep_server_command_line_state(monkeypatch)
    monkeypatch.setattr(mcp_server, "_restore_stdout", lambda: None)
    monkeypatch.setattr(mcp_server, "_start_idle_exit_watchdog", lambda: None)
    monkeypatch.setattr(mcp_server, "_start_write_stall_watchdog", lambda: None)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    palace = tmp_path / "palace"
    # --collection is a light-server flag the proxy lets through; the server's
    # parser has to ignore it.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mempalace-mcp",
            "--palace",
            str(palace),
            "--backend",
            "sqlite_exact",
            "--collection",
            "c",
            "--read-only",
        ],
    )

    module = mcp_proxy._LocalServer().load()

    assert module is mcp_server
    assert mcp_server._config.palace_path == str(palace)
    assert mcp_server._resolve_kg_path() == str(palace / "knowledge_graph.sqlite3")
    assert mcp_server._READ_ONLY is True
    assert os.environ["MEMPALACE_BACKEND"] == "sqlite_exact"


def test_local_fallback_restores_stdout_before_a_flag_can_be_refused(monkeypatch):
    """A refused flag must not leave fd 1 pointing at stderr: the proxy keeps
    answering over stdout after a failed load, and a client waiting there would
    never see a response."""
    from _mcp_server_helpers import _keep_server_command_line_state

    from mempalace import mcp_server
    from mempalace.backends.registry import BackendUnavailableError

    _keep_server_command_line_state(monkeypatch)
    restored = []
    monkeypatch.setattr(mcp_server, "_restore_stdout", lambda: restored.append(True))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "argv", ["mempalace-mcp", "--backend", "no-such-backend"])

    with pytest.raises(BackendUnavailableError):
        mcp_proxy._LocalServer().load()

    assert restored == [True]


def test_a_failed_local_load_is_not_retried(monkeypatch):
    calls = []

    def explode():
        calls.append("import")
        raise RuntimeError("import failed")

    monkeypatch.setattr(mcp_proxy, "_import_server", explode)
    server = mcp_proxy._LocalServer()

    with pytest.raises(RuntimeError, match="import failed"):
        server.load()
    with pytest.raises(RuntimeError, match="import failed"):
        server.load()

    assert calls == ["import"]


class TestSearchSettings:
    """A search this session configured itself is not answered by a hub that may not share it."""

    @staticmethod
    def _call(name, req_id=11):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": {"query": "q"}},
        }

    @pytest.fixture(autouse=True)
    def _no_overrides(self, monkeypatch):
        from mempalace.hub_client import SEARCH_OVERRIDE_ENV_VARS

        for name in SEARCH_OVERRIDE_ENV_VARS:
            monkeypatch.delenv(name, raising=False)

    @pytest.fixture
    def hub(self, monkeypatch):
        forwarded = []

        def forward(base_url, headers, request, palace_path):
            forwarded.append(request["params"]["name"])
            return {"jsonrpc": "2.0", "id": request["id"], "result": {"content": []}}

        _set_hub(monkeypatch, _Hub(forward))
        return forwarded

    def test_a_search_with_matching_settings_goes_to_the_hub(self, hub):
        local = _LoadedLocal(_FakeServer())

        mcp_proxy._handle(self._call("mempalace_search"), "/p", local)

        assert hub == ["mempalace_search"]
        assert local.load_count == 0

    @pytest.mark.parametrize("tool", ["mempalace_search", "mempalace_check_duplicate"])
    def test_an_env_override_keeps_the_search_local(self, hub, monkeypatch, tool):
        monkeypatch.setenv("MEMPALACE_HYBRID_BM25_WEIGHT", "0.9")
        server = _FakeServer()
        local = _LoadedLocal(server)

        out = mcp_proxy._handle(self._call(tool), "/p", local)

        assert hub == []
        assert [c["params"]["name"] for c in server.calls] == [tool]
        # The hub is alive, so the answer carries no hub-is-gone notice.
        assert out["result"]["content"] == [{"type": "text", "text": "{}"}]

    def test_an_explicit_backend_keeps_the_search_local(self, hub):
        server = _FakeServer()
        local = _LoadedLocal(server)

        mcp_proxy._handle(self._call("mempalace_search"), "/p", local, backend="sqlite_exact")

        assert hub == []
        assert len(server.calls) == 1

    def test_other_tools_still_go_to_the_hub_with_an_override(self, hub, monkeypatch):
        monkeypatch.setenv("MEMPALACE_PGVECTOR_SHARED_NAMESPACE", "team")
        local = _LoadedLocal(_FakeServer())

        mcp_proxy._handle(self._call("mempalace_list_wings"), "/p", local, backend="chroma")

        assert hub == ["mempalace_list_wings"]
        assert local.load_count == 0

    def test_main_hands_the_backend_flag_to_the_loop(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(sys, "argv", ["mempalace-mcp", "--palace", "/p", "--backend=chroma"])
        _set_hub(monkeypatch, _Hub())
        monkeypatch.setattr(
            mcp_proxy,
            "_run_proxy_loop",
            lambda palace_path, backend=None: seen.update(palace=palace_path, backend=backend),
        )

        mcp_proxy.main()

        assert seen == {"palace": "/p", "backend": "chroma"}
