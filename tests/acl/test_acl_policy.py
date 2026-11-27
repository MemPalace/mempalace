"""Scope plumbing, the policy table, and deny ordering in call_tool."""

import re
import threading
from pathlib import Path

import pytest

from mempalace import acl, mcp_server as mcp
from mempalace.tool_capabilities import ACL_PENDING, ACL_POLICY, TOOL_CAPABILITIES

LOCKED = acl.Scope("locked", "g1", read=frozenset({"alpha"}), write=frozenset({"alpha"}))
READONLY = acl.Scope("locked", "g1ro", read=frozenset({"alpha"}))
DENIED_TOOLS = sorted(
    {t for t, (kind, _) in ACL_POLICY.items() if kind == "deny"} | set(ACL_PENDING)
)
ALLOWED_TOOLS = sorted(set(TOOL_CAPABILITIES) - set(DENIED_TOOLS))


def rpc(method, params=None, rid=1):
    return mcp.handle_request({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


def call(tool, arguments=None):
    return rpc("tools/call", {"name": tool, "arguments": arguments or {}})


def test_policy_is_complete_and_well_formed():
    assert set(ACL_POLICY) == set(TOOL_CAPABILITIES)
    assert set(ACL_PENDING) <= set(ACL_POLICY)
    assert all(kind in ("scoped", "kg", "deny") for kind, _ in ACL_POLICY.values())
    assert set(TOOL_CAPABILITIES) == set(mcp.TOOLS)


def test_completeness_check_catches_a_new_tool(monkeypatch):
    monkeypatch.setitem(TOOL_CAPABILITIES, "mempalace_new_tool", ("read", ""))
    assert set(TOOL_CAPABILITIES) - set(ACL_POLICY) == {"mempalace_new_tool"}
    # and an unclassified tool is denied rather than allowed
    with acl.request_scope(LOCKED):
        assert acl.denies_tool("mempalace_new_tool")


def test_wing_arg_names_exist_in_the_schemas():
    for tool, (_, wing_arg) in ACL_POLICY.items():
        if wing_arg:
            assert wing_arg in mcp.TOOLS[tool]["input_schema"]["properties"], tool


def test_allowed_set_is_exactly_the_three_leak_free_tools():
    assert ALLOWED_TOOLS == [
        "mempalace_add_drawer",
        "mempalace_diary_write",
        "mempalace_get_aaak_spec",
    ]
    assert len(DENIED_TOOLS) == 44


def test_default_scope_follows_the_process_flag():
    assert acl.current_scope() is acl.ADMIN
    acl.enable()
    assert acl.current_scope() is acl.DENIED


def test_bare_thread_is_denied_with_acl_on_and_admin_off():
    seen = {}

    def probe(label):
        seen[label] = (acl.current_scope().kind, call("mempalace_add_drawer"))

    t = threading.Thread(target=probe, args=("off",))
    t.start()
    t.join()
    acl.enable()
    t = threading.Thread(target=probe, args=("on",))
    t.start()
    t.join()
    assert seen["off"][0] == "admin"
    assert seen["on"][0] == "denied"
    assert seen["on"][1]["error"]["code"] == -32601
    # a thread started from inside a request does not inherit its scope either
    inner = {}
    with acl.request_scope(LOCKED):
        t = threading.Thread(target=lambda: inner.update(kind=acl.current_scope().kind))
        t.start()
        t.join()
    assert inner["kind"] == "denied"


def test_contextvar_isolated_across_threads_and_reset():
    acl.enable()
    barrier = threading.Barrier(2)
    seen = {}

    def run(name):
        scope = acl.Scope("locked", name, read=frozenset({name}), write=frozenset({name}))
        with acl.request_scope(scope):
            barrier.wait(timeout=5)
            seen[name] = acl.current_scope().subject
            barrier.wait(timeout=5)

    threads = [threading.Thread(target=run, args=(n,)) for n in ("g1", "g2")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert seen == {"g1": "g1", "g2": "g2"}
    assert acl.current_scope() is acl.DENIED  # reset in finally


def test_scope_reset_even_when_the_request_raises():
    with pytest.raises(RuntimeError):
        with acl.request_scope(LOCKED):
            raise RuntimeError("boom")
    assert acl.current_scope() is acl.ADMIN


def test_request_scope_cannot_set_internal():
    with pytest.raises(ValueError):
        with acl.request_scope(acl.INTERNAL):
            pass


def test_internal_is_set_only_by_server_started_code():
    root = Path(acl.__file__).resolve().parents[1]
    allowed = {
        "acl/__init__.py",
        "mcp_server/protocol.py",
        "mcp_server/runtime.py",
        "mcp_server/http.py",
    }
    pattern = re.compile(r"run_internal\(|\bINTERNAL\b|_scope_var")
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        hits = [m for m in pattern.finditer(path.read_text(encoding="utf-8"))]
        if rel not in allowed:
            assert not hits, f"{rel} touches the INTERNAL scope"
    for rel in ("mcp_server/protocol.py", "mcp_server/runtime.py", "mcp_server/http.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert len(re.findall(r"run_internal\(", text)) == len(
            re.findall(r"target=_run_internal\(", text)
        ), f"{rel}: run_internal must wrap thread targets only"
        assert "_scope_var" not in text


def test_internal_thread_bypasses():
    acl.enable()
    seen = {}
    t = threading.Thread(
        target=acl.run_internal(lambda: seen.update(kind=acl.current_scope().kind))
    )
    t.start()
    t.join()
    assert seen["kind"] == "internal"
    assert acl.current_scope() is acl.DENIED


def test_tools_list_exact_for_locked_and_full_for_admin():
    acl.enable()
    with acl.request_scope(LOCKED):
        names = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
    assert sorted(names) == ALLOWED_TOOLS
    with acl.request_scope(acl.ADMIN):
        assert len(rpc("tools/list")["result"]["tools"]) == len(TOOL_CAPABILITIES)
    assert rpc("tools/list")["result"]["tools"] == []  # no scope under ACL: nothing


@pytest.mark.parametrize("tool", DENIED_TOOLS)
@pytest.mark.parametrize(
    "arguments",
    [{}, {"bogus": 1}, {"wing": 5, "drawer_id": ["x"], "items": "no"}, {"wing": "alpha"}],
)
def test_denied_tools_answer_exactly_like_a_missing_tool(tool, arguments, monkeypatch):
    called = []
    for name, entry in mcp.TOOLS.items():
        real = entry["handler"]
        monkeypatch.setitem(
            entry,
            "handler",
            lambda *a, _real=real, _n=name, **k: called.append(_n) or _real(*a, **k),
        )
    acl.enable()
    with acl.request_scope(LOCKED):
        got = call(tool, arguments)
        ghost = call("mempalace_no_such_tool", arguments)
    assert got == {**ghost, "error": {**ghost["error"], "message": f"Unknown tool: {tool}"}}
    assert got["error"]["code"] == -32601
    assert called == []
    # the same holds for a caller with no scope at all
    assert call(tool, arguments)["error"]["code"] == -32601


def test_denied_message_names_no_tool_list():
    with acl.request_scope(LOCKED):
        text = str(call("mempalace_status"))
    assert "mempalace_add_drawer" not in text


@pytest.mark.parametrize("scope", [LOCKED, READONLY])
@pytest.mark.parametrize(
    "arguments",
    [
        {},  # wing missing
        {"room": "r", "content": "c"},
        {"wing": "beta", "room": "r", "content": "c"},
        {"wing": "no_such_wing", "room": "r", "content": "c"},
        {"wing": "", "room": "r", "content": "c"},
        {"wing": "   ", "room": "r", "content": "c"},
    ],
)
def test_wing_check_runs_before_the_database(scope, arguments, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("database reached")

    monkeypatch.setattr(mcp, "_get_collection", boom)
    if scope is READONLY:
        arguments = {**arguments, "wing": "alpha", "room": "r", "content": "c"}
    acl.enable()
    with acl.request_scope(scope):
        response = call("mempalace_add_drawer", arguments)
    assert response["error"] == {"code": -32602, "message": "Invalid params: wing not available"}


def test_wing_is_normalized_before_the_handler(monkeypatch):
    seen = {}
    monkeypatch.setitem(
        mcp.TOOLS["mempalace_add_drawer"],
        "handler",
        lambda wing, room, content, **k: seen.update(wing=wing) or {"success": True},
    )
    scope = acl.Scope(
        "locked", "g", read=frozenset({"alpha_team"}), write=frozenset({"alpha_team"})
    )
    acl.enable()
    with acl.request_scope(scope):
        call("mempalace_add_drawer", {"wing": "Alpha-Team", "room": "r", "content": "c"})
    assert seen["wing"] == "alpha_team"
