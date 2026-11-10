"""Current citation associations and raw path context stay within read authority."""

import json
from types import SimpleNamespace
from urllib.parse import quote

import chromadb
import pytest

from commonplace import sessions
from commonplace.handoff.models import ThreadMessage
from commonplace.handoff.recall import Recall
from mempalace.palace import get_closets_collection
from tests._commonplace_helpers import (  # noqa: F401
    assert_member_drawer_access,
    functional_worker_transport,
    hub_fixture,
    shared_readers,
)

TOOLS = ["mempalace_kg_query", "mempalace_get_drawer", "mempalace_get_drawers"]
SCOPES = ["memory:read", "private:read", "threads:read", "kg:read"]


@pytest.fixture
def case(hub, kg, collection, config, monkeypatch, functional_worker_transport):  # noqa: F811
    h = hub
    h.supervisor.get(h.workspace)
    uid = h.db.create_user("reader@example.test")
    h.db.add_member(h.workspace, uid, "owner", actor=h.admin)
    token = h.token(
        "reader",
        user=uid,
        tools=TOOLS,
        scopes=SCOPES,
        read_wings=["*"],
        write_wings=["shared"],
        trust="trusted",
    )
    member = h.db.authenticate_token(token)
    owner = sessions.person_principal(h.db, h.admin, h.workspace)
    cookie = sessions.redeem_login_link(
        h.db, sessions.create_login_link(h.db, "owner@example.com", h.workspace)
    )
    for p in (member, owner):
        assert p.policy.trust == "trusted" and p.policy.sees_all_wings
        assert all(p.policy.has_scope(scope) for scope in SCOPES)
        assert all(p.policy.refusal(tool) is None for tool in TOOLS)
    assert not member.person and member.role == "owner"
    assert owner.person and owner.role == "owner"
    store = h.gateway.handoff.store_for(h.workspace)
    c = SimpleNamespace(
        h=h,
        kg=kg,
        col=collection,
        config=config,
        member=member,
        owner=owner,
        cookie=cookie,
        token=token,
        store=store,
        calls=[],
        source_calls=[],
        reads=[],
        lookups=[],
    )
    original_read = Recall.can_read

    def read(self, principal, room, **kwargs):
        c.reads.append((principal.workspace_id, room))
        return original_read(self, principal, room, **kwargs)

    monkeypatch.setattr(Recall, "can_read", read)
    original_call = h.gateway.mediator.workers.call_tool

    def call(principal, name, arguments):
        c.calls.append((principal.workspace_id, name, arguments))
        return original_call(principal, name, arguments)

    monkeypatch.setattr(h.gateway.mediator.workers, "call_tool", call)
    original_store = h.gateway.handoff.store_for

    def store_for(workspace):
        c.lookups.append(workspace)
        return original_store(workspace)

    monkeypatch.setattr(h.gateway.handoff, "store_for", store_for)
    for name in ("by_source", "by_source_family"):
        original = getattr(store, name)

        def lookup(workspace, *args, _original=original, _name=name, **kwargs):
            c.lookups.append(workspace)
            c.source_calls.append(_name)
            return _original(workspace, *args, **kwargs)

        monkeypatch.setattr(store, name, lookup)
    c.primary = stored(c, "capture:primary:codex:readable")
    foreign = h.db.create_user("foreign@example.test")
    c.hidden = stored(c, f"capture:{foreign}:codex:opaque7", "private_chats")
    for p in (member, owner):
        assert original_read(h.gateway.handoff.recall, p, c.primary.id)
        assert not original_read(h.gateway.handoff.recall, p, c.hidden.id)
    return c


def stored(c, source, wing="shared", workspace=None):
    return c.store.store(
        workspace or c.h.workspace,
        source,
        [ThreadMessage(0, "user", "message", "Exact words", "Exact words")],
        actor=c.member.user_id,
        wing=wing,
    )


def invoke(c, name, role="member", **arguments):
    headers = {"Cookie": f"{sessions.COOKIE}={c.cookie}"} if role == "owner" else None
    status, raw = c.h.raw(
        None if role == "owner" else c.token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mempalace_" + name, "arguments": arguments},
        },
        headers=headers,
    )
    assert status == 200, raw
    reply = json.loads(raw)
    assert "error" not in reply, reply
    return json.loads(reply["result"]["content"][0]["text"])


def target(c, kind, room, *, identifier="opaque-local-id", parent=None):
    col = c.col if kind == "drawer" else get_closets_collection(c.config.palace_path)
    meta = {"wing": "shared", "room": room}
    if parent:
        meta.update(parent_entry_id=parent, chunk_index=0)
    col.add(ids=[identifier], documents=["Exact target words"], metadatas=[meta])
    assert col.get(ids=[identifier])["metadatas"][0]["room"] == room
    return col


def citation(row, kind):
    return row["sources"][0] if kind == "drawer" else row


def seed(c, field, value):
    c.kg.add_triple(
        "TypedFact",
        "keeps",
        "Exact  original words",
        wing="shared",
        sensitivity="public",
        source_thread=c.primary.id,
        **{field: value},
    )


def fact(c, role="member"):
    row = invoke(c, "kg_query", role, entity="TypedFact")["facts"][0]
    assert row["object"] == "Exact  original words"
    assert (c.h.workspace, c.primary.id) in c.reads
    assert all(w == c.h.workspace for w in c.lookups)
    assert all(w == c.h.workspace for w, _, _ in c.calls)
    return row


@pytest.mark.parametrize("kind", ["drawer", "closet"])
def test_s09_moved(case, kind):
    c = case
    field = "source_drawer_id" if kind == "drawer" else "source_closet"
    col = target(c, kind, c.primary.id)
    seed(c, field, "opaque-local-id")
    control = fact(c)
    assert citation(control, kind)[field] == "opaque-local-id"
    col.update(ids=["opaque-local-id"], metadatas=[{"wing": "shared", "room": c.hidden.id}])
    row = fact(c)
    assert field not in citation(row, kind), "s09 opaque moved citation retained"
    assert all(field not in source for source in row["sources"])
    for item in [control, *control["sources"]]:
        item.pop(field, None)
    assert row == control


@pytest.mark.parametrize("kind", ["drawer", "closet"])
def test_s09_workspace(case, kind, tmp_path):
    """An unproved local target cannot borrow a separate palace fixture's metadata."""
    c = case
    field = "source_drawer_id" if kind == "drawer" else "source_closet"
    local = target(c, kind, c.hidden.id)
    assert local.get(ids=["opaque-local-id"])["metadatas"][0]["room"] == c.hidden.id
    z = c.h.db.create_workspace("elsewhere", c.h.admin)
    other = stored(c, "capture:other:codex:readable", workspace=z)
    assert c.store.get(other.id).workspace_id == z
    client = chromadb.PersistentClient(path=str(tmp_path / "foreign-palace"))
    try:
        zcol = client.get_or_create_collection(
            "mempalace_drawers" if kind == "drawer" else "mempalace_closets"
        )
        zcol.add(
            ids=["opaque-local-id"],
            documents=["Readable foreign words"],
            metadatas=[{"wing": "shared", "room": other.id}],
        )
        assert zcol.get(ids=["opaque-local-id"])["metadatas"][0]["room"] == other.id
        seed(c, field, "opaque-local-id")
        assert (
            c.kg._conn().execute(f"SELECT {field} FROM triples").fetchone()[0] == "opaque-local-id"
        )
        row = fact(c)
        assert field not in citation(row, kind), "s09 legacy local citation unproved"
    finally:
        client.close()


@pytest.mark.parametrize("location", ["hidden_directory"])
def test_s09_raw_file(case, location):
    c = case
    raw = "notes/" + c.hidden.id + "/readable.md"
    c.col.add(
        ids=["readable-drawer"],
        documents=["Exact drawer words"],
        metadatas=[{"wing": "shared", "room": c.primary.id, "source_file": raw}],
    )
    reply = invoke(c, "get_drawer", drawer_id="readable-drawer")
    assert reply["content"] == "Exact drawer words"
    assert "source_file" not in reply["metadata"], "s09 basename lost reference context"


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("kind", ["drawer", "closet"])
@pytest.mark.parametrize("encoded", [False, True])
def test_s09_readable_citation(case, role, kind, encoded):
    c = case
    field = "source_drawer_id" if kind == "drawer" else "source_closet"
    identifier = "opaque local:id" if encoded else "opaque-local-id"
    target(c, kind, c.primary.id, identifier=identifier)
    value = quote(quote(identifier, safe=""), safe="") if encoded else identifier
    seed(c, field, value)
    assert citation(fact(c, role), kind)[field] == value


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("raw", ["plain/notes.md", "readable"])
def test_s09_readable_file(case, role, raw):
    c = case
    raw = raw if raw != "readable" else "notes/" + c.primary.id + "/notes.md"
    c.col.add(
        ids=["readable-drawer"],
        documents=["Exact drawer words"],
        metadatas=[{"wing": "shared", "room": c.primary.id, "source_file": raw}],
    )
    reply = invoke(c, "get_drawer", role, drawer_id="readable-drawer")
    assert reply["metadata"]["source_file"] == "notes.md"


@pytest.mark.parametrize("role", ["member", "owner"])
def test_s09_batch_positions(case, role):
    c = case
    for rid, parent, index in [
        ("piece-b", "logical", 1),
        ("piece-a", "logical", 0),
        ("single", None, 0),
    ]:
        meta = {"wing": "shared", "room": c.primary.id, "source_file": "plain/notes.md"}
        if parent:
            meta.update(parent_entry_id=parent, chunk_index=index)
        c.col.add(ids=[rid], documents=[rid], metadatas=[meta])
    result = invoke(c, "get_drawers", role, drawer_ids=["single", "logical", "piece-b"])
    assert [r["drawer_id"] for r in result["results"]] == ["single", "logical", "piece-b"]
    assert [r["content"] for r in result["results"]] == ["single", "piece-apiece-b", "piece-b"]
    assert all(r["metadata"]["source_file"] == "notes.md" for r in result["results"])


@pytest.mark.parametrize("role", ["member", "owner"])
def test_s09_canonical_tokens(case, role):
    c = case
    seed(c, "source_file", "notes/" + c.primary.id + "/notes.md")
    row = fact(c, role)
    assert row["sources"][0]["source_file"] == "notes/" + c.primary.id + "/notes.md"
    with c.kg._lock:
        c.kg._conn().execute("UPDATE fact_sources SET source_file=?", (c.hidden.id,))
        c.kg._conn().commit()
    assert "source_file" not in fact(c, role)["sources"][0]


@pytest.mark.parametrize("role", ["member", "owner"])
def test_s09_real_lookup_spies(case, role):
    from commonplace.handoff.mirror_access import reader

    c = case
    access = reader(c.h.gateway, getattr(c, role))
    assert access.reference(c.primary.source_thread_id)
    exact = stored(c, "capture:primary:codex:other#sha256:" + "a" * 64)
    assert access.reference(exact.source_thread_id)
    seed(c, "source_file", c.primary.source_thread_id)
    assert fact(c, role)["sources"][0]["source_file"] == c.primary.source_thread_id
    target(c, "drawer", c.primary.id)
    invoke(c, "get_drawer", role, drawer_id="opaque-local-id")
    bulk = c.h.gateway.mediator.workers.call_tool(
        getattr(c, role), "mempalace_get_drawers", {"drawer_ids": ["opaque-local-id"]}
    )
    assert bulk["results"][0]["content"] == "Exact target words"
    assert {"by_source", "by_source_family"} <= set(c.source_calls)
    assert c.lookups and all(w == c.h.workspace for w in c.lookups)
    assert any(name == "mempalace_get_drawers" for _, name, _ in c.calls)


@pytest.mark.parametrize("kind", ["wildcard", "literal"])
def test_member_shared_reader_contract(hub, shared_readers, kind):  # noqa: F811
    principal = hub.db.authenticate_token(shared_readers[kind])
    assert principal.role == "member" and not principal.person
    assert not principal.policy.sees_all_wings
    assert not principal.policy.has_scope("private:read")
    assert principal.policy.can_read_wing("wing_fixture")
    assert principal.policy.can_read_wing("shared")
    assert not principal.policy.can_read_wing("private_notes")
    assert not principal.policy.can_read_wing("private_chats")
    assert_member_drawer_access(hub, shared_readers[kind])
