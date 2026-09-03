"""Tests for issue #2231: malformed JSON-RPC envelope shape should yield a
well-formed JSON-RPC error (carrying the request id) rather than an
exception that is caught-and-swallowed and leaves the client hanging."""
from __future__ import annotations

from typing import Any

import pytest

# mcp_server.py is a self-contained module imported the way the production
# code (mcp_server.py itself, the stdio/HTTP wrappers, and the existing
# test file) imports it — by absolute name after the package dir is on
# sys.path (see tests/test_mcp_server.py).
import conftest  # noqa: F401  (ensure package root is importable)
import mempalace.mcp_server as mcp_server_mod


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _dispatch(request: dict[str, Any]) -> Any:
    """Dispatch a single JSON-RPC request through the same entry point the
    stdio server uses (handle_request), returning either a response dict,
    None (notification), or raising (which is exactly the bug we're
    fixing — the stdio loop's except/continue would swallow it)."""
    return mcp_server_mod.handle_request(request)


# ---------------------------------------------------------------------------
# 1. Envelope-shape guards: method / params must be the right type when
#    non-null, otherwise we return a JSON-RPC -32600 (Invalid Request) that
#    carries the client's id.  These used to reach .startswith() / .get()
#    / ** / in and raise a TypeError/AttributeError which both transports
#    swallow, so the client hangs on that id forever.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_method",
    [123, True, None or "x" and {"nested": 1}, [1, 2], 3.14],
    ids=["int", "bool-true", "dict", "list", "float"],
)
def test_method_wrong_type_returns_invalid_request(bad_method: Any) -> None:
    req = {"jsonrpc": "2.0", "id": 42, "method": 42 if bad_method is None else bad_method,
           "params": {}}
    if bad_method == 3.14:
        req["method"] = 3.14
    elif bad_method == [1, 2]:
        req["method"] = [1, 2]
    elif bad_method == {"nested": 1}:
        req["method"] = {"nested": 1}
    elif bad_method is True:
        req["method"] = True
    elif bad_method == 123:
        req["method"] = 123
    resp = _dispatch(req)
    assert resp is not None
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 42
    assert "error" in resp
    assert resp["error"]["code"] == -32600
    assert "method" in resp["error"]["message"]


def test_method_null_still_falls_to_unknown_method() -> None:
    """method: null (present but null) keeps its previous meaning — no
    method → -32601 Unknown method, not -32600 Invalid Request."""
    resp = _dispatch({"jsonrpc": "2.0", "id": 7, "method": None, "params": {}})
    assert resp is not None
    assert resp["id"] == 7
    assert "error" in resp
    assert resp["error"]["code"] == -32601


@pytest.mark.parametrize(
    "bad_params",
    ["oops", [1, 2, 3], 5, True, {"a": 1, "b": 2} if False else [1]],
    ids=["str", "list", "int", "bool", "list2"],
)
def test_params_wrong_type_returns_invalid_request(bad_params: Any) -> None:
    req = {"jsonrpc": "2.0", "id": 99, "method": "initialize", "params": bad_params}
    resp = _dispatch(req)
    assert resp is not None
    assert resp["id"] == 99
    assert "error" in resp
    assert resp["error"]["code"] == -32600
    assert "params" in resp["error"]["message"]


def test_params_null_still_allowed_on_initialize() -> None:
    """params: null (present but null) is legal JSON-RPC and must still
    initialize successfully."""
    resp = _dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": None})
    assert resp is not None
    assert resp["id"] == 1
    assert "result" in resp
    assert "protocolVersion" in resp["result"]


def test_notification_shape_gets_no_response() -> None:
    """A notification (no id) with a wrong-typed method should still get
    NO response (None), matching the "Unknown method" fall-through and
    JSON-RPC notification semantics."""
    assert _dispatch({"jsonrpc": "2.0", "method": 123}) is None
    assert _dispatch({"jsonrpc": "2.0", "method": "initialize", "params": [1, 2]}) is None


def test_request_not_a_dict_gets_invalid_request_id_none() -> None:
    """A request that is not a dict (e.g. the entire body was the string
    'oops') → -32600 with id null (JSON-RPC Parse error convention)."""
    resp = _dispatch("oops" if False else "oops")
    assert resp is not None
    assert "error" in resp
    assert resp["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# 2. tools/call field guards: params must be a dict with a string "name"
#    and an object "arguments" (when present).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"arguments": {}},
        {"name": 123},
        {"name": [1, 2]},
        {"name": {"a": 1}},
        {"name": True},
        {"name": "mempalace_status", "arguments": [1, 2]},
        {"name": "mempalace_status", "arguments": "oops"},
        {"name": "mempalace_status", "arguments": 5},
        {"name": "mempalace_status", "arguments": True},
    ],
    ids=[
        "params_none", "params_empty", "args_only",
        "name_int", "name_list", "name_dict", "name_bool",
        "args_list", "args_str", "args_int", "args_bool",
    ],
)
def test_tools_call_invalid_field_types_return_invalid_params(params: Any) -> None:
    req = {"jsonrpc": "2.0", "id": 55, "method": "tools/call", "params": params}
    resp = _dispatch(req)
    assert resp is not None
    assert resp["id"] == 55
    assert "error" in resp
    assert resp["error"]["code"] == -32602


def test_tools_call_valid_status_still_works() -> None:
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "mempalace_status"}}
    resp = _dispatch(req)
    assert resp is not None
    assert resp["id"] == 1
    assert "result" in resp
    assert resp["result"].get("isError") in (None, False)


# ---------------------------------------------------------------------------
# 3. _request_is_mutating must not raise on malformed params/name/arguments
#    (the stdio hub proxy reads the request before dispatch; a TypeError
#    here used to slip through the same except/continue path).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("params", [None, "oops", [1], {"name": 1}, {"name": "tools_nonexistent"}])
def test_mutating_predicate_tolerates_bad_shape(params: Any) -> None:
    req = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": params}
    result = mcp_server_mod._request_is_mutating(req)
    assert isinstance(result, bool)
