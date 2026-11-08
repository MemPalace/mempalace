"""Private overview protocol: bounded pages, completeness and fresh metadata."""

import copy
from dataclasses import replace

import pytest

from commonplace.gateway import Mediator, WorkerClient, _replace_result, _tool_result
from commonplace.handoff.recall import Recall
from mempalace.read_scope import PAGE_SIZE, descriptor, metadata_page
from tests.test_card253_presence import (  # noqa: F401
    force_path,
    functional_worker_transport,
    hub_fixture,
    result,
    search_args,
    seed,
    transport,
    world as presence_world,
)


@pytest.fixture
def world(request):
    return request.getfixturevalue("presence_world")


def add_many(w, n):
    for start in range(0, n, PAGE_SIZE):
        ids = [f"bulk-{i:05}" for i in range(start, min(start + PAGE_SIZE, n))]
        w.col.add(
            ids=ids,
            documents=["verbatim"] * len(ids),
            metadatas=[{"wing": "wing_fixture", "room": w.readable.id}] * len(ids),
            embeddings=[[1.0] + [0.0] * 383] * len(ids),
        )


def probe(w, after=None):
    return w.mcp.tool_get_taxonomy(thread_scope={"probe": True, "after": after, "timeout": 60.0})


def test_complete_probes_and_bounded_batches(world, monkeypatch):
    w = world
    add_many(w, 1003)
    pages, after, rows = [], None, []
    while True:
        page = probe(w, after)
        assert isinstance(page.get("probe"), dict)
        batch = page["probe"]["rows"]
        pages.append(len(batch))
        rows.extend(batch)
        after = page["probe"]["next"]
        if after is None:
            break
    assert pages == [500, 500, 3]
    assert len({r["id"] for r in rows}) == 1003
    calls = []
    original = WorkerClient.post

    def post(self, principal, request):
        scope = request.get("params", {}).get("arguments", {}).get("thread_scope")
        if scope is not None:
            assert principal.workspace_id == w.h.workspace
            calls.append(copy.deepcopy(scope))
            if "allow" in scope:
                assert len(scope["allow"]) <= PAGE_SIZE
        return original(self, principal, request)

    monkeypatch.setattr(WorkerClient, "post", post)

    def no_words(*args, **kwargs):
        raise AssertionError("overview loaded thread words")

    monkeypatch.setattr(type(w.h.gateway.handoff.store_for(w.h.workspace)), "messages", no_words)
    assert result(w, "mempalace_list_wings") == {"wings": {"wing_fixture": 1003}}
    assert len([s for s in calls if s.get("probe")]) == 3
    assert [len(s["allow"]) for s in calls if "allow" in s] == [500, 500, 3]
    # Direct SQL projections never select a document column.
    from mempalace.backends import _inproc_sqlite

    original_open = _inproc_sqlite.open_reader
    statements = []

    def open_reader(*args, **kwargs):
        conn = original_open(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(_inproc_sqlite, "open_reader", open_reader)
    assert result(w, "mempalace_list_wings")["wings"] == {"wing_fixture": 1003}
    assert statements and not any(
        "SELECT document," in sql or "SELECT id,document" in sql for sql in statements
    )


def test_empty_allow_and_unscoped_compatibility(world):
    w = world
    seed(w)
    for name, key in (("tool_list_wings", "wings"), ("tool_get_taxonomy", "taxonomy")):
        tool = getattr(w.mcp, name)
        assert tool(thread_scope={"allow": [], "timeout": 60.0}) == {key: {}}
        assert tool() == tool(thread_scope=None)
        assert tool()[key]
    # Exercise the worker transport as well as direct private calls.
    assert result(w, "mempalace_list_wings") == {"wings": {"wing_fixture": 1}}


@pytest.mark.parametrize("context", ["none", "absent", "no_handoff"])
@pytest.mark.parametrize(
    "tool", ["mempalace_list_wings", "mempalace_get_taxonomy", "mempalace_search"]
)
def test_r12_no_gateway_mediator_preserves_ordinary_rows_only(world, context, tool):
    """No thread authority must hide thread presence without losing ordinary rows."""
    from types import SimpleNamespace

    from mempalace.stored_text import KEYWORD_ONLY

    w = world
    hidden_ids = seed(w)
    w.col._inner.add(
        ids=["ordinary"],
        documents=["ordinary readable words"],
        metadatas=[{"wing": "wing_fixture", "room": "notes", KEYWORD_ONLY: True}],
    )
    principal = w.h.db.authenticate_token(w.tokens["F"])
    raw = w.h.gateway.mediator.workers.call_tool(principal, "mempalace_list_wings", {})
    assert raw["wings"]["wing_fixture"] > 1 and "wing_hidden" in raw["wings"]
    mediator = Mediator(w.h.db, w.h.gateway.mediator.workers)
    if context == "absent":
        del mediator.gateway
    elif context == "no_handoff":
        mediator.gateway = SimpleNamespace()

    def read():
        if tool == "mempalace_search":
            return mediator._fan_out_search(principal, tool, {"query": "zznonmatchingfixture"})
        return mediator._before_wing_list(principal, tool, {}, [])["respond"]

    if tool == "mempalace_search":
        expected = mediator.workers.call_tool(
            principal, tool, {"query": "zznonmatchingfixture", "wings": ["wing_fixture"]}
        )
        assert expected["results"] == []
        expected["wings_searched"] = ["wing_fixture"]
    else:
        expected = (
            {"wings": {"wing_fixture": 1}}
            if tool == "mempalace_list_wings"
            else {"taxonomy": {"wing_fixture": {"notes": 1}}}
        )
    reply = read()
    assert reply == expected
    # Even a thread readable through the attached gateway is unresolved here.
    w.col.delete(ids=hidden_ids + ["readable-0"])
    w.mcp._invalidate_overview_caches()
    assert read() == reply


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"allow": "all", "timeout": 60},
        {"allow": [{"id": "x"}], "timeout": 60},
        {"allow": [], "timeout": -1},
        {"probe": True, "after": "bad", "timeout": 60},
    ],
)
def test_invalid_scope_schema(world, scope):
    w = world
    seed(w)
    assert w.mcp.tool_list_wings(thread_scope=scope) == {"wings": {}}
    assert w.mcp.tool_get_taxonomy(thread_scope=scope) == {"taxonomy": {}}
    assert result(w, "mempalace_list_wings")["wings"] == {"wing_fixture": 1}


def test_changed_new_descriptors_and_warm_cache(world, monkeypatch):
    w = world
    seed(w)
    w.col.add(
        ids=["unchanged"],
        documents=["still readable"],
        metadatas=[{"wing": "wing_fixture", "room": w.readable.id}],
        embeddings=[[1.0] + [0.0] * 383],
    )
    raw = copy.deepcopy(w.mcp._sqlite_taxonomy())
    original = WorkerClient.post
    changes = []

    def post(self, principal, request):
        response = original(self, principal, request)
        scope = request.get("params", {}).get("arguments", {}).get("thread_scope", {})
        if scope.get("probe") and not changes:
            changes.append(True)
            w.col.update(ids=["readable-0"], metadatas=[{"room": w.hidden.id}])
            w.col.add(
                ids=["new"],
                documents=["new readable"],
                metadatas=[{"wing": "wing_fixture", "room": w.readable.id}],
                embeddings=[[1.0] + [0.0] * 383],
            )
        return response

    monkeypatch.setattr(WorkerClient, "post", post)
    assert result(w, "mempalace_list_wings") == {"wings": {"wing_fixture": 1}}
    assert changes and raw[1]["wing_fixture"][w.readable.id] == 2
    # A later request can acquire the newly inserted row; the moved row stays hidden.
    assert result(w, "mempalace_list_wings") == {"wings": {"wing_fixture": 2}}


@pytest.mark.parametrize(
    "fault",
    [
        "missing_probe",
        "nonadvancing",
        "bad_rows",
        "mismatched_taxonomy",
        "duplicate_page",
        "backwards_terminal",
    ],
)
def test_incomplete_or_invalid_pages_fail_closed(world, monkeypatch, fault):
    w = world
    add_many(w, 501)
    original = WorkerClient.post
    hits = []
    first_rows = []

    def post(self, principal, request):
        response = original(self, principal, request)
        scope = request.get("params", {}).get("arguments", {}).get("thread_scope", {})
        if not scope.get("probe"):
            return response
        value = copy.deepcopy(_tool_result(response))
        hits.append(scope.get("after"))
        if fault == "missing_probe":
            value.pop("probe", None)
        elif fault == "nonadvancing":
            value["probe"]["next"] = scope.get("after") or "id:"
        elif fault == "bad_rows":
            value["probe"]["rows"] = [{"id": "bad"}]
        elif fault == "mismatched_taxonomy":
            value["taxonomy"] = {}
        elif fault in ("duplicate_page", "backwards_terminal"):
            if not first_rows:
                first_rows.extend(value["probe"]["rows"])
            else:
                value["probe"]["rows"] = first_rows[:1]
                if fault == "backwards_terminal":
                    value["probe"]["rows"][0]["id"] = "a-earlier-unseen"
                value["probe"]["next"] = None
                value["taxonomy"] = {"wing_fixture": {w.readable.id: 1}}
        return _replace_result(response, value)

    monkeypatch.setattr(WorkerClient, "post", post)
    assert result(w, "mempalace_list_wings") == {"wings": {}}
    assert hits


def test_metadata_fallback_pages_and_alignment(world, monkeypatch):
    w = world
    add_many(w, 1001)
    original_get = w.col.get
    calls = []

    def get(**kwargs):
        assert kwargs["include"] == ["metadatas"]
        assert kwargs.get("limit", len(kwargs.get("ids", []))) <= 500
        calls.append(kwargs)
        return original_get(**kwargs)

    monkeypatch.setattr(w.col, "get", get)
    monkeypatch.setattr(w.mcp, "_selected_backend_name", lambda: "third_party")
    force_path(w, monkeypatch, "no_facets")
    assert result(w, "mempalace_list_wings") == {"wings": {"wing_fixture": 1001}}
    assert [c["offset"] for c in calls if "offset" in c] == [0, 500, 1000]
    assert len([c for c in calls if "ids" in c]) == 3
    # Backends may return id batches in a different order: descriptors retain association.
    row_ids = ["bulk-00002", "bulk-00000", "bulk-00001"]
    rows, cursor = metadata_page(w.mcp._config, "third_party", lambda: w.col, ids=row_ids)
    assert cursor is None and {i for i, _ in rows} == set(row_ids)
    assert all(descriptor(i, m)["room"] == w.readable.id for i, m in rows)


def test_workspace_binding_authorization_memo_and_revocation(world, monkeypatch):
    w = world
    seed(w)
    # A readable duplicate id in another workspace cannot authorize the hidden row in X.
    z = w.h.db.create_workspace("other", w.h.admin)
    other = w.h.gateway.handoff.store_for(z)
    with other._lock:
        other._conn.execute(
            "INSERT INTO threads(id,workspace_id,source_thread_id,message_count,chunk_count,sha256,wing,created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                w.hidden.id,
                z,
                "native-other-workspace",
                1,
                1,
                "0" * 64,
                "threads",
                "2026-01-01",
                w.h.admin,
            ),
        )
    member = w.h.db.authenticate_token(w.tokens["A"])
    assert w.h.gateway.handoff.recall.can_read(replace(member, workspace_id=z), w.hidden.id)
    seen = []
    original = Recall.can_read

    def can_read(self, principal, room):
        seen.append((principal.workspace_id, room))
        return original(self, principal, room)

    monkeypatch.setattr(Recall, "can_read", can_read)
    assert result(w, "mempalace_list_wings") == {"wings": {"wing_fixture": 1}}
    assert seen and {workspace for workspace, _ in seen} == {w.h.workspace}
    # Hidden references shared by multiple wings are resolved only once per projection.
    assert [room for _, room in seen].count(w.hidden.id) == 1
    seen.clear()
    monkeypatch.setattr(Recall, "can_read", lambda *args: False)
    assert result(w, "mempalace_list_wings") == {"wings": {}}


def test_expired_acquisition_discards_partial_search_and_shares_deadline(world, monkeypatch):
    from time import monotonic

    from commonplace.rules import checks

    w = world
    seed(w, hidden_wings=("wing_hidden",))
    original = WorkerClient.post
    original_call = WorkerClient.call_tool
    calls = []
    hits = []

    def call(self, principal, name, args):
        calls.append(name)
        return original_call(self, principal, name, args)

    def post(self, principal, request):
        response = original(self, principal, request)
        scope = request.get("params", {}).get("arguments", {}).get("thread_scope", {})
        if scope.get("probe"):
            hits.append(checks._deadline.get())
            # Exhaust the actual enclosing acquisition deadline, rather than
            # only injecting a one-shot transport fault.
            checks._deadline.set(monotonic() - 1)
        return response

    monkeypatch.setattr(WorkerClient, "call_tool", call)
    monkeypatch.setattr(WorkerClient, "post", post)
    assert result(w, "mempalace_search", search_args(), "F") == {
        "facts": [],
        "query": search_args()["query"],
        "results": [],
        "wings_searched": [],
    }
    assert hits and all(end is not None for end in hits)
    assert "mempalace_search" not in calls
