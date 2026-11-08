"""KG visible counts, pages and aggregates."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace.handoff.models import ThreadMessage
from mempalace import mcp_server as _mcp
from tests._commonplace_helpers import functional_worker_transport, hub_fixture  # noqa: F401

_GET_KG = _mcp._get_kg

TOOLS = [
    "mempalace_kg_query",
    "mempalace_kg_timeline",
    "mempalace_kg_stats",
    "mempalace_kg_list_wings",
]


@pytest.fixture
def world(hub, monkeypatch, tmp_path, request):
    from mempalace import mcp_server as mcp

    # The shared hub replaces _get_kg with one graph. Exercise the production
    # palace-keyed cache so control worlds and absent-graph reads are real.
    monkeypatch.setattr(mcp, "_get_kg", _GET_KG)
    monkeypatch.setattr(mcp, "_kg_by_path", {})
    monkeypatch.setattr(mcp, "_palace_flag_given", True)
    backend_name = "sqlite_exact"
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
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
    store = hub.gateway.handoff.store_for(hub.workspace)
    readable = store.store(
        hub.workspace,
        "native-readable",
        [ThreadMessage(0, "user", "message", "readable", "readable")],
        actor=member,
    )
    other = hub.db.create_user("other@example.test")
    hub.db.add_member(hub.workspace, other, "member", actor=hub.admin)
    hidden = store.store(
        hub.workspace,
        f"capture:{other}:native-hidden",
        [ThreadMessage(0, "user", "message", "hidden", "hidden")],
        actor=other,
        wing="private_chats",
    )

    class Tokens(dict):
        def __repr__(self):
            return "<synthetic credentials>"

    tokens = Tokens()
    tokens["A"] = hub.token(
        "reader-A",
        user=member,
        tools=TOOLS,
        scopes=["memory:read", "private:read", "threads:read", "kg:read"],
        read_wings=["*"],
        trust="trusted",
    )
    principal = hub.db.authenticate_token(tokens["A"])
    assert principal.role != "owner" and principal.policy.has_scope("threads:read")
    assert principal.policy.trust == "trusted" and principal.policy.has_scope("kg:read")
    assert principal.policy.sees_all_wings
    assert hub.gateway.handoff.recall.can_read(principal, readable.id)
    assert not hub.gateway.handoff.recall.can_read(principal, hidden.id)
    assert store.get(hidden.id) is not None
    for tool in TOOLS:
        assert principal.policy.refusal(tool) is None
    mcp._invalidate_overview_caches()
    yield SimpleNamespace(
        h=hub,
        col=collection,
        mcp=mcp,
        readable=readable,
        hidden=hidden,
        tokens=tokens,
        patch=monkeypatch,
    )
    hub.gateway.handoff.close()
    hub.gateway.webhooks.stop()
    backend.close()
    for graph in mcp._kg_by_path.values():
        graph.close()


@pytest.fixture
def kgworld(world, request):
    request.getfixturevalue("functional_worker_transport")
    w = world
    w.kg = w.mcp._get_kg()
    w.kg.add_triple(
        "fixed_entity",
        "uses",
        "readable",
        wing="wing_fixture",
        source_thread=w.readable.id,
        valid_from="2020-01-01",
    )
    w.hidden_fact = w.kg.add_triple(
        "fixed_entity",
        "hidden_pred",
        "hidden_endpoint",
        wing="wing_fixture",
        source_thread=w.hidden.id,
        valid_from="2010-01-01",
    )
    w.hidden_wing = w.kg.add_triple(
        "hidden_subject",
        "hidden_pred",
        "hidden_endpoint",
        wing="wing_hidden",
        source_thread=w.hidden.id,
    )
    assert w.kg.sources([w.hidden_fact])[w.hidden_fact][0]["source_thread"] == w.hidden.id
    return w


def call(w, tool, **args):
    result, text = w.h.call(w.tokens["A"], tool, **args)
    assert result is not None, text
    return result


def remove_hidden(w):
    # Clone unchanged readable rows, ids and timestamps into a separate palace.
    control = Path(w.mcp._config.palace_path).with_name("control")
    control.mkdir(exist_ok=True)
    w.patch.setattr(w.mcp._config, "_palace_path_override", str(control))
    destination = w.mcp._resolve_kg_path()
    with sqlite3.connect(destination) as target:
        with w.kg._lock:
            w.kg._conn().backup(target)
    w.kg = w.mcp._get_kg()
    w.mcp._invalidate_overview_caches()
    with w.kg._lock:
        conn = w.kg._conn()
        conn.execute("DELETE FROM triples WHERE id IN (?, ?)", (w.hidden_fact, w.hidden_wing))
        conn.commit()


def test_card253_s07_query_count(kgworld):
    w = kgworld
    reply = call(w, TOOLS[0], entity="fixed_entity", wings=["wing_fixture"])
    assert reply["count"] == 1, "s07 query counted hidden fact"
    remove_hidden(w)
    assert reply == call(w, TOOLS[0], entity="fixed_entity", wings=["wing_fixture"])


@pytest.mark.parametrize("offset", [0, 1])
def test_card253_s07_timeline_page(kgworld, offset):
    w = kgworld
    reply = call(w, TOOLS[1], wings=["wing_fixture"], limit=1, offset=offset)
    remove_hidden(w)
    assert reply == call(w, TOOLS[1], wings=["wing_fixture"], limit=1, offset=offset), (
        "s07 H shifted timeline page"
    )


@pytest.mark.parametrize("operation", ["stats", "wings"])
def test_card253_s07_aggregate(kgworld, operation):
    w = kgworld
    tool = TOOLS[2 if operation == "stats" else 3]
    reply = call(w, tool)
    remove_hidden(w)
    assert reply == call(w, tool), "s07 hidden KG aggregate exposed"


@pytest.mark.parametrize("primary_hidden", [False, True])
def test_card253_s07_mixed_sources_guard(kgworld, primary_hidden):
    w = kgworld
    first, second = (w.hidden, w.readable) if primary_hidden else (w.readable, w.hidden)
    fact = w.kg.add_triple(
        "mixed", "uses", "verbatim words", wing="wing_fixture", source_thread=first.id
    )
    with w.kg._lock:
        w.kg._add_source(w.kg._conn(), fact, second.id, None, None, None, None, "2020-01-01")
        w.kg._conn().commit()
    reply = call(w, TOOLS[0], entity="mixed")
    assert reply["count"] == 1
    assert reply["facts"][0]["object"] == "verbatim words"
    assert all(s.get("source_thread") != w.hidden.id for s in reply["facts"][0]["sources"])
    assert w.hidden.id not in str(reply)


def test_card253_s07_readable_semantics_guard(kgworld):
    w = kgworld
    remove_hidden(w)
    w.kg.add_triple(
        "readable",
        "past",
        "fixed_entity",
        wing="wing_fixture",
        source_thread=w.readable.id,
        valid_from="2000-01-01",
        valid_to="2001-01-01",
    )
    w.kg.add_triple(
        "fixed_entity",
        "future",
        "later",
        wing="wing_fixture",
        source_thread=w.readable.id,
        valid_from="2099-01-01",
    )
    reply = call(w, TOOLS[0], entity="fixed_entity")
    assert [len(reply[k]) for k in ("active_facts", "historical_facts", "future_facts")] == [
        1,
        1,
        1,
    ]
    assert call(w, TOOLS[0], entity="fixed_entity", direction="incoming")["count"] == 1
    assert call(w, TOOLS[0], entity="fixed_entity", as_of="2000-06-01")["count"] == 1
    # This compatibility guard has no orphan rows; orphan exclusion is covered
    # by the aggregate regression witness.
    with w.kg._lock:
        w.kg._conn().execute(
            "DELETE FROM entities WHERE id NOT IN (SELECT subject FROM triples UNION SELECT object FROM triples)"
        )
        w.kg._conn().commit()
    stats = call(w, TOOLS[2])
    assert stats == {
        "entities": 3,
        "triples": 3,
        "current_facts": 2,
        "expired_facts": 1,
        "relationship_types": ["future", "past", "uses"],
        "wings": ["wing_fixture"],
    }


def test_card253_s07_candidates_guard(kgworld):
    w = kgworld
    reply = call(w, TOOLS[0], entity="hidden")
    assert reply["facts"] == [] and not reply.get("candidates")


def test_card253_s07_owner_guard(kgworld):
    from commonplace import sessions

    w = kgworld
    owner = sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
    assert owner.person and owner.role == "owner" and owner.policy.trust == "trusted"
    assert owner.policy.has_scope("threads:read")
    assert not w.h.gateway.handoff.recall.can_read(owner, w.hidden.id)
    assert w.h.gateway.handoff.recall.can_read(owner, w.readable.id)
    remove_hidden(w)
    reply = w.h.gateway.call_memory_tool(owner, TOOLS[0], {"entity": "fixed_entity"})
    assert reply["count"] == 1


@pytest.mark.parametrize("change", ["primary", "range", "wing", "sensitivity", "sources"])
def test_card253_s07_source_revalidation(kgworld, monkeypatch, change):
    from commonplace.gateway import WorkerClient

    w = kgworld
    original = WorkerClient.call_tool
    reached = []

    def call_tool(self, principal, name, args):
        if "allow" in args.get("thread_scope", {}):
            reached.append(True)
            with w.kg._lock:
                if change == "primary":
                    w.kg._conn().execute("UPDATE triples SET source_thread=?", (w.hidden.id,))
                elif change == "range":
                    w.kg._conn().execute("UPDATE fact_sources SET source_seq_end=42")
                elif change == "sources":
                    w.kg._conn().execute("DELETE FROM fact_sources")
                elif change == "wing":
                    w.kg._conn().execute("UPDATE triples SET wing='wing_moved'")
                else:
                    w.kg._conn().execute("UPDATE triples SET sensitivity='secret'")
                w.kg._conn().commit()
        return original(self, principal, name, args)

    monkeypatch.setattr(WorkerClient, "call_tool", call_tool)
    reply = call(w, TOOLS[0], entity="fixed_entity")
    assert reached and reply["count"] == 0


def test_card253_s07_no_graph_write(world, request, monkeypatch, tmp_path):
    request.getfixturevalue("functional_worker_transport")
    w = world
    palace = tmp_path / "absent-graph"
    monkeypatch.setattr(w.mcp._config, "_palace_path_override", str(palace))

    def forbidden(*args, **kwargs):
        raise AssertionError("KG read wrote intent log")

    monkeypatch.setattr(w.mcp, "_wal_log", forbidden)
    for tool, args in (
        (TOOLS[0], {"entity": "missing"}),
        (TOOLS[1], {}),
        (TOOLS[2], {}),
        (TOOLS[3], {}),
    ):
        reply = call(w, tool, **args)
        assert not reply.get("error")
    assert not palace.exists()


@pytest.mark.parametrize("fault", ["malformed", "timeout"])
def test_card253_s07_fail_closed(world, monkeypatch, fault):
    from commonplace.gateway import WorkerClient
    from commonplace.rules import checks

    w = world
    hits = []

    def broken(self, principal, name, args):
        hits.append(True)
        if fault == "timeout":
            raise TimeoutError("private details")
        return {"probe": {"facts": [], "next": "invalid"}}

    monkeypatch.setattr(WorkerClient, "call_tool", broken)
    with checks.deadline(1):
        assert call(w, TOOLS[1], limit=3, offset=2) == {
            "entity": "all",
            "timeline": [],
            "count": 0,
            "total": 0,
            "limit": 3,
            "offset": 2,
        }
    assert hits


def test_card253_s07_owner_isolation(kgworld):
    from commonplace import sessions

    w = kgworld
    owner = sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
    assert owner.person and owner.role == "owner" and owner.policy.trust == "trusted"
    assert owner.policy.has_scope("threads:read")
    assert w.h.gateway.handoff.recall.can_read(owner, w.readable.id)
    assert not w.h.gateway.handoff.recall.can_read(owner, w.hidden.id)
    reply = w.h.gateway.call_memory_tool(owner, TOOLS[0], {"entity": "fixed_entity"})
    assert reply["count"] == len(reply["facts"]) == 1
    assert w.hidden.id not in str(reply)


def test_card253_s07_complete_pages(kgworld, monkeypatch):
    from commonplace.gateway import WorkerClient

    w = kgworld
    for i in range(501):
        w.kg.add_triple(
            "bulk", "uses", f"entry {i}", wing="wing_fixture", source_thread=w.readable.id
        )
    original = WorkerClient.call_tool
    probes, finals = [], []

    def observed(self, principal, name, args):
        result = original(self, principal, name, args)
        if args.get("thread_scope", {}).get("probe"):
            probes.append(result["probe"])
        elif "allow" in args.get("thread_scope", {}):
            finals.append(result)
        return result

    monkeypatch.setattr(WorkerClient, "call_tool", observed)
    reply = call(w, TOOLS[2])
    assert len(probes) == 2 and all(len(p["facts"]) <= 500 for p in probes)
    assert sum(len(p["facts"]) for p in probes) == 504
    assert reply == finals[0]
    assert reply["triples"] == 502
    # Reusing the warm graph must still acquire current authority each request.
    principal = w.h.db.authenticate_token(w.tokens["A"])
    from commonplace.handoff.recall import Recall

    original_read = Recall.can_read

    def revoked(self, p, thread):
        return (
            False
            if p.connection_id == principal.connection_id and thread == w.readable.id
            else original_read(self, p, thread)
        )

    monkeypatch.setattr(Recall, "can_read", revoked)
    assert call(w, TOOLS[2])["triples"] == 0


def test_card253_s07_final_gate_keeps_pages(kgworld, monkeypatch):
    from commonplace.gateway import WorkerClient

    w = kgworld
    original = WorkerClient.call_tool
    finals = []

    def observed(self, principal, name, args):
        result = original(self, principal, name, args)
        if "allow" in args.get("thread_scope", {}):
            finals.append(result)
        return result

    monkeypatch.setattr(WorkerClient, "call_tool", observed)
    reply = call(w, TOOLS[1], limit=1)
    assert len(finals) == 1
    assert reply["count"] == reply["total"] == finals[0]["total"] == 1
    assert [r["id"] for r in reply["timeline"]] == [r["id"] for r in finals[0]["timeline"]]


@pytest.mark.parametrize("scope", [{"probe": True}, {"allow": []}])
def test_card253_s07_search_scope_unforgeable(kgworld, monkeypatch, scope):
    from datetime import datetime, timezone

    from commonplace.gateway import WorkerClient
    from mempalace.searcher import facts

    original_search = facts.search_facts
    now = datetime.now(timezone.utc)

    def frozen_search(*args, **kwargs):
        return original_search(*args, now=now, **kwargs)

    monkeypatch.setattr(facts, "search_facts", frozen_search)

    w = kgworld
    original = WorkerClient.post
    forwarded = []

    def observed(self, principal, request):
        params = request.get("params", {})
        if request.get("method") == "tools/call" and params.get("name") == TOOLS[0]:
            forwarded.append(dict(params["arguments"]))
        return original(self, principal, request)

    monkeypatch.setattr(WorkerClient, "post", observed)
    expected = call(w, TOOLS[0], entity="fixed_entity", search=True)
    reply = call(w, TOOLS[0], entity="fixed_entity", search=True, thread_scope=scope)
    # Each public call discards client scope and issues its own probe/final read.
    assert len(forwarded) == 4
    comparable = [
        {**args, "thread_scope": {k: v for k, v in args["thread_scope"].items() if k != "deadline"}}
        for args in forwarded
    ]
    assert comparable[0] == comparable[2] and comparable[1] == comparable[3]
    assert comparable[0]["thread_scope"] == {"probe": True, "after": None}
    allowed = forwarded[1]["thread_scope"]["allow"]
    assert allowed and all(w.hidden.id not in row["threads"] for row in allowed)
    assert reply == expected and "probe" not in reply
    assert w.hidden.id not in str(reply)


def test_card253_s07_scope_not_discovered(kgworld):
    w = kgworld
    listing = w.h.rpc(w.tokens["A"], "tools/list")["result"]["tools"]
    selected = [tool for tool in listing if tool["name"] in TOOLS]
    assert len(selected) == len(TOOLS)
    for tool in selected:
        for key in ("inputSchema", "input_schema"):
            assert "thread_scope" not in tool.get(key, {}).get("properties", {})


@pytest.mark.parametrize("args", [{"limit": "invalid"}, {"offset": None}, {"limit": True}])
def test_card253_s07_invalid_pagination(world, args):
    reply = world.h.rpc(world.tokens["A"], "tools/call", {"name": TOOLS[1], "arguments": args})
    if args.get("offset", 0) is None:
        from commonplace.gateway import _tool_result

        assert _tool_result(reply) == {
            "entity": "all",
            "timeline": [],
            "count": 0,
            "total": 0,
            "limit": 100,
            "offset": 0,
        }
    else:
        assert reply.get("error") == {
            "code": -32602,
            "message": f"Invalid params for tool {TOOLS[1]}: 'limit' must be an integer",
        }


@pytest.fixture
def fixed_search_clock(monkeypatch):
    from datetime import datetime, timezone

    from mempalace.searcher import facts

    original = facts.search_facts
    now = datetime.now(timezone.utc)

    def frozen(*args, **kwargs):
        return original(*args, now=now, **kwargs)

    monkeypatch.setattr(facts, "search_facts", frozen)


@pytest.mark.parametrize("flag", ["false", "no", "off", "0", " FALSE "])
@pytest.mark.parametrize("entity", ["fixed_entity", "hidden_subject", "Hidden"])
def test_card253_s07_r1_string_boolean(kgworld, flag, entity):
    w = kgworld
    named = w.kg.add_triple(
        "Hidden Partner", "uses", "hidden_endpoint", wing="wing_hidden", source_thread=w.hidden.id
    )
    assert call(w, TOOLS[0], entity="fixed_entity", search=False)["count"] == 1
    present = call(w, TOOLS[0], entity=entity, search=flag)
    remove_hidden(w)
    with w.kg._lock:
        w.kg._conn().execute("DELETE FROM triples WHERE id=?", (named,))
        w.kg._conn().commit()
    absent = call(w, TOOLS[0], entity=entity, search=flag)
    assert present == absent, "string boolean changed hidden counts or labels"
    assert present.get("denied") and present.get("error") == "'search' must be true or false"


@pytest.mark.parametrize("limit", [1, 2, 3])
@pytest.mark.parametrize("enrich", [False, True])
def test_card253_s07_r1_search_page(kgworld, fixed_search_clock, limit, enrich):
    w = kgworld
    # Fill even the enrichment route's fourfold overfetch with H-only facts.
    hidden = [
        w.kg.add_triple(
            "fixed_entity",
            f"hidden{i}",
            f"secret{i}",
            wing="wing_fixture",
            source_thread=w.hidden.id,
        )
        for i in range(13)
    ]
    with w.kg._lock:
        for fact in hidden:
            w.kg._conn().execute(
                "UPDATE triples SET approval_status='approved' WHERE id=?", (fact,)
            )
        w.kg._conn().commit()
    if enrich:
        token = w.h.token(
            "search-reader",
            user=w.h.db.authenticate_token(w.tokens["A"]).user_id,
            tools=[*TOOLS, "mempalace_search"],
            scopes=["memory:read", "private:read", "threads:read", "kg:read"],
            read_wings=["*"],
            trust="trusted",
        )
        w.tokens["A"] = token
        tool, args = (
            "mempalace_search",
            {"query": "fixed entity", "limit": limit, "wing": "wing_fixture"},
        )
    else:
        tool, args = TOOLS[0], {"entity": "fixed entity", "search": True, "limit": limit}
    if enrich:
        import shutil

        palace = Path(w.mcp._config.palace_path)
        shutil.copytree(palace, palace.with_name("control"), dirs_exist_ok=True)
    present = call(w, tool, **args)
    remove_hidden(w)
    with w.kg._lock:
        for fact in hidden:
            w.kg._conn().execute("DELETE FROM triples WHERE id=?", (fact,))
        w.kg._conn().commit()
    absent = call(w, tool, **args)
    assert present == absent, "hidden facts shifted search ranking or page"
    assert [f["object"] for f in present["facts"]] == ["readable"]


@pytest.mark.parametrize("limit,offset", [("1", "0"), (" +2 ", "1"), (3.0, 0.0)])
def test_card253_s07_r1_numeric_page(kgworld, limit, offset):
    w = kgworld
    typed = call(w, TOOLS[1], wings=["wing_fixture"], limit=int(limit), offset=int(offset))
    converted = call(w, TOOLS[1], wings=["wing_fixture"], limit=limit, offset=offset)
    assert converted == typed, "protocol integer input lost the timeline"


@pytest.mark.parametrize("flag", ["false", "no", "off", "0", " FALSE "])
def test_card253_s07_r1_strict_worker_search(world, flag):
    refused = False
    try:
        world.mcp.validate_tool_arguments(TOOLS[0], {"entity": "fixed_entity", "search": flag})
    except ValueError:
        refused = True
    assert refused, "worker coerced a non-boolean search switch"


def test_card253_s07_visible_corpus_guard(kgworld, fixed_search_clock):
    w = kgworld
    w.kg.add_triple("fixed", "uses", "secondary", wing="wing_fixture", source_thread=w.readable.id)
    present = call(w, TOOLS[0], entity="fixed entity", search=True, limit=100)
    remove_hidden(w)
    absent = call(w, TOOLS[0], entity="fixed entity", search=True, limit=100)
    # H-equality guard: this already passed before the parity correction.
    assert present == absent, "hidden corpus changed the visible search result"
    assert present["facts"]


@pytest.fixture
def parity_corpus(world):
    w = world
    w.kg = w.mcp._get_kg()
    for subject, predicate, obj in (
        ("fixed_entity", "uses", "readable"),
        ("fixed", "uses", "secondary"),
        ("Will Chen", "works_at", "Acme"),
    ):
        w.kg.add_triple(subject, predicate, obj, wing="wing_fixture", source_thread=w.readable.id)
    return w


def scoped_search(w, query, **kwargs):
    allow = w.kg.read_candidates()["facts"]
    return w.mcp.tool_kg_query(query, search=True, thread_scope={"allow": allow}, **kwargs)


@pytest.mark.parametrize("query", ["Will", "fixed entity"])
def test_card253_s07_r2_local_parity(parity_corpus, fixed_search_clock, query):
    w = parity_corpus
    local = w.mcp.tool_kg_query(query, search=True, limit=100)
    scoped = scoped_search(w, query, limit=100)
    assert scoped == local, "scoped search differs from the local visible-corpus scorer"
    assert len(local["facts"]) == 1


def test_card253_s07_r2_seed_cap(parity_corpus, fixed_search_clock):
    w = parity_corpus
    for i in range(70):
        w.kg.add_triple(
            f"needle {i}", "uses", "same", wing="wing_fixture", source_thread=w.readable.id
        )
    local = w.mcp.tool_kg_query("needle", search=True, limit=100)
    assert len(local["facts"]) == 50
    assert scoped_search(w, "needle", limit=100) == local, "scoped search ignored the seed cap"


def test_card253_s07_r2_authenticated_will(kgworld):
    w = kgworld
    w.kg.add_triple(
        "Will Chen", "works_at", "Acme", wing="wing_fixture", source_thread=w.readable.id
    )
    reply = call(w, TOOLS[0], entity="Will", search=True)
    assert [row["subject"] for row in reply["facts"]] == ["Will Chen"], "readable name vanished"


@pytest.mark.parametrize("flag", [None, "yes"])
def test_card253_s07_r2_unknown_search_flag(kgworld, flag):
    w = kgworld
    w.tokens["A"] = w.h.token(
        "extra-key-reader",
        user=w.h.db.authenticate_token(w.tokens["A"]).user_id,
        tools=[*TOOLS, "mempalace_search"],
        scopes=["memory:read", "private:read", "threads:read", "kg:read"],
        read_wings=["*"],
        trust="trusted",
    )
    normal = call(w, "mempalace_search", query="fixed", include_facts=False)
    extra = w.h.rpc(
        w.tokens["A"],
        "tools/call",
        {
            "name": "mempalace_search",
            "arguments": {
                "query": "fixed",
                "include_facts": False,
                "search": flag,
            },
        },
    )
    assert normal.get("results") is not None
    # Unknown keys retain worker protocol handling, not the KG-specific Denied.
    assert extra.get("error", {}).get("code") == -32602, "non-KG search key hit the KG refusal"


@pytest.mark.parametrize("expire", [False, True])
def test_card253_s07_r2_scan_deadline_lock(parity_corpus, fixed_search_clock, monkeypatch, expire):
    import threading

    from commonplace.rules import checks
    from mempalace.searcher import facts

    w = parity_corpus
    for i in range(3):
        w.kg.add_triple("scanmarker", "uses", f"item{i}", source_thread=w.readable.id)
    original = facts._words
    observed = []
    deadline_token = checks._deadline.set(None)

    def words(text, *args, **kwargs):
        if text.startswith("scanmarker uses "):
            acquired = []

            def inspect_lock():
                held = w.kg._lock.acquire(blocking=False)
                acquired.append(held)
                if held:
                    w.kg._lock.release()

            inspector = threading.Thread(target=inspect_lock)
            inspector.start()
            inspector.join(timeout=1)
            observed.append(acquired == [True])
            if expire:
                checks._deadline.set(-1)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(facts, "_words", words)
    try:
        result = scoped_search(w, "scanmarker", limit=100)
    finally:
        checks._deadline.reset(deadline_token)
    assert observed, "real candidate/scoring branch not reached"
    assert all(observed), "scan held the palace lock"
    if expire:
        assert len(observed) == 1 and result.get("facts") == [], "scan kept working after deadline"


def test_card253_s07_r2_worker_expiry(parity_corpus):
    import time

    w = parity_corpus
    allow = w.kg.read_candidates()["facts"]
    result = w.mcp.tool_kg_query(
        "fixed", search=True, thread_scope={"allow": allow, "deadline": time.time() - 1}
    )
    assert result == {"facts": []}, "worker ignored the gateway expiry"


@pytest.mark.parametrize("flag", [1, "yes"])
def test_card253_s07_r3_missing_search_snapshot(parity_corpus, fixed_search_clock, flag):
    w = parity_corpus
    allow = w.kg.read_candidates()["facts"]
    for i in range(70):
        w.kg.add_triple(f"fixed entity {i}", "uses", "hidden", source_thread=w.hidden.id)
    boolean = w.mcp.tool_kg_query("fixed entity", search=True, thread_scope={"allow": allow})
    truthy = w.mcp.tool_kg_query("fixed entity", search=flag, thread_scope={"allow": allow})
    assert truthy.get("facts") == [], "scoped search fell back to the global index"
    assert boolean["facts"]
    assert (
        w.mcp.tool_kg_query("fixed entity", search=True, thread_scope={"allow": allow}) == boolean
    )


@pytest.mark.parametrize(
    "tool,arguments",
    [
        (TOOLS[0], {"entity": "fixed", "offset": 0}),
        (TOOLS[0], {"entity": "fixed", "search": True, "bogus": 1}),
        (TOOLS[0], {"entity": "fixed", "search": False, "bogus": 1}),
        (TOOLS[1], {"entity": "fixed", "bogus": 1}),
        (TOOLS[2], {"bogus": 1}),
        (TOOLS[3], {"bogus": 1}),
    ],
)
def test_card253_s07_r3_undeclared_key(kgworld, tool, arguments):
    assert call(kgworld, TOOLS[0], entity="fixed_entity")["facts"]
    reply = kgworld.h.rpc(kgworld.tokens["A"], "tools/call", {"name": tool, "arguments": arguments})
    key = "offset" if "offset" in arguments else "bogus"
    assert reply.get("error") == {
        "code": -32602,
        "message": f"Unknown parameter '{key}' for tool {tool}",
    }, "undeclared key was hidden by the projection envelope"


@pytest.mark.parametrize(
    "query",
    [
        "fixed entity",
        "Will",
        "Ask Will about Acme",
        "fixed",
        "uses",
        "Acme",
        "what does Will work at",
        "fixed entity uses",
        "entity",
        "hidden",
        "needle",
    ],
)
def test_card253_s07_r3_hidden_parity_guard(parity_corpus, fixed_search_clock, query):
    w = parity_corpus
    for i in range(70):
        w.kg.add_triple(f"needle {i}", "uses", "same", source_thread=w.readable.id)
    local = w.mcp.tool_kg_query(query, search=True, limit=100)
    allow = w.kg.read_candidates()["facts"]
    for i in range(70):
        w.kg.add_triple(f"fixed entity {i}", "uses", f"hidden{i}", source_thread=w.hidden.id)
        w.kg.add_triple(f"needle h{i}", "uses", "same", source_thread=w.hidden.id)
    scoped = w.mcp.tool_kg_query(query, search=True, limit=100, thread_scope={"allow": allow})
    assert scoped == local, "hidden rows affected parity or the selective seed cap"
    if query == "needle":
        assert len(local["facts"]) == 50


@pytest.mark.parametrize("tool,search", [(TOOLS[0], False), (TOOLS[1], False), (TOOLS[0], True)])
@pytest.mark.parametrize("spelling", ["fixture", "wing_fixture"])
def test_card253_s07_r4_literal_reader(kgworld, fixed_search_clock, tool, search, spelling):
    w = kgworld
    w.tokens["A"] = w.h.token(
        "literal-fact-reader",
        user=w.h.db.authenticate_token(w.tokens["A"]).user_id,
        tools=TOOLS,
        scopes=["memory:read", "private:read", "threads:read", "kg:read"],
        read_wings=["wing_fixture", "threads"],
        trust="trusted",
    )
    principal = w.h.db.authenticate_token(w.tokens["A"])
    assert not principal.policy.sees_all_wings
    assert principal.policy.can_read_wing(spelling)
    args = {"entity": "fixed_entity", "wings": [spelling]}
    if tool == TOOLS[0]:
        args["search"] = search
    present = call(w, tool, **args)
    rows = present["timeline" if tool == TOOLS[1] else "facts"]
    assert rows, "non-owner shorthand discarded readable facts"
    remove_hidden(w)
    assert present == call(w, tool, **args), "hidden facts changed a literal-reader page"


@pytest.mark.parametrize(
    "tool,args,message",
    [
        (TOOLS[1], {"limit": "abc"}, "'limit' must be an integer"),
        (TOOLS[1], {"limit": -1}, "'limit' must be at least 1"),
        (TOOLS[1], {"offset": "x"}, "'offset' must be an integer"),
        (TOOLS[0], {"entity": "fixed_entity", "limit": "abc"}, "'limit' must be an integer"),
    ],
)
def test_card253_s07_r4_pagination_protocol(kgworld, tool, args, message):
    w = kgworld
    assert call(w, TOOLS[0], entity="fixed_entity")["facts"]
    reply = w.h.rpc(w.tokens["A"], "tools/call", {"name": tool, "arguments": args})
    assert reply.get("error") == {
        "code": -32602,
        "message": f"Invalid params for tool {tool}: {message}",
    }, "malformed pagination became an empty successful result"


@pytest.mark.parametrize(
    "tool,args",
    [
        (TOOLS[0], {"entity": "fixed_entity", "as_of": "not-a-date"}),
        (TOOLS[0], {"entity": "fixed_entity", "as_of": 5}),
        (TOOLS[0], {"entity": "fixed_entity", "direction": "sideways"}),
        (TOOLS[0], {"entity": "fixed_entity", "direction": 5}),
        (TOOLS[0], {"entity": ""}),
        (TOOLS[1], {"entity": ""}),
    ],
)
def test_card253_s07_r5_handler_validation(kgworld, tool, args):
    w = kgworld
    typed = w.mcp.validate_tool_arguments(tool, args)
    expected = w.mcp.TOOLS[tool]["handler"](**typed)
    assert set(expected) == {"error"} and isinstance(expected["error"], str)
    present = call(w, tool, **args)
    assert present == expected, "invalid declared argument became a silent empty result"
    remove_hidden(w)
    assert call(w, tool, **args) == present, "hidden facts changed an argument error"


@pytest.mark.parametrize(
    "args,message",
    [
        ({}, "Missing required parameter 'entity' for tool mempalace_kg_query"),
        ({"entity": None}, "Missing required parameter 'entity' for tool mempalace_kg_query"),
        (
            {"entity": ["x"]},
            "Invalid params for tool mempalace_kg_query: 'entity' must be a string",
        ),
        (
            {"entity": "fixed_entity", "sensitivities": ["unknown"]},
            "Invalid params for tool mempalace_kg_query: 'sensitivities[0]' must be one of 'public', 'internal', 'private', 'secret'",
        ),
    ],
)
def test_card253_s07_r5_protocol_validation(kgworld, args, message):
    w = kgworld
    assert call(w, TOOLS[0], entity="fixed_entity")["facts"]
    expected = {"code": -32602, "message": message}

    def request():
        return w.h.rpc(w.tokens["A"], "tools/call", {"name": TOOLS[0], "arguments": args})

    present = request()
    assert present.get("error") == expected, "invalid declared argument lost its protocol error"
    remove_hidden(w)
    absent = request()
    assert {key: value for key, value in absent.items() if key != "id"} == {
        key: value for key, value in present.items() if key != "id"
    }, "hidden facts changed a protocol error"


def test_card253_s07_r5_sqlite_offset_error(kgworld):
    w = kgworld
    assert call(w, TOOLS[0], entity="fixed_entity")["facts"]
    reply = w.h.rpc(
        w.tokens["A"], "tools/call", {"name": TOOLS[1], "arguments": {"offset": 10**30}}
    )
    assert reply.get("error") == {
        "code": -32000,
        "message": "Internal tool error",
        "data": {"error_class": "OverflowError"},
    }, "unrepresentable SQLite offset became an empty success"


@pytest.mark.parametrize("kind", ["owner", "wildcard"])
@pytest.mark.parametrize(
    "tool,args,protocol",
    [
        (TOOLS[0], {"entity": "fixed_entity"}, None),
        (TOOLS[0], {"entity": "fixed_entity", "search": True}, None),
        (TOOLS[1], {}, None),
        (
            TOOLS[0],
            {"entity": "fixed_entity", "limit": 0},
            "Invalid params for tool mempalace_kg_query: 'limit' must be at least 1",
        ),
        (
            TOOLS[0],
            {"entity": None},
            "Missing required parameter 'entity' for tool mempalace_kg_query",
        ),
    ],
)
def test_card253_s07_r6_blank_wings(kgworld, kind, tool, args, protocol):
    from commonplace import sessions
    from commonplace.gateway import _tool_response

    w = kgworld
    principal = (
        sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
        if kind == "owner"
        else w.h.db.authenticate_token(w.tokens["A"])
    )
    assert principal.policy.sees_all_wings
    assert (principal.role == "owner") == (kind == "owner")
    assert w.h.gateway.call_memory_tool(principal, TOOLS[0], {"entity": "fixed_entity"})["facts"]

    def request():
        return w.h.gateway.mediator._tools_call(
            principal,
            {
                "jsonrpc": "2.0",
                "id": 700,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {**args, "wings": ["  "]}},
            },
        )

    expected = (
        {"jsonrpc": "2.0", "id": 700, "error": {"code": -32602, "message": protocol}}
        if protocol
        else _tool_response(700, {"error": "wings must be a list of names"})
    )
    present = request()
    assert present == expected, "blank wing replaced develop's validation error with Denied"
    remove_hidden(w)
    assert request() == present, "hidden facts changed blank-wing validation"


@pytest.mark.parametrize("kind", ["owner", "wildcard"])
@pytest.mark.parametrize(
    "args",
    [
        {"entity": "fixed_entity", "wings": ["-"]},
        {"entity": "fixed_entity", "wings": ["_"]},
        {"entity": "fixed_entity", "wings": ["."]},
        {"entity": "fixed_entity", "wings": ["Wing_"]},
        {"entity": "fixed_entity", "wings": ["wing_"]},
        {"entity": "fixed_entity", "wings": ["wing_fixture", "wing_"]},
        {"entity": None, "wings": ["wing_"]},
    ],
)
def test_card253_s07_r7_canonical_empty_wings(kgworld, kind, args):
    from commonplace import sessions
    from commonplace.gateway import _tool_result

    w = kgworld
    principal = (
        sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
        if kind == "owner"
        else w.h.db.authenticate_token(w.tokens["A"])
    )
    assert principal.policy.sees_all_wings
    assert (principal.role == "owner") == (kind == "owner")
    assert w.h.gateway.call_memory_tool(principal, TOOLS[0], {"entity": "fixed_entity"})["facts"]

    def request():
        return w.h.gateway.mediator._tools_call(
            principal,
            {
                "jsonrpc": "2.0",
                "id": 701,
                "method": "tools/call",
                "params": {"name": TOOLS[0], "arguments": args},
            },
        )

    present = request()
    if args["entity"] is None:
        assert present == {
            "jsonrpc": "2.0",
            "id": 701,
            "error": {
                "code": -32602,
                "message": "Missing required parameter 'entity' for tool mempalace_kg_query",
            },
        }, "canonical-empty wing masked the required-entity error"
    else:
        result = _tool_result(present)
        assert isinstance(result, dict) and "facts" in result, (
            "sees-all canonical-empty wing was refused with Denied"
        )
        assert result["count"] == (1 if "wing_fixture" in args["wings"] else 0)
        assert bool(result["facts"]) == ("wing_fixture" in args["wings"])
    remove_hidden(w)
    assert request() == present, "hidden facts changed canonical-empty wing results"


@pytest.mark.parametrize("kind", ["owner", "wildcard"])
def test_card253_s07_r7_canonical_empty_hidden_fact(kgworld, kind):
    from commonplace import sessions
    from commonplace.gateway import _tool_result
    from commonplace.policy import canonical_wing

    w = kgworld
    # Local legacy storage can contain names the gateway cannot grant for writes.
    with w.kg._lock:
        w.kg._conn().execute("UPDATE triples SET wing='-' WHERE id=?", (w.hidden_wing,))
        w.kg._conn().commit()
    assert canonical_wing("-") == ""
    assert any(
        row["id"] == w.hidden_wing and row["wing"] == "-" for row in w.kg.read_candidates()["facts"]
    )
    principal = (
        sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
        if kind == "owner"
        else w.h.db.authenticate_token(w.tokens["A"])
    )
    assert principal.policy.sees_all_wings and not principal.policy.can_read_wing("-")
    assert not w.h.gateway.handoff.recall.can_read(principal, w.hidden.id)
    assert w.h.gateway.call_memory_tool(principal, TOOLS[0], {"entity": "fixed_entity"})["facts"]

    def request():
        return w.h.gateway.mediator._tools_call(
            principal,
            {
                "jsonrpc": "2.0",
                "id": 702,
                "method": "tools/call",
                "params": {
                    "name": TOOLS[0],
                    "arguments": {"entity": "hidden_subject", "wings": ["-"]},
                },
            },
        )

    present = request()
    result = _tool_result(present)
    assert isinstance(result, dict) and result.get("facts") == [] and result.get("count") == 0, (
        "canonical-empty legacy wing lost the visible-only empty result"
    )
    remove_hidden(w)
    assert request() == present, "canonical-empty H-only fact changed the public response"
