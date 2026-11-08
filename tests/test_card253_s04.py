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

TOOLS = ["mempalace_list_tunnels", "mempalace_follow_tunnels"]
hub = hub_fixture


@pytest.fixture
def world(hub, monkeypatch, tmp_path, request):
    from mempalace import mcp_server as mcp, palace_graph as graph

    monkeypatch.setattr(graph, "MempalaceConfig", lambda: mcp._config)
    monkeypatch.setattr(mcp, "_READ_ONLY", True)
    backend_name = "sqlite_exact"
    monkeypatch.setenv("MEMPALACE_BACKEND", backend_name)
    monkeypatch.setattr(mcp._config, "_palace_path_override", str(tmp_path / "exact"))
    from mempalace.backends import PalaceRef
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend = SQLiteExactBackend()
    collection = backend.get_collection(
        palace=PalaceRef(id=mcp._config.palace_path, local_path=mcp._config.palace_path),
        collection_name=mcp._config.collection_name,
        create=True,
    )
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    collection = EmbeddingCollection(collection)
    monkeypatch.setattr(mcp, "_get_collection", lambda **kwargs: collection)
    monkeypatch.setattr(mcp, "_selected_backend_name", lambda: backend_name)
    monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: False)
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
        assert principal.role != "owner" and principal.policy.has_scope("threads:read")
        assert principal.policy.sees_all_wings is (kind == "A")
        assert hub.gateway.handoff.recall.can_read(principal, readable.id)
        assert not hub.gateway.handoff.recall.can_read(principal, hidden.id)
        assert store.get(hidden.id) is not None
        for tool in TOOLS:
            assert principal.policy.refusal(tool) is None
    hits = []
    for name in ("list_tunnels", "follow_tunnels"):
        original = getattr(mcp, name)

        def spy(*a, _name=name, _original=original, **kw):
            hits.append(_name)
            return _original(*a, **kw)

        monkeypatch.setattr(mcp, name, spy)
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


def tunnels(w, endpoint=None):
    from mempalace import palace_graph as graph

    w.col._inner.add(
        ids=["drawer"],
        documents=["verbatim private words"],
        metadatas=[{"wing": "wing_fixture", "room": w.hidden.id if endpoint else w.readable.id}],
        embeddings=[[1.0] + [0.0] * 383],
    )
    row = {
        "id": "explicit-test",
        "source": {"wing": "wing_fixture", "room": w.readable.id},
        "target": {"wing": "wing_other", "room": "ordinary"},
        "label": "unchanged",
    }
    if endpoint:
        row[endpoint]["drawer_id"] = "drawer"
    graph._save_tunnels([row], w.mcp._config)
    assert graph._load_tunnels(w.mcp._config) == [row]
    return row


def control_tunnels(w, rows):
    from mempalace import palace_graph as graph

    source = Path(w.mcp._config.palace_path)
    target = source.parent / "control" / "palace"
    shutil.copytree(source, target)
    w.mcp._config._palace_path_override = str(target)
    w.mcp._invalidate_overview_caches()
    graph.invalidate_graph_cache()
    graph._save_tunnels(rows, w.mcp._config)


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_card253_s04_tunnel(world, functional_worker_transport, endpoint):
    w = world
    tunnels(w, endpoint)
    reply = call(w, "mempalace_list_tunnels", {"wing": "wing_fixture"})
    assert "list_tunnels" in w.hits
    assert "explicit-test" not in [r["id"] for r in reply], "s04 moved drawer tunnel retained"
    control_tunnels(w, [])
    assert reply == call(w, "mempalace_list_tunnels", {"wing": "wing_fixture"}) == []


def test_card253_s04_follow_source(world, functional_worker_transport):
    w = world
    tunnels(w, "source")
    args = {"wing": "wing_fixture", "room": w.readable.id, "record": False}
    reply = call(w, "mempalace_follow_tunnels", args)
    control_tunnels(w, [])
    control = call(w, "mempalace_follow_tunnels", args)
    # Prove that the real follow branch remains reachable for a readable tunnel.
    from mempalace import palace_graph as graph

    visible = {
        "id": "control",
        "source": {"wing": "wing_fixture", "room": w.readable.id},
        "target": {"wing": "wing_other", "room": "ordinary"},
    }
    graph._save_tunnels([visible], w.mcp._config)
    assert len(call(w, "mempalace_follow_tunnels", args)) == 1
    assert "follow_tunnels" in w.hits
    assert reply == control == [], "s04 follow ignored source drawer"


@pytest.mark.parametrize("reverse", [False, True])
def test_card253_s04_room_only(world, functional_worker_transport, reverse):
    from mempalace import palace_graph as graph

    w = world
    row = tunnels(w)
    assert call(w, "mempalace_list_tunnels", {}) == [row]
    pair = row["target" if reverse else "source"]
    reply = call(w, "mempalace_follow_tunnels", {**pair, "record": False})
    assert len(reply) == 1
    assert reply[0]["direction"] == ("incoming" if reverse else "outgoing")
    assert graph._load_tunnels(w.mcp._config) == [row]


@pytest.mark.parametrize("change", ["traversal", "metadata"])
def test_card253_s04_unrelated_updates_preserved(
    world, functional_worker_transport, monkeypatch, change
):
    from commonplace.gateway import WorkerClient
    from mempalace import palace_graph as graph

    w = world
    row = tunnels(w)
    row["source"]["drawer_id"] = "drawer"
    graph._save_tunnels([row], w.mcp._config)
    assert call(w, "mempalace_list_tunnels", {}) == [row]
    original = WorkerClient.call_tool
    hits = []

    def call_tool(self, principal, name, args):
        reply = original(self, principal, name, args)
        if args.get("thread_scope", {}).get("probe"):
            hits.append(name)
            if change == "traversal":
                graph.record_tunnel_traversal([row["id"]], w.mcp._config)
            else:
                w.col._inner.update(ids=["drawer"], metadatas=[{"note": "unrelated"}])
        return reply

    monkeypatch.setattr(WorkerClient, "call_tool", call_tool)
    reply = call(w, "mempalace_list_tunnels", {})
    current = graph._load_tunnels(w.mcp._config)
    assert hits
    assert reply == current, "readable tunnel lost after unrelated update"
    if change == "traversal":
        assert reply[0]["access_count"] == 1


def test_card253_s04_delete_before_page_boundary(world, functional_worker_transport, monkeypatch):
    from commonplace.gateway import WorkerClient
    from mempalace import palace_graph as graph

    w = world
    row = tunnels(w)
    rows = [{**row, "id": f"tunnel-{n:04}"} for n in range(251)]
    graph._save_tunnels(rows, w.mcp._config)
    assert call(w, "mempalace_list_tunnels", {}) == rows
    original = WorkerClient.call_tool
    hits = []

    def call_tool(self, principal, name, args):
        reply = original(self, principal, name, args)
        if args.get("thread_scope", {}).get("probe") and not hits:
            hits.append(name)
            graph.delete_tunnel(rows[0]["id"])
        return reply

    monkeypatch.setattr(WorkerClient, "call_tool", call_tool)
    reply = call(w, "mempalace_list_tunnels", {})
    assert hits == ["mempalace_list_tunnels"]
    assert reply == rows[1:], "readable tunnel skipped at page boundary"
