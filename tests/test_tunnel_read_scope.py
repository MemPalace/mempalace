"""Endpoint proof, races and metadata-only protocol guards."""

# ruff: noqa: F811

import copy

import pytest

from commonplace.gateway import WorkerClient
from tests.test_card253_s04 import (  # noqa: F401
    call,
    functional_worker_transport,
    hub,
    tunnels,
    world,
)


def save(w, rows):
    from mempalace import palace_graph as graph

    graph._save_tunnels(rows, w.mcp._config)


def readable_drawer(w, row, endpoint):
    w.col._inner.update(ids=["drawer"], metadatas=[{"wing": "wing_fixture", "room": w.readable.id}])
    row[endpoint]["drawer_id"] = "drawer"
    save(w, [row])


@pytest.mark.parametrize("endpoint", ["source", "target"])
@pytest.mark.parametrize("reverse", [False, True])
def test_readable_drawer_both_directions(world, functional_worker_transport, endpoint, reverse):
    w = world
    row = tunnels(w)
    readable_drawer(w, row, endpoint)
    assert call(w, "mempalace_list_tunnels", {}) == [row]
    reply = call(
        w,
        "mempalace_follow_tunnels",
        {k: v for k, v in row["target" if reverse else "source"].items() if k != "drawer_id"},
    )
    assert len(reply) == 1
    if endpoint == ("source" if reverse else "target"):
        assert reply[0]["drawer_preview"] == "verbatim private words"
    assert call(w, "mempalace_list_tunnels", {}) == [row]


@pytest.mark.parametrize("bad", ["missing", "empty", "malformed", "hidden_room", "hidden_target"])
def test_fail_closed_endpoint(world, functional_worker_transport, bad, monkeypatch):
    w = world
    row = tunnels(w)
    if bad == "missing":
        row["source"]["drawer_id"] = "absent"
    elif bad == "empty":
        row["source"]["drawer_id"] = ""
    elif bad == "malformed":
        row["source"] = []
    else:
        row["source" if bad == "hidden_room" else "target"]["room"] = w.hidden.id
    save(w, [row])
    original = w.col.get
    calls = []

    def get(*a, **kw):
        calls.append(kw)
        assert "documents" not in kw.get("include", []), "hidden content hydrated"
        return original(*a, **kw)

    monkeypatch.setattr(w.col, "get", get)
    monkeypatch.setattr(w.mcp, "_READ_ONLY", False)
    assert call(w, "mempalace_list_tunnels", {}) == []
    assert (
        call(w, "mempalace_follow_tunnels", {"wing": "wing_fixture", "room": w.readable.id}) == []
    )
    from mempalace import palace_graph as graph

    assert graph._load_tunnels(w.mcp._config) == [row]


@pytest.mark.parametrize("change", ["drawer", "endpoint", "revoke"])
def test_probe_final_race(world, functional_worker_transport, monkeypatch, change):
    w = world
    row = tunnels(w)
    readable_drawer(w, row, "source")
    assert call(w, "mempalace_list_tunnels", {}) == [row]
    original = WorkerClient.call_tool
    hits = []

    def call_tool(self, principal, name, args):
        reply = original(self, principal, name, args)
        if args.get("thread_scope", {}).get("probe"):
            hits.append(principal.workspace_id)
            if change == "drawer":
                w.col._inner.update(
                    ids=["drawer"], metadatas=[{"wing": "wing_fixture", "room": w.hidden.id}]
                )
            elif change == "endpoint":
                row["target"]["room"] = w.hidden.id
                save(w, [row])
            else:
                from commonplace.handoff.recall import Recall

                real_can_read = Recall.can_read
                monkeypatch.setattr(
                    Recall,
                    "can_read",
                    lambda self, p, tid: (
                        False if tid == w.readable.id else real_can_read(self, p, tid)
                    ),
                )
        return reply

    monkeypatch.setattr(WorkerClient, "call_tool", call_tool)
    assert (
        call(
            w,
            "mempalace_follow_tunnels",
            {k: v for k, v in row["source"].items() if k != "drawer_id"},
        )
        == []
    )
    assert hits == [w.h.workspace]


def test_owner_and_warm_isolation(world, functional_worker_transport):
    from commonplace import sessions

    w = world
    row = tunnels(w, "source")
    owner = sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
    assert owner.person and owner.role == "owner"
    assert owner.policy.trust == "trusted" and owner.policy.has_scope("threads:read")
    assert w.h.gateway.handoff.recall.can_read(owner, w.readable.id)
    assert w.h.gateway.handoff.recall.can_read(owner, w.hidden.id)
    assert call(w, "mempalace_list_tunnels", {}) == []
    assert w.h.gateway.call_memory_tool(owner, "mempalace_list_tunnels", {}) == [row]
    assert call(w, "mempalace_list_tunnels", {}) == []


def test_empty_allow_and_changed_descriptor(world):
    w = world
    row = tunnels(w)
    readable_drawer(w, row, "source")
    page = w.mcp.tool_list_tunnels(thread_scope={"probe": True, "after": None, "timeout": 60})
    assert page["probe"]["rows"]
    allowed = page["probe"]["rows"]
    assert w.mcp.tool_list_tunnels(thread_scope={"allow": allowed, "timeout": 60}) == [row]
    assert w.mcp.tool_list_tunnels(thread_scope={"allow": [], "timeout": 60}) == []
    w.col._inner.update(ids=["drawer"], metadatas=[{"wing": "wing_fixture", "room": w.hidden.id}])
    assert w.mcp.tool_list_tunnels(thread_scope={"allow": allowed, "timeout": 60}) == []
    assert w.mcp.tool_list_tunnels() == [row]


def test_complete_bounded_pages(world, functional_worker_transport, monkeypatch):
    w = world
    row = tunnels(w)
    rows = [{**copy.deepcopy(row), "id": f"tunnel-{n}"} for n in range(503)]
    save(w, rows)
    original = WorkerClient.call_tool
    scopes = []

    def call_tool(self, principal, name, args):
        scope = args.get("thread_scope")
        if scope:
            scopes.append(copy.deepcopy(scope))
            assert principal.workspace_id == w.h.workspace
            assert len(scope.get("allow", [])) <= 250
        assert name != "mempalace_get_drawers"
        return original(self, principal, name, args)

    monkeypatch.setattr(WorkerClient, "call_tool", call_tool)
    assert call(w, "mempalace_list_tunnels", {}) == sorted(rows, key=lambda r: r["id"])
    assert len([s for s in scopes if s.get("probe")]) == 3
    assert [len(s["allow"]) for s in scopes if "allow" in s] == [250, 250, 3]


def test_alias_and_traversal_recording(world, functional_worker_transport, monkeypatch):
    from mempalace import palace_graph as graph

    w = world
    row = tunnels(w)
    assert call(w, "mempalace_list_tunnels", {"wing": "wing-fixture"}) == [row]
    monkeypatch.setattr(w.mcp, "_READ_ONLY", False)
    result = call(w, "mempalace_follow_tunnels", {"wing": "wing-fixture", "room": w.readable.id})
    assert len(result) == 1
    stored = graph._load_tunnels(w.mcp._config)
    assert stored[0]["access_count"] == 1
    assert stored[0]["source"] == row["source"]


def test_owner_hidden_drawer(world, functional_worker_transport):
    from commonplace import sessions
    from commonplace.handoff.models import ThreadMessage

    w = world
    row = tunnels(w, "source")
    member = w.h.db.authenticate_token(w.tokens["A"])
    hidden = w.h.gateway.handoff.store_for(w.h.workspace).store(
        w.h.workspace,
        f"capture:{member.user_id}:other-private",
        [ThreadMessage(0, "user", "message", "private", "private")],
        actor=member.user_id,
        wing="private_chats",
    )
    w.col._inner.update(ids=["drawer"], metadatas=[{"wing": "wing_fixture", "room": hidden.id}])
    owner = sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
    assert owner.person and owner.role == "owner" and owner.policy.trust == "trusted"
    assert owner.policy.has_scope("threads:read") and owner.policy.sees_all_wings
    assert not w.h.gateway.handoff.recall.can_read(owner, hidden.id)
    assert w.h.gateway.handoff.recall.can_read(owner, w.readable.id)
    assert owner.policy.refusal("mempalace_list_tunnels") is None
    assert w.h.gateway.call_memory_tool(owner, "mempalace_list_tunnels", {}) == []
    assert row["source"]["drawer_id"] == "drawer"


@pytest.mark.parametrize("fault", ["metadata", "page", "cursor"])
def test_private_faults_empty(world, functional_worker_transport, monkeypatch, fault):
    from mempalace import tunnel_scope

    w = world
    row = tunnels(w)
    readable_drawer(w, row, "source")
    hits = []
    if fault == "metadata":

        def broken(*a, **kw):
            hits.append("metadata")
            raise RuntimeError("synthetic unavailable")

        monkeypatch.setattr(tunnel_scope, "metadata_page", broken)
    else:
        original = WorkerClient.call_tool

        def broken(self, principal, name, args):
            reply = original(self, principal, name, args)
            if args.get("thread_scope", {}).get("probe"):
                hits.append("page")
                if fault == "page":
                    return {"probe": None}
                reply["probe"]["next"] = 0
            return reply

        monkeypatch.setattr(WorkerClient, "call_tool", broken)
    assert call(w, "mempalace_list_tunnels", {}) == []
    assert hits


def test_explicit_deadline_no_transport_shim(world, monkeypatch):
    from commonplace.handoff.tunnels import project
    from commonplace.rules import checks

    w = world
    tunnels(w)
    principal = w.h.db.authenticate_token(w.tokens["A"])
    mediator = w.h.gateway.mediator
    with checks.deadline(30):
        assert project(w.h.gateway, principal, mediator.workers, "mempalace_list_tunnels", {})
    original = checks.remaining
    hits = []

    def expired(timeout):
        hits.append(timeout)
        if timeout == 0:
            raise TimeoutError("expired")
        return original(timeout)

    monkeypatch.setattr(checks, "remaining", expired)
    # Authority and deadline are real; the explicit expiry must discard output.
    principal = w.h.db.authenticate_token(w.tokens["A"])
    mediator = w.h.gateway.mediator
    assert project(w.h.gateway, principal, mediator.workers, "mempalace_list_tunnels", {}) == []
    assert hits


@pytest.mark.parametrize(
    "metadata",
    [{"wing": "wing_fixture"}, {"room": "ordinary"}, {"wing": "wing_fixture", "room": "?"}],
)
def test_unproved_drawer_association(world, functional_worker_transport, metadata):
    w = world
    row = tunnels(w)
    # Replace the complete metadata rather than merging an update into it.
    w.col._inner.delete(ids=["drawer"])
    w.col._inner.add(
        ids=["drawer"],
        documents=["private words"],
        metadatas=[metadata],
        embeddings=[[1.0] + [0.0] * 383],
    )
    row["source"]["drawer_id"] = "drawer"
    save(w, [row])
    assert call(w, "mempalace_list_tunnels", {}) == []


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_hidden_drawer_never_hydrated_or_recorded(
    world, functional_worker_transport, monkeypatch, endpoint
):
    from mempalace import palace_graph as graph

    w = world
    hidden = tunnels(w, endpoint)
    w.col._inner.add(
        ids=["visible"],
        documents=["visible words"],
        metadatas=[{"wing": "wing_other", "room": "ordinary"}],
        embeddings=[[1.0] + [0.0] * 383],
    )
    visible = copy.deepcopy(hidden)
    visible["id"] = "visible-tunnel"
    visible["source"].pop("drawer_id", None)
    visible["target"]["drawer_id"] = "visible"
    save(w, [hidden, visible])
    reads, records = [], []
    original_get, original_record = w.col.get, graph.record_tunnel_traversal

    def get(*a, **kw):
        reads.append(copy.deepcopy(kw))
        return original_get(*a, **kw)

    def record(ids, *a, **kw):
        records.append(list(ids))
        return original_record(ids, *a, **kw)

    monkeypatch.setattr(w.col, "get", get)
    monkeypatch.setattr(graph, "record_tunnel_traversal", record)
    args = {"wing": "wing_fixture", "room": w.readable.id}
    reply = call(w, "mempalace_follow_tunnels", args)
    assert [r["tunnel_id"] for r in reply] == [visible["id"]]
    assert reply[0]["drawer_preview"] == "visible words"
    assert reads and any(
        "visible" in r.get("ids", []) and "documents" in r.get("include", []) for r in reads
    )
    assert all("drawer" not in r.get("ids", []) for r in reads), "hidden drawer hydrated"
    assert records == []
    monkeypatch.setattr(w.mcp, "_READ_ONLY", False)
    call(w, "mempalace_follow_tunnels", args)
    assert records == [[visible["id"]]]
    assert all("drawer" not in r.get("ids", []) for r in reads), "hidden drawer hydrated"
    stored = graph._load_tunnels(w.mcp._config)
    assert next(r for r in stored if r["id"] == hidden["id"]) == hidden


def test_client_scope_ignored(world, functional_worker_transport):
    w = world
    tunnels(w, "source")
    assert call(w, "mempalace_list_tunnels", {"thread_scope": {"allow": []}}) == []
    from mempalace import palace_graph as graph

    rows = graph._load_tunnels(w.mcp._config)
    rows[0]["source"].pop("drawer_id")
    save(w, rows)
    assert call(w, "mempalace_list_tunnels", {"thread_scope": {"allow": []}}) == rows


@pytest.mark.parametrize(
    "tool,args",
    [
        ("mempalace_list_tunnels", {"wing": "../bad"}),
        ("mempalace_follow_tunnels", {"wing": "../bad", "room": "ok"}),
        ("mempalace_follow_tunnels", {"wing": "ok", "room": "../bad"}),
    ],
)
def test_invalid_name_envelope(world, tool, args):
    w = world
    tunnels(w)
    direct = (
        w.mcp.tool_list_tunnels(**args)
        if tool.endswith("list_tunnels")
        else w.mcp.tool_follow_tunnels(**args)
    )
    assert call(w, tool, args) == direct


def test_probe_never_selects_documents(world, monkeypatch):
    from mempalace.backends import _inproc_sqlite

    w = world
    row = tunnels(w)
    readable_drawer(w, row, "source")
    statements = []
    original = _inproc_sqlite.open_reader

    def open_reader(*a, **kw):
        conn = original(*a, **kw)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(_inproc_sqlite, "open_reader", open_reader)
    page = w.mcp.tool_list_tunnels(thread_scope={"probe": True, "after": None, "timeout": 60})
    assert page["probe"]["rows"]
    selects = [s.lower() for s in statements if s.lstrip().lower().startswith("select")]
    assert selects and any("metadata_json" in s for s in selects)
    assert all("document," not in s and "document " not in s for s in selects)
