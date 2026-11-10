"""Logical pages authorize physical membership before grouping and slicing."""

import pytest

from commonplace.handoff.models import ThreadMessage
from tests._commonplace_helpers import functional_worker_transport, hub_fixture  # noqa: F401


@pytest.fixture
def page_world(hub, collection, monkeypatch, request):
    request.getfixturevalue("functional_worker_transport")
    from mempalace import mcp_server as mcp

    monkeypatch.setenv("COMMONPLACE_RULES", "off")
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    monkeypatch.delenv("MEMPALACE_BACKEND", raising=False)
    monkeypatch.setattr(mcp, "_get_collection", lambda **kw: collection)
    member = hub.db.create_user("page-member@example.test")
    hub.db.add_member(hub.workspace, member, "owner", actor=hub.admin)
    # Private-read fixture holders must be owners; filer boundaries remain independent.
    store = hub.gateway.handoff.store_for(hub.workspace)
    visible = store.store(
        hub.workspace, "visible", [ThreadMessage(0, "user", "message", "v", "v")], actor=member
    )
    other = hub.db.create_user("private-author@example.test")
    hidden = store.store(
        hub.workspace,
        f"capture:{other}:hidden",
        [ThreadMessage(0, "user", "message", "h", "h")],
        actor=other,
        wing="private_chats",
    )
    token = hub.token(
        "page-reader",
        user=member,
        tools=["mempalace_list_drawers", "mempalace_get_drawers"],
        scopes=["memory:read", "private:read", "threads:read"],
        read_wings=["*"],
        trust="trusted",
    )
    principal = hub.db.authenticate_token(token)
    assert principal.role == "owner" and principal.policy.sees_all_wings
    assert all(
        principal.policy.has_scope(s) for s in ("memory:read", "private:read", "threads:read")
    )
    assert principal.policy.refusal("mempalace_list_drawers") is None
    assert hub.gateway.handoff.recall.can_read(principal, visible.id)
    assert not hub.gateway.handoff.recall.can_read(principal, hidden.id)
    yield hub, mcp, collection, token, visible.id, hidden.id
    hub.gateway.handoff.close()
    hub.gateway.webhooks.stop()


def seed(w, hidden=True, chunks=False):
    _, _, col, _, visible, private = w
    ids = ["b-visible", "c-visible"]
    metas = [{"wing": "wing_fixture", "room": visible, "filed_at": "2026-10-01"} for _ in ids]
    if chunks:
        for n, meta in enumerate(metas):
            meta.update(parent_drawer_id="b-logical", chunk_index=n)
    if hidden:
        ids.insert(0, "a-hidden")
        metas.insert(
            0,
            {
                "wing": "wing_fixture",
                "room": private,
                "filed_at": "2026-10-01",
                **({"parent_drawer_id": "b-logical", "chunk_index": -1} if chunks else {}),
            },
        )
    col.add(ids=ids, metadatas=metas, documents=[f"text {i}" for i in ids])
    assert col.get(ids=["a-hidden"], include=["metadatas"])["ids"] == (
        ["a-hidden"] if hidden else []
    )


def page(w, offset=0, **args):
    h, _, _, token, _, _ = w
    result, raw = h.call(
        token, "mempalace_list_drawers", wing="wing_fixture", limit=1, offset=offset, **args
    )
    assert result is not None, raw
    return result


@pytest.mark.parametrize("path", ["sqlite", "sdk"])
def test_card253_s06_drawer_page(page_world, monkeypatch, path):
    w = page_world
    _, mcp, col, _, _, _ = w
    monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: path == "sqlite")
    from mempalace.backends import chroma_sqlite

    branches = []
    sql_read = chroma_sqlite.sqlite_list_id_metadata
    sdk_get = type(col).get

    def sqlite_read(*args, **kwargs):
        branches.append("sqlite")
        return sql_read(*args, **kwargs)

    def sdk_read(self, *args, **kwargs):
        if "documents" in kwargs.get("include", []):
            branches.append("sdk")
        return sdk_get(self, *args, **kwargs)

    monkeypatch.setattr(chroma_sqlite, "sqlite_list_id_metadata", sqlite_read)
    monkeypatch.setattr(type(col), "get", sdk_read)
    hits = []
    original = mcp._collapse_drawer_rows

    def collapse(ids, docs, metas):
        value = original(ids, docs, metas)
        hits.append([d["drawer_id"] for d in value])
        return value

    monkeypatch.setattr(mcp, "_collapse_drawer_rows", collapse)
    seed(w)
    assert (
        original(
            ["a-hidden", "b-visible", "c-visible"],
            [],
            col.get(ids=["a-hidden", "b-visible", "c-visible"], include=["metadatas"])["metadatas"],
        )[0]["drawer_id"]
        == "a-hidden"
    )
    present = [page(w, n) for n in (0, 1)]
    assert hits and path in branches
    assert all(ids[0] == "a-hidden" for ids in hits if "a-hidden" in ids)
    col.delete(ids=["a-hidden"])
    mcp._invalidate_overview_caches()
    control = [page(w, n) for n in (0, 1)]
    assert present == control, "s06 H shifted drawer page"
    assert all(p["total"] == 2 and p["count"] == 1 for p in present)


def test_card253_s06_readable_guards(page_world):
    w = page_world
    seed(w, hidden=False, chunks=True)
    result = page(w, since="2026-10-01", before="2026-10-02")
    assert result["total"] == result["count"] == 1
    assert result["drawers"][0]["chunk_ids"] == ["b-visible", "c-visible"]
    assert result["drawers"][0]["content_preview"] == "text b-visibletext c-visible"
    assert page(w, since="2026-10-02")["total"] == 0
    h, _, _, token, _, _ = w
    batch, _ = h.call(token, "mempalace_get_drawers", drawer_ids=["b-visible", "missing"])
    assert batch["count"] == 2 and batch["errors"] == 1
    assert batch["results"][1]["drawer_id"] == "missing"


@pytest.mark.parametrize("path", ["sqlite", "sdk"])
def test_card253_s06_chunk_visibility(page_world, monkeypatch, path):
    from commonplace import sessions

    w = page_world
    h, mcp, _, token, _, hidden = w
    monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: path == "sqlite")
    seed(w, chunks=True)
    result = page(w)
    assert result["total"] == result["count"] == 1
    drawer = result["drawers"][0]
    assert drawer["chunk_ids"] == ["b-visible", "c-visible"]
    assert drawer["content_preview"] == "text b-visibletext c-visible"
    owner = sessions.person_principal(h.db, h.admin, h.workspace)
    assert owner.person and owner.role == "owner"
    assert owner.policy.trust == "trusted" and owner.policy.has_scope("threads:read")
    assert not h.gateway.handoff.recall.can_read(owner, hidden)
    assert (
        h.gateway.call_memory_tool(owner, "mempalace_list_drawers", {"wing": "fixture", "limit": 1})
        == result
    )


@pytest.mark.parametrize("path", ["sqlite", "sdk"])
def test_card253_s06_changed_descriptor(page_world, monkeypatch, path):
    from commonplace.gateway import WorkerClient

    w = page_world
    _, mcp, col, _, _, hidden = w
    seed(w, hidden=False)
    monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: path == "sqlite")
    original = WorkerClient.call_tool
    calls = []

    def call(self, principal, name, args):
        if name == "mempalace_list_drawers" and "thread_scope" in args:
            calls.append(name)
            col.update(ids=["b-visible"], metadatas=[{"room": hidden}])
        return original(self, principal, name, args)

    monkeypatch.setattr(WorkerClient, "call_tool", call)
    result = page(w)
    assert calls
    assert result["total"] == 1 and result["drawers"][0]["drawer_id"] == "c-visible"


@pytest.mark.parametrize("fault", ["malformed", "raised", "deadline"])
def test_card253_s06_incomplete_projection(fault):
    from types import SimpleNamespace

    from commonplace.gateway import _tool_response
    from commonplace.handoff.drawer_pages import project
    from commonplace.rules import checks

    calls = []

    class Workers:
        def post(self, principal, request):
            return _tool_response(
                request["id"], {"drawers": [], "total": 0, "count": 0, "offset": 7, "limit": 2}
            )

        def call_tool(self, *args):
            calls.append(True)
            if fault == "raised":
                raise RuntimeError("private failure")
            if fault == "deadline":
                checks.remaining(0)
            return {"probe": {"rows": [], "next": "bad"}, "taxonomy": {}}

    principal = SimpleNamespace(workspace_id="x")
    result = project(
        SimpleNamespace(),
        principal,
        Workers(),
        {"offset": 7, "limit": 2},
        timeout=0 if fault == "deadline" else 1,
    )
    assert result == {"drawers": [], "total": 0, "count": 0, "offset": 7, "limit": 2}
    assert calls == ([] if fault == "deadline" else [True])


@pytest.mark.parametrize("incomplete", [False, True])
def test_card253_s06_complete_enumeration(page_world, monkeypatch, incomplete):
    from commonplace.gateway import WorkerClient
    from commonplace.handoff import mirror_access

    w = page_world
    _, mcp, col, _, visible, _ = w
    monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: False)
    seed(w)
    ids = [f"d-visible-{n:03}" for n in range(501)]
    col.add(
        ids=ids,
        metadatas=[{"wing": "wing_fixture", "room": visible} for _ in ids],
        documents=["visible"] * len(ids),
    )
    original, gates = WorkerClient.call_tool, []
    probes = []
    gate = mirror_access.gate

    def checked_gate(gateway, principal, rule, result):
        cleaned = gate(gateway, principal, rule, result)
        if isinstance(result, dict) and "drawers" in result:
            gates.append(True)
            assert cleaned == result
        return cleaned

    def call(self, principal, name, args):
        result = original(self, principal, name, args)
        if name == "mempalace_get_taxonomy":
            probes.append(args["thread_scope"]["after"])
            assert len(result["probe"]["rows"]) <= 500
            if incomplete and len(probes) == 2:
                return {"taxonomy": {}, "probe": {"rows": [], "next": "broken"}}
        return result

    monkeypatch.setattr(WorkerClient, "call_tool", call)
    monkeypatch.setattr(mirror_access, "gate", checked_gate)
    result = page(w, offset=502)
    assert len(probes) == 2 and gates
    assert result["total"] == (0 if incomplete else 503)
    assert result["count"] == (0 if incomplete else 1)


def test_card253_s06_provenance_scrub_keeps_visible_total(page_world):
    w = page_world
    _, _, col, _, _, hidden = w
    seed(w, hidden=False)
    col.update(ids=["b-visible"], metadatas=[{"source_thread": hidden}])
    result = page(w)
    assert result.get("total") == 2 and result["count"] == 1
    assert "source_thread" not in result["drawers"][0]["metadata"]


def literal_token(w, *, import_read=False, wings=None):
    h, _, _, token, visible, hidden = w
    member = h.db.authenticate_token(token)
    scopes = ["memory:read", "private:read", "threads:read"]
    if import_read:
        scopes.append("memory:import:read")
    token = h.token(
        f"literal-page-reader-{import_read}",
        user=member.user_id,
        tools=["mempalace_list_drawers"],
        scopes=scopes,
        read_wings=wings or ["wing_fixture", "threads"],
        trust="trusted",
    )
    principal = h.db.authenticate_token(token)
    assert principal.role == "owner" and not principal.policy.sees_all_wings
    assert principal.policy.refusal("mempalace_list_drawers") is None
    assert h.gateway.handoff.recall.can_read(principal, visible)
    assert not h.gateway.handoff.recall.can_read(principal, hidden)
    reply, _ = h.call(token, "mempalace_list_drawers", wing="wing_fixture")
    assert reply is not None and "error" not in reply
    return token


@pytest.mark.parametrize("path", ["sqlite", "sdk"])
def test_card253_s06_r1_literal_import_total(page_world, monkeypatch, path):
    w = page_world
    h, mcp, col, _, _, _ = w
    monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: path == "sqlite")
    seed(w)
    col.update(
        ids=["b-visible", "c-visible"], metadatas=[{"source_file": "/notes/readable.md"}] * 2
    )
    token = literal_token(w, import_read=True)
    assert h.db.authenticate_token(token).policy.has_scope("memory:import:read")
    present = [
        h.call(token, "mempalace_list_drawers", wing="wing_fixture", limit=1, offset=n)[0]
        for n in (0, 1)
    ]
    assert all(p.get("total") == 2 and p.get("count") == 1 for p in present), (
        "s06 import reader lost visible total"
    )
    plain = literal_token(w)
    plain_present = [
        h.call(plain, "mempalace_list_drawers", wing="wing_fixture", limit=1, offset=n)[0]
        for n in (0, 1)
    ]
    col.delete(ids=["a-hidden"])
    mcp._invalidate_overview_caches()
    control = [
        h.call(token, "mempalace_list_drawers", wing="wing_fixture", limit=1, offset=n)[0]
        for n in (0, 1)
    ]
    assert present == control
    plain_control = [
        h.call(plain, "mempalace_list_drawers", wing="wing_fixture", limit=1, offset=n)[0]
        for n in (0, 1)
    ]
    assert plain_present == plain_control
    assert plain_control == control


@pytest.mark.parametrize("case", ["since", "offset", "limit_null"])
def test_card253_s06_r1_gateway_errors(page_world, case):
    import json

    from commonplace.gateway import _tool_result
    from mempalace.date_window import parse_date_bound

    w = page_world
    h, _, _, _, _, _ = w
    seed(w, hidden=False)
    token = w[3]
    assert h.call(token, "mempalace_list_drawers", wing="wing_fixture")[0]["total"] == 2
    extra = {
        "since": {"since": "not-a-date"},
        "offset": {"offset": "abc"},
        "limit_null": {"limit": None},
    }[case]
    body = {
        "jsonrpc": "2.0",
        "id": 123,
        "method": "tools/call",
        "params": {
            "name": "mempalace_list_drawers",
            "arguments": {"wing": "wing_fixture", **extra},
        },
    }
    status, raw = h.raw(token, body)
    assert status == 200, "s06 invalid pagination escaped as HTTP 500"
    reply = json.loads(raw)
    if case == "since":
        with pytest.raises(ValueError) as exc:
            parse_date_bound("not-a-date", "since")
        message = str(exc.value)
        assert _tool_result(reply) == {"error": message}, "s06 invalid filter became empty success"
    elif case == "offset":
        assert reply == {
            "jsonrpc": "2.0",
            "id": 123,
            "error": {
                "code": -32602,
                "message": "Invalid params for tool mempalace_list_drawers: 'offset' must be an integer",
            },
        }, "s06 invalid offset changed RPC error"
    else:
        result = _tool_result(reply)
        assert result is not None and result.get("total") == 2 and result.get("limit") == 20
        assert [d["drawer_id"] for d in result["drawers"]] == ["b-visible", "c-visible"]


@pytest.mark.parametrize("case", ["missing_wing", "unreadable_wing"])
def test_card253_s06_r1_denial_text(page_world, case):
    w = page_world
    h, _, _, _, _, _ = w
    token = literal_token(w, wings=["wing_fixture", "wing_other", "threads"])
    args = {} if case == "missing_wing" else {"wing": "denied"}
    result, _ = h.call(token, "mempalace_list_drawers", **args)
    message = (
        "pass a wing: this connection reads wing_fixture, wing_other, threads"
        if case == "missing_wing"
        else "this connection can't read wing 'denied'"
    )
    assert result == {"error": message, "denied": True}, "s06 denial text changed"


@pytest.mark.parametrize("case", ["limit_null", "offset_abc", "since_bad", "_private_chats"])
def test_card253_s06_r2_private_page_args(page_world, monkeypatch, case):
    import json

    from commonplace.gateway import _tool_result
    from commonplace.handoff.recall import Recall
    from mempalace.date_window import parse_date_bound

    h = page_world[0]
    token = h.token(
        "private-page-reader",
        tools=["mempalace_list_drawers"],
        scopes=["memory:read", "memory:import:read"],
        read_wings=["wing_fixture"],
        trust="trusted",
    )
    principal = h.db.authenticate_token(token)
    assert principal.policy.has_scope("memory:import:read")
    assert h.gateway.handoff.recall.import_reader(principal)
    assert not principal.person and principal.role == "owner"
    warm, _ = h.call(token, "mempalace_list_drawers", wing="wing_fixture")
    assert warm == {"drawers": [], "total": 0, "count": 0, "offset": 0, "limit": 20}
    original = Recall.import_listing
    calls = []

    def listing(self, reader, **kwargs):
        calls.append(kwargs)
        return original(self, reader, **kwargs)

    monkeypatch.setattr(Recall, "import_listing", listing)
    control, _ = h.call(token, "mempalace_list_drawers", wing="private_chats")
    assert control == {"drawers": [], "count": 0}
    assert calls == [{"limit": 20, "offset": 0, "room": None}]
    calls.clear()
    extra = {
        "limit_null": {"limit": None},
        "offset_abc": {"offset": "abc"},
        "since_bad": {"since": "not-a-date"},
        "_private_chats": {},
    }[case]
    request = {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "tools/call",
        "params": {
            "name": "mempalace_list_drawers",
            "arguments": {
                "wing": "_private_chats" if case == "_private_chats" else "private_chats",
                **extra,
            },
        },
    }
    status, raw = h.raw(token, request)
    assert status == 200, "s06 private page args escaped as HTTP 500"
    reply = json.loads(raw)
    if case in ("limit_null", "_private_chats"):
        assert _tool_result(reply) == control, "s06 private listing changed"
        assert calls == [{"limit": 20, "offset": 0, "room": None}]
    elif case == "offset_abc":
        assert reply == {
            "jsonrpc": "2.0",
            "id": 42,
            "error": {
                "code": -32602,
                "message": "Invalid params for tool mempalace_list_drawers: 'offset' must be an integer",
            },
        }, "s06 private offset changed RPC error"
        assert not calls
    else:
        with pytest.raises(ValueError) as exc:
            parse_date_bound("not-a-date", "since")
        assert _tool_result(reply) == {"error": str(exc.value)}, (
            "s06 private invalid since became empty success"
        )
        assert not calls
