"""Graph projection protocol faults and live authorization boundaries."""


# ruff: noqa: F811

import copy

import pytest

from tests._commonplace_helpers import functional_worker_transport, hub_fixture  # noqa: F401
from tests.test_card253_s05 import call, seed, world  # noqa: F401

hub = hub_fixture


@pytest.mark.parametrize("fault", ["raise", "shape", "duplicate", "cursor", "final", "tunnels"])
def test_graph_failure_is_empty(world, functional_worker_transport, monkeypatch, fault):
    from commonplace.gateway import WorkerClient

    w = world
    seed(w, [{"wing": "wing_a", "room": "visible"}])
    assert call(w, "mempalace_graph_stats", {})["total_rooms"] == 1
    original = WorkerClient.call_tool

    def changed(self, principal, name, args):
        scope = args.get("thread_scope", {})
        result = original(self, principal, name, args)
        if name == "mempalace_list_tunnels" and fault == "tunnels":
            return {"error": "secret diagnostic"}
        if name != "mempalace_graph_stats":
            return result
        if scope.get("probe"):
            if fault == "raise":
                raise RuntimeError("secret diagnostic")
            if fault == "shape":
                return {"error": "secret diagnostic"}
            if fault == "duplicate":
                result["rows"] *= 2
            if fault == "cursor":
                result["next"] = "broken"
        elif fault == "final":
            return {"error": "secret diagnostic"}
        return result

    monkeypatch.setattr(WorkerClient, "call_tool", changed)
    reply = call(w, "mempalace_graph_stats", {})
    assert reply == {
        "total_rooms": 0,
        "total_room_instances": 0,
        "tunnel_rooms": 0,
        "passive_tunnel_rooms": 0,
        "explicit_tunnels": 0,
        "total_edges": 0,
        "total_connections": 0,
        "rooms_per_wing": {},
        "top_tunnels": [],
    }


@pytest.mark.parametrize("change", ["move", "date", "new", "unrelated", "revoke"])
def test_graph_final_revalidation(world, functional_worker_transport, monkeypatch, change):
    from commonplace.gateway import WorkerClient

    w = world
    seed(w, [{"wing": "wing_a", "room": w.readable.id, "date": "2026"}])
    assert call(w, "mempalace_graph_stats", {})["total_rooms"] == 1
    original = WorkerClient.call_tool
    changed = []

    def raced(self, principal, name, args):
        result = original(self, principal, name, args)
        if (
            name == "mempalace_graph_stats"
            and args.get("thread_scope", {}).get("probe")
            and not changed
        ):
            changed.append(True)
            if change in ("move", "date", "unrelated"):
                meta = {
                    "move": {"room": w.hidden.id},
                    "date": {"date": "2099"},
                    "unrelated": {"note": "kept"},
                }[change]
                w.col._inner.update(ids=["row-0000"], metadatas=[meta])
            elif change == "new":
                w.col._inner.add(
                    ids=["new"],
                    documents=["words"],
                    metadatas=[{"wing": "wing_b", "room": "new"}],
                    embeddings=[[1.0] + [0.0] * 383],
                )
            else:
                store = w.h.gateway.handoff.store_for(w.h.workspace)
                with store._lock:
                    store._conn.execute(
                        "UPDATE threads SET wing='private_chats', source_thread_id=? WHERE id=?",
                        (f"capture:{w.h.admin}:revoked", w.readable.id),
                    )
        return result

    monkeypatch.setattr(WorkerClient, "call_tool", raced)
    reply = call(w, "mempalace_graph_stats", {})
    assert changed
    assert reply["total_rooms"] == (1 if change in ("unrelated", "new") else 0)


def test_graph_complete_pages_and_cache(world, functional_worker_transport):
    from commonplace.sessions import person_principal
    from mempalace import palace_graph as graph

    w = world
    rows = [{"wing": "wing_a", "room": f"visible-{i:04}"} for i in range(505)]
    rows.append({"wing": "wing_b", "room": w.hidden.id})
    seed(w, rows)
    raw = graph.build_graph(config=w.mcp._config)
    saved = copy.deepcopy(raw)
    assert len(raw[0]) == 506
    a = call(w, "mempalace_graph_stats", {})
    assert a["total_rooms"] == 505
    owner = person_principal(w.h.db, w.h.admin, w.h.workspace)
    assert w.h.gateway.call_memory_tool(owner, "mempalace_graph_stats", {})["total_rooms"] == 506
    assert call(w, "mempalace_graph_stats", {}) == a
    assert raw == saved
    assert len(call(w, "mempalace_traverse", {"start_room": "visible-0000"})) == 50
    suggestions = call(w, "mempalace_traverse", {"start_room": "thr"})
    assert suggestions == {"error": "Room 'thr' not found", "suggestions": []}


def test_graph_deadline(world, monkeypatch):
    from commonplace.gateway import WorkerClient
    from commonplace.handoff.graph import project
    from commonplace.rules import checks

    w = world
    seed(w, [{"wing": "wing_a", "room": "visible"}])
    principal = w.h.db.authenticate_token(w.tokens["A"])
    assert (
        project(
            w.h.gateway,
            principal,
            w.h.gateway.mediator.workers,
            "mempalace_graph_stats",
            {},
            timeout=60,
        )["total_rooms"]
        == 1
    )
    original = WorkerClient.call_tool

    def expired(self, principal, name, args):
        result = original(self, principal, name, args)
        monkeypatch.setattr(checks, "remaining", lambda _: (_ for _ in ()).throw(TimeoutError()))
        return result

    monkeypatch.setattr(WorkerClient, "call_tool", expired)
    assert (
        project(
            w.h.gateway,
            principal,
            w.h.gateway.mediator.workers,
            "mempalace_graph_stats",
            {},
            timeout=60,
        )["total_rooms"]
        == 0
    )


def test_owner_still_requires_thread_authority(world, functional_worker_transport):
    from commonplace.handoff.models import ThreadMessage
    from commonplace.sessions import person_principal

    w = world
    other = w.h.db.create_user("another@example.test")
    w.h.db.add_member(w.h.workspace, other, "member", actor=w.h.admin)
    store = w.h.gateway.handoff.store_for(w.h.workspace)
    hidden = store.store(
        w.h.workspace,
        f"capture:{other}:private",
        [ThreadMessage(0, "user", "message", "private", "private")],
        actor=other,
        wing="private_chats",
    )
    owner = person_principal(w.h.db, w.h.admin, w.h.workspace)
    assert owner.person and owner.role == "owner" and owner.policy.has_scope("threads:read")
    assert not w.h.gateway.handoff.recall.can_read(owner, hidden.id)
    seed(w, [{"wing": "wing_a", "room": "visible"}, {"wing": "wing_b", "room": hidden.id}])
    reply = w.h.gateway.call_memory_tool(owner, "mempalace_graph_stats", {})
    assert reply["total_rooms"] == 1 and reply["rooms_per_wing"] == {"wing_a": 1}


def test_projection_keeps_legacy_readable_ties(world, functional_worker_transport, monkeypatch):
    w = world
    # SQL graph grouping orders room names; collection traversal keeps storage order.
    seed(
        w,
        [
            {"wing": wing, "room": room, "hall": "h"}
            for room in ("z-first", "a-second")
            for wing in ("wing_a", "wing_b")
        ],
    )
    sql = call(w, "mempalace_find_tunnels", {})
    assert [row["room"] for row in sql] == ["a-second", "z-first"]
    monkeypatch.setattr(w.mcp, "_selected_backend_name", lambda: "collection_fixture")
    client = call(w, "mempalace_find_tunnels", {})
    assert [row["room"] for row in client] == ["z-first", "a-second"]
    assert sorted(sql, key=lambda row: row["room"]) == sorted(client, key=lambda row: row["room"])
    assert call(w, "mempalace_find_tunnels", {"wing_a": " "}) == client
    assert call(w, "mempalace_traverse", {"start_room": "missing"}) == {
        "error": "Room 'missing' not found",
        "suggestions": [],
    }


def test_scope_is_internal_and_empty_allow_is_empty(world, functional_worker_transport):
    from mempalace.graph_scope import scoped_rows

    w = world
    seed(w, [{"wing": "wing_a", "room": "visible"}])
    expected = call(w, "mempalace_graph_stats", {})
    assert expected["total_rooms"] == 1
    assert call(w, "mempalace_graph_stats", {"thread_scope": {"allow": []}}) == expected
    assert scoped_rows(
        {"allow": [], "timeout": 60}, w.mcp._config, "sqlite_exact", w.mcp._get_collection
    ) == {"rows": []}
    tools = w.h.rpc(w.tokens["A"], "tools/list")["result"]["tools"]
    assert all(
        "thread_scope" not in tool.get("inputSchema", {}).get("properties", {}) for tool in tools
    )


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_stats_require_both_tunnel_endpoints(world, functional_worker_transport, endpoint):
    from mempalace import palace_graph as graph

    w = world
    seed(w, [{"wing": "wing_a", "room": "visible"}])
    row = {
        "id": "endpoint",
        "source": {"wing": "wing_a", "room": "visible"},
        "target": {"wing": "wing_b", "room": "ordinary"},
    }
    graph._save_tunnels([row], w.mcp._config)
    assert call(w, "mempalace_graph_stats", {})["explicit_tunnels"] == 1
    row[endpoint]["room"] = w.hidden.id
    graph._save_tunnels([row], w.mcp._config)
    assert graph._load_tunnels(w.mcp._config) == [row]
    assert call(w, "mempalace_graph_stats", {})["explicit_tunnels"] == 0


@pytest.mark.parametrize(
    "listing",
    [
        None,
        {"result": {"tools": []}},
        {"result": {"tools": [{"name": "mempalace_graph_stats", "inputSchema": {}}]}},
    ],
)
def test_graph_schema_fault_is_empty(world, functional_worker_transport, monkeypatch, listing):
    w = world
    seed(w, [{"wing": "wing_a", "room": "visible"}])
    assert call(w, "mempalace_graph_stats", {})["total_rooms"] == 1
    monkeypatch.setattr(w.h.gateway.mediator, "_tools_list", lambda *args: listing)
    assert call(w, "mempalace_graph_stats", {})["total_rooms"] == 0


@pytest.mark.parametrize("label", [None, "absent", 0, False, [], {}])
def test_tunnel_label_type_guard(world, functional_worker_transport, label):
    from mempalace import palace_graph as graph

    w = world
    seed(w, [{"wing": "wing_a", "room": "visible"}])
    row = {
        "id": "label-type",
        "source": {"wing": "wing_a", "room": "visible"},
        "target": {"wing": "wing_b", "room": "ordinary"},
    }
    if label != "absent":
        row["label"] = label
    graph._save_tunnels([row], w.mcp._config)
    expected = [row] if label is None or label == "absent" else []
    assert call(w, "mempalace_list_tunnels", {}) == expected
    assert call(w, "mempalace_graph_stats", {})["explicit_tunnels"] == len(expected)
    assert graph._load_tunnels(w.mcp._config) == [row]
