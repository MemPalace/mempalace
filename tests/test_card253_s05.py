"""Current endpoint proof for explicit tunnel reads."""

# ruff: noqa: F811

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace.gateway import _tool_result
from commonplace.handoff.models import ThreadMessage
from tests._commonplace_helpers import functional_worker_transport, hub_fixture  # noqa: F401

TOOLS = [
    "mempalace_traverse",
    "mempalace_find_tunnels",
    "mempalace_graph_stats",
    "mempalace_list_tunnels",
    "mempalace_follow_tunnels",
]
hub = hub_fixture


@pytest.fixture
def world(hub, monkeypatch, tmp_path, request):
    from mempalace import mcp_server as mcp, palace_graph as graph

    monkeypatch.setattr(graph, "MempalaceConfig", lambda: mcp._config)
    monkeypatch.setattr(mcp, "_READ_ONLY", True)
    backend_name = getattr(request, "param", "sqlite_exact")
    monkeypatch.setenv("MEMPALACE_BACKEND", backend_name)
    monkeypatch.setattr(mcp._config, "_palace_path_override", str(tmp_path / "exact"))
    from mempalace.backends import PalaceRef
    from mempalace.backends.chroma import ChromaBackend
    from mempalace.backends.compact import CompactBackend
    from mempalace.backends.rust_exact import RustExactBackend
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend = {
        "sqlite_exact": SQLiteExactBackend,
        "rust_exact": RustExactBackend,
        "chroma": ChromaBackend,
        "compact": CompactBackend,
    }[backend_name]()
    collection = backend.get_collection(
        palace=PalaceRef(id=mcp._config.palace_path, local_path=mcp._config.palace_path),
        collection_name=mcp._config.collection_name,
        create=True,
    )
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    collection = EmbeddingCollection(collection)
    monkeypatch.setattr(mcp, "_get_collection", lambda **kwargs: collection)
    monkeypatch.setattr(mcp, "_selected_backend_name", lambda: backend_name)
    monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: backend_name == "chroma")
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    monkeypatch.setenv("COMMONPLACE_RULES", "off")
    member = hub.db.create_user("member@example.test")
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
    store = hub.gateway.handoff.store_for(hub.workspace)
    readable = store.store(
        hub.workspace,
        "native-readable",
        [ThreadMessage(0, "user", "message", "readable", "readable")],
        actor=member,
    )
    hidden = store.store(
        hub.workspace,
        f"capture:{hub.admin}:native-hidden",
        [ThreadMessage(0, "user", "message", "hidden", "hidden")],
        actor=hub.admin,
        wing="private_chats",
    )

    class Tokens(dict):
        def __repr__(self):
            return "<synthetic credentials>"

    tokens = Tokens()
    for kind, wings in (("A", ["*"]),):
        tokens[kind] = hub.token(
            f"reader-{kind}",
            user=member,
            tools=TOOLS,
            scopes=["memory:read", "private:read", "threads:read"],
            read_wings=wings,
            trust="trusted",
        )
        principal = hub.db.authenticate_token(tokens[kind])
        assert principal.role != "owner" and principal.policy.trust == "trusted"
        assert all(
            principal.policy.has_scope(scope)
            for scope in ("memory:read", "private:read", "threads:read")
        )
        assert principal.policy.sees_all_wings is (kind == "A")
        assert hub.gateway.handoff.recall.can_read(principal, readable.id)
        assert not hub.gateway.handoff.recall.can_read(principal, hidden.id)
        assert store.get(hidden.id) is not None
        for tool in TOOLS:
            assert principal.policy.refusal(tool) is None
    hits = []
    for name in (
        "traverse",
        "find_tunnels",
        "graph_stats",
        "_sqlite_graph_stats",
        "list_tunnels",
        "follow_tunnels",
    ):
        original = getattr(mcp, name)

        def spy(*a, _name=name, _original=original, **kw):
            hits.append(_name)
            return _original(*a, **kw)

        monkeypatch.setattr(mcp, name, spy)
    from commonplace.gateway import WorkerClient

    worker_call = WorkerClient.call_tool

    def worker_spy(self, principal, name, args):
        hits.append(name)
        return worker_call(self, principal, name, args)

    monkeypatch.setattr(WorkerClient, "call_tool", worker_spy)
    mcp._invalidate_overview_caches()
    yield SimpleNamespace(
        h=hub, col=collection, mcp=mcp, readable=readable, hidden=hidden, tokens=tokens, hits=hits
    )
    hub.gateway.handoff.close()
    hub.gateway.webhooks.stop()
    backend.close()


def call(w, tool, args):
    args = dict(args)
    # The base RPC schema omits record; read-only mode passes record=False.
    args.pop("record", None)
    status, raw = w.h.raw(
        w.tokens["A"],
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
    )
    assert status == 200
    result = _tool_result(json.loads(raw))
    assert result is not None, raw
    return result


def seed(w, rows):
    from mempalace import palace_graph as graph

    w.col._inner.add(
        ids=[f"row-{i:04}" for i in range(len(rows))],
        documents=["verbatim" for _ in rows],
        metadatas=rows,
        embeddings=[[1.0] + [0.0] * 383 for _ in rows],
    )
    if hasattr(w, "after_seed"):
        w.after_seed()
    graph.invalidate_graph_cache()
    w.mcp._invalidate_overview_caches()


def control(w, rows, monkeypatch):
    from mempalace import palace_graph as graph

    source = Path(w.mcp._config.palace_path)
    target = source.parent / "control" / "palace"
    shutil.copytree(source, target)
    w.mcp._config._palace_path_override = str(target)
    from mempalace.backends import PalaceRef
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend = SQLiteExactBackend()
    col = backend.get_collection(
        palace=PalaceRef(id=str(target), local_path=str(target)),
        collection_name=w.mcp._config.collection_name,
        create=False,
    )
    col.delete(ids=[f"row-{i:04}" for i in range(2, len(rows))])
    monkeypatch.setattr(w.mcp, "_get_collection", lambda **kw: col)
    graph._save_tunnels([], w.mcp._config)
    graph.invalidate_graph_cache()
    w.mcp._invalidate_overview_caches()
    return backend


@pytest.mark.parametrize("path", ["sqlite", "collection"])
def test_card253_s05_stats(world, functional_worker_transport, monkeypatch, path):
    w = world
    if path == "collection":
        monkeypatch.setattr(w.mcp, "_selected_backend_name", lambda: "collection_fixture")
    rows = [
        {"wing": "wing_a", "room": "visible", "hall": "h"},
        {"wing": "wing_b", "room": "visible", "hall": "h"},
        {"wing": "private_chats", "room": w.hidden.id, "hall": "secret"},
    ]
    seed(w, rows)
    from mempalace import palace_graph as graph

    graph._save_tunnels(
        [
            {
                "id": "hidden-tunnel",
                "source": {"wing": "wing_a", "room": "visible"},
                "target": {"wing": "private_chats", "room": w.hidden.id},
            }
        ],
        w.mcp._config,
    )
    reply = call(w, "mempalace_graph_stats", {})
    assert any(
        name in w.hits for name in ("mempalace_graph_stats", "graph_stats", "_sqlite_graph_stats")
    )
    backend = control(w, rows, monkeypatch)
    try:
        expected = call(w, "mempalace_graph_stats", {})
        assert expected["total_rooms"] == 1 and expected["total_room_instances"] == 2
        assert reply == expected, "s05 graph counts include H"
    finally:
        backend.close()


def test_card253_s05_traverse(world, functional_worker_transport):
    w = world
    seed(
        w,
        [
            {"wing": "wing_a", "room": "readable_start"},
            {"wing": "wing_b", "room": "distant_room"},
            {"wing": "wing_a", "room": w.hidden.id},
            {"wing": "wing_b", "room": w.hidden.id},
        ],
    )
    reply = call(w, "mempalace_traverse", {"start_room": "readable_start", "max_hops": 2})
    assert ("mempalace_traverse" in w.hits or "traverse" in w.hits) and isinstance(reply, list)
    assert "readable_start" in [r["room"] for r in reply]
    assert "distant_room" not in [r["room"] for r in reply], "s05 H bridged readable nodes"


def test_card253_s05_find_top(world, functional_worker_transport, monkeypatch):
    w = world
    store = w.h.gateway.handoff.store_for(w.h.workspace)
    rows = [
        {"wing": wing, "room": "visible_candidate", "hall": "h"} for wing in ("wing_a", "wing_b")
    ]
    for i in range(50):
        thread = store.store(
            w.h.workspace,
            f"capture:{w.h.admin}:hidden-{i}",
            [ThreadMessage(0, "user", "message", "hidden", "hidden")],
            actor=w.h.admin,
            wing="private_chats",
        )
        principal = w.h.db.authenticate_token(w.tokens["A"])
        assert not w.h.gateway.handoff.recall.can_read(principal, thread.id)
        rows.extend(
            {"wing": wing, "room": thread.id, "hall": "secret", "date": "2099"}
            for wing in ("wing_a", "wing_b", "wing_a")
        )
    seed(w, rows)
    args = {"wing_a": "wing_a", "wing_b": "wing_b"}
    reply = call(w, "mempalace_find_tunnels", args)
    assert "mempalace_find_tunnels" in w.hits or "find_tunnels" in w.hits
    backend = control(w, rows, monkeypatch)
    try:
        expected = call(w, "mempalace_find_tunnels", args)
        assert [r["room"] for r in expected] == ["visible_candidate"]
        assert reply == expected, "s05 H displaced visible graph candidate"
    finally:
        backend.close()


@pytest.mark.parametrize(
    "tool", ["mempalace_list_tunnels", "mempalace_follow_tunnels", "mempalace_graph_stats"]
)
def test_card253_s05_label(world, functional_worker_transport, tool):
    from mempalace import palace_graph as graph

    w = world
    seed(w, [{"wing": "wing_a", "room": "visible"}])
    row = {
        "id": "label-tunnel",
        "source": {"wing": "wing_a", "room": "visible"},
        "target": {"wing": "wing_b", "room": "ordinary"},
        "label": f"see {w.hidden.id}",
    }
    graph._save_tunnels([row], w.mcp._config)
    assert graph._load_tunnels(w.mcp._config) == [row]
    args = {"wing": "wing_a", "room": "visible"} if tool == "mempalace_follow_tunnels" else {}
    reply = call(w, tool, args)
    assert "mempalace_list_tunnels" in w.hits or tool == "mempalace_graph_stats"
    assert reply["explicit_tunnels"] == 0 if tool == "mempalace_graph_stats" else reply == [], (
        "s05 hidden tunnel label retained"
    )


@pytest.mark.parametrize("path", ["sqlite", "collection"])
def test_card253_s05_readable_guard(world, functional_worker_transport, monkeypatch, path):
    from mempalace import palace_graph as graph

    w = world
    if path == "collection":
        monkeypatch.setattr(w.mcp, "_selected_backend_name", lambda: "collection_fixture")
        monkeypatch.setattr(w.mcp, "_graph_sqlite_reader", lambda: None)
    rows = [
        {"wing": wing, "room": "visible", "hall": "hall", "date": date, "note": "stored untouched"}
        for wing, date in (("wing_a", "2026-01"), ("wing_b", "2026-02"))
    ]
    seed(w, rows)
    row = {
        "id": "readable",
        "source": {"wing": "wing_a", "room": "visible"},
        "target": {"wing": "wing_b", "room": "ordinary"},
        "label": "👩‍💻 50% ready",
        "custom": {"preserve": True},
        "access_count": 9,
    }
    graph._save_tunnels([row], w.mcp._config)
    assert call(w, "mempalace_list_tunnels", {}) == [row]
    assert (
        call(w, "mempalace_follow_tunnels", {"wing": "wing_a", "room": "visible"})[0]["label"]
        == row["label"]
    )
    found = call(w, "mempalace_find_tunnels", {"wing_a": "wing-a"})
    assert found == [
        {
            "room": "visible",
            "wings": ["wing_a", "wing_b"],
            "halls": ["hall"],
            "count": 2,
            "recent": "2026-02",
        }
    ]
    stats = call(w, "mempalace_graph_stats", {})
    assert stats == {
        "total_rooms": 1,
        "total_room_instances": 2,
        "tunnel_rooms": 1,
        "passive_tunnel_rooms": 1,
        "explicit_tunnels": 1,
        "total_edges": 1,
        "total_connections": 2,
        "rooms_per_wing": {"wing_a": 1, "wing_b": 1},
        "top_tunnels": [{"room": "visible", "wings": ["wing_a", "wing_b"], "count": 2}],
    }
    assert graph._load_tunnels(w.mcp._config) == [row]
    assert (
        w.col.get(ids=["row-0000"], include=["metadatas"])["metadatas"][0]["note"]
        == "stored untouched"
    )


def test_card253_s05_literal_guard(world):
    w = world
    w.tokens["A"] = w.h.token(
        "literal",
        user=w.h.db.authenticate_token(w.tokens["A"]).user_id,
        tools=TOOLS,
        scopes=["memory:read", "threads:read"],
        read_wings=["wing_a"],
        trust="trusted",
    )
    principal = w.h.db.authenticate_token(w.tokens["A"])
    assert principal.role != "owner" and not principal.policy.sees_all_wings
    for tool, args in (
        ("mempalace_graph_stats", {}),
        ("mempalace_find_tunnels", {}),
        ("mempalace_traverse", {"start_room": "visible"}),
    ):
        assert principal.policy.refusal(tool) is not None
        status, raw = w.h.raw(
            w.tokens["A"],
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            },
        )
        assert status == 200 and json.loads(raw)["error"]["code"] == -32601


def test_card253_s05_owner_guard(world, functional_worker_transport):
    from commonplace.sessions import person_principal

    w = world
    owner = person_principal(w.h.db, w.h.admin, w.h.workspace)
    assert owner.person and owner.role == "owner" and owner.policy.trust == "trusted"
    assert owner.policy.has_scope("threads:read")
    assert w.h.gateway.handoff.recall.can_read(owner, w.hidden.id)
    seed(w, [{"wing": "wing_a", "room": w.hidden.id}, {"wing": "wing_b", "room": w.hidden.id}])
    assert (
        w.h.gateway.call_memory_tool(owner, "mempalace_graph_stats", {})["total_room_instances"]
        == 2
    )
    assert (
        w.h.gateway.call_memory_tool(owner, "mempalace_find_tunnels", {})[0]["room"] == w.hidden.id
    )


@pytest.mark.parametrize(
    "args",
    [
        {"max_hops": []},
        {"max_hops": None},
        {"max_hops": "2"},
        {"start_room": []},
        {"max_hops": True},
        {"max_hops": 2.0},
    ],
)
def test_card253_s05_odd_inputs(world, functional_worker_transport, args):
    w = world
    seed(w, [{"wing": "wing_a", "room": "visible"}])
    expected = call(w, "mempalace_traverse", {"start_room": "visible"})
    reply = rpc(w, "mempalace_traverse", {"start_room": "visible", **args})
    if args.get("max_hops") is True or args.get("max_hops") == [] or args.get("start_room") == []:
        key = "start_room" if "start_room" in args else "max_hops"
        kind = "string" if key == "start_room" else "integer"
        assert reply.get("error") == {
            "code": -32602,
            "message": f"Invalid params for tool mempalace_traverse: '{key}' must be a{'' if kind == 'string' else 'n'} {kind}",
        }, "graph argument error shape changed"
    else:
        assert _tool_result(reply) == expected, "graph argument coercion changed"


def rpc(w, tool, args):
    status, raw = w.h.raw(
        w.tokens["A"],
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
    )
    assert status == 200
    return json.loads(raw)


@pytest.mark.parametrize("world", ["sqlite_exact", "chroma"], indirect=True)
@pytest.mark.parametrize("legacy", ["wing-question", "room-question", "registry"])
def test_card253_s05_r1_legacy(world, functional_worker_transport, legacy):
    w = world
    rows = [{"wing": wing, "room": "visible", "hall": "h"} for wing in ("wing_a", "wing_b")]
    if legacy == "wing-question":
        rows.append({"wing": "?", "room": "legacy", "hall": "h"})
    elif legacy == "room-question":
        rows.append({"wing": "wing_a", "room": "?", "hall": "h"})
    else:
        rows.extend(
            {"wing": wing, "room": "_registry", "ingest_mode": "registry", "hall": "h"}
            for wing in ("wing_a", "wing_b")
        )
    seed(w, rows)
    expected = w.mcp.tool_graph_stats()
    assert expected["total_rooms"] == 2
    reply = call(w, "mempalace_graph_stats", {})
    assert "mempalace_graph_stats" in w.hits
    assert reply == expected, "readable legacy graph stats changed"
    assert call(w, "mempalace_find_tunnels", {}) == w.mcp.tool_find_tunnels(), (
        "readable legacy ranking changed"
    )
    assert call(w, "mempalace_traverse", {"start_room": "visible"}) == w.mcp.tool_traverse_graph(
        "visible"
    ), "readable legacy traversal changed"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("mempalace_graph_stats", {"bogus": 1}),
        ("mempalace_find_tunnels", {"max_hops": 2}),
        ("mempalace_traverse", {"start_room": "visible", "bogus": True}),
        ("mempalace_graph_stats", {"x": None, "y": 2}),
    ],
)
def test_card253_s05_r1_unknown_arguments(world, tool, args):
    w = world
    unknown = [key for key in args if key != "start_room"]
    quoted = ", ".join(f"'{key}'" for key in unknown)
    word = "parameter" if len(unknown) == 1 else "parameters"
    reply = rpc(w, tool, args)
    assert reply == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": f"Unknown {word} {quoted} for tool {tool}"},
    }, "unknown graph argument lost its RPC error"
    assert not w.hits, "unknown argument reached graph probe"


@pytest.mark.parametrize("world", ["sqlite_exact", "chroma"], indirect=True)
@pytest.mark.parametrize(
    "tool", ["mempalace_list_tunnels", "mempalace_follow_tunnels", "mempalace_graph_stats"]
)
def test_card253_s05_r1_null_label(world, functional_worker_transport, tool):
    from mempalace import palace_graph as graph

    w = world
    seed(w, [{"wing": "wing_a", "room": "visible"}])
    row = {
        "id": "null-label",
        "source": {"wing": "wing_a", "room": "visible"},
        "target": {"wing": "wing_b", "room": "ordinary"},
        "label": None,
        "extra": "preserve",
    }
    graph._save_tunnels([row], w.mcp._config)
    assert graph._load_tunnels(w.mcp._config) == [row]
    args = {"wing": "wing_a", "room": "visible"} if tool == "mempalace_follow_tunnels" else {}
    local = {
        "mempalace_list_tunnels": w.mcp.tool_list_tunnels,
        "mempalace_follow_tunnels": w.mcp.tool_follow_tunnels,
        "mempalace_graph_stats": w.mcp.tool_graph_stats,
    }[tool]
    expected = local(**args)
    reply = call(w, tool, args)
    assert reply == expected, "null label removed readable tunnel"
    assert graph._load_tunnels(w.mcp._config) == [row]


@pytest.mark.parametrize("world", ["chroma"], indirect=True)
def test_card253_s05_r1_chroma_zero_hall(world, functional_worker_transport):
    w = world
    seed(w, [{"wing": wing, "room": "visible", "hall": 0} for wing in ("wing_a", "wing_b")])
    expected = w.mcp.tool_graph_stats()
    assert expected["total_edges"] == 1
    assert call(w, "mempalace_graph_stats", {}) == expected, "Chroma zero hall lost its edge"
    assert call(w, "mempalace_find_tunnels", {}) == w.mcp.tool_find_tunnels(), (
        "Chroma zero hall changed ranking metadata"
    )


@pytest.mark.parametrize("world", ["chroma"], indirect=True)
def test_card253_s05_r1_chroma_fallback_order(world, functional_worker_transport, monkeypatch):
    from mempalace import read_scope

    w = world
    seed(
        w,
        [
            {"wing": wing, "room": room, "hall": "h"}
            for room in ("z-first", "a-second")
            for wing in ("wing_a", "wing_b")
        ],
    )
    monkeypatch.setattr(read_scope, "_chroma_page", lambda *a: None)
    monkeypatch.setattr(w.mcp, "_graph_sqlite_reader", lambda: None)
    monkeypatch.setattr(w.mcp, "_sqlite_graph_stats", lambda: None)
    expected = w.mcp.tool_find_tunnels()
    assert [row["room"] for row in expected] == ["z-first", "a-second"]
    assert call(w, "mempalace_find_tunnels", {}) == expected, (
        "Chroma fallback changed readable ties"
    )


@pytest.mark.parametrize("world", ["sqlite_exact", "chroma", "compact"], indirect=True)
@pytest.mark.parametrize("operation", ["siblings", "suggestions", "cap"])
def test_card253_s05_r2_order(world, functional_worker_transport, operation):
    w = world
    if operation == "cap":
        rows = [{"wing": wing, "room": "start", "hall": "h"} for wing in ("wing_a", "wing_c")]
        rows += [{"wing": "wing_a", "room": "solo", "hall": "h"}]
        rows += [{"wing": "wing_c", "room": f"r{i:02}", "hall": "h"} for i in range(60)]
        args = {"start_room": "start", "max_hops": 1}
    else:
        rows = [
            {"wing": wing, "room": room, "hall": "h"}
            for room, wings in (
                ("z-first", ("wing_a", "wing_c")),
                ("a-second", ("wing_b", "wing_c")),
            )
            for wing in wings
        ]
        rows += [{"wing": "wing_c", "room": "start", "hall": "h"}]
        args = {"start_room": "" if operation == "suggestions" else "start", "max_hops": 1}
    seed(w, rows)
    expected = w.mcp.tool_traverse_graph(**args)
    if operation == "cap":
        assert isinstance(expected, list) and len(expected) == 50
    elif operation == "siblings":
        assert len(expected) == 3 and expected[1]["count"] == expected[2]["count"] == 2
    else:
        assert len(expected["suggestions"]) == 3
    assert call(w, "mempalace_traverse", args) == expected, "graph readable order or cap changed"
    if operation == "siblings":
        assert call(w, "mempalace_find_tunnels", {}) == w.mcp.tool_find_tunnels(), (
            "graph sibling ranking changed"
        )
    assert call(w, "mempalace_graph_stats", {}) == w.mcp.tool_graph_stats(), (
        "graph stats tie convention changed"
    )


@pytest.mark.parametrize("world", ["sqlite_exact", "chroma"], indirect=True)
@pytest.mark.parametrize(
    "field,value",
    [
        ("wing", 7),
        ("wing", 7.5),
        ("wing", 1e20),
        ("wing", 0),
        ("room", 7),
        ("room", 7.5),
        ("room", 1e20),
        ("room", 0),
        ("hall", 0),
        ("hall", 0.0),
        ("hall", 7),
        ("hall", 7.5),
    ],
)
def test_card253_s05_r2_numeric(world, functional_worker_transport, field, value):
    w = world
    rows = [{"wing": "wing_c", "room": "start", "hall": "h"}]
    if field == "hall":
        rows += [{"wing": wing, "room": "number", "hall": value} for wing in ("wing_a", "wing_c")]
    else:
        rows += [{"wing": "wing_c", "room": "number", "hall": "h", field: value}]
    seed(w, rows)
    expected = w.mcp.tool_graph_stats()
    assert expected["total_rooms"] == 2
    assert call(w, "mempalace_graph_stats", {}) == expected, (
        "numeric graph placement or edge changed"
    )
    assert call(w, "mempalace_find_tunnels", {}) == w.mcp.tool_find_tunnels(), (
        "numeric graph hall changed"
    )
    assert call(w, "mempalace_traverse", {"start_room": "start"}) == w.mcp.tool_traverse_graph(
        "start"
    ), "numeric graph traversal changed"


@pytest.mark.parametrize("world", ["sqlite_exact", "compact"], indirect=True)
@pytest.mark.parametrize("field", ["wing", "room", "hall"])
@pytest.mark.parametrize("value", [10**30, -(10**30), True, False, 7, 1e20])
def test_card253_s05_r3_native_loci(world, functional_worker_transport, field, value):
    w = world
    rows = [{"wing": "wing_c", "room": "start", "hall": "h"}]
    rows += [
        {"wing": wing, "room": "number", "hall": "h", field: value} for wing in ("wing_a", "wing_c")
    ]
    seed(w, rows)
    expected = w.mcp.tool_graph_stats()
    assert expected["total_rooms"] >= 1
    assert call(w, "mempalace_graph_stats", {}) == expected, "native locus statistics changed"
    assert call(w, "mempalace_find_tunnels", {}) == w.mcp.tool_find_tunnels(), (
        "native locus ranking changed"
    )
    assert call(w, "mempalace_traverse", {"start_room": "start"}) == w.mcp.tool_traverse_graph(
        "start"
    ), "native locus traversal changed"


@pytest.mark.parametrize("operation", ["siblings", "suggestions", "cap"])
def test_card253_s05_r3_locusless(world, functional_worker_transport, operation):
    import sqlite3

    from mempalace.backends.sqlite_exact import _documents_has_locus_columns

    def remove_loci():
        path = Path(world.mcp._config.palace_path) / "sqlite_exact.sqlite3"
        with sqlite3.connect(path) as conn:
            indexes = conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='documents'"
            ).fetchall()
            for name, sql in indexes:
                if sql and any(key in sql for key in ("wing", "room", "hall")):
                    conn.execute('DROP INDEX "' + name.replace('"', '""') + '"')
            for key in ("wing", "room", "hall"):
                conn.execute(f"ALTER TABLE documents DROP COLUMN {key}")
            assert not _documents_has_locus_columns(conn)

    world.after_seed = remove_loci
    test_card253_s05_r2_order(world, functional_worker_transport, operation)


@pytest.mark.parametrize("world", ["rust_exact"], indirect=True)
@pytest.mark.parametrize("field", ["wing", "room", "hall"])
@pytest.mark.parametrize("value", [10**30, -(10**30)])
def test_card253_s05_r3_rust_large(world, functional_worker_transport, field, value):
    test_card253_s05_r3_native_loci(world, functional_worker_transport, field, value)


@pytest.mark.parametrize("world", ["compact"], indirect=True)
@pytest.mark.parametrize("field", ["wing", "room", "hall"])
@pytest.mark.parametrize("value", [0, 0.0])
def test_card253_s05_r3_compact_zero_guard(world, functional_worker_transport, field, value):
    test_card253_s05_r3_native_loci(world, functional_worker_transport, field, value)


@pytest.mark.parametrize("world", ["chroma"], indirect=True)
@pytest.mark.parametrize("field", ["wing", "room", "hall"])
@pytest.mark.parametrize("value", [True, False])
def test_card253_s05_r3_chroma_bool_guard(world, functional_worker_transport, field, value):
    test_card253_s05_r3_native_loci(world, functional_worker_transport, field, value)
