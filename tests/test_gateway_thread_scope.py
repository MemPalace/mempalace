"""The gateway applies one workspace-local read rule to mutation references."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from commonplace import sessions
from commonplace.gateway import (
    Denied,
    Hidden,
    Mediator,
    WorkerUnavailable,
    _tool_response,
    _tool_result,
)
from commonplace.handoff import mirror_access as access_mod, provenance_refs as refs_mod
from commonplace.handoff.facts import VERSION
from commonplace.handoff.models import ThreadMessage
from commonplace.handoff.recall import Recall
from commonplace.handoff.thread_store import ThreadStore
from commonplace.policy import Policy
from mempalace import fact_provenance, mcp_server
from tests import test_card228_thread_mutations as mutation_tests
from tests.test_handoff_recall import reader as principal_for
from tests.test_imported_history_search import world  # noqa: F401

SCOPES = mutation_tests.SCOPES
case = mutation_tests.case


@pytest.fixture
def env(tmp_path):
    stores = {ws: ThreadStore(str(tmp_path / (ws + ".sqlite3"))) for ws in ("X", "Z")}
    principal = replace(
        principal_for(),
        workspace_id="X",
        policy=Policy.build(
            profile="full", trust="trusted", scopes=SCOPES, read_wings=["*"], write_wings=["*"]
        ),
    )
    ctx = SimpleNamespace(store_for=lambda ws: stores[ws], _stores=stores)
    ctx.recall = Recall(ctx.store_for)
    gateway = SimpleNamespace(handoff=ctx)
    workers = Mock()
    workers.call_tool.return_value = {"success": True, "probe": {"facts": [], "next": None}}
    mediator = Mediator(Mock(), workers)
    mediator.gateway = gateway
    mediator._audit = Mock()
    yield SimpleNamespace(
        stores=stores, principal=principal, gateway=gateway, workers=workers, mediator=mediator
    )
    for store in stores.values():
        store.close()


def thread(e, *, user=None, ws="X", source=None):
    user = user or e.principal.user_id
    return e.stores[ws].store(
        ws,
        source or f"import-private:{user}:g",
        [ThreadMessage(0, "user", "message", "synthetic words", "synthetic words")],
        actor=user,
        wing="private_threads",
    )


def args(**kw):
    return {"subject": "A", "predicate": "b", "object": "C", "wing": "shared", **kw}


def check(e, **kw):
    return e.mediator._check_provenance(e.principal, "mempalace_kg_save", args(**kw))


def refusal(e, **kw):
    result = None
    try:
        check(e, **kw)
    except Hidden as error:
        result = error.result
    assert result is not None, "Expected a hidden citation to be refused"
    return result


def test_u3_a_client_scope_is_removed(env, monkeypatch):
    seen = []
    monkeypatch.setattr(
        env.mediator, "_before_kg_write", lambda p, n, a, notes: seen.append(dict(a))
    )
    env.workers.post.return_value = _tool_response(1, {"success": True, "closed": 0})
    reply = env.mediator._tools_call(
        env.principal,
        {
            "id": 1,
            "params": {
                "name": "mempalace_kg_invalidate",
                "arguments": args(thread_scope={"allow": [{"id": "hidden"}]}),
            },
        },
    )
    assert seen and "thread_scope" not in seen[0] and _tool_result(reply)["closed"] == 0


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"probe": {}},
        {"probe": {"facts": [], "next": "x"}},
        {"probe": {"facts": [{"id": "x", "wing": "shared", "threads": [4]}], "next": None}},
    ],
)
def test_u3_b_malformed_probe_fails_closed(env, result):
    env.workers.call_tool.return_value = result
    with pytest.raises(Denied, match="could not check what this changes"):
        env.mediator._scope_kg_write(env.principal, "mempalace_kg_save", args())


def test_u3_b_validation_reply_is_unchanged(env):
    error = {"success": False, "error": "bad date"}
    env.workers.call_tool.return_value = error
    assert env.mediator._scope_kg_write(env.principal, "mempalace_kg_save", args()) == {
        "respond": error
    }


def test_u3_c_worker_errors_propagate(env):
    env.workers.call_tool.side_effect = WorkerUnavailable()
    with pytest.raises(WorkerUnavailable):
        env.mediator._scope_kg_write(env.principal, "mempalace_kg_invalidate", args())


def test_u3_d_reader_errors_are_hidden(env):
    env.gateway.handoff.recall.can_read = Mock(side_effect=TimeoutError())
    with pytest.raises(Hidden):
        check(env, source_thread="thr_aa")


@pytest.mark.parametrize(
    "wing,threads",
    [
        ("shared", []),
        ("private_threads", []),
        ("shared", ["thr_aa"]),
        ("private_threads", ["thr_aa"]),
        (None, ["ext-1"]),
        ("shared", ["thr_aa", "thr_bb"]),
    ],
)
def test_u3_e_fact_visibility_matches_read_gate(env, wing, threads):
    env.gateway.handoff.recall.can_read = lambda p, t: t == "thr_aa"
    candidate = {"id": "fact", "wing": wing, "threads": threads}
    payload = {
        "id": "fact",
        "fact": "synthetic",
        "wing": wing,
        "sources": [{"source_thread": t} for t in threads],
    }
    reply = access_mod.gate(env.gateway, env.principal, "kg_read", {"facts": [payload]})
    assert access_mod.fact_visible(
        candidate, access_mod.reader(env.gateway, env.principal)
    ) == bool(reply["facts"])


def test_u3_f_i_room_keys_all_principals(env):
    def can_read(t):
        return t == "thr_aa"

    taxonomy = {
        "taxonomy": {
            "private_threads": {"thr_bb": 1},
            "shared": {"thr_aa": 2, "thr_bb": 3, "notes": 4},
        }
    }
    assert access_mod.filter_room_keys("wing_list", taxonomy, can_read) == {
        "taxonomy": {"shared": {"thr_aa": 2, "notes": 4}}
    }
    assert access_mod.filter_room_keys(
        "read_wing", {"rooms": {"thr_aa": 1, "thr_bb": 2}}, can_read
    ) == {"rooms": {"thr_aa": 1}}
    wings = {"wings": {"private_threads": 10}}
    assert access_mod.filter_room_keys("wing_list", wings, can_read) is wings


def test_u3_g_hidden_pages_followed(env):
    env.gateway.handoff.recall.can_read = lambda p, t: False
    pages = [
        {
            "facts": [{"id": str(i), "wing": "private_threads", "threads": ["thr_aa"]}],
            "next": str(i) if i < 4 else None,
        }
        for i in range(5)
    ]
    env.workers.call_tool.side_effect = [{"success": True, "probe": p} for p in pages]
    a = args()
    env.mediator._scope_kg_write(env.principal, "mempalace_kg_invalidate", a)
    assert env.workers.call_tool.call_count == 5 and a["thread_scope"] == {"allow": []}


@pytest.mark.parametrize(
    "field", ["source_thread", "source_url", "source_file", "source_drawer_id", "source_closet"]
)
def test_u3_h_hidden_equals_missing_citations(env, field):
    hidden = thread(env, user="other")
    replies = []
    for value in [hidden.id, "thr_ffff"]:
        replies.append(refusal(env, **{field: value}))
    if field == "source_drawer_id":
        assert all(r["error"].startswith("Drawer not found:") for r in replies)
    else:
        assert replies[0] == replies[1] == {"success": False, "error": "Source not found"}
    check(
        env,
        source_thread="ext-plain",
        source_file="/data/ordinary.md",
        source_url="https://example.test/chat",
    )


def test_u3_j_thread_scope_hidden_in_tools_list(env):
    env.workers.post.return_value = {
        "id": 1,
        "result": {
            "tools": [
                {
                    "name": "mempalace_kg_save",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "thread_scope": {"type": "object"},
                        },
                    },
                }
            ]
        },
    }
    reply = env.mediator._tools_list(env.principal, {"id": 1})
    found = next(t for t in reply["result"]["tools"] if t["name"] == "mempalace_kg_save")
    assert "thread_scope" not in found["inputSchema"]["properties"]


def test_u3_k_repeated_next_rejected(env):
    env.workers.call_tool.side_effect = [
        {"probe": {"facts": [{"id": "a", "wing": "shared", "threads": []}], "next": "a"}}
    ] * 2
    with pytest.raises(Denied):
        env.mediator._scope_kg_write(env.principal, "mempalace_kg_save", args())


def test_extraction_by_non_reader_uploader_does_not_wedge_cursor(case, monkeypatch):
    c = case
    ctx = c.w.gateway.handoff
    store = ctx.store_for(c.w.me["workspace"])
    stored = store.store(
        c.w.me["workspace"],
        f"import-private:{c.w.me['user']}:extraction",
        [
            ThreadMessage(
                0,
                "user",
                "message",
                "Kestrel deploys on port 9000.",
                "Kestrel deploys on port 9000.",
            )
        ],
        actor=c.w.me["user"],
        wing="shared",
    )
    member = sessions.person_principal(c.w.client.db, c.user, c.w.me["workspace"])
    owner = sessions.person_principal(c.w.client.db, c.w.me["user"], c.w.me["workspace"])
    assert member.role == "member" and member.policy.can_read_wing(stored.wing)
    filed = []
    monkeypatch.setattr(ctx.service, "save_fact", lambda p, a: filed.append(a))
    before = store.fact_cursor(stored.id, VERSION)
    ctx.service._extract_later(member, stored)
    ctx.service.wait_for_facts(30)
    assert not filed and store.fact_cursor(stored.id, VERSION) == before
    ctx.service._extract_later(owner, stored)
    ctx.service.wait_for_facts(30)
    assert filed and store.fact_cursor(stored.id, VERSION) == stored.message_count


def test_u3_m_scrub_provenance_before_early_return_and_every_dict(env):
    own, hidden = thread(env), thread(env, user="other")
    response = {
        "facts": [
            {
                "fact": "neutral",
                "source_file": hidden.id,
                "source_seq_start": 4,
                "sources": [{"source_file": hidden.id, "source_seq_end": 5}],
            },
            {"fact": "all hidden", "source_thread": hidden.id},
            {
                "fact": "mixed",
                "source_thread": hidden.id,
                "source_seq_start": 6,
                "source_closet": "opaque",
                "sources": [{"source_thread": own.id}, {"source_thread": hidden.id}],
            },
        ],
        "other": {"origin_link": "t3-thread://v1/" + hidden.id, "source_seq_end": 8},
    }
    result = access_mod.gate(env.gateway, env.principal, "kg_read", response)
    first, mixed = result["facts"]
    assert first == {"fact": "neutral", "sources": [{}]}
    assert (
        "source_closet" not in mixed
        and "source_seq_start" not in mixed
        and mixed["sources"] == [{"source_thread": own.id}]
    )
    assert result["other"] == {}


@pytest.mark.parametrize("role", ["owner", "member"])
@pytest.mark.parametrize("visible", [False, True])
def test_u3_n_scoped_source_delete_never_unfiltered(env, role, visible):
    principal = replace(env.principal, role=role)
    env.workers.call_tool.return_value = {
        "probe": {"pairs": [["shared", "notes"]] if visible else [], "roomless": []}
    }
    a = {"source_file": "source"}
    env.mediator._scope_delete_by_source(principal, "mempalace_delete_by_source", a)
    assert a["thread_scope"]["allow"] == {
        "pairs": [["shared", "notes"]] if visible else [],
        "roomless_ids": [],
        "expect": {},
    }


def test_u3_o_schema_field_coverage():
    for name in ["kg_add", "kg_save", "kg_supersede", "kg_invalidate"]:
        keys = set(mcp_server.TOOLS["mempalace_" + name]["input_schema"]["properties"])
        assert keys <= refs_mod.PROVENANCE_FIELDS | refs_mod.NON_PROVENANCE
        assert not refs_mod.PROVENANCE_FIELDS & refs_mod.NON_PROVENANCE
    source_map = {
        "thread": "source_thread",
        "seq_start": "source_seq_start",
        "seq_end": "source_seq_end",
        "url": "source_url",
    }
    from commonplace import extensions

    schema = extensions.get("fact_save").schema()["inputSchema"]
    assert set(schema["properties"]["source"]["properties"]) == set(source_map)
    assert set(source_map.values()) <= refs_mod.PROVENANCE_FIELDS | refs_mod.NON_PROVENANCE
    assert set(fact_provenance.COLUMNS) <= refs_mod.READ_FIELDS
    import inspect
    import re

    from mempalace.knowledge_graph import KnowledgeGraph

    selected = inspect.getsource(KnowledgeGraph.sources)
    columns = set(re.findall(r"(?:triple_id|source_\w+|added_at)", selected))
    assert columns <= refs_mod.READ_FIELDS


NORMALIZER = [
    (None, ()),
    ("thr_ab", ("thr_ab",)),
    ("thr_%61b", ("thr_ab",)),
    ("thr_%2561b", ("thr_ab",)),
    ("thr_%252561b", ("thr_ab",)),
    ("thr_%25252561b", ("thr_ab",)),
    ("thr_%2525252561b", None),
    ("thr_%FFab", None),
    ("thr_\u200bab", None),
    ("thr_%E2%80%8Bab", None),
    ("thr_%25E2%2580%258Bab", None),
    ("\ufeffthr_ab", None),
    ("%EF%BB%BFthr_ab", None),
    ("\nplain", None),
    ("%00plain", None),
    ("https://x/a%0Ab", None),
    ("/data/thr_2024_notes.md", ("thr_2024",)),
    ("t3-thread://v1//thr_ab/thr_cd", ("thr_ab", "thr_cd")),
    ("capture:a:g#sha256:abc", ("capture:a:g#sha256:abc",)),
    ("/data/plain.md", ()),
    ("ext-plain", ()),
    ("mcp:plain", ()),
    ("import:plain", ()),
    (12, None),
]


@pytest.mark.parametrize("raw,expected", NORMALIZER)
def test_u3_p_normalizer_table(raw, expected):
    result = refs_mod.refs(raw)
    assert (result is refs_mod.REFUSE) if expected is None else result.tokens == expected


@pytest.mark.parametrize("raw,expected", NORMALIZER)
def test_u3_q_token_write_read_agreement(env, raw, expected):
    if not isinstance(raw, str):
        return
    parsed = refs_mod.refs(raw)
    if parsed is not refs_mod.REFUSE and not parsed.tokens:
        return
    env.gateway.handoff.recall.can_read = lambda p, t: t == "thr_ab"
    access = access_mod.reader(env.gateway, env.principal)
    popped = "source_file" not in access_mod.scrub_provenance(
        {"source_file": raw, "source_seq_start": 3}, access
    )
    try:
        check(env, source_file=raw)
        refused = False
    except Hidden:
        refused = True
    assert popped == refused


@pytest.mark.parametrize("ws", ["X", "Z"])
def test_u3_r_lookups_follow_principal_workspace(env, monkeypatch, ws):
    env.principal = replace(env.principal, workspace_id=ws)
    stored = thread(env, ws=ws)
    seen = []
    real = env.gateway.handoff.store_for
    env.gateway.handoff.store_for = lambda workspace: seen.append(workspace) or real(workspace)
    real_family = env.stores[ws].by_source_family
    monkeypatch.setattr(
        env.stores[ws],
        "by_source_family",
        lambda workspace, t: seen.append(workspace) or real_family(workspace, t),
    )
    real_exact = env.stores[ws].by_source
    monkeypatch.setattr(
        env.stores[ws],
        "by_source",
        lambda workspace, t: seen.append(workspace) or real_exact(workspace, t),
    )
    thread(env, ws=ws, source=stored.source_thread_id + "#sha256:1")

    def worker(p, name, a):
        seen.append(p.workspace_id)
        if name == "mempalace_get_drawers":
            assert a["drawer_ids"] == ["opaque-drawer"]
            return {"results": [{"wing": "shared", "room": "notes"}]}
        assert a["thread_scope"]["closet"] == "opaque-closet"
        return {"cited": {"closet": ["shared", "notes"]}}

    env.workers.call_tool.side_effect = worker
    refused = False
    try:
        check(env, source_thread=stored.source_thread_id)
        check(env, source_thread=stored.source_thread_id + "#sha256:1")
        check(env, source_drawer_id="  opaque%2Ddrawer  ", source_closet="opaque%2Dcloset")
    except Hidden:
        refused = True
    assert not refused and seen and set(seen) == {ws}


def test_u3_s_private_family_resolution(env):
    base = f"import-private:{env.principal.user_id}:g"
    thread(env, user="other", source=base.replace("_", "X") + "#sha256:1")
    own = thread(env, source=base + "#sha256:2")
    check(env, source_thread=base)
    assert access_mod.reader(env.gateway, env.principal).reference(base)
    # An exact unreadable hash variant may never borrow another readable sibling.
    hidden = thread(env, user="other", source="capture:Ab:g#sha256:1")
    env.gateway.handoff.recall.can_read = lambda p, t: t != hidden.id
    thread(env, source="capture:Ab:g#sha256:2")
    with pytest.raises(Hidden):
        check(env, source_thread="capture:Ab:g#sha256:1")
    for missing in [base.replace("_", "%"), "capture:ab:g"]:
        with pytest.raises(Hidden):
            check(env, source_thread=missing)
    assert own.id
    # Source ids admit separators, so the whole private token remains literal.
    stored = thread(env, source=base + "/file.md")
    check(env, source_thread=stored.source_thread_id)
    env.workers.call_tool.return_value = {"cited": {"closet": ["private_threads", hidden.id]}}
    refusal(env, source_closet="opaque-closet")


def test_u3_t_response_memoisation(env, monkeypatch):
    stored = thread(env)
    source = stored.source_thread_id
    real_parser = refs_mod.refs
    parser = Mock(side_effect=real_parser)
    monkeypatch.setattr(refs_mod, "refs", parser)
    family = Mock(wraps=env.stores["X"].by_source_family)
    monkeypatch.setattr(env.stores["X"], "by_source_family", family)
    payload = {
        "rows": [{"source_file": source, "origin_link": source} for _ in range(6)]
        + [{"source_file": "capture:missing:g"} for _ in range(6)]
    }
    access_mod.gate(env.gateway, env.principal, "search", payload)
    assert parser.call_count == 2 and family.call_count == 2
    access_mod.gate(env.gateway, env.principal, "search", payload)
    assert parser.call_count == 4 and family.call_count == 4
    access_mod.gate(env.gateway, replace(env.principal, user_id="other"), "search", payload)
    assert parser.call_count == 6 and family.call_count == 6


def test_roomless_hidden_drawer_veto_and_normalized_tokens(env):
    own, hidden = thread(env), thread(env, user="other")
    records = [
        dict(
            kind="revision",
            id=str(i),
            drawer_id="drawer",
            drawer=pair,
            tokens=tokens,
            refused=refused,
            expect="snapshot",
        )
        for i, (pair, tokens, refused) in enumerate(
            [
                (["private_threads", hidden.id], [own.id], False),
                ("missing", [hidden.id], False),
                ("error", [own.id], False),
                ("missing", [own.id], True),
                ("missing", [], False),
                (["shared", "notes"], [], False),
                ("missing", [own.id], False),
            ]
        )
    ]
    env.workers.call_tool.return_value = {"probe": {"pairs": [], "roomless": records}}
    a = {"source_file": "source"}
    env.mediator._scope_delete_by_source(env.principal, "mempalace_delete_by_source", a)
    assert a["thread_scope"]["allow"]["roomless_ids"] == ["revision:5", "revision:6"]


def test_scoped_drawer_reply_omits_source_wide_closet_count(env):
    p = replace(env.principal, policy=Policy.build(read_wings=["shared"], trust="standard"))
    response = _tool_response(1, {"success": True, "closets_deleted": 3})
    result = env.mediator._after(p, "mempalace_delete_drawer", "drawer_write", {}, response)
    assert "closets_deleted" not in _tool_result(result)


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"facts": [], "next": "x"},
        {"facts": [{"id": "a", "wing": "shared", "threads": []}], "next": "b"},
        {"facts": [{"id": "a", "wing": "shared", "threads": [4]}], "next": None},
    ],
)
def test_strict_page_validation(page):
    assert not access_mod.valid_fact_page(page)


def test_see_all_cannot_cite_opaque_hidden_drawer(env, monkeypatch):
    monkeypatch.setattr(
        env.mediator,
        "_drawer_wings",
        Mock(side_effect=Hidden({"success": False, "error": "hidden"})),
    )
    assert refusal(env, source_drawer_id="opaque-drawer") == {
        "success": False,
        "error": "Drawer not found: opaque-drawer",
    }
