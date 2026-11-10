"""Public overview and search presence boundaries using native stored threads."""

import json
from types import SimpleNamespace

import pytest

from commonplace.gateway import (
    _tool_result,
)
from commonplace.handoff.models import ThreadMessage
from tests._commonplace_helpers import (  # noqa: F401
    assert_member_drawer_access,
    functional_worker_transport,
    hub_fixture,
    shared_readers,
)

TOOLS = ["mempalace_status", "mempalace_list_rooms", "mempalace_get_taxonomy"]
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
        assert principal.role == "owner" and principal.policy.has_scope("threads:read")
        assert principal.policy.sees_all_wings is (kind == "A")
        assert hub.gateway.handoff.recall.can_read(principal, readable.id)
        assert not hub.gateway.handoff.recall.can_read(principal, hidden.id)
        assert store.get(hidden.id) is not None
        for tool in TOOLS:
            assert principal.policy.refusal(tool) is None
    diagnostic = mcp._sqlite_integrity_payload

    def fixed_diagnostic():
        return {**diagnostic(), "palace": "synthetic-palace", "sqlite_path": ""}

    monkeypatch.setattr(mcp, "_sqlite_integrity_payload", fixed_diagnostic)
    mcp._invalidate_overview_caches()
    backends = [backend]
    yield SimpleNamespace(
        h=hub,
        col=collection,
        mcp=mcp,
        readable=readable,
        hidden=hidden,
        tokens=tokens,
        patch=monkeypatch,
        backends=backends,
    )
    hub.gateway.handoff.close()
    hub.gateway.webhooks.stop()
    for item in backends:
        item.close()


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
    import shutil
    from pathlib import Path

    from mempalace.backends import PalaceRef
    from mempalace.backends.embedding_wrapper import EmbeddingCollection
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    old = Path(w.mcp._config.palace_path)
    path = old.with_name(old.name + "-control")
    shutil.copytree(old, path)
    backend = SQLiteExactBackend()
    col = EmbeddingCollection(
        backend.get_collection(
            palace=PalaceRef(id=str(path), local_path=str(path)),
            collection_name=w.mcp._config.collection_name,
            create=False,
        )
    )
    col.delete(ids=hidden_ids)
    w.backends.append(backend)
    w.col = col
    w.patch.setattr(w.mcp._config, "_palace_path_override", str(path))
    w.patch.setattr(w.mcp, "_get_collection", lambda **kwargs: col)
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


def divergence(w, monkeypatch, hits):
    from pathlib import Path

    from mempalace.backends import chroma_sqlite

    original = w.mcp._sqlite_taxonomy

    def counts(*args):
        hits.append("divergence")
        return original()

    monkeypatch.setattr(chroma_sqlite, "_sqlite_wing_room_counts", counts)
    Path(w.mcp._config.palace_path, "chroma.sqlite3").touch()
    monkeypatch.setattr(w.mcp, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(w.mcp, "_vector_disabled", True)
    monkeypatch.setattr(w.mcp, "_vector_disabled_reason", "synthetic divergence")
    monkeypatch.setattr(
        w.mcp, "_vector_capacity_status", {"sqlite_count": 4, "hnsw_count": 2, "divergence": 2}
    )


@pytest.mark.parametrize("path", ["sqlite", "facet", "fallback", "divergence"])
def test_card253_s02_status(world, monkeypatch, path):
    w = world
    hidden = seed(w)
    hits = force_path(
        w,
        monkeypatch,
        "sqlite" if path == "divergence" else "no_facets" if path == "fallback" else path,
    )
    if path == "divergence":
        divergence(w, monkeypatch, hits)
        local = w.mcp.tool_status()
        assert "divergence" in hits
        assert local["vector_disabled"] is True
        assert local["vector_disabled_reason"] == "synthetic divergence"
    reply = result(w, "mempalace_status")
    assert "sqlite" in hits
    if path == "facet":
        assert "facet_call" in hits
    if path == "fallback":
        assert "tally" in hits
    control(w, hidden)
    expected = result(w, "mempalace_status")
    assert reply["total_drawers"] == 1, "s02 status counted H"
    assert reply == expected


@pytest.mark.parametrize("path", ["sqlite", "facet", "fallback"])
def test_card253_s02_rooms(world, monkeypatch, path):
    w = world
    seed(w, hidden_wings=())
    w.col._inner.add(
        ids=["legacy"],
        documents=["legacy verbatim"],
        metadatas=[{"wing": "private_threads", "room": "legacy_missing_thread"}],
        embeddings=[[1.0] + [0.0] * 383],
    )
    assert (
        w.col.get(ids=["legacy"], include=["metadatas"])["metadatas"][0]["room"]
        == "legacy_missing_thread"
    )
    hits = force_path(w, monkeypatch, "no_facets" if path == "fallback" else path)
    reply = result(w, "mempalace_list_rooms")
    assert "sqlite" in hits
    if path == "facet":
        assert "facet_call" in hits
    if path == "fallback":
        assert "tally" in hits
    assert isinstance(reply.get("rooms"), dict), reply
    assert "legacy_missing_thread" not in reply["rooms"], "s02 mirror room lacks proof"
    control(w, ["legacy"])
    assert reply == result(w, "mempalace_list_rooms")


def test_card253_s02_capacity(world, monkeypatch):
    w = world
    seed(w)
    hits = []
    divergence(w, monkeypatch, hits)
    reply = result(w, "mempalace_status")
    assert "hnsw_capacity" not in reply, "s02 global capacity exposed"


@pytest.mark.parametrize("tool", ["mempalace_list_rooms", "mempalace_get_taxonomy"])
def test_card253_s02_guard(world, tool):
    w = world
    hidden = seed(w)
    reply = result(w, tool)
    assert w.hidden.id not in json.dumps(reply)
    control(w, hidden)
    assert reply == result(w, tool)
    assert w.readable.id in json.dumps(reply)


@pytest.mark.parametrize("tool", ["mempalace_list_rooms", "mempalace_status"])
def test_card253_s02_owner_guard(world, tool):
    from commonplace import sessions

    w = world
    seed(w, hidden_wings=())
    owner = sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
    assert owner.person and owner.role == "owner"
    assert owner.policy.trust == "trusted" and owner.policy.has_scope("threads:read")
    assert owner.policy.refusal(tool) is None
    assert w.h.gateway.handoff.recall.can_read(owner, w.readable.id)
    reply = w.h.gateway.call_memory_tool(owner, tool, {})
    assert reply["rooms"] == {w.readable.id: 1}
    if tool == "mempalace_status":
        assert reply["total_drawers"] == 1


def test_card253_s02_alias_unknown_local_guard(world):
    w = world
    w.col._inner.add(
        ids=["ordinary", "alias", "unknown"],
        documents=["one", "two", "three"],
        metadatas=[
            {"wing": "project", "room": "notes"},
            {"wing": "wing_project", "room": "notes"},
            {"kind": "drawer"},
        ],
        embeddings=[[1.0] + [0.0] * 383] * 3,
    )
    reply = result(w, "mempalace_list_rooms", {"wing": "project"})
    assert reply["rooms"] == {"notes": 2}
    reply = result(w, "mempalace_status")
    assert reply["rooms"]["unknown"] == 1
    assert reply["total_drawers"] == 3
    assert w.mcp.tool_status()["total_drawers"] == 3


@pytest.mark.parametrize("tool", ["mempalace_list_rooms", "mempalace_status"])
@pytest.mark.parametrize(
    "mode", ["incomplete", "changed", "empty", "partial_tally", "facet_raises"]
)
def test_card253_s02_protocol(world, monkeypatch, tool, mode):
    from commonplace.gateway import WorkerClient

    w = world
    seed(w)
    hits = []
    original = WorkerClient.call_tool
    if mode == "partial_tally":
        force_path(w, monkeypatch, "no_facets")

        def broken(*args, **kwargs):
            hits.append("tally")
            raise RuntimeError("private failure details")

        monkeypatch.setattr(w.mcp, "_tally_wings_rooms", broken)
    if mode == "facet_raises":
        hits = force_path(w, monkeypatch, "facet_raises")

    def call(self, principal, name, args):
        scope = args.get("thread_scope", {})
        if mode == "empty" and scope.get("probe"):
            w.col.update(
                ids=["readable-0"], metadatas=[{"wing": "wing_fixture", "room": w.hidden.id}]
            )
        if mode == "changed" and "allow" in scope and scope["allow"]:
            w.col.update(
                ids=["readable-0"], metadatas=[{"wing": "wing_fixture", "room": w.hidden.id}]
            )
            hits.append("changed")
        reply = original(self, principal, name, args)
        if mode == "incomplete" and scope.get("probe"):
            hits.append("probe")
            raise RuntimeError("private failure details")
        return reply

    monkeypatch.setattr(WorkerClient, "call_tool", call)
    reply = result(w, tool)
    if mode == "facet_raises":
        assert reply["rooms"] == {w.readable.id: 1}
        assert "facet_call" in hits and "tally" in hits
    else:
        assert reply["rooms"] == {}
        if tool == "mempalace_status":
            assert reply["total_drawers"] == 0 and reply["wings"] == {}
        assert "private failure details" not in json.dumps(reply)


@pytest.mark.parametrize("tool", ["mempalace_list_rooms", "mempalace_status"])
def test_card253_s02_protocol_cache_revocation(world, tool):
    import copy

    from commonplace import sessions

    w = world
    seed(w)
    raw = w.mcp._sqlite_taxonomy()
    metadata = w.mcp._get_cached_metadata(w.col)
    saved = copy.deepcopy((raw, metadata))
    a = result(w, tool)
    owner = sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
    b = w.h.gateway.call_memory_tool(owner, tool, {})
    assert b["rooms"][w.hidden.id] == 3
    assert result(w, tool) == a
    assert (raw, metadata) == saved
    store = w.h.gateway.handoff.store_for(w.h.workspace)
    with store._lock:
        store._conn.execute(
            "UPDATE threads SET wing='private_chats', source_thread_id=? WHERE id=?",
            (f"capture:{w.h.admin}:revoked", w.readable.id),
        )
    assert result(w, tool)["rooms"] == {}


@pytest.mark.parametrize("tool", ["mempalace_list_rooms", "mempalace_status"])
def test_card253_s02_protocol_complete(world, tool):
    w = world
    seed(w, visible=501)
    reply = result(w, tool)
    assert reply["rooms"] == {w.readable.id: 501}
    if tool == "mempalace_status":
        assert reply["total_drawers"] == 501 and len(reply["wings"]) == 501


@pytest.mark.parametrize("mode", ["projection_deadline"])
@pytest.mark.parametrize("tool", ["mempalace_list_rooms", "mempalace_status"])
def test_card253_s02_protocol_deadline(world, monkeypatch, mode, tool):
    from commonplace.gateway import WorkerClient

    w = world
    seed(w)
    original = WorkerClient.call_tool
    hits = []

    def call(self, principal, name, args):
        reply = original(self, principal, name, args)
        if args.get("thread_scope", {}).get("probe"):
            hits.append(True)
            raise TimeoutError("private timed out acquisition")
        return reply

    monkeypatch.setattr(WorkerClient, "call_tool", call)
    reply = result(w, tool)
    assert hits
    assert reply["rooms"] == {}
    if tool == "mempalace_status":
        assert reply["total_drawers"] == 0
    assert "private timed out acquisition" not in json.dumps(reply)


@pytest.mark.parametrize("tool", ["mempalace_list_rooms", "mempalace_status"])
def test_card253_s02_protocol_late_final_fault(world, monkeypatch, tool):
    from mempalace import read_scope

    w = world
    seed(w, visible=501)
    original = read_scope.metadata_page
    finals = []

    def page(*args, **kwargs):
        if kwargs.get("ids") is not None:
            finals.append(len(kwargs["ids"]))
            if len(finals) == 2:
                raise RuntimeError("private final batch failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(read_scope, "metadata_page", page)
    reply = result(w, tool)
    assert finals == [500, 1]
    assert reply["rooms"] == {}
    if tool == "mempalace_status":
        assert reply["total_drawers"] == 0 and reply["wings"] == {}
    assert "incomplete" not in reply


def test_card253_s02_protocol_integrity(world, monkeypatch):
    w = world
    seed(w)
    hits = []
    divergence(w, monkeypatch, hits)
    monkeypatch.setattr(w.mcp, "_ensure_sqlite_integrity_status", lambda: None)
    monkeypatch.setattr(w.mcp, "_sqlite_integrity_errors", ["synthetic integrity fault"])
    reply = result(w, "mempalace_status")
    assert reply["total_drawers"] == 1
    assert reply["rooms"] == {w.readable.id: 1}
    assert reply["sqlite_integrity_failed"] is True and reply["partial"] is True
    assert reply["error"] == "SQLite integrity check failed"
    assert "hnsw_capacity" not in reply


@pytest.mark.parametrize("mode", ["raises", "malformed"])
def test_card253_s02_protocol_diagnostic_failure(world, monkeypatch, mode):
    from commonplace.gateway import WorkerClient

    w = world
    seed(w)
    original = WorkerClient.call_tool
    hits = []

    def call(self, principal, name, args):
        reply = original(self, principal, name, args)
        if name == "mempalace_status":
            hits.append(True)
            if mode == "raises":
                raise RuntimeError("private diagnostic failure")
            return {"raw_failure": "private diagnostic failure"}
        return reply

    monkeypatch.setattr(WorkerClient, "call_tool", call)
    assert result(w, "mempalace_status") == {"total_drawers": 0, "wings": {}, "rooms": {}}
    assert hits


@pytest.mark.parametrize("phase", ["probe", "final"])
@pytest.mark.parametrize("extra", ["hnsw_capacity", "raw_failure"])
def test_card253_s02_protocol_decoration_validation(world, monkeypatch, phase, extra):
    from commonplace.gateway import WorkerClient

    w = world
    seed(w)
    original = WorkerClient.call_tool
    hits = []

    def call(self, principal, name, args):
        reply = original(self, principal, name, args)
        scope = args.get("thread_scope", {})
        target = scope.get("probe") if phase == "probe" else scope.get("allow")
        if name == "mempalace_status" and target:
            hits.append(True)
            return {**reply, extra: "private data"}
        return reply

    monkeypatch.setattr(WorkerClient, "call_tool", call)
    reply = result(w, "mempalace_status")
    assert hits
    assert reply["total_drawers"] == 0 and reply["wings"] == {} and reply["rooms"] == {}
    assert extra not in reply and "private data" not in json.dumps(reply)


@pytest.mark.parametrize("mode", ["normal", "incomplete"])
def test_card253_s02_r1_vector_reason(world, monkeypatch, mode):
    from mempalace import read_scope

    w = world
    hidden = seed(w)
    probes = []
    monkeypatch.setattr(w.mcp, "_is_chroma_backend", lambda: True)
    monkeypatch.setattr(w.mcp, "_vector_disabled", False)
    monkeypatch.setattr(w.mcp, "_vector_disabled_reason", "")
    monkeypatch.setattr(w.mcp, "_vector_capacity_status", None)

    def capacity(palace, collection):
        count = w.col.count()
        message = f"HNSW index holds 0 elements but sqlite has {count} embeddings"
        probes.append((palace, count, message))
        return {"diverged": True, "message": message, "sqlite_count": count, "hnsw_count": 0}

    monkeypatch.setattr(w.mcp, "hnsw_capacity_status", capacity)
    acquired = []
    if mode == "incomplete":
        original = read_scope.metadata_page

        def page(*args, **kwargs):
            rows = original(*args, **kwargs)
            acquired.append(bool(rows[0]))
            raise RuntimeError("incomplete metadata acquisition")

        monkeypatch.setattr(read_scope, "metadata_page", page)
    reply = result(w, "mempalace_status")
    assert probes and probes[-1][1] == 4
    assert w.mcp._vector_disabled_reason == probes[-1][2]
    control(w, hidden)
    expected = result(w, "mempalace_status")
    assert probes[-1][1] == 1
    assert reply["vector_disabled"] is expected["vector_disabled"] is True
    assert reply == expected, "authenticated vector reason depends on hidden rows"
    assert "embeddings" not in reply["vector_disabled_reason"], "raw capacity probe text exposed"
    assert reply["total_drawers"] == (0 if mode == "incomplete" else 1)
    if mode == "incomplete":
        assert acquired == [True, True]


@pytest.mark.parametrize("mode", ["normal", "integrity"])
def test_card253_s02_r1_diagnostics(world, monkeypatch, mode):
    w = world
    seed(w)
    private_palace = "synthetic-private-palace-value"
    private_sqlite = "synthetic-private-sqlite-value"
    original = w.mcp._sqlite_integrity_payload

    def diagnostic():
        return {**original(), "palace": private_palace, "sqlite_path": private_sqlite}

    monkeypatch.setattr(w.mcp, "_sqlite_integrity_payload", diagnostic)
    if mode == "integrity":
        monkeypatch.setattr(w.mcp, "_ensure_sqlite_integrity_status", lambda: None)
        monkeypatch.setattr(w.mcp, "_sqlite_integrity_errors", ["synthetic integrity fault"])
    reply = result(w, "mempalace_status")
    assert reply["total_drawers"] == 1
    assert not {"sqlite_integrity", "library_versions", "updates"}.intersection(reply), (
        "authenticated transport diagnostics exposed"
    )
    assert private_palace not in json.dumps(reply) and private_sqlite not in json.dumps(reply)
    assert '"palace"' not in json.dumps(reply) and '"sqlite_path"' not in json.dumps(reply)
    if mode == "integrity":
        assert reply["sqlite_integrity_failed"] is True
        assert reply["error"] == "SQLite integrity check failed" and reply["partial"] is True


@pytest.mark.parametrize("wing", [5, True, [], {}])
def test_card253_s02_r1_invalid_wing(world, wing):
    from commonplace.handoff.overview import summary

    w = world
    reply = result(w, "mempalace_list_rooms", {"wing": wing})
    assert reply == {"error": "'wing' must be a string", "denied": True}
    principal = w.h.db.authenticate_token(w.tokens["A"])
    assert summary(
        w.h.gateway, principal, w.h.gateway.mediator.workers, "mempalace_list_rooms", wing
    ) == {"error": "wing must be a non-empty string"}
    assert result(w, "mempalace_status")["total_drawers"] == 0


def test_card253_s02_r1_no_palace(world, monkeypatch, tmp_path):
    w = world
    monkeypatch.setattr(w.mcp._config, "_palace_path_override", str(tmp_path / "absent"))
    monkeypatch.setattr(w.mcp, "_get_collection", lambda **kwargs: None)
    monkeypatch.setattr(w.mcp, "_collection_open_error", None)
    w.mcp._invalidate_overview_caches()
    local = w.mcp.tool_status()
    assert local == w.mcp._no_palace()
    reply = result(w, "mempalace_status")
    assert reply["total_drawers"] == 0 and reply["wings"] == {} and reply["rooms"] == {}
    assert not {"sqlite_integrity", "library_versions", "updates", "hnsw_capacity"}.intersection(
        reply
    )


def test_card253_s02_r1_scope_guard(world):
    w = world
    listing = w.h.rpc(w.tokens["A"], "tools/list")["result"]["tools"]
    status = next(tool for tool in listing if tool["name"] == "mempalace_status")
    assert "thread_scope" not in status["inputSchema"]["properties"]
    with pytest.raises(ValueError, match="thread_scope must be an object"):
        w.mcp.tool_status(thread_scope=5)
    # Keep the functional transport guard meaningful, beyond local validation.
    assert result(w, "mempalace_status")["total_drawers"] == 0


@pytest.mark.parametrize(
    "key",
    [
        "source_thread",
        "source_url",
        "source_file",
        "source_drawer_id",
        "source_closet",
        "origin_id",
        "origin_link",
        "approval_link",
        "source_link",
    ],
)
@pytest.mark.parametrize(
    "tool,locus",
    [("mempalace_list_rooms", "room"), ("mempalace_status", "room"), ("mempalace_status", "wing")],
)
def test_card253_s02_r2_named_counts(world, key, tool, locus):
    w = world
    seed(w, hidden_wings=())
    metadata = {"wing": "wing_fixture", "room": "ordinary"}
    metadata[locus] = key
    w.col._inner.add(
        ids=["named-count"],
        documents=["visible verbatim words"],
        metadatas=[metadata],
        embeddings=[[1.0] + [0.0] * 383],
    )
    assert w.col.get(ids=["named-count"], include=["metadatas"])["metadatas"] == [metadata]
    reply = result(w, tool)
    counts = reply["rooms" if locus == "room" else "wings"]
    assert counts.get(key) == 1, "visible provenance-named count omitted"
    if tool == "mempalace_status":
        assert sum(reply["rooms"].values()) == reply["total_drawers"] == 2
        assert sum(reply["wings"].values()) == reply["total_drawers"]


def test_card253_s02_r2_room_sum(world):
    w = world
    seed(w, hidden_wings=())
    w.col._inner.add(
        ids=["named-count"],
        documents=["visible verbatim words"],
        metadatas=[{"wing": "wing_fixture", "room": "source_file"}],
        embeddings=[[1.0] + [0.0] * 383],
    )
    reply = result(w, "mempalace_status")
    assert sum(reply["rooms"].values()) == reply["total_drawers"] == 2, (
        "visible room sum differs from total"
    )


def test_card253_s02_r2_gate_boundary(world, monkeypatch):
    import copy

    from commonplace.handoff import mirror_access
    from mempalace import palace_graph as graph

    w = world
    seed(w)
    member = w.h.db.authenticate_token(w.tokens["A"])
    w.tokens["guard"] = w.h.token(
        "boundary-reader",
        user=member.user_id,
        tools=TOOLS + ["mempalace_list_tunnels", "mempalace_get_drawer"],
        scopes=["memory:read", "private:read", "threads:read"],
        read_wings=["*"],
        trust="trusted",
    )
    principal = w.h.db.authenticate_token(w.tokens["guard"])
    assert principal.policy.sees_all_wings
    assert w.h.gateway.handoff.recall.can_read(principal, w.readable.id)
    assert not w.h.gateway.handoff.recall.can_read(principal, w.hidden.id)
    for tool in TOOLS + ["mempalace_list_tunnels", "mempalace_get_drawer"]:
        assert principal.policy.refusal(tool) is None
    monkeypatch.setattr(graph, "MempalaceConfig", lambda: w.mcp._config)
    tunnel = {
        "id": "visible-boundary",
        "source": {"wing": "wing_fixture", "room": w.readable.id},
        "target": {"wing": "wing_other", "room": "ordinary"},
    }
    graph._save_tunnels([tunnel], w.mcp._config)
    assert graph._load_tunnels(w.mcp._config) == [tunnel]
    w.col._inner.update(ids=["readable-0"], metadatas=[{"source_file": 42}])
    assert w.col.get(ids=["readable-0"], include=["metadatas"])["metadatas"][0]["source_file"] == 42
    real_gate = mirror_access.gate
    calls, phase = [], []

    def gate(gateway, caller, rule, payload):
        calls.append((phase[-1], copy.deepcopy(payload)))
        return real_gate(gateway, caller, rule, payload)

    monkeypatch.setattr(mirror_access, "gate", gate)
    for tool in ("mempalace_list_rooms", "mempalace_status"):
        phase[:] = [tool]
        reply = result(w, tool, kind="guard")
        assert reply["rooms"] == {w.readable.id: 1}
        if tool == "mempalace_status":
            assert reply["wings"] == {"wing_fixture": 1}
            assert reply["total_drawers"] == 1
    assert calls == [], "authorized count replies reached the generic gate"
    phase[:] = ["tunnel"]
    assert result(w, "mempalace_list_tunnels", kind="guard") == [tunnel]
    assert calls[-1] == ("tunnel", [tunnel])
    phase[:] = ["drawer"]
    drawer = result(w, "mempalace_get_drawer", {"drawer_id": "readable-0"}, kind="guard")
    assert calls[-1][0] == "drawer"
    assert calls[-1][1]["metadata"]["source_file"] == 42
    assert "source_file" not in drawer["metadata"]
    assert {name for name, _ in calls} == {"tunnel", "drawer"}


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
