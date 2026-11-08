"""Standalone overview entry points require workspace and thread authority."""

import pytest

from commonplace.gateway import Mediator, _tool_result


@pytest.mark.parametrize("tool", ["mempalace_list_wings", "mempalace_get_taxonomy"])
def test_policy_only_tools_call_fails_closed_without_workspace(tool):
    from types import SimpleNamespace

    from commonplace.policy import Policy
    from tests._overview_helpers import overview_result

    principal = SimpleNamespace(
        policy=Policy.build(
            profile="standard", scopes=["memory:read"], read_wings=["shared"], trust="standard"
        )
    )
    rooms = {"shared": "notes", "vault": "notes"}
    workers = SimpleNamespace(call_tool=lambda who, name, args: overview_result(name, args, rooms))
    mediator = Mediator(None, workers)
    assert principal.policy.refusal(tool) is None
    raw = workers.call_tool(principal, tool, {})
    key = "wings" if tool == "mempalace_list_wings" else "taxonomy"
    assert "vault" in raw[key] and not principal.policy.can_read_wing("vault")
    reply = mediator._tools_call(
        principal, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}}
    )
    assert _tool_result(reply) == {key: {}}, "workspace-less direct overview exposed presence"


@pytest.mark.parametrize("workspace_bound", [False, True], ids=["policy_only", "workspace_bound"])
@pytest.mark.parametrize("visible_count", [1, 64])
def test_policy_only_fanout_hides_unreadable_thread_wing(workspace_bound, visible_count):
    from types import SimpleNamespace

    from commonplace.policy import Policy
    from tests._overview_helpers import overview_result

    principal = SimpleNamespace(
        policy=Policy.build(
            profile="standard", scopes=["memory:read"], read_wings=["*"], trust="standard"
        )
    )
    if workspace_bound:
        principal.workspace_id = "fixture-workspace"
    rooms = {f"visible_{i:02}": "notes" for i in range(visible_count)}
    rooms["hidden_only"] = "thr_deadbeef"
    calls = []

    def call_tool(who, name, args):
        overview = overview_result(name, args, rooms)
        if overview is not None:
            return overview
        calls.append(args)
        return {"query": args["query"], "results": []}

    mediator = Mediator(None, SimpleNamespace(call_tool=call_tool))
    assert principal.policy.can_read_wing("hidden_only")
    assert "hidden_only" in call_tool(principal, "mempalace_list_wings", {})["wings"]

    def read():
        from commonplace.gateway import Denied

        try:
            return mediator._fan_out_search(principal, "mempalace_search", {"query": "needle"})
        except Denied as exc:
            return {"denied": True, "error": str(exc)}

    reply = read()
    expected_wings = sorted(w for w in rooms if w != "hidden_only") if workspace_bound else []
    assert reply == {"query": "needle", "results": [], "wings_searched": expected_wings}, (
        "unreadable thread wing changed wings_searched or Denied"
    )
    del rooms["hidden_only"]
    assert read() == reply
    assert all("hidden_only" not in args["wings"] for args in calls)
