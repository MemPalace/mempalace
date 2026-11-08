"""Outgoing source bundles retain only proved provenance and dependent ranges."""

import pytest

from commonplace import sessions
from commonplace.gateway import WorkerClient
from mempalace.ids import make_drawer_id_from_content
from tests._commonplace_helpers import functional_worker_transport, hub_fixture  # noqa: F401
from tests.test_card253_presence import world  # noqa: F401

TOOLS = [
    "mempalace_add_drawer",
    "mempalace_get_drawer",
    "mempalace_get_drawers",
    "mempalace_list_drawers",
    "mempalace_search",
    "mempalace_list_tunnels",
]
TEXT = "Verbatim bundle words."


@pytest.fixture
def case(world, functional_worker_transport, monkeypatch):  # noqa: F811
    w = world
    member = w.h.db.authenticate_token(w.tokens["A"])
    token = w.h.token(
        "bundle-reader",
        user=member.user_id,
        tools=TOOLS,
        scopes=["memory:read", "memory:write", "private:read", "threads:read"],
        read_wings=["*"],
        write_wings=["wing_fixture"],
        write_wing="wing_fixture",
        trust="trusted",
    )
    from commonplace.handoff.models import ThreadMessage

    other = w.h.db.create_user("other-bundle@example.test")
    w.h.db.add_member(w.h.workspace, other, "member", actor=w.h.admin)
    w.hidden = w.h.gateway.handoff.store_for(w.h.workspace).store(
        w.h.workspace,
        f"capture:{other}:native-hidden-bundle",
        [ThreadMessage(0, "user", "message", "hidden", "hidden")],
        actor=other,
        wing="private_chats",
    )
    w.member = w.h.db.authenticate_token(token)
    w.owner = sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
    w.calls = []
    original = WorkerClient.post

    def post(self, principal, request):
        w.calls.append(request.get("params", {}).get("name"))
        return original(self, principal, request)

    monkeypatch.setattr(WorkerClient, "post", post)
    return w


def invoke(w, tool, args, role="member"):
    principal = getattr(w, role)
    assert principal.policy.trust == "trusted"
    assert all(
        principal.policy.has_scope(s) for s in ("memory:read", "private:read", "threads:read")
    )
    assert principal.policy.sees_all_wings
    assert principal.policy.refusal(tool) is None
    assert w.h.gateway.handoff.recall.can_read(principal, w.readable.id)
    assert not w.h.gateway.handoff.recall.can_read(principal, w.hidden.id)
    if role == "owner":
        assert principal.person and principal.role == "owner"
    return w.h.gateway.call_memory_tool(principal, tool, args)


def seed(w, metadata):
    identifier = make_drawer_id_from_content("wing_fixture", "notes", TEXT)
    w.col.add(
        ids=[identifier],
        documents=[TEXT],
        metadatas=[{"wing": "wing_fixture", "room": "notes", **metadata}],
        embeddings=[[1.0] + [0.0] * 383],
    )
    assert (
        w.col.get(ids=[identifier], include=["metadatas"])["metadatas"][0]["source_thread"]
        == metadata["source_thread"]
    )
    return identifier


@pytest.mark.parametrize("kind", ["thread", "seq"])
def test_card253_s10_retry(case, kind):
    w = case
    metadata = {"source_thread": w.hidden.id}
    if kind == "seq":
        metadata.update(source_seq_start=0, source_seq_end=0)
    identifier = seed(w, metadata)
    reply = invoke(w, TOOLS[0], {"wing": "wing_fixture", "room": "notes", "content": TEXT})
    assert TOOLS[0] in w.calls
    assert (
        reply["success"]
        and reply["reason"] == "already_exists"
        and reply["drawer_id"] == identifier
    )
    assert "source_kept" not in reply, "s10 hidden retry bundle retained"


def test_card253_s10_orphan_seq(case):
    w = case
    identifier = seed(w, {"source_thread": w.hidden.id, "source_seq_start": 0, "source_seq_end": 0})
    reply = invoke(w, TOOLS[1], {"drawer_id": identifier})
    assert TOOLS[1] in w.calls
    assert reply["content"] == TEXT
    assert "source_thread" not in reply["metadata"]
    assert not {"source_seq_start", "source_seq_end"}.intersection(reply["metadata"]), (
        "s10 orphan source range retained"
    )


@pytest.mark.parametrize("role", ["member", "owner"])
def test_card253_s10_readable_guard(case, role):
    w = case
    identifier = seed(
        w,
        {
            "source_thread": w.readable.id,
            "source_seq_start": 0,
            "source_seq_end": 0,
            "source_file": "notes/original.md",
        },
    )
    reply = invoke(w, TOOLS[0], {"wing": "wing_fixture", "room": "notes", "content": TEXT}, role)
    assert reply["source_kept"] == {"thread": w.readable.id, "seq_start": 0, "seq_end": 0}
    drawer = invoke(w, TOOLS[1], {"drawer_id": identifier}, role)
    assert drawer["content"] == TEXT
    assert drawer["metadata"]["source_file"] == "original.md"
    assert drawer["metadata"]["source_thread"] == w.readable.id


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("hidden", [False, True])
def test_card253_s10_memory_guard(case, monkeypatch, role, hidden):
    from commonplace import memory_tools

    w = case
    thread = w.hidden.id if hidden else w.readable.id
    identifier = seed(w, {"source_thread": thread, "source_seq_start": 0, "source_seq_end": 0})
    drawer = invoke(w, TOOLS[1], {"drawer_id": identifier}, role)
    monkeypatch.setattr(memory_tools, "_cards", lambda *args: [])
    record = memory_tools._record(None, drawer)
    assert record["text"] == TEXT
    assert record["source"] == (
        None if hidden else {"thread": thread, "seq_start": 0, "seq_end": 0}
    )


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("operation", ["get", "batch", "list", "search", "retry"])
def test_card253_s10_head_operations(case, operation, role):
    w = case
    metadata = {"source_thread": w.hidden.id, "source_seq_start": 0, "source_seq_end": 0}
    identifier = seed(w, metadata)
    if operation == "retry":
        reply = invoke(
            w, TOOLS[0], {"wing": "wing_fixture", "room": "notes", "content": TEXT}, role
        )
        assert reply["success"] and reply["drawer_id"] == identifier
        assert "source_kept" not in reply
    else:
        if operation == "get":
            reply = invoke(w, TOOLS[1], {"drawer_id": identifier}, role)
        elif operation == "batch":
            w.col.add(
                ids=["sibling"],
                documents=["Sibling words"],
                metadatas=[{"wing": "wing_fixture", "room": "notes"}],
                embeddings=[[1.0] + [0.0] * 383],
            )
            reply = invoke(w, TOOLS[2], {"drawer_ids": [identifier, "sibling"]}, role)
            assert "Sibling words" in str(reply)
        elif operation == "list":
            reply = invoke(w, TOOLS[3], {"wing": "wing_fixture", "room": "notes"}, role)
        else:
            reply = invoke(
                w,
                TOOLS[4],
                {"query": "bundle", "wing": "wing_fixture", "include_facts": False},
                role,
            )
        assert TEXT in str(reply)
        assert w.hidden.id not in str(reply)
        assert "source_seq_start" not in str(reply) and "source_seq_end" not in str(reply)
    raw = w.col.get(ids=[identifier], include=["metadatas", "documents"])
    assert raw["documents"] == [TEXT]
    assert all(raw["metadatas"][0][key] == value for key, value in metadata.items())


@pytest.mark.parametrize("parent", ["hidden", "missing", "invalid", "readable"])
@pytest.mark.parametrize("shape", ["flat", "source", "source_kept"])
def test_card253_s10_head_bundle_shapes(case, parent, shape):
    from commonplace.handoff.mirror_access import gate

    w = case
    # Exercise the worker too, then cover nested legacy shapes not produced by
    # current drawer storage. The retry public witness covers the real producer.
    identifier = seed(w, {"source_thread": w.readable.id})
    invoke(w, TOOLS[1], {"drawer_id": identifier})
    value = {
        "hidden": w.hidden.id,
        "missing": None,
        "invalid": "not-a-thread",
        "readable": w.readable.id,
    }[parent]
    thread, start, end = (
        ("source_thread", "source_seq_start", "source_seq_end")
        if shape == "flat"
        else ("thread", "seq_start", "seq_end")
    )
    bundle = {start: 7, end: 9}
    if value is not None:
        bundle[thread] = value
    payload = bundle if shape == "flat" else {shape: bundle}
    import copy

    original = copy.deepcopy(payload)
    cleaned = gate(w.h.gateway, w.member, "drawer_read", payload)
    assert payload == original
    if parent in {"readable", "invalid"}:
        assert cleaned == payload
    elif shape == "flat":
        assert start not in cleaned and end not in cleaned
    else:
        assert shape not in cleaned


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize(
    "operation", ["get", "retry", "memory", "batch", "list", "search_text_unchanged"]
)
def test_card253_s10_r1_private_parent(case, monkeypatch, role, operation):
    from commonplace import memory_tools
    from commonplace.handoff.models import ThreadMessage

    w = case
    principal = getattr(w, role)
    source = f"capture:{principal.user_id}:native-readable-bundle"
    thread = w.h.gateway.handoff.store_for(w.h.workspace).store(
        w.h.workspace,
        source,
        [ThreadMessage(0, "user", "message", "readable", "readable")],
        actor=principal.user_id,
        wing="private_chats",
    )
    assert w.h.gateway.handoff.recall.can_read(principal, thread.id)
    identifier = seed(w, {"source_thread": source, "source_seq_start": 0, "source_seq_end": 0})
    if operation in {"batch", "list", "search_text_unchanged"}:
        if operation == "batch":
            reply = invoke(w, TOOLS[2], {"drawer_ids": [identifier]}, role)
        elif operation == "list":
            reply = invoke(w, TOOLS[3], {"wing": "wing_fixture", "room": "notes"}, role)
        else:
            args = {"query": "bundle", "wing": "wing_fixture", "include_facts": False}
            raw = w.h.gateway.mediator.workers.call_tool(principal, TOOLS[4], args)
            reply = invoke(w, TOOLS[4], args, role)
            # Drawer search does not emit source_thread/ranges. Preserve the
            # real worker's complete hit projection for this readable parent.
            assert reply["results"] == raw["results"]
            assert TEXT in str(reply)
            return

        def rows(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from rows(child)
            elif isinstance(value, list):
                for child in value:
                    yield from rows(child)

        metadata = next(row for row in rows(reply) if row.get("source_thread") == source)
        assert metadata["source_seq_start"] == metadata["source_seq_end"] == 0
        assert TEXT in str(reply)
    elif operation == "retry":
        reply = invoke(
            w, TOOLS[0], {"wing": "wing_fixture", "room": "notes", "content": TEXT}, role
        )
        assert reply.get("source_kept") == {"thread": source, "seq_start": 0, "seq_end": 0}
    else:
        drawer = invoke(w, TOOLS[1], {"drawer_id": identifier}, role)
        if operation == "get":
            assert drawer["metadata"].get("source_thread") == source
            assert drawer["metadata"].get("source_seq_start") == 0
            assert drawer["metadata"].get("source_seq_end") == 0
        else:
            monkeypatch.setattr(memory_tools, "_cards", lambda *args: [])
            assert memory_tools._record(None, drawer)["source"] == {
                "thread": source,
                "seq_start": 0,
                "seq_end": 0,
            }


@pytest.mark.parametrize("source", ["mcp:probe", "ext-probe", "import:probe"])
def test_card253_s10_r1_token_free_flat(case, source):
    w = case
    identifier = seed(w, {"source_thread": source, "source_seq_start": 0, "source_seq_end": 0})
    reply = invoke(w, TOOLS[1], {"drawer_id": identifier})
    assert reply["metadata"].get("source_thread") == source
    assert reply["metadata"].get("source_seq_start") == 0
    assert reply["metadata"].get("source_seq_end") == 0


@pytest.mark.parametrize("role", ["member", "owner"])
def test_card253_s10_r1_native_memory_echo(case, role):
    import json

    from tests.test_commonplace_memory import _call, _http

    w = case
    source = {"thread": w.readable.id, "seq_start": 0, "seq_end": 0}
    identifier = seed(
        w, {"source_thread": source["thread"], "source_seq_start": 0, "source_seq_end": 0}
    )
    principal = getattr(w, role)
    token = w.h.token(
        "echo",
        user=principal.user_id,
        profile="full",
        trust="trusted",
        scopes=["memory:read", "private:read", "threads:read"],
        read_wings=["*"],
    )
    got = _call(w.h, token, "memory_get", id=identifier)["content"][0]["text"]
    assert f"source={json.dumps(source)}" in got.split("\nquoted data: ", 1)[0]
    assert _http(w.h, token, f"/api/memory/{identifier}")[1]["source"] == source


@pytest.mark.parametrize("role", ["member", "owner"])
def test_card253_s10_r2_tunnel_endpoints(case, monkeypatch, tmp_path, role):
    from mempalace import palace_graph

    w = case
    monkeypatch.setattr(
        palace_graph, "_get_tunnel_file", lambda *a, **kw: str(tmp_path / "tunnels.json")
    )
    row = {
        "id": "readable-tunnel",
        "source": {"wing": "wing_fixture", "room": "notes"},
        "target": {"wing": "wing_fixture", "room": "destination"},
        "label": "readable",
        "kind": "explicit",
    }
    palace_graph._save_tunnels([row])
    assert palace_graph._load_tunnels() == [row]
    reply = invoke(w, "mempalace_list_tunnels", {}, role)
    assert "mempalace_list_tunnels" in w.calls
    assert reply == [row], "readable tunnel endpoints changed"


def test_card253_s10_r2_gate_endpoint(case):
    from commonplace.handoff.mirror_access import gate

    w = case
    identifier = seed(w, {"source_thread": w.readable.id})
    invoke(w, TOOLS[1], {"drawer_id": identifier})
    row = {
        "source": {"wing": "wing_fixture", "room": "notes"},
        "target": {"wing": "wing_fixture", "room": "destination"},
    }
    assert gate(w.h.gateway, w.member, "all_wings", row) == row


def test_card253_s10_r2_unsourced_fact_shape(case):
    from commonplace.handoff.mirror_access import gate

    w = case
    identifier = seed(w, {"source_thread": w.readable.id})
    invoke(w, TOOLS[1], {"drawer_id": identifier})
    fact = {
        "subject": "Person",
        "predicate": "likes",
        "object": "words",
        "source_thread": None,
        "source_seq_start": None,
        "source_seq_end": None,
    }
    assert gate(w.h.gateway, w.member, "kg_read", fact) == fact


HIDDEN_TEXT = "Hidden tunnel words."


@pytest.mark.parametrize("end", ["source", "target"])
@pytest.mark.parametrize("role", ["member", "owner"])
def test_card253_s10_r3_hidden_endpoint_drawer(case, monkeypatch, tmp_path, role, end):
    from mempalace import palace_graph

    w = case
    monkeypatch.setattr(
        palace_graph, "_get_tunnel_file", lambda *a, **kw: str(tmp_path / "tunnels.json")
    )
    identifier = make_drawer_id_from_content("private_chats", w.hidden.id, HIDDEN_TEXT)
    w.col.add(
        ids=[identifier],
        documents=[HIDDEN_TEXT],
        metadatas=[{"wing": "private_chats", "room": w.hidden.id}],
        embeddings=[[1.0] + [0.0] * 383],
    )
    ends = {
        "source": {"wing": "wing_fixture", "room": "notes"},
        "target": {"wing": "wing_fixture", "room": "destination"},
    }
    ends[end]["drawer_id"] = identifier
    row = {"id": "probe-tunnel", **ends, "label": "probe", "kind": "explicit"}
    palace_graph._save_tunnels([row])
    assert palace_graph._load_tunnels() == [row]
    reply = invoke(w, "mempalace_list_tunnels", {}, role)
    assert "mempalace_list_tunnels" in w.calls
    assert identifier not in str(reply), f"hidden {end} drawer id returned"
    assert w.hidden.id not in str(reply), f"hidden thread id returned via {end} drawer id"
    w.col.delete(ids=[identifier])
    w.mcp._invalidate_overview_caches()
    assert invoke(w, "mempalace_list_tunnels", {}, role) == reply


@pytest.mark.parametrize("end", ["source", "target"])
@pytest.mark.parametrize(
    "value", [None, 42, "", "unresolved", "readable", ["x"], {"a": 1}, True, 0.5]
)
def test_card253_s10_r3_head_endpoint_proof(case, monkeypatch, tmp_path, end, value):
    from mempalace import palace_graph

    w = case
    identifier = seed(w, {"source_thread": w.readable.id})
    value = identifier if value == "readable" else value
    monkeypatch.setattr(
        palace_graph, "_get_tunnel_file", lambda *a, **kw: str(tmp_path / "tunnels.json")
    )
    row = {
        "id": "proof-tunnel",
        "source": {"wing": "wing_fixture", "room": "notes"},
        "target": {"wing": "wing_fixture", "room": "destination"},
    }
    row[end]["drawer_id"] = value
    palace_graph._save_tunnels([row])
    from commonplace.handoff.mirror_access import gate

    expected = row if value == identifier else {k: v for k, v in row.items() if k != end}
    assert gate(w.h.gateway, w.member, "all_wings", row) == expected
    reply = invoke(w, "mempalace_list_tunnels", {})
    # Develop's tunnel projection rejects malformed/unresolved drawer
    # descriptors before the final endpoint scrub; None is not a descriptor.
    assert reply == ([expected] if value is None or value == identifier else [])


def test_card253_s10_r3_head_nested_flat_alias(case):
    from commonplace.handoff.mirror_access import gate

    w = case
    identifier = seed(w, {"source_thread": w.readable.id})
    invoke(w, TOOLS[1], {"drawer_id": identifier})
    payload = {"source": {"source_thread": w.hidden.id, "source_seq_start": 0, "source_seq_end": 0}}
    assert gate(w.h.gateway, w.member, "drawer_read", payload) == {}


@pytest.mark.parametrize("key", ["source", "source_kept"])
def test_card253_s10_r4_alias_only(case, key):
    from commonplace.handoff.mirror_access import gate

    w = case
    invoke(w, TOOLS[1], {"drawer_id": seed(w, {"source_thread": w.readable.id})})
    payload = {key: {"source_thread": w.readable.id, "source_seq_start": 0, "source_seq_end": 0}}
    assert gate(w.h.gateway, w.member, "drawer_read", payload) == payload


def test_card253_s10_r4_guard_mixed_alias(case):
    from commonplace.handoff.mirror_access import gate

    w = case
    invoke(w, TOOLS[1], {"drawer_id": seed(w, {"source_thread": w.readable.id})})
    payload = {
        "source": {
            "thread": w.readable.id,
            "source_thread": w.hidden.id,
            "source_seq_start": 0,
            "source_seq_end": 0,
        }
    }
    assert gate(w.h.gateway, w.member, "drawer_read", payload) == {
        "source": {"thread": w.readable.id}
    }


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("kind", ["plain", "chunked"])
def test_card253_s10_r4_guard_endpoint_compat(case, monkeypatch, tmp_path, role, kind):
    from mempalace import palace_graph

    w = case
    assert w.mcp._selected_backend_name() == "sqlite_exact"
    parts = ["Chunked words one. ", "Chunked words two."] if kind == "chunked" else ["Plain words."]
    logical = make_drawer_id_from_content("wing_fixture", "notes", "".join(parts))
    w.col.add(
        ids=[
            f"{logical}_chunk_{i:06d}" if kind == "chunked" else logical for i in range(len(parts))
        ],
        documents=parts,
        metadatas=[
            {
                "wing": "wing_fixture",
                "room": "notes",
                **({"parent_drawer_id": logical, "chunk_index": i} if kind == "chunked" else {}),
            }
            for i in range(len(parts))
        ],
        embeddings=[[1.0] + [0.0] * 383 for _ in parts],
    )
    drawer = invoke(w, TOOLS[1], {"drawer_id": logical}, role)
    assert drawer["content"] == "".join(parts)
    monkeypatch.setattr(
        palace_graph, "_get_tunnel_file", lambda *a, **kw: str(tmp_path / "tunnels.json")
    )
    row = {
        "id": "compat-tunnel",
        "source": {"wing": "wing_fixture", "room": "notes"},
        "target": {"wing": "wing_fixture", "room": "destination", "drawer_id": logical},
    }
    palace_graph._save_tunnels([row])
    from commonplace.handoff.mirror_access import gate

    expected = [row] if kind == "plain" else [{k: v for k, v in row.items() if k != "target"}]
    assert gate(w.h.gateway, getattr(w, role), "all_wings", [row]) == expected
    # The upstream tunnel projection now omits the whole unproved tunnel.
    assert invoke(w, "mempalace_list_tunnels", {}, role) == ([row] if kind == "plain" else [])


@pytest.mark.parametrize("role", ["member", "owner"])
def test_card253_s10_r5_token_free_retry(case, role):
    w = case
    identifier = seed(
        w, {"source_thread": "owner-chat", "source_seq_start": 0, "source_seq_end": 0}
    )
    reply = invoke(w, TOOLS[0], {"wing": "wing_fixture", "room": "notes", "content": TEXT}, role)
    assert reply.get("source_kept") == {"thread": "owner-chat", "seq_start": 0, "seq_end": 0}
    assert reply["success"] and reply["drawer_id"] == identifier
