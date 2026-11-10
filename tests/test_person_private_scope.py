"""Authenticated person reads hide private data and private-dependent counts."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from commonplace import sessions
from mempalace.ids import make_drawer_id_from_content
from tests._commonplace_helpers import hub_fixture  # noqa: F401

PRIVATE = "PRIVATE_CANARY_8d73b92a"
SHARED = "SHARED_CANARY_610ea24f"
PRIVATE_ROOM = "private_topic"
SHARED_ROOM = "shared_topic"
PRIVATE_ID = make_drawer_id_from_content("private_notes", PRIVATE_ROOM, PRIVATE)
SHARED_ID = make_drawer_id_from_content("shared", SHARED_ROOM, SHARED)

# Only tools without a scoped projection may lose their shared control.
REFUSED = {"mempalace_check_duplicate", "mempalace_mesh_peers"}
CASES = [
    ("web_search", "/api/memory/search?q=CANARY&limit=100", None, None),
    (
        "web_search_theme",
        "/api/memory/search?q=CANARY&theme=private_notes",
        "/api/memory/search?q=CANARY&theme=shared",
        None,
    ),
    ("web_get", f"/api/memory/{PRIVATE_ID}", f"/api/memory/{SHARED_ID}", None),
    ("web_map", "/api/memory/map", None, None),
    ("memory_search", {"query": "CANARY", "limit": 100}, None, None),
    ("memory_get", {"id": PRIVATE_ID}, {"id": SHARED_ID}, None),
    ("mempalace_search", {"query": "CANARY", "limit": 100}, None, None),
    ("mempalace_get_drawer", {"drawer_id": PRIVATE_ID}, {"drawer_id": SHARED_ID}, None),
    ("mempalace_get_drawers", {"drawer_ids": [PRIVATE_ID]}, {"drawer_ids": [SHARED_ID]}, None),
    ("mempalace_list_drawers", {"wing": "private_notes"}, {"wing": "shared"}, None),
    ("mempalace_list_drawers_all", {}, None, None),
    ("mempalace_list_wings", {}, None, "private_notes"),
    ("mempalace_get_taxonomy", {}, None, "private_notes"),
    ("mempalace_status", {}, None, "private_notes"),
    ("mempalace_graph_stats", {}, None, "private_notes"),
    ("mempalace_list_rooms", {"wing": "private_notes"}, {"wing": "shared"}, PRIVATE_ROOM),
    ("mempalace_list_rooms_all", {}, None, PRIVATE_ROOM),
    ("mempalace_kg_query", {"entity": "ScopeProbe"}, None, None),
    ("mempalace_kg_query_search", {"entity": "CANARY", "search": True, "limit": 100}, None, None),
    ("mempalace_kg_timeline", {"entity": "ScopeProbe"}, None, None),
    ("mempalace_kg_stats", {}, None, "private_notes"),
    ("mempalace_kg_list_wings", {}, None, "private_notes"),
    (
        "mempalace_traverse",
        {"start_room": PRIVATE_ROOM},
        {"start_room": SHARED_ROOM},
        "private_notes",
    ),
    ("mempalace_find_tunnels", {}, None, "private_notes"),
    ("mempalace_list_tunnels", {}, None, "private_notes"),
    ("mempalace_list_hallways", {}, None, "private_notes"),
    ("mempalace_follow_tunnels", {"wing": "shared", "room": SHARED_ROOM}, None, "private_notes"),
    (
        "mempalace_diary_read",
        {"agent_name": "scopeprobe", "wing": "private_notes"},
        {"agent_name": "scopeprobe", "wing": "shared"},
        PRIVATE,
    ),
    (
        "mempalace_check_duplicate",
        {"content": PRIVATE, "threshold": 0.0},
        {"content": SHARED, "threshold": 0.0},
        None,
    ),
]


def _name(case):
    return (
        "mempalace_kg_query" if case == "mempalace_kg_query_search" else case.removesuffix("_all")
    )


def _payload(response):
    status, body = response
    assert status == 200, response
    assert "error" not in body, body
    result = body["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def world(hub, kg, monkeypatch, tmp_path):
    from mempalace import hallways, mcp_server as mcp
    from mempalace.searcher import facts

    fixed_now = datetime.now(timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    # Keep the before/after oracle sensitive to counts and ranks, not elapsed time.
    monkeypatch.setattr(facts, "datetime", FixedDateTime)

    monkeypatch.setenv("COMMONPLACE_RULES", "off")
    monkeypatch.setattr(
        hallways, "_get_hallway_file", lambda *a, **k: str(tmp_path / "hallways.json")
    )
    monkeypatch.setattr(hallways, "_legacy_hallway_file", lambda: str(tmp_path / "hallways.json"))
    monkeypatch.setattr(mcp, "_node_profile_cache", {})
    monkeypatch.setattr(mcp, "_last_profile_drawer_count", None)
    uid = hub.db.create_user("member@example.test")
    hub.db.add_member(hub.workspace, uid, "member", actor=hub.admin)
    assert uid != hub.admin and hub.db.role(hub.workspace, uid) == "member"

    def cookie(email):
        token = sessions.create_login_link(hub.db, email, hub.workspace)
        value = sessions.redeem_login_link(hub.db, token)
        assert value
        return {"Cookie": f"{sessions.COOKIE}={value}"}

    owner, member = cookie("owner@example.com"), cookie("member@example.test")

    def http(headers, path, body=None):
        status, raw = hub.raw(
            None,
            b"" if body is None else body,
            headers=headers,
            method="GET" if body is None else "POST",
            path=path,
        )
        assert status < 500, (status, raw)
        return status, json.loads(raw)

    def rpc(headers, name, args=None):
        return http(
            headers,
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args or {}},
            },
        )

    def save(wing, text, room):
        status, result = http(owner, "/api/memory", {"text": text, "theme": wing, "topic": room})
        assert status == 201, result
        return result["id"]

    assert save("shared", SHARED, SHARED_ROOM) == SHARED_ID
    other = save("project_shared", SHARED, SHARED_ROOM)
    kg.add_triple(
        "ScopeProbe",
        "shared_probe",
        SHARED,
        wing="shared",
        sensitivity="internal",
        source_drawer_id=SHARED_ID,
    )
    _payload(
        rpc(
            owner,
            "mempalace_diary_write",
            {"agent_name": "scopeprobe", "entry": SHARED, "wing": "shared"},
        )
    )
    _payload(
        rpc(
            owner,
            "mempalace_create_tunnel",
            {
                "source_wing": "shared",
                "source_room": SHARED_ROOM,
                "target_wing": "project_shared",
                "target_room": SHARED_ROOM,
                "source_drawer_id": SHARED_ID,
                "target_drawer_id": other,
                "label": "shared link",
            },
        )
    )
    records = [
        {
            "id": "shared-hallway",
            "wing": "shared",
            "rooms": [SHARED_ROOM],
            "label": SHARED,
            "co_occurrence_count": 2,
        }
    ]
    hallways._save_hallways(records)
    assert hallways.list_hallways() == records

    def insert_private():
        assert save("private_notes", PRIVATE, PRIVATE_ROOM) == PRIVATE_ID
        kg.add_triple(
            "ScopeProbe",
            "private_probe",
            PRIVATE,
            wing="private_notes",
            sensitivity="private",
            source_drawer_id=PRIVATE_ID,
        )
        _payload(
            rpc(
                owner,
                "mempalace_diary_write",
                {"agent_name": "scopeprobe", "entry": PRIVATE, "wing": "private_notes"},
            )
        )
        _payload(
            rpc(
                owner,
                "mempalace_create_tunnel",
                {
                    "source_wing": "private_notes",
                    "source_room": PRIVATE_ROOM,
                    "target_wing": "shared",
                    "target_room": SHARED_ROOM,
                    "source_drawer_id": PRIVATE_ID,
                    "target_drawer_id": SHARED_ID,
                    "label": "private link",
                },
            )
        )
        # A same-name room in private storage makes inferred graph tunnels too.
        save("private_notes", PRIVATE + " bridge", SHARED_ROOM)
        hallways._save_hallways(
            [
                *records,
                {
                    "id": "private-hallway",
                    "wing": "private_notes",
                    "rooms": [PRIVATE_ROOM],
                    "label": PRIVATE,
                    "co_occurrence_count": 99,
                },
            ]
        )
        assert len(hallways.list_hallways()) == 2
        mcp._invalidate_overview_caches()

    def read(headers, case, shared=False):
        label, args, shared_args, _ = case
        chosen = shared_args if shared and shared_args is not None else args
        return (
            http(headers, chosen)
            if label.startswith("web_")
            else rpc(headers, _name(label), chosen)
        )

    w = SimpleNamespace(
        hub=hub,
        owner=owner,
        member=member,
        member_id=uid,
        rpc=rpc,
        http=http,
        save=save,
        insert_private=insert_private,
        read=read,
        mcp=mcp,
    )
    try:
        yield w
    finally:
        hub.gateway.handoff.close()
        hub.gateway.webhooks.stop()


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_private_read_path(world, case):
    w = world
    absent = w.read(w.member, case)
    w.insert_private()
    owner = w.read(w.owner, case)
    shared = w.read(w.member, case, shared=True)
    present = w.read(w.member, case)
    needle = case[3] or PRIVATE
    assert needle in json.dumps(owner), ("owner private control", owner)
    name = _name(case[0])
    if name in REFUSED:
        # The whole tool is hidden, using exactly the unknown-tool envelope.
        unavailable = w.rpc(w.member, "unavailable_scope_probe")
        expected = (
            unavailable[0],
            {
                **unavailable[1],
                "error": {**unavailable[1]["error"], "message": f"Unknown tool: {name}"},
            },
        )
        assert present == absent == expected, "whole-palace read remained available"
    else:
        shared_needle = (
            SHARED_ROOM
            if case[3] == PRIVATE_ROOM
            else "shared"
            if case[3] == "private_notes"
            else SHARED
        )
        assert shared_needle in json.dumps(shared), ("member shared control", shared)
        assert needle not in json.dumps(present), ("member received private data", present)
        assert present == absent, ("hidden data changed response or counts", present, absent)


def test_mesh_private_count(world):
    w = world
    name = "mempalace_mesh_peers"
    absent = w.rpc(w.member, name)
    before = _payload(w.rpc(w.owner, name))["self"]["profile"]["drawers"]
    assert isinstance(before, int) and before > 0
    # Insert exactly one private drawer, without diary, graph or shared changes.
    assert w.save("private_notes", PRIVATE, PRIVATE_ROOM) == PRIVATE_ID
    w.mcp._node_profile_cache.clear()
    after = _payload(w.rpc(w.owner, name))["self"]["profile"]["drawers"]
    assert after == before + 1, "owner must observe the isolated private count"
    present = w.rpc(w.member, name)
    unknown = w.rpc(w.member, "unavailable_scope_probe")
    expected = (
        unknown[0],
        {**unknown[1], "error": {**unknown[1]["error"], "message": f"Unknown tool: {name}"}},
    )
    assert absent == present == expected, "member sees private-dependent estate counts"


@pytest.mark.parametrize("role", [None, "", "member", "admin", "OWNER", "unexpected"])
def test_unproven_role_has_no_private_read(world, monkeypatch, role):
    w = world
    monkeypatch.setattr(w.hub.db, "role", lambda *args: role)
    principal = sessions.person_principal(w.hub.db, w.hub.admin, w.hub.workspace)
    if principal is not None:
        assert not principal.policy.has_scope("private:read")
        assert principal.policy.can_read_wing("shared")
        assert not principal.policy.can_read_wing("private_notes")
    assert not sessions.person_policy().has_scope("private:read")


def test_owner_policy_and_current_role(world, monkeypatch):
    w = world
    owner = sessions.person_principal(w.hub.db, w.hub.admin, w.hub.workspace)
    assert owner.policy.has_scope("private:read") and owner.policy.sees_all_wings
    original = w.hub.db.role
    monkeypatch.setattr(
        w.hub.db, "role", lambda ws, uid: "unexpected" if uid == w.hub.admin else original(ws, uid)
    )
    changed = sessions.person_principal(w.hub.db, w.hub.admin, w.hub.workspace)
    assert changed is not None and not changed.policy.has_scope("private:read")
    monkeypatch.setattr(w.hub.db, "role", lambda *args: None)
    assert sessions.person_principal(w.hub.db, w.hub.admin, w.hub.workspace) is None
