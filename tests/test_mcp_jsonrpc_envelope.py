"""JSON-RPC envelope handling: every request carrying an id gets a response.

The stdio loop used to swallow any exception raised while handling a request
and continue without writing anything, so a client that sent an id waited
forever. The reachable way in was the envelope read: `method` and `params`
were taken with `or ""` / `or {}`, which rescues only falsy values, so a
truthy non-string / non-dict reached `.startswith()` / `.get()`.
"""

import io
import json
import sys

import pytest

import mempalace.mcp_server as mcp


def _run_loop(monkeypatch, lines):
    """Drive _run_stdio_loop over `lines` and return the parsed responses.

    Startup side effects (preflight probe, watchdog threads, embedder warmup)
    are stubbed out: they open the palace, and this exercises the protocol
    loop only. stdin is a StringIO, so the loop always reaches EOF and exits.
    """
    for name in (
        "_restore_stdout",
        "_startup_preflight",
        "_start_idle_exit_watchdog",
        "_start_write_stall_watchdog",
        "_maybe_eager_warmup_embedder",
    ):
        monkeypatch.setattr(mcp, name, lambda *a, **k: None)
    # No hub configured: dispatch stays local instead of proxying.
    monkeypatch.setattr(mcp, "_hub_proxy_target", lambda: None)

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(line + "\n" for line in lines)))
    monkeypatch.setattr(sys, "stdout", out)

    mcp._run_stdio_loop()

    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


class TestEnvelopeShapes:
    """handle_request must answer malformed envelopes instead of raising."""

    @pytest.mark.parametrize("method", [123, True, 1.5, ["notifications/x"], {"a": 1}])
    def test_non_string_method_answers_instead_of_raising(self, method):
        resp = mcp.handle_request({"jsonrpc": "2.0", "id": 99, "method": method})

        assert resp is not None
        assert resp["id"] == 99
        assert resp["error"]["code"] == -32601
        # The message must name the real problem, not render an empty name.
        assert "expected a string" in resp["error"]["message"]

    @pytest.mark.parametrize("method", [123, True, ["x"]])
    def test_non_string_method_without_id_stays_silent(self, method):
        """A notification carries no id, so it still gets no response."""
        assert mcp.handle_request({"jsonrpc": "2.0", "method": method}) is None

    @pytest.mark.parametrize("params", ["oops", 5, True, ["a"], [], 0, "", False, None])
    def test_odd_params_never_raise(self, params):
        """Malformed params degrade to "no params" rather than crashing.

        `initialize` reads params, so this is the branch that used to raise.
        """
        resp = mcp.handle_request(
            {"jsonrpc": "2.0", "id": 100, "method": "initialize", "params": params}
        )

        assert resp["id"] == 100
        assert resp["result"]["serverInfo"]["name"] == "mempalace"

    @pytest.mark.parametrize("params", [[], 0, "", False, ["a"], "oops"])
    def test_params_ignoring_methods_keep_working(self, params):
        """`ping` never reads params, so odd params must not make it fail.

        Falsy values were normalised to {} long before this guard existed;
        rejecting them would break requests the server used to answer.
        """
        resp = mcp.handle_request({"jsonrpc": "2.0", "id": 60, "method": "ping", "params": params})

        assert resp == {"jsonrpc": "2.0", "id": 60, "result": {}}

    def test_null_id_is_a_request_and_gets_answered(self):
        """`id: null` is a Request, not a notification, so it owes a reply."""
        resp = mcp.handle_request({"jsonrpc": "2.0", "id": None, "method": "ping", "params": []})

        assert resp == {"jsonrpc": "2.0", "id": None, "result": {}}

    def test_missing_params_key_still_works(self):
        resp = mcp.handle_request({"jsonrpc": "2.0", "id": 101, "method": "initialize"})

        assert resp["result"]["serverInfo"]["name"] == "mempalace"

    @pytest.mark.parametrize(
        "params",
        [
            {"name": "mempalace_status", "arguments": 5},
            {"name": "mempalace_status", "arguments": True},
            {"name": {}, "arguments": {}},
            {"name": ["x"], "arguments": {}},
        ],
    )
    def test_malformed_tools_call_members_answer_instead_of_raising(self, params):
        """The same `or {}` trap one level down: name/arguments were unchecked."""
        resp = mcp.handle_request(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": params}
        )

        assert resp["id"] == 5
        assert resp["error"]["code"] == -32602


class TestRequestIsMutating:
    """The replay guard must return a verdict, never raise."""

    @pytest.mark.parametrize("params", ["x", ["a"], 5, True, None])
    def test_non_dict_params_is_not_mutating(self, params):
        assert mcp._request_is_mutating({"method": "tools/call", "params": params}) is False

    def test_unhashable_name_is_not_mutating(self):
        assert mcp._request_is_mutating({"method": "tools/call", "params": {"name": {}}}) is False

    def test_real_mutating_tool_still_detected(self):
        name = next(iter(mcp._MUTATING_TOOLS))

        assert mcp._request_is_mutating({"method": "tools/call", "params": {"name": name}})


class TestStdioLoopAlwaysResponds:
    """The loop must never drop a request that carried an id."""

    def test_malformed_json_gets_parse_error(self, monkeypatch):
        responses = _run_loop(
            monkeypatch,
            ['{"jsonrpc":"2.0","id":1,"method":"ping"', '{"jsonrpc":"2.0","id":2,"method":"ping"}'],
        )

        assert responses[0] == {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"},
        }
        # The loop stays alive and serves the next line.
        assert responses[1]["id"] == 2

    def test_non_string_method_is_answered_over_stdio(self, monkeypatch):
        responses = _run_loop(
            monkeypatch,
            ['{"jsonrpc":"2.0","id":7,"method":123}', '{"jsonrpc":"2.0","id":8,"method":"ping"}'],
        )

        assert [r["id"] for r in responses] == [7, 8]
        assert responses[0]["error"]["code"] == -32601

    def test_handler_exception_is_answered_not_swallowed(self, monkeypatch):
        """An unexpected failure below dispatch still owes the client a reply."""

        def _boom(_request):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(mcp, "_dispatch_stdio_request", _boom)

        responses = _run_loop(monkeypatch, ['{"jsonrpc":"2.0","id":11,"method":"ping"}'])

        assert len(responses) == 1
        assert responses[0]["id"] == 11
        assert responses[0]["error"]["code"] == -32603
        # The message must not leak the exception text to the client.
        assert "kaboom" not in json.dumps(responses[0])

    def test_notification_failure_stays_silent(self, monkeypatch):
        """No id means no response, even when dispatch raises."""

        def _boom(_request):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(mcp, "_dispatch_stdio_request", _boom)

        assert _run_loop(monkeypatch, ['{"jsonrpc":"2.0","method":"notifications/x"}']) == []

    def test_successful_notification_writes_nothing(self, monkeypatch):
        """The ordinary MCP notification still produces no output."""
        responses = _run_loop(
            monkeypatch,
            [
                '{"jsonrpc":"2.0","method":"notifications/initialized"}',
                '{"jsonrpc":"2.0","id":12,"method":"ping"}',
            ],
        )

        assert [r["id"] for r in responses] == [12]
