"""Pagination, source bounds and unchanged gateway controls for Admin."""

import json

import pytest

from commonplace.policy import Policy
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_admin_api import agent, clock, cookie, get
from tests.test_commonplace_web import request


@pytest.mark.parametrize("section", ["users", "connections"])
def test_inventory_pagination_and_query_validation(hub, section):
    headers = cookie(hub)
    outsider = hub.db.create_user("elsewhere@example.com")
    other = hub.db.create_workspace("elsewhere", outsider)
    hub.db.create_token(
        workspace_ref=other,
        user_ref=outsider,
        name="same-name",
        client="synthetic",
        policy=Policy.build(scopes=["memory:read"]),
        actor=outsider,
    )
    if section == "users":
        for i in range(101):
            uid = hub.db.create_user(f"member-{i}@example.com", f"Member {i}")
            hub.db.add_member(hub.workspace, uid, "member", actor=hub.admin)
        field, expected = "members", 102
    else:
        for i in range(102):
            agent(hub, f"agent-{i}")
        field, expected = "connections", 102
    status, first, _ = get(hub, section, headers)
    assert status == 200 and first["count"] == expected
    assert len(first[field]) == 100 and first["has_more"]
    assert "elsewhere@example.com" not in json.dumps(first) and "same-name" not in json.dumps(first)
    status, second, _ = get(hub, section, headers, query="?cursor=" + first["next_cursor"])
    assert status == 200 and second["count"] == expected
    assert len(second[field]) == 2 and not second["has_more"] and second["next_cursor"] is None
    ids = [r["id"] for r in first[field] + second[field]]
    assert len(ids) == len(set(ids)) == expected
    sibling = "connections" if section == "users" else "users"
    assert get(hub, sibling, headers, query="?cursor=" + first["next_cursor"])[0] == 400
    # An owner session in another workspace still cannot reuse this cursor.
    third = hub.db.create_workspace("second-owned", hub.admin)
    other_headers = cookie(hub, workspace=third)
    assert get(hub, section, other_headers, third, "?cursor=" + first["next_cursor"])[0] == 400
    for query in (
        "?workspace=elsewhere",
        "?cursor=",
        "?cursor=%%%",
        "?cursor=e30",
        "?cursor=a&cursor=b",
        "?header=x",
        "?cursor",
    ):
        status, data, _ = get(hub, section, headers, query=query)
        assert status == 400 and data == {"error": "Invalid admin query"}


def test_admin_source_deadline_uses_injected_monotonic(hub):
    headers = cookie(hub)
    ticks = iter([0, 3])
    clock(hub, monotonic=lambda: next(ticks, 3))
    status, data, _ = get(hub, "users", headers)
    assert status == 503 and data == {"error": "Users unavailable; try again"}


def test_gateway_machine_worker_and_mutation_controls(hub):
    headers = cookie(hub)
    assert get(hub, "users", headers)[0] == 200
    for kind, expected, error in (
        ("machine", 404, "not found"),
        ("worker", 403, "tool not allowed for this connection"),
    ):
        cid, bearer = agent(hub, kind)
        hub.db._conn.execute(
            "INSERT INTO runner_connections VALUES (?,?,?)", (cid, "synthetic-machine", kind)
        )
        status, data, _ = get(hub, "users", bearer)
        assert status == expected and data == {"error": error}
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        status, _, _ = request(
            hub,
            f"/api/admin/workspaces/{hub.workspace}/users",
            method,
            headers={**headers, "Content-Type": "application/json"},
        )
        assert status == 404
    for section in ("runtime", "limits", "stuck"):
        assert get(hub, section, headers)[0] == 200
    for path in ("/api/admin/instance/deploy", "/api/admin/instance/backups"):
        assert request(hub, path, headers=headers)[0] == 200
    status, _, raw = request(hub, "/healthz")
    assert status == 200 and json.loads(raw) == {"ok": True}
