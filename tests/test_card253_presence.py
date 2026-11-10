"""Public overview and search presence boundaries using native stored threads."""

import json
from types import SimpleNamespace

import pytest

from commonplace.gateway import (
    MAX_FANOUT_WINGS,
    Mediator,
    WorkerClient,
    _replace_result,
    _tool_result,
)
from commonplace.handoff.models import ThreadMessage
from commonplace.rules import checks
from tests._commonplace_helpers import (  # noqa: F401
    assert_member_drawer_access,
    functional_worker_transport,
    hub_fixture,
    shared_readers,
)

TOOLS = ["mempalace_list_wings", "mempalace_get_taxonomy", "mempalace_search"]
QUERY = "zznonmatchingpresencefixture"


@pytest.fixture(autouse=True)
def transport(request):
    if (
        getattr(request.node, "callspec", None) is None
        or request.node.callspec.params.get("mode") != "projection_deadline"
    ):
        request.getfixturevalue("functional_worker_transport")


@pytest.fixture
def world(hub, monkeypatch, tmp_path, request):
    from mempalace import mcp_server as mcp

    backend_name = "compact" if "r12" in request.node.name else "sqlite_exact"
    monkeypatch.setenv("MEMPALACE_BACKEND", backend_name)
    monkeypatch.setattr(mcp._config, "_palace_path_override", str(tmp_path / "exact"))
    from mempalace.backends import PalaceRef
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    if backend_name == "compact":
        from mempalace.backends.compact import CompactBackend

        backend = CompactBackend()
    else:
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
    hub.db.add_member(hub.workspace, member, "owner", actor=hub.admin)
    # Private-read fixture holders must be owners; filer boundaries remain independent.
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
    for kind, wings in (("A", ["*"]), ("F", ["private_*", "wing_*"])):
        tokens[kind] = hub.token(
            f"reader-{kind}",
            user=member,
            tools=TOOLS,
            scopes=["memory:read", "private:read", "threads:read"],
            read_wings=wings,
            trust="trusted",
        )
        principal = hub.db.authenticate_token(tokens[kind])
        assert principal.role == "owner" and principal.policy.has_scope("threads:read")
        assert principal.policy.sees_all_wings is (kind == "A")
        assert hub.gateway.handoff.recall.can_read(principal, readable.id)
        assert not hub.gateway.handoff.recall.can_read(principal, hidden.id)
        assert store.get(hidden.id) is not None
        for tool in TOOLS:
            assert principal.policy.refusal(tool) is None
    mcp._invalidate_overview_caches()
    yield SimpleNamespace(
        h=hub, col=collection, mcp=mcp, readable=readable, hidden=hidden, tokens=tokens
    )
    hub.gateway.handoff.close()
    hub.gateway.webhooks.stop()
    backend.close()


def seed(w, visible=1, hidden_wings=("wing_fixture", "wing_hidden", "private_threads")):
    ids, metas = [], []
    for n in range(visible):
        ids.append(f"readable-{n}")
        metas.append(
            {
                "wing": "wing_fixture" if visible == 1 else f"wing_visible_{n:03}",
                "room": w.readable.id,
            }
        )
    hidden_ids = []
    for n, wing in enumerate(hidden_wings):
        hidden_ids.append(f"hidden-{n}")
        ids.append(hidden_ids[-1])
        metas.append({"wing": wing, "room": w.hidden.id})
    # The compact fixture intentionally has metadata/text-only drawers. Its
    # supported null vectors make a nonmatching query truly empty without
    # replacing worker search or changing the public RPC arguments.
    if w.mcp._selected_backend_name() == "compact":
        from mempalace.stored_text import KEYWORD_ONLY

        for meta in metas:
            meta[KEYWORD_ONLY] = True
    options = (
        {}
        if w.mcp._selected_backend_name() == "compact"
        else {"embeddings": [[1.0] + [0.0] * 383 for _ in ids]}
    )
    w.col._inner.add(
        ids=ids,
        metadatas=metas,
        documents=[f"ordinary words {n}" for n in range(len(ids))],
        **options,
    )
    for kind in w.tokens:
        policy = w.h.db.authenticate_token(w.tokens[kind]).policy
        assert all(policy.can_read_wing(m["wing"]) for m in metas)
    assert set(w.col.get(ids=hidden_ids, include=["metadatas"])["ids"]) == set(hidden_ids)
    return hidden_ids


def rpc(w, tool, args=None, kind="A"):
    status, raw = w.h.raw(
        w.tokens[kind],
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        },
    )
    assert status == 200
    return json.loads(raw)


def result(w, tool, args=None, kind="A"):
    reply = rpc(w, tool, args, kind)
    parsed = _tool_result(reply)
    assert parsed is not None, reply
    return parsed


def control(w, hidden_ids):
    w.col.delete(ids=hidden_ids)
    w.mcp._invalidate_overview_caches()


def force_path(w, monkeypatch, path):
    hits = []
    original = w.mcp._sqlite_taxonomy

    def fast():
        hits.append("sqlite")
        value = original()
        return value if path == "sqlite" else None

    monkeypatch.setattr(w.mcp, "_sqlite_taxonomy", fast)
    original_supports = w.mcp._supports_metadata_facets

    def supports(col):
        hits.append("facets")
        return path != "no_facets" and original_supports(col)

    monkeypatch.setattr(w.mcp, "_supports_metadata_facets", supports)
    # Chroma's production wrapper supplies facets, unlike the fixture's raw collection.
    wrapper = w.mcp._get_collection()
    original_facet = type(wrapper).facet_counts

    def facets(self, *a, **k):
        hits.append("facet_call")
        if path == "facet_raises":
            raise RuntimeError("fixture facets unavailable")
        return original_facet(self, *a, **k)

    monkeypatch.setattr(type(wrapper), "facet_counts", facets)
    original_tally = w.mcp._tally_wings_rooms

    def tally(*a, **k):
        hits.append("tally")
        return original_tally(*a, **k)

    monkeypatch.setattr(w.mcp, "_tally_wings_rooms", tally)
    return hits


@pytest.mark.parametrize("path", ["sqlite", "facet", "no_facets", "facet_raises"])
def test_card253_r01_wing_counts(world, monkeypatch, path):
    w = world
    hidden = seed(w)
    hits = force_path(w, monkeypatch, path)
    reply = result(w, "mempalace_list_wings")
    assert "sqlite" in hits
    if path in ("no_facets", "facet_raises"):
        assert "tally" in hits
    if path in ("facet", "facet_raises"):
        assert "facet_call" in hits
    print("branch reached:", path, hits)
    control(w, hidden)
    expected = result(w, "mempalace_list_wings")
    assert reply["wings"] == {"wing_fixture": 1} == expected["wings"]


@pytest.mark.parametrize("mode", ["partial_tally"])
def test_card253_r01_error_exit(world, monkeypatch, mode):
    w = world
    hidden = seed(w)
    force_path(w, monkeypatch, "no_facets")
    hits = []

    def broken(meta, wings=None, taxonomy=None, **kwargs):
        hits.append(True)
        if wings is not None:
            wings["wing_hidden"] = 99
        if taxonomy is not None:
            taxonomy["wing_hidden"] = {w.hidden.id: 99}
        raise RuntimeError("partial fixture fault")

    monkeypatch.setattr(w.mcp, "_tally_wings_rooms", broken)
    reply = result(w, "mempalace_list_wings")
    assert hits
    control(w, hidden)
    expected = result(w, "mempalace_list_wings")
    print("branch reached: partial tally", len(hits))
    assert reply == {"wings": {}} == expected


def fanout_spy(monkeypatch):
    calls = []
    original = Mediator._fan_out_search

    def run(self, principal, name, args):
        calls.append((principal.workspace_id, principal.policy.read_wings, dict(args)))
        assert not principal.policy.sees_all_wings
        assert principal.policy.has_scope("threads:read")
        assert "wing" not in args and "wings" not in args and "cli_compatible" not in args
        return original(self, principal, name, args)

    monkeypatch.setattr(Mediator, "_fan_out_search", run)
    return calls


def search_args():
    return {"query": QUERY, "include_facts": False, "limit": 1}


@pytest.mark.parametrize("hidden_wing", ["private_threads", "hidden_only_readable_wing"])
def test_card253_r12_search_wings_searched(world, monkeypatch, hidden_wing):
    w = world
    hidden = seed(
        w, hidden_wings=("private_threads" if hidden_wing == "private_threads" else "wing_hidden",)
    )
    calls = fanout_spy(monkeypatch)
    reply = result(w, "mempalace_search", search_args(), "F")
    assert len(calls) == 1
    assert not reply.get("error"), reply
    assert reply.get("results") == [] and reply.get("facts") == []
    assert isinstance(reply, dict) and isinstance(reply.get("wings_searched"), list)
    control(w, hidden)
    expected = result(w, "mempalace_search", search_args(), "F")
    assert len(calls) == 2
    print("fan-out reached in both worlds:", calls)
    assert reply["wings_searched"] == expected["wings_searched"], "r12 hidden-only wing exposed"
    assert reply == expected


@pytest.mark.parametrize("mode", ["below_limit", "above_limit"])
def test_card253_r12_search_denied_count(world, monkeypatch, mode):
    w = world
    n = MAX_FANOUT_WINGS + (mode == "above_limit")
    hidden = seed(w, visible=n, hidden_wings=("wing_hidden",))
    assert len(w.mcp.tool_list_wings()["wings"]) == n + 1
    calls = fanout_spy(monkeypatch)
    reply = rpc(w, "mempalace_search", search_args(), "F")
    assert len(calls) == 1
    control(w, hidden)
    assert len(w.mcp.tool_list_wings()["wings"]) == n
    expected = rpc(w, "mempalace_search", search_args(), "F")
    assert len(calls) == 2
    print("fan-out limit branch reached in both worlds:", n, calls)
    assert reply == expected, "r12 hidden wing changed public denial"
    if mode == "above_limit":
        assert _tool_result(reply)["error"] == f"pass a wing: this connection reads {n} wings"


def payload_fault(monkeypatch, payload):
    hits = []
    original = WorkerClient.post

    def post(self, principal, request):
        response = original(self, principal, request)
        if request.get("params", {}).get("name") in TOOLS[:2]:
            hits.append(request["params"]["name"])
            return _replace_result(response, payload)
        return response

    monkeypatch.setattr(WorkerClient, "post", post)
    return hits


FAULT_CASES = [
    pytest.param(
        kind, tool, shape, id=f"non_dict_worker-{kind}-{tool.removeprefix('mempalace_')}-{shape}"
    )
    for kind in ("A", "F")
    for tool in TOOLS[:2]
    for shape in ("non_dict", "missing_map")
]


@pytest.mark.parametrize("kind,tool,shape", FAULT_CASES)
def test_card253_g08_backend_paths(world, monkeypatch, kind, tool, shape):
    w = world
    hidden = seed(w)
    payload = (
        ["card253-acquisition-fault"]
        if shape == "non_dict"
        else {"card253_acquisition_fault": True}
    )
    hits = payload_fault(monkeypatch, payload)
    reply = result(w, tool, kind=kind)
    assert hits
    control(w, hidden)
    expected = result(w, tool, kind=kind)
    print("acquisition fault reached in both worlds:", hits)
    assert len(hits) >= 2
    empty = {"wings" if tool == TOOLS[0] else "taxonomy": {}}
    assert reply == empty, "g08 acquisition failure escaped normalization"
    assert reply == expected


@pytest.mark.parametrize(
    "mode", ["projection_deadline", "acquisition_falsy", "acquisition_keyless"]
)
def test_card253_g14_search_and_consumers(world, monkeypatch, mode):
    w = world
    hidden = seed(w, hidden_wings=("wing_hidden",))
    calls = fanout_spy(monkeypatch)
    search_calls = []
    original_call = WorkerClient.call_tool

    def call(self, principal, name, args):
        if name == "mempalace_search":
            search_calls.append(name)
        return original_call(self, principal, name, args)

    monkeypatch.setattr(WorkerClient, "call_tool", call)
    hits = []
    if mode == "projection_deadline":
        original_post = WorkerClient._post

        def post(self, worker, principal, request):
            if request.get("params", {}).get("name") not in TOOLS[:2]:
                return original_post(self, worker, principal, request)
            prior = checks.remaining
            fired = False

            def remaining(timeout):
                nonlocal fired
                if not fired:
                    fired = True
                    hits.append(request["params"]["name"])
                    raise TimeoutError("rules deadline expired")
                return prior(timeout)

            checks.remaining = remaining
            try:
                return original_post(self, worker, principal, request)
            finally:
                checks.remaining = prior

        monkeypatch.setattr(WorkerClient, "_post", post)
    else:
        hits = payload_fault(
            monkeypatch, [] if mode == "acquisition_falsy" else {"card253_acquisition_fault": True}
        )
    reply = rpc(w, "mempalace_search", search_args(), "F")
    assert len(calls) == 1 and len(hits) == 1
    control(w, hidden)
    expected = rpc(w, "mempalace_search", search_args(), "F")
    assert len(calls) == 2 and len(hits) == 2
    assert not search_calls
    print("fan-out acquisition branch/fault reached in both worlds:", hits)
    empty = {"query": QUERY, "results": [], "wings_searched": [], "facts": []}
    assert _tool_result(reply) == empty, "g14 acquisition timeout escaped empty search"
    assert reply == expected


def test_card253_g01_readable_pairs(world):
    from commonplace import sessions

    w = world
    own = w.h.gateway.handoff.store_for(w.h.workspace).store(
        w.h.workspace,
        f"capture:{w.h.admin}:own",
        [ThreadMessage(0, "user", "message", "own words", "own words")],
        actor=w.h.admin,
        wing="private_chats",
    )
    w.col.add(
        ids=["own", "other"],
        documents=["own words", "shared words"],
        metadatas=[
            {"wing": "private_threads", "room": own.id},
            {"wing": "wing_fixture", "room": w.readable.id},
        ],
        embeddings=[[1.0] + [0.0] * 383] * 2,
    )
    owner = sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
    assert owner.policy.trust == "trusted" and owner.policy.has_scope("threads:read")
    assert w.h.gateway.handoff.recall.can_read(owner, own.id)
    assert w.h.gateway.handoff.recall.can_read(owner, w.readable.id)
    reply = w.h.gateway.call_memory_tool(owner, "mempalace_list_wings", {})
    assert reply == {"wings": {"private_threads": 1, "wing_fixture": 1}}
    assert w.h.gateway.call_memory_tool(owner, "mempalace_get_taxonomy", {}) == {
        "taxonomy": {"private_threads": {own.id: 1}, "wing_fixture": {w.readable.id: 1}}
    }


@pytest.mark.parametrize(
    "backend_name,path",
    [
        ("sqlite_exact", "sqlite"),
        ("sqlite_exact", "facet"),
        ("sqlite_exact", "no_facets"),
        ("chroma", "sqlite"),
        ("chroma", "no_facets"),
    ],
)
def test_card253_g05_unknown_bookkeeping(world, monkeypatch, tmp_path, backend_name, path):
    w = world
    backend = None
    if backend_name == "chroma":
        from mempalace.backends import PalaceRef
        from mempalace.backends.chroma import ChromaBackend

        backend = ChromaBackend()
        palace = str(tmp_path / "chroma")
        monkeypatch.setattr(w.mcp._config, "_palace_path_override", palace)
        w.col = backend.get_collection(
            palace=PalaceRef(id=palace, local_path=palace),
            collection_name=w.mcp._config.collection_name,
            create=True,
        )
        monkeypatch.setattr(w.mcp, "_get_collection", lambda **kwargs: w.col)
        monkeypatch.setattr(w.mcp, "_selected_backend_name", lambda: "chroma")
        monkeypatch.setattr(w.mcp, "_is_chroma_backend", lambda: True)
    try:
        metas = [
            {"source_file": "absent"},
            {"wing": None, "room": None, "source_file": "null"},
            {"wing": "?", "room": "?"},
            {"wing": "fixture", "room": "notes", "chunk_index": 0},
            {"wing": "wing_fixture", "room": "notes", "chunk_index": 1},
            {"wing": "private_threads", "room": "?"},
            {"wing": "private_threads", "room": "malformed-thread"},
            {"wing": "unknown", "room": "unknown", "is_sentinel": True},
            {"wing": "unknown", "room": "unknown", "ingest_mode": "registry"},
        ]
        if backend_name == "chroma":
            metas = [{k: v for k, v in meta.items() if v is not None} for meta in metas]
        w.col.add(
            ids=[f"unknown-{n}" for n in range(len(metas))],
            documents=["unchanged words"] * len(metas),
            metadatas=metas,
            embeddings=[[1.0] + [0.0] * 383] * len(metas),
        )
        w.mcp._invalidate_overview_caches()
        hits = force_path(w, monkeypatch, path)
        assert result(w, "mempalace_list_wings") == {
            "wings": {"unknown": 3, "fixture": 1, "wing_fixture": 1}
        }
        assert result(w, "mempalace_get_taxonomy") == {
            "taxonomy": {
                "unknown": {"unknown": 3},
                "fixture": {"notes": 1},
                "wing_fixture": {"notes": 1},
            }
        }
        assert hits
        assert w.col.get(ids=["unknown-3"], include=["documents"])["documents"] == [
            "unchanged words"
        ]
    finally:
        if backend is not None:
            backend.close()


@pytest.mark.parametrize("path", ["sqlite", "facet", "no_facets", "facet_raises"])
def test_card253_g08_taxonomy_paths(world, monkeypatch, path):
    w = world
    seed(w)
    hits = force_path(w, monkeypatch, path)
    assert result(w, "mempalace_get_taxonomy") == {"taxonomy": {"wing_fixture": {w.readable.id: 1}}}
    assert "sqlite" in hits
    if path in ("facet", "facet_raises"):
        assert "facet_call" in hits
    if path in ("no_facets", "facet_raises"):
        assert "tally" in hits


def test_card253_g08_no_palace_and_partial_taxonomy(world, monkeypatch, tmp_path):
    w = world
    seed(w)
    force_path(w, monkeypatch, "no_facets")
    hits = []

    def partial(meta, taxonomy=None, **kwargs):
        hits.append(True)
        taxonomy["wing_hidden"] = {w.hidden.id: 99}
        raise OSError("private storage details")

    monkeypatch.setattr(w.mcp, "_tally_wings_rooms", partial)
    assert result(w, "mempalace_get_taxonomy") == {"taxonomy": {}}
    assert hits
    monkeypatch.setattr(w.mcp._config, "_palace_path_override", str(tmp_path / "absent"))
    monkeypatch.setattr(w.mcp, "_get_collection", lambda **kwargs: None)
    legacy = w.mcp.tool_list_wings()
    assert "wings" not in legacy
    assert result(w, "mempalace_list_wings") == {"wings": {}}
    assert result(w, "mempalace_get_taxonomy") == {"taxonomy": {}}
    assert w.mcp.tool_list_wings() == legacy


def test_card253_g09_cache_isolation(world, monkeypatch):
    import copy

    from commonplace import sessions

    w = world
    seed(w)
    raw = w.mcp._sqlite_taxonomy()
    metadata = w.mcp._get_cached_metadata(w.col)
    original = copy.deepcopy((raw, metadata))
    a = result(w, "mempalace_list_wings")
    owner = sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
    b = w.h.gateway.call_memory_tool(owner, "mempalace_list_wings", {})
    assert b["wings"]["wing_fixture"] == 2
    assert a == result(w, "mempalace_list_wings") == {"wings": {"wing_fixture": 1}}
    assert (raw, metadata) == original
    # Warm raw caches cannot preserve a readable association after a move.
    w.col.update(ids=["readable-0"], metadatas=[{"wing": "wing_fixture", "room": w.hidden.id}])
    assert result(w, "mempalace_list_wings") == {"wings": {}}
    w.col.update(ids=["readable-0"], metadatas=[{"wing": "wing_fixture", "room": w.readable.id}])
    assert result(w, "mempalace_list_wings") == a
    store = w.h.gateway.handoff.store_for(w.h.workspace)
    with store._lock:
        store._conn.execute(
            "UPDATE threads SET wing='private_chats', source_thread_id=? WHERE id=?",
            (f"capture:{w.h.admin}:revoked", w.readable.id),
        )
    assert result(w, "mempalace_list_wings") == {"wings": {}}


def test_card253_g10_internal_scope_unforgeable(world):
    w = world
    seed(w)
    listing = w.h.rpc(w.tokens["A"], "tools/list")["result"]["tools"]
    for tool in listing:
        assert "thread_scope" not in tool.get("inputSchema", {}).get("properties", {})
        assert "probe" not in tool.get("inputSchema", {}).get("properties", {})
    expected = result(w, "mempalace_list_wings")
    assert result(w, "mempalace_list_wings", {"thread_scope": {"probe": True}}) == expected
    assert result(w, "mempalace_list_wings", {"thread_scope": {"allow": []}}) == expected
    assert w.mcp.tool_list_wings()["wings"]["wing_fixture"] == 2


def test_card253_g14_consumers_and_selection(world):
    from commonplace import sessions
    from commonplace.web.agents import _read_wings

    w = world
    seed(w, hidden_wings=("wing_hidden",))
    member = w.h.db.authenticate_token(w.tokens["A"])
    assert w.h.gateway.call_memory_tool(member, "mempalace_list_wings", {}) == {
        "wings": {"wing_fixture": 1}
    }
    # Use an authenticated owner whose hidden fixture belongs to another person.
    owner = sessions.person_principal(w.h.db, member.user_id, w.h.workspace)
    assert _read_wings(w.h.gateway, owner, {"read_wings": ["wing_fixture"]}) == ["fixture"]
    with pytest.raises(ValueError):
        _read_wings(w.h.gateway, owner, {"read_wings": ["wing_hidden"]})
    for args in ({"wings": ["wing_hidden"]}, {"wings": ["fixture"]}, {"cli_compatible": True}):
        reply = result(w, "mempalace_search", {**search_args(), **args}, "F")
        assert not reply.get("error")
        assert reply["wings_searched"] == (
            [] if args.get("wings") == ["wing_hidden"] else ["wing_fixture"]
        )


@pytest.mark.parametrize("tool", TOOLS)
def test_r12_readable_native_room_cold_store(world, monkeypatch, tool):
    """A first request after restart must retain the same readable room."""
    w = world
    seed(w, hidden_wings=("wing_hidden",))
    calls = fanout_spy(monkeypatch) if tool == "mempalace_search" else None
    ctx = w.h.gateway.handoff
    # Close the setup connection: the next request must open the persisted
    # workspace just as a gateway with indexing disabled does after startup.
    ctx.close()
    assert ctx._stores == {}
    args = search_args() if tool == "mempalace_search" else {}
    cold = result(w, tool, args, "F")
    ctx.store_for(w.h.workspace)
    warm = result(w, tool, args, "F")
    expected = {
        "mempalace_list_wings": {"wings": {"wing_fixture": 1}},
        "mempalace_get_taxonomy": {"taxonomy": {"wing_fixture": {w.readable.id: 1}}},
    }
    if calls is not None:
        assert len(calls) == 2
        assert warm["wings_searched"] == ["wing_fixture"]
        assert cold["wings_searched"] == ["wing_fixture"], "readable room missing from cold fan-out"
    else:
        assert warm == expected[tool]
        assert cold == expected[tool], "readable room missing from cold overview"
    assert cold == warm


@pytest.mark.parametrize("tool", TOOLS)
def test_r12_readable_import_room_presence(world, monkeypatch, tool):
    """Explicit import consent keeps a readable grant room in the overview."""
    w = world
    ctx = w.h.gateway.handoff
    store = ctx.store_for(w.h.workspace)
    text = "verbatim imported archive\n"
    thread = store.store(
        w.h.workspace,
        f"import-private:{w.h.admin}:claude-code:readable-archive",
        [ThreadMessage(0, "system", "other", "", text, name="import.raw")],
        actor=w.h.admin,
        wing="private_chats",
    )
    w.tokens["F"] = w.h.token(
        "consenting-import-reader",
        tools=TOOLS,
        scopes=["memory:read", "private:read", "memory:import:read"],
        read_wings=["private_*", "wing_*"],
        trust="trusted",
    )
    principal = w.h.db.authenticate_token(w.tokens["F"])
    assert not principal.person and not principal.policy.sees_all_wings
    assert ctx.recall.import_reader(principal)
    assert ctx.recall.can_read(principal, thread.id)
    assert not ctx.recall.ordinary_record(principal, thread.id)
    view = ctx.recall._import_view(principal, thread)
    assert view.proves(thread.id, 0, 0)
    from mempalace.stored_text import KEYWORD_ONLY

    w.col._inner.add(
        ids=["consented-import-drawer"],
        metadatas=[{"wing": "wing_import", "room": thread.id, KEYWORD_ONLY: True}],
        documents=[text],
    )
    calls = []
    original = Mediator._fan_out_search

    def fanout(self, principal, name, args):
        calls.append(dict(args))
        assert not principal.policy.sees_all_wings
        assert principal.policy.has_scope("memory:import:read")
        assert "wing" not in args and "wings" not in args
        return original(self, principal, name, args)

    monkeypatch.setattr(Mediator, "_fan_out_search", fanout)
    reply = result(w, tool, search_args() if tool == "mempalace_search" else {}, "F")
    if tool == "mempalace_search":
        assert len(calls) == 1
        assert reply["wings_searched"] == ["wing_import"], (
            "readable import room missing from fan-out"
        )
    elif tool == "mempalace_list_wings":
        assert reply == {"wings": {"wing_import": 1}}, "readable import room missing from wings"
    else:
        assert reply == {"taxonomy": {"wing_import": {thread.id: 1}}}, (
            "readable import room missing from taxonomy"
        )


@pytest.mark.parametrize("tool", TOOLS)
def test_r12_ordinary_thr_prefix_room_stays_visible(world, tool):
    """An ordinary room name must use the existing thread-id grammar."""
    from mempalace.stored_text import KEYWORD_ONLY

    w = world
    w.col._inner.add(
        ids=["ordinary-notes"],
        metadatas=[{"wing": "wing_notes", "room": "thr_notes", KEYWORD_ONLY: True}],
        documents=["ordinary notes"],
    )
    reply = result(w, tool, search_args() if tool == "mempalace_search" else {}, "F")
    if tool == "mempalace_search":
        assert reply["wings_searched"] == ["wing_notes"]
    elif tool == "mempalace_list_wings":
        assert reply == {"wings": {"wing_notes": 1}}
    else:
        assert reply == {"taxonomy": {"wing_notes": {"thr_notes": 1}}}


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
