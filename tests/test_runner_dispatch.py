"""Worker credentials are default-denied before extension route handlers run."""

import pytest

from tests.runner_harness import (
    card,
    cli_token,
    http,
    owners,  # noqa: F401
    signup_hub,  # noqa: F401
)
from tests.test_runner_owner import enroll


@pytest.mark.parametrize("prefix", [False, True], ids=["exact", "prefix"])
def test_worker_dispatch_denies_unmapped_scope_only_route(signup_hub, owners, prefix):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    called = []

    def probe(handler):
        principal = hub.gateway.principal_for(handler)
        if not principal.policy.has_scope("memory:write"):
            handler._send_json(403, {"error": "needs memory:write"})
            return
        called.append(principal.connection_id)
        handler._send_json(200, {"ok": True})

    path = "/api/scope-only-probe/"
    if prefix:
        hub.gateway.add_prefix_route("POST", path, probe)
        path += "child"
    else:
        hub.gateway.add_route("POST", path, probe)
    _, control = cli_token(
        owner,
        "ordinary-agent",
        scopes=["memory:write"],
    )
    assert http(hub.port, "POST", path, token=control, body={})[0] == 200
    called.clear()
    status, _ = http(hub.port, "POST", path, token=machine["worker_token"], body={})
    assert status == 403, "worker reached an unmapped scope-only route"
    assert called == [], "dispatch must refuse before the extension handler runs"


def test_worker_route_walk_stays_within_tool_tuple(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    created = card(hub, owner)
    number = created["number"]
    token = machine["worker_token"]
    allowed = {
        ("POST", "/mcp"),
        ("GET", f"/api/cards/{number}"),
        ("PATCH", f"/api/cards/{number}"),
        ("POST", f"/api/cards/{number}/comments"),
        ("POST", f"/api/cards/{number}/heartbeat"),
        ("POST", f"/api/cards/{number}/handoff"),
    }
    routes = set(hub.gateway.routes)
    for method, prefix, _ in hub.gateway.prefix_routes:
        routes.add((method, prefix))
        routes.add((method, prefix + "probe"))
    routes |= allowed | {
        ("POST", f"/api/cards/{number}/approve"),
        ("POST", f"/api/cards/{number}/decide"),
        ("GET", "/api/orch/jobs"),
        ("POST", "/api/orch/jobs"),
        ("GET", "/api/boards/shared/cards"),
        ("POST", "/api/threads/probe/recall"),
    }
    successes = set()
    for method, path in sorted(routes):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"} if path == "/mcp" else {}
        status, _ = http(hub.port, method, path, token=token, body=body)
        if 200 <= status < 300:
            successes.add((method, path))
    assert successes <= allowed, f"worker reached routes outside its tuple: {successes - allowed}"
    assert {("POST", "/mcp"), ("GET", f"/api/cards/{number}")} <= successes


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("GET", ""),
        ("PATCH", ""),
        ("POST", "/comments"),
        ("POST", "/heartbeat"),
        ("POST", "/handoff"),
    ],
)
def test_worker_dispatch_allows_mapped_rest_tuple(signup_hub, owners, method, suffix):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    path = "/api/cards/1" + suffix
    hub.gateway.add_route(method, path, lambda handler: handler._send_json(200, {"ok": True}))
    assert http(hub.port, method, path, token=machine["worker_token"], body={})[0] == 200
