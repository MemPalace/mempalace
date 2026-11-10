"""Thread read boundaries also constrain mutations and provenance citations."""

import json
from types import SimpleNamespace

import pytest

from commonplace.handoff.models import ThreadMessage
from commonplace.policy import Policy
from mempalace import hallways
from mempalace.palace import get_aaak_collection, get_closets_collection
from mempalace.revisions import append_drawer_revision, load_drawer_revisions
from tests.test_imported_history_search import (
    functional_worker_transport,  # noqa: F401
    member,
    mirror_record,
    operator,
    private_mirror,
    public_rooms,
    refused,
    rpc,
    shared_mirror,
    shared_read_control,
    tool_value,
    world,  # noqa: F401
)

SCOPES = [
    "memory:read",
    "memory:write",
    "memory:delete",
    "private:read",
    "private:write",
    "threads:read",
    "kg:read",
    "kg:write",
]


def token(w, user=None, scoped=False):
    owner = w.client.db.role(w.me["workspace"], user or w.me["user"]) == "owner"
    return operator(
        w,
        user=user,
        policy=Policy.build(
            profile="full",
            trust="trusted",
            scopes=[s for s in SCOPES if owner or not s.startswith("private:")],
            read_wings=["shared", "private_threads"] if scoped else ["*"],
            write_wings=["*"],
        ),
    )[1]


def call(w, actor, name, **args):
    status, reply = rpc(w, actor, name if name == "fact_save" else "mempalace_" + name, args)
    assert status == 200, reply
    return tool_value(reply)


def store_thread(w, actor=None, source=None, wing="shared"):
    actor = actor or w.me["user"]
    store = w.gateway.handoff.store_for(w.me["workspace"])
    return store.store(
        w.me["workspace"],
        source or f"import-private:{actor}:synthetic",
        [ThreadMessage(0, "user", "message", "Synthetic words", "Synthetic words")],
        actor=actor,
        wing=wing,
    )


@pytest.fixture
def case(world, config, kg, tmp_path, monkeypatch):  # noqa: F811
    w = world
    owner = token(w)
    # Sign-in may provision the member's session through the workspace worker.
    # Start it outside the HTTP client's timed authentication request.
    w.gateway.supervisor.get(w.me["workspace"])
    user = member(w)
    actor = token(w, user)
    principal = w.gateway.authenticate("Bearer " + actor)
    assert principal.role == "member" and not principal.policy.has_scope("private:read")
    shared_read_control(w, actor)
    text = "zqmutationfixture synthetic verbatim words"
    ids = shared_mirror(w, private_mirror(w, text), text)
    record = mirror_record(w, ids, text, owner)
    assert record.get("room") and record.get("drawer_id"), record
    from mempalace import mcp_server

    record["source_file"] = mcp_server._get_collection().get(
        ids=[record["drawer_id"]], include=["metadatas"]
    )["metadatas"][0]["source_file"]
    monkeypatch.setattr(
        hallways, "_get_hallway_file", lambda *a, **k: str(tmp_path / "hallways.json")
    )
    monkeypatch.setattr(
        hallways, "_legacy_hallway_file", lambda: str(tmp_path / "legacy-hallways.json")
    )
    return SimpleNamespace(
        w=w,
        owner=owner,
        actor=actor,
        user=user,
        record=record,
        room=record["room"],
        kg=kg,
        palace=config.palace_path,
    )


@pytest.mark.parametrize("wing", [None, "shared"])
@pytest.mark.parametrize("cli", [False, True], ids=["structured", "cli"])
def test_search_hides_ordinary_copy_protected_source(case, wing, cli):
    args = {"query": "qqprovenance", **({"wing": wing} if wing else {})}
    added = call(
        case.w,
        case.owner,
        "add_drawer",
        wing="shared",
        room="ordinary-copy",
        content="qqprovenance ordinary copy words",
        source_file=f"handoff:{case.room}#v2:0-0:conversation:0",
    )
    assert added.get("success")
    owner = call(case.w, case.owner, "search", cli_compatible=cli, **args)
    assert case.room in json.dumps(owner) and "handoff:" in json.dumps(owner)
    result = call(case.w, case.actor, "search", cli_compatible=cli, **args)
    encoded = json.dumps(result)
    assert "qqprovenance ordinary copy words" in encoded
    assert case.room not in encoded, "Protected thread id leaked through ordinary-copy provenance"
    assert "handoff:" not in encoded, "Protected source leaked through ordinary-copy provenance"


@pytest.mark.parametrize("wing", [None, "shared"])
@pytest.mark.parametrize("cli", [False, True], ids=["structured", "cli"])
def test_search_hides_candidate_counts(case, wing, cli):
    args = {
        "query": "zqmutationfixture synthetic verbatim words",
        "limit": 1,
        **({"wing": wing} if wing else {}),
    }
    owner = call(case.w, case.owner, "search", **args)
    assert owner.get("total_before_filter", 0) > 0 and owner.get("results")
    result = call(case.w, case.actor, "search", cli_compatible=cli, **args)
    assert result.get("results") == []
    assert "total_before_filter" not in result, "Hidden-candidate count leaked"
    if cli:
        assert set(result) <= {"cli_output", "query", "wings_searched", "results", "facts"}, (
            "Restricted CLI exposed structured-only fields"
        )


@pytest.mark.parametrize("wing", [None, "shared"])
@pytest.mark.parametrize("query", ["Public fixture", "qqnomatchzz"], ids=["hit", "empty"])
def test_shared_cli_output_matches_owner(case, wing, query):
    args = {"query": query, "cli_compatible": True, **({"wing": wing} if wing else {})}
    args["room"] = "read-control" if query == "Public fixture" else "absent-control-room"
    owner = call(case.w, case.owner, "search", **args)
    result = call(case.w, case.actor, "search", **args)
    if query == "Public fixture":
        assert "Public fixture independently readable control" in owner["cli_output"]
        assert "Match:" in owner["cli_output"] and "=" * 60 in owner["cli_output"]
    else:
        assert f'No results found for: "{query}"' in owner["cli_output"]
    assert result["cli_output"] == owner["cli_output"], "Restricted CLI formatting changed"
    assert set(result) <= {"cli_output", "query", "wings_searched", "results", "facts"}, (
        "Restricted CLI exposed structured-only fields"
    )
    assert result.get("results") == [], "Restricted CLI exposed hit objects"


@pytest.mark.parametrize("wing", [None, "shared"])
@pytest.mark.parametrize("query", ["zqmutationfixture", "synthetic", "verbatim", "words"])
def test_cli_search_hides_protected_shared_copy(case, wing, query):
    args = {"cli_compatible": True, **({"wing": wing} if wing else {})}
    visible = call(case.w, case.actor, "search", query="Public fixture", **args)
    assert "Public fixture independently readable control" in visible.get("cli_output", "")
    structured = call(case.w, case.actor, "search", query=query, **({"wing": wing} if wing else {}))
    assert case.room not in json.dumps(structured)
    result = call(case.w, case.actor, "search", query=query, **args)
    output = result.get("cli_output", "")
    assert "zqmutationfixture synthetic verbatim words" not in output, (
        "Protected-room body leaked into CLI output"
    )
    assert case.room not in output, "Protected thread room leaked into CLI output"
    assert "handoff:" not in output, "Protected mirror source leaked into CLI output"


@pytest.mark.parametrize("wing", [None, "shared"])
def test_cli_search_owner_keeps_existing_renderer(case, wing, monkeypatch):
    seen = []
    original = case.w.gateway.mediator.workers.post

    def post(principal, request):
        params = request.get("params", {})
        if params.get("name") == "mempalace_search":
            seen.append(params["arguments"].get("cli_compatible"))
        return original(principal, request)

    monkeypatch.setattr(case.w.gateway.mediator.workers, "post", post)
    result = call(
        case.w,
        case.owner,
        "search",
        query="zqmutationfixture",
        cli_compatible=True,
        **({"wing": wing} if wing else {}),
    )
    assert "zqmutationfixture synthetic verbatim words" in result.get("cli_output", "")
    assert seen and all(seen), "Every-wing principal no longer uses the worker CLI renderer"


def save(c, obj="old", actor=None, wing="shared", **kwargs):
    args = dict(
        subject="SyntheticProject",
        predicate="deploy_port",
        object=obj,
        wing=wing,
        sensitivity="internal",
        valid_from="2026-01-01",
    )
    args.update(kwargs)
    result = call(c.w, actor or c.owner, "kg_save", **args)
    assert result.get("success"), result
    return result


def hidden(c, obj="old", wing="shared"):
    return save(c, obj, wing=wing, source_thread=c.room)


def query(c, actor=None, **kwargs):
    return call(c.w, actor or c.owner, "kg_query", entity="SyntheticProject", **kwargs)


def current(c, obj="old"):
    return any(f.get("object") == obj and f.get("current") for f in query(c).get("facts", []))


def mutate(c, name, actor=None, **kwargs):
    args = dict(subject="SyntheticProject", predicate="deploy_port", wing="shared")
    if name == "kg_supersede":
        args.update(old_object="old", new_object="new", at="2026-02-01")
    else:
        args["object"] = "old"
    args.update(kwargs)
    return call(c.w, actor or c.actor, name, **args)


def test_card228_r01_member_cannot_invalidate_hidden_fact(case):
    hidden(case)
    mutate(case, "kg_invalidate")
    assert current(case)  # RED


def test_card228_r02_invalidate_count_is_no_oracle(case):
    hidden(case)
    actual = mutate(case, "kg_invalidate")
    absent = mutate(case, "kg_invalidate", object="absent")
    actual.pop("fact", None)
    absent.pop("fact", None)
    assert actual == absent  # RED


def test_card228_r03_member_cannot_supersede_hidden_fact(case):
    hidden(case)
    mutate(case, "kg_supersede")
    assert current(case)  # RED


def test_card228_r03b_supersede_does_not_reuse_hidden_successor(case):
    kept = hidden(case, "new")
    result = mutate(case, "kg_supersede")
    assert result.get("triple_id") != kept["triple_id"]  # RED


def test_card228_r04_member_kg_save_does_not_merge_hidden_fact(case):
    kept = hidden(case)
    result = mutate(case, "kg_save")
    assert result.get("action") != "merged" and result.get("triple_id") != kept["triple_id"]  # RED


def test_card228_r05_member_kg_save_single_valued_keeps_hidden_fact(case):
    hidden(case)
    mutate(case, "kg_save", object="new", single_valued=True, valid_from="2026-02-01")
    assert current(case)  # RED


def test_card228_r06_member_fact_save_single_valued_keeps_hidden_fact(case):
    hidden(case)
    call(
        case.w,
        case.actor,
        "fact_save",
        subject="SyntheticProject",
        predicate="deploy_port",
        object="new",
        theme="shared",
        single_valued=True,
        valid_from="2026-02-01",
        source={
            "thread": "ext-member-1",
            "seq_start": 1,
            "seq_end": 1,
            "url": "https://example.test/c/1",
        },
    )
    assert current(case)  # RED


def test_card228_r07_member_kg_add_does_not_merge_hidden_fact(case):
    kept = hidden(case)
    result = mutate(case, "kg_add")
    assert result.get("triple_id") != kept["triple_id"]  # RED


def test_card228_r08_member_cannot_delete_mirror_copies_by_source(case):
    refused(
        case.w,
        case.actor,
        "mempalace_delete_by_source",
        source_file=case.record["source_file"],
        dry_run=False,
    )
    result = call(case.w, case.owner, "get_drawer", drawer_id=case.record["drawer_id"])
    assert "zqmutationfixture" in result.get("content", "")  # RED


def test_card228_r09_delete_by_source_dry_run_is_no_oracle(case):
    source = case.record["source_file"]
    actual = refused(case.w, case.actor, "mempalace_delete_by_source", source_file=source)
    absent = refused(
        case.w,
        case.actor,
        "mempalace_delete_by_source",
        source_file="handoff:thr_ffff#v2:0-0:conversation:0",
    )
    actual.pop("source_file", None)
    absent.pop("source_file", None)
    assert actual == absent  # RED


def tunnel(c, drawer=False, actor=None):
    actor = actor or c.owner
    public_rooms(c.w, actor)
    result = call(
        c.w,
        actor,
        "create_tunnel",
        source_wing="shared",
        source_room="source",
        target_wing="shared",
        target_room="destination" if drawer else c.room,
        **({"target_drawer_id": c.record["drawer_id"]} if drawer else {}),
    )
    assert result.get("id"), result
    return result["id"]


def test_card228_r10_member_cannot_delete_thread_tunnel(case):
    tid = tunnel(case)
    refused(case.w, case.actor, "mempalace_delete_tunnel", tunnel_id=tid)
    rows = call(case.w, case.owner, "list_tunnels")
    assert any(t["id"] == tid for t in rows)  # RED


def hallway(c, wing="shared"):
    hid = "hallway-synthetic"
    hallways._save_hallways(
        [dict(id=hid, wing=wing, rooms=["source", c.room], entity_a="A", entity_b="B", label="A B")]
    )
    return hid


@pytest.mark.parametrize("kind", ["mirror", "shared"])
def test_card228_r11_member_cannot_delete_thread_hallway(case, kind):
    hid = hallway(case, "private_threads" if kind == "mirror" else "shared")
    refused(case.w, case.actor, "mempalace_delete_hallway", hallway_id=hid)
    assert any(h["id"] == hid for h in hallways.list_hallways())  # RED


@pytest.mark.parametrize("scope", ["see_all", "scoped"])
def test_card228_r12_taxonomy_hides_unreadable_thread_rooms(case, scope):
    actor = token(case.w, case.user, scope == "scoped")
    shared_read_control(case.w, actor)
    reply = call(case.w, actor, "get_taxonomy")
    assert case.room not in json.dumps(reply)  # RED


def test_card228_r13_member_single_valued_mixed_rivals(case):
    hidden(case, wing="shared")
    save(case, "visible", wing="shared")
    mutate(
        case, "kg_save", wing="shared", object="new", single_valued=True, valid_from="2026-02-01"
    )
    rows = {f["object"]: f for f in query(case)["facts"]}
    assert rows["old"]["current"] and not rows["visible"]["current"]  # RED


def test_card228_r14_invalidate_interval_error_is_no_oracle(case):
    hidden(case)
    actual = mutate(case, "kg_invalidate", ended="2025-01-01")
    absent = mutate(case, "kg_invalidate", object="absent", ended="2025-01-01")
    actual.pop("fact", None)
    absent.pop("fact", None)
    assert actual == absent  # RED


def test_card228_r15_taxonomy_drops_emptied_mirror_wing(case):
    wing = "project_hidden"
    control = shared_read_control(case.w, case.actor, wing=wing)
    result = call(
        case.w,
        case.owner,
        "add_drawer",
        wing=wing,
        room=case.room,
        content="Hidden room in an independently readable wing",
    )
    assert result.get("success"), result
    call(case.w, case.owner, "delete_drawer", drawer_id=control)
    assert wing in call(case.w, case.owner, "get_taxonomy").get("taxonomy", {})
    result = call(case.w, case.actor, "get_taxonomy")
    assert wing not in result.get("taxonomy", {}), "Emptied hidden wing leaked into taxonomy"


CITE_TOOLS = [
    "kg_save",
    "kg_add",
    "kg_supersede",
    "fact_save",
    "kg_save_padded",
    "kg_add_padded",
    "kg_supersede_padded",
]


def cite(c, name="kg_save", **fields):
    save(c, "visible", wing="shared")
    args = dict(
        subject="SyntheticProject", predicate="deploy_port", object="visible", wing="shared"
    )
    if name.endswith("_padded"):
        name = name.removesuffix("_padded")
        fields["source_thread"] = " " + fields["source_thread"] + " "
    if name == "kg_supersede":
        save(c, "prior", wing="shared")
        args.pop("object")
        args.update(old_object="prior", new_object="visible", at="2026-02-01")
    if name == "fact_save":
        args["theme"] = args.pop("wing")
        args["source"] = {"thread": fields.pop("source_thread"), "seq_start": 1, "seq_end": 1}
    return call(c.w, c.actor, name, **args, **fields)


@pytest.mark.parametrize("tool", CITE_TOOLS)
def test_card228_r16_member_merge_cannot_attach_hidden_thread(case, tool):
    cite(case, tool, source_thread=case.room)
    assert case.room not in json.dumps(query(case, case.actor)), "Hidden thread citation leaked"
    assert any(
        f.get("object") == "visible" for f in query(case, case.actor).get("facts", [])
    )  # RED


def test_card228_r17_member_cannot_delete_drawer_endpoint_tunnel(case):
    tid = tunnel(case, True)
    refused(case.w, case.actor, "mempalace_delete_tunnel", tunnel_id=tid)
    assert any(t["id"] == tid for t in call(case.w, case.owner, "list_tunnels"))  # RED


def test_card228_r18_list_rooms_hides_unreadable_thread_keys(case):
    assert case.room not in json.dumps(call(case.w, case.actor, "list_rooms"))
    assert case.room not in json.dumps(call(case.w, case.actor, "list_rooms", wing="shared"))  # RED


def test_card228_r19_scoped_member_cannot_invalidate_hidden_fact(case):
    hidden(case)
    mutate(case, "kg_invalidate", actor=token(case.w, case.user, True))
    assert current(case)  # RED


@pytest.mark.parametrize("tool", ["kg_save", "kg_add"])
def test_card228_r20_member_cannot_cite_hidden_drawer(case, tool):
    cite(case, tool, source_drawer_id=case.record["drawer_id"])
    assert case.record["drawer_id"] not in json.dumps(query(case, case.actor))  # RED


def test_card228_r21_member_cannot_cite_hidden_thread_link(case):
    cite(case, source_thread="ext-1", source_url="t3-thread://v1/" + case.room)
    assert case.room not in json.dumps(query(case, case.actor))  # RED


@pytest.mark.parametrize("kind", ["import_private", "capture"])
def test_card228_r22_member_cannot_cite_private_source_id(case, kind):
    source = (
        f"import-private:{case.w.me['user']}:synthetic"
        if kind == "import_private"
        else "capture:owner:s1"
    )
    store_thread(
        case.w,
        source=source,
        wing="shared" if kind == "import_private" else "private_chats",
    )
    cite(case, source_thread=source)
    assert source not in json.dumps(query(case, case.actor))  # RED


def roomless(c, source="/synthetic/source.md", drawer="missing-drawer", actor=None):
    call(
        c.w,
        actor or c.owner,
        "add_drawer",
        wing="shared",
        room="notes",
        content="zqvisiblesourcedrawer",
        source_file=source,
    )
    rid = append_drawer_revision(c.palace, drawer, "prior words", {"source_file": source})
    return source, rid


def survives(c, rid):
    return any(r["revision_id"] == rid for r in load_drawer_revisions(c.palace))


def test_card228_r23_member_delete_by_source_keeps_roomless_rows(case):
    source, rid = roomless(case)
    refused(case.w, case.actor, "mempalace_delete_by_source", source_file=source, dry_run=False)
    assert survives(case, rid)  # RED


@pytest.mark.parametrize("tool", ["kg_save", "kg_add"])
def test_card228_r24_member_cannot_publish_hidden_id_in_source_file(case, tool):
    cite(case, tool, source_file=case.room)
    assert case.room not in json.dumps(query(case, case.actor))  # RED


def closet(c, room=None):
    room = room or c.room
    cid = "closet_shared_" + room + "_abc123"
    col = get_closets_collection(c.palace, create=True)
    col.upsert(
        ids=[cid],
        documents=["synthetic pointer"],
        metadatas=[{"wing": "shared", "room": room, "source_file": "/synthetic/closet.md"}],
    )
    return cid


@pytest.mark.parametrize("tool", ["kg_add"])
def test_card228_r25_member_cannot_publish_hidden_id_in_source_closet(case, tool):
    cid = closet(case)
    call(
        case.w,
        case.actor,
        tool,
        subject="SyntheticProject",
        predicate="deploy_port",
        object="closet-cite",
        wing="shared",
        source_closet=cid,
    )
    assert case.room not in json.dumps(query(case, case.actor))  # RED


@pytest.mark.parametrize(
    "variant",
    [
        "t3_pct2f",
        "app_pct252f",
        "double_slash",
        "query",
        "upper_hex",
        "zero_width",
        "pct_zero_width",
    ],
)
def test_card228_r26_member_cannot_publish_hidden_id_variant(case, variant):
    suffix = case.room[4:]
    variants = {
        "t3_pct2f": ("source_url", "t3-thread://v1/" + case.room + "%2Fx"),
        "app_pct252f": ("source_url", "/app/threads/" + case.room + "%252Fx"),
        "double_slash": ("source_url", "t3-thread://v1//" + case.room),
        "query": ("source_url", "t3-thread://v1/" + case.room + "?x"),
        "upper_hex": ("source_thread", "thr_" + suffix.upper()),
        "zero_width": ("source_thread", "thr_\u200b" + suffix),
        "pct_zero_width": ("source_thread", "thr_%E2%80%8B" + suffix),
    }
    key, value = variants[variant]
    cite(case, **{key: value}, **({"source_thread": "ext-variant"} if key == "source_url" else {}))
    assert suffix.lower() not in json.dumps(query(case, case.actor)).lower()  # RED


def test_card228_r27_owner_delete_by_source_keeps_other_filers_roomless_revision(case):
    other = store_thread(case.w, actor=case.user)
    drawer = call(
        case.w,
        case.actor,
        "add_drawer",
        wing="shared",
        room=other.id,
        content="other filer's private words",
    )["drawer_id"]
    source, rid = roomless(case, drawer=drawer)
    call(case.w, case.owner, "delete_by_source", source_file=source, dry_run=False)
    assert survives(case, rid)  # RED


@pytest.mark.parametrize("role", ["owner", "member"])
def test_card228_r27b_delete_by_source_keeps_hidden_drawer_despite_readable_token(case, role):
    actor_user = case.w.me["user"] if role == "owner" else case.user
    hidden_user = case.user if role == "owner" else case.w.me["user"]
    visible = store_thread(case.w, actor=actor_user, source=f"import-private:{actor_user}:visible")
    other = store_thread(case.w, actor=hidden_user, source=f"import-private:{hidden_user}:other")
    creator = case.actor if role == "owner" else case.owner
    drawer = call(
        case.w,
        creator,
        "add_drawer",
        wing="shared",
        room=other.id,
        content="hidden association words",
    )["drawer_id"]
    source, rid = roomless(case, source="/x/" + visible.id + ".md", drawer=drawer)
    if role == "owner":
        call(case.w, case.owner, "delete_by_source", source_file=source, dry_run=False)
    else:
        refused(case.w, case.actor, "mempalace_delete_by_source", source_file=source, dry_run=False)
    assert survives(case, rid)  # RED


def test_card228_r27c_delete_by_source_keeps_encoded_hidden_token_row(case):
    visible = store_thread(case.w, actor=case.user)
    source, rid = roomless(
        case, source="/x/" + visible.id + "/%74%68%72%5f" + case.room[4:] + ".md"
    )
    refused(case.w, case.actor, "mempalace_delete_by_source", source_file=source, dry_run=False)
    assert survives(case, rid)  # RED


def test_card228_r28_owner_delete_by_source_keeps_unassociated_roomless_row(case):
    source, rid = roomless(case)
    call(case.w, case.owner, "delete_by_source", source_file=source, dry_run=False)
    assert survives(case, rid)  # RED


@pytest.mark.parametrize("variant", ["underscore", "percent", "ascii_case"])
def test_card228_r29_member_private_family_refusal(case, variant):
    base, sibling = {
        "underscore": ("import-private:usr_1:g", "import-private:usrX1:g"),
        "percent": ("capture:a%:g", "capture:abc:g"),
        "ascii_case": ("capture:Ab:g", "capture:ab:g"),
    }[variant]
    store_thread(case.w, actor=case.user, source=sibling + "#sha256:1")
    cite(case, source_thread=base)
    assert base not in json.dumps(query(case, case.actor))  # RED


def legacy(c, field, value):
    tid = (
        c.kg.add_triple(
            "SyntheticProject",
            "deploy_port",
            "legacy",
            wing="shared",
            **{field: value},
            source_thread=None if field == "source_thread" else "ext-neutral",
        )
        if field != "source_thread"
        else c.kg.add_triple(
            "SyntheticProject", "deploy_port", "legacy", wing="shared", source_thread=value
        )
    )
    # Verify storage before observing both public response paths.
    conn = c.kg._conn()
    stored = dict(conn.execute("SELECT * FROM triples WHERE id=?", (tid,)).fetchone())
    sources = c.kg.sources([tid])
    assert value in json.dumps([stored, sources])
    replies = [query(c, c.actor), call(c.w, c.actor, "kg_timeline", entity="SyntheticProject")]
    return json.dumps(replies)


def test_card228_r30_legacy_source_file_is_scrubbed(case):
    assert case.room not in legacy(case, "source_file", case.room)  # RED


def test_card228_r31_legacy_source_closet_is_scrubbed(case):
    assert case.room not in legacy(
        case, "source_closet", "closet_private_threads_" + case.room + "_abc"
    )  # RED


def test_card228_r32_legacy_private_source_thread_is_scrubbed(case):
    source = f"import-private:{case.w.me['user']}:legacy"
    store_thread(case.w, source=source)
    reply = legacy(case, "source_thread", source)
    assert source not in reply and case.room not in reply  # RED


def test_card228_r33_legacy_t3_source_url_is_scrubbed(case):
    assert case.room not in legacy(case, "source_url", "t3-thread://v1/" + case.room)  # RED


def test_card228_g01_owner_invalidates_and_supersedes_own_thread_fact(case):
    hidden(case)
    first = mutate(case, "kg_invalidate", actor=case.owner, ended="2026-02-01")
    hidden(case, "second")
    second = mutate(case, "kg_supersede", actor=case.owner, old_object="second", at="2026-03-01")
    assert first.get("closed") == 1 and second.get("success")


def test_card228_g02_owner_kg_save_merges_own_thread_fact(case):
    kept = hidden(case)
    assert mutate(case, "kg_save", actor=case.owner).get("triple_id") == kept["triple_id"]


def test_card228_g03_member_mutates_visible_fact_in_same_wing(case):
    own = store_thread(case.w, actor=case.user)
    save(case, actor=case.actor, source_thread=own.id)
    assert mutate(case, "kg_invalidate").get("closed") == 1


def test_card228_g04_owner_deletes_own_mirror_copies_by_source(case):
    result = call(
        case.w,
        case.owner,
        "delete_by_source",
        source_file=case.record["source_file"],
        dry_run=False,
    )
    assert result.get("deleted", 0) > 0


def test_card228_g05_owner_deletes_thread_tunnel_and_hallway(case):
    tid = tunnel(case)
    # Every room of a mirror hallway must be readable, including the control room.
    hid = hallway(case, "shared")
    assert call(case.w, case.owner, "delete_tunnel", tunnel_id=tid) == {"deleted": tid} and call(
        case.w, case.owner, "delete_hallway", hallway_id=hid
    ) == {"deleted": True}
    assert all(t["id"] != tid for t in call(case.w, case.owner, "list_tunnels"))
    assert all(h["id"] != hid for h in hallways.list_hallways())


def test_card228_g06_owner_taxonomy_keeps_own_thread_room(case):
    assert case.room in json.dumps(call(case.w, case.owner, "get_taxonomy"))


def test_card228_g07_owner_kg_add_and_fact_save_merge_own_thread_fact(case):
    kept = hidden(case)
    added = mutate(case, "kg_add", actor=case.owner)
    reply = call(
        case.w,
        case.owner,
        "fact_save",
        subject="SyntheticProject",
        predicate="deploy_port",
        object="old",
        theme="shared",
        source={"thread": case.room},
    )
    assert added.get("triple_id") == kept["triple_id"] and not reply.get("error")


def test_card228_g08_member_mutates_fact_with_one_readable_source(case):
    hidden(case)
    own = store_thread(case.w, actor=case.user)
    # Legacy multi-source setup: neither filer gains authority to cite the other's thread.
    case.kg.add_triple(
        "SyntheticProject", "deploy_port", "old", wing="shared", source_thread=own.id
    )
    assert mutate(case, "kg_invalidate").get("closed") == 1


def test_card228_g09_owner_single_valued_save_closes_rivals_across_pages(case, monkeypatch):
    from mempalace import knowledge_graph as mod

    monkeypatch.setattr(mod, "_PROBE_PAGE", 2, raising=False)
    calls = []
    original = case.w.gateway.mediator.workers.call_tool

    def recording(principal, name, args):
        if args.get("thread_scope", {}).get("probe"):
            calls.append(args)
        return original(principal, name, args)

    monkeypatch.setattr(case.w.gateway.mediator.workers, "call_tool", recording)
    for i in range(5):
        save(case, str(i), source_thread=case.room)
    calls.clear()
    save(case, "new", single_valued=True, valid_from="2026-02-01")
    # The pin has no probe interface; the behavioral guard remains meaningful there.
    assert all(not f["current"] for f in query(case)["facts"] if f["object"] != "new") and (
        len(calls) >= 2 if hasattr(mod.KnowledgeGraph, "mutation_candidates") else True
    )


def test_card228_g10_owner_deletes_drawer_endpoint_tunnel(case):
    tid = tunnel(case, True)
    call(case.w, case.owner, "delete_tunnel", tunnel_id=tid)
    assert not any(t["id"] == tid for t in call(case.w, case.owner, "list_tunnels"))


def test_card228_g11_owner_list_rooms_keeps_own_thread_room(case):
    assert case.room in json.dumps(call(case.w, case.owner, "list_rooms"))


def test_card228_g12_owner_merge_cites_own_thread(case):
    save(case, wing="shared")
    save(case, wing="shared", source_thread=case.room)
    assert case.room in json.dumps(query(case))


def orphan_rows(c, roomless_rows=False):
    source = "/x/" + c.room + ".md"
    visible = call(
        c.w,
        c.owner,
        "add_drawer",
        wing="shared",
        room="notes",
        content="visible orphan control",
        source_file=source,
    )
    drawer = visible["drawer_id"] if roomless_rows else "deleted-drawer"
    meta = {"source_file": source, **({} if roomless_rows else {"wing": "shared", "room": "notes"})}
    rid = append_drawer_revision(c.palace, drawer, "old revision", meta)
    aaak = get_aaak_collection(c.palace, create=True)
    aaak.upsert(ids=[drawer], documents=["synthetic abbreviation"], metadatas=[meta])
    if roomless_rows:
        closets = get_closets_collection(c.palace, create=True)
        closets.upsert(
            ids=["roomless-closet"], documents=["pointer"], metadatas=[{"source_file": source}]
        )
    call(c.w, c.owner, "delete_by_source", source_file=source, dry_run=False)
    return (
        not survives(c, rid)
        and not aaak.get(ids=[drawer])["ids"]
        and (not roomless_rows or not closets.get(ids=["roomless-closet"])["ids"])
    )


def test_card228_g13_owner_delete_by_source_purges_orphan_aaak_and_revisions(case):
    assert orphan_rows(case)


def test_card228_g14_owner_delete_by_source_purges_roomless_rows(case):
    assert orphan_rows(case, True)


def test_card228_g15_owner_cites_own_drawer(case):
    result = save(case, wing="shared", source_drawer_id=case.record["drawer_id"])
    assert result.get("success") and case.record["drawer_id"] in json.dumps(query(case))


def test_card228_g16_owner_cites_own_thread_link(case):
    save(case, wing="shared", source_url="t3-thread://v1/" + case.room)
    assert case.room in json.dumps(query(case))


def test_card228_g17_owner_cites_own_private_source_id(case):
    stored = store_thread(case.w)
    save(case, wing="shared", source_thread=stored.source_thread_id)
    assert stored.source_thread_id in json.dumps(query(case))


def test_card228_g18_owner_padded_own_thread_attaches(case):
    save(case, wing="shared", source_thread=" " + case.room + " ")
    assert case.room in json.dumps(query(case))


def test_card228_g19_owner_own_thread_in_source_file(case):
    save(case, wing="shared", source_file="/x/" + case.room + ".md")
    assert case.room in json.dumps(query(case))


def test_card228_g20_owner_own_closet_id_attaches(case):
    cid = closet(case)
    result = call(
        case.w,
        case.owner,
        "kg_add",
        subject="SyntheticProject",
        predicate="deploy_port",
        object="old",
        wing="shared",
        source_closet=cid,
    )
    assert result.get("success") and cid in json.dumps(query(case))


def test_card228_g21_unscoped_drawer_delete_purges_roomless_revision(case):
    from mempalace.mcp_server import tool_delete_drawer

    rid = append_drawer_revision(case.palace, case.record["drawer_id"], "old", {})
    tool_delete_drawer(case.record["drawer_id"])
    assert not survives(case, rid)


def test_card228_g22_owner_cites_sha256_variant_base_id(case):
    source = f"import-private:{case.w.me['user']}:variant"
    store_thread(case.w, source=source + "#sha256:" + "a" * 64)
    save(case, wing="shared", source_thread=source)
    assert source in json.dumps(query(case))


@pytest.mark.parametrize("variant", ["t3_pct2f", "app_pct2f"])
def test_card228_g23_owner_encoded_own_link_attaches(case, variant):
    url = ("t3-thread://v1/" if variant == "t3_pct2f" else "/app/threads/") + case.room + "%2Fx"
    save(case, wing="shared", source_url=url)
    assert url in json.dumps(query(case))


def test_card228_g24_owner_double_encoded_own_thread_attaches(case):
    # Choose a real readable id whose first hex character needs two decode rounds.
    encoded = "thr_%25" + format(ord(case.room[4]), "02x") + case.room[5:]
    save(case, wing="shared", source_thread=encoded)
    assert encoded in json.dumps(query(case))


def test_card228_g25_owner_private_family_is_literal(case):
    source = f"import-private:{case.w.me['user']}:literal"
    store_thread(case.w, actor=case.user, source=source.replace("_", "X") + "#sha256:1")
    store_thread(case.w, source=source + "#sha256:2")
    save(case, wing="shared", source_thread=source)
    assert source in json.dumps(query(case))


def other_filer_case(c):
    """Seed a member-filed shared drawer unreadable to the existing owner."""
    other = store_thread(c.w, actor=c.user, source=f"import-private:{c.user}:other-copy")
    source = f"handoff:{other.id}#v2:0-0:conversation:0"
    result = call(
        c.w,
        c.actor,
        "add_drawer",
        wing="shared",
        room=other.id,
        content="other filer's exact hidden words",
        source_file=source,
    )
    assert result.get("success"), result
    record = {"drawer_id": result["drawer_id"], "room": other.id, "source_file": source}
    assert "other filer's" in call(c.w, c.actor, "get_drawer", drawer_id=record["drawer_id"]).get(
        "content", ""
    )
    assert "other filer's" not in call(
        c.w, c.owner, "get_drawer", drawer_id=record["drawer_id"]
    ).get("content", "")
    return SimpleNamespace(**{**vars(c), "room": other.id, "record": record})


def test_card228_r08_owner_cannot_delete_other_filer_mirror(case):
    c = other_filer_case(case)
    call(c.w, c.owner, "delete_by_source", source_file=c.record["source_file"], dry_run=False)
    assert "other filer's" in call(c.w, c.actor, "get_drawer", drawer_id=c.record["drawer_id"]).get(
        "content", ""
    ), "Other filer's hidden drawer was deleted"


def test_card228_r09_owner_hidden_source_dry_run_is_no_oracle(case):
    c = other_filer_case(case)
    actual = call(c.w, c.owner, "delete_by_source", source_file=c.record["source_file"])
    absent = call(
        c.w, c.owner, "delete_by_source", source_file="handoff:thr_ffff#v2:0-0:conversation:0"
    )
    actual.pop("source_file", None)
    absent.pop("source_file", None)
    assert actual == absent, "Hidden source leaked into dry-run counts"


def test_card228_r10_owner_cannot_delete_other_filer_thread_tunnel(case):
    c = other_filer_case(case)
    tid = tunnel(c, actor=c.actor)
    call(c.w, c.owner, "delete_tunnel", tunnel_id=tid)
    assert any(t["id"] == tid for t in call(c.w, c.actor, "list_tunnels")), (
        "Other filer's hidden tunnel was deleted"
    )


@pytest.mark.parametrize("kind", ["mirror", "shared"])
def test_card228_r11_owner_cannot_delete_other_filer_thread_hallway(case, kind):
    c = other_filer_case(case)
    hid = hallway(c, "private_threads" if kind == "mirror" else "shared")
    call(c.w, c.owner, "delete_hallway", hallway_id=hid)
    assert any(h["id"] == hid for h in hallways.list_hallways()), (
        "Other filer's hidden hallway was deleted"
    )


def test_card228_r17_owner_cannot_delete_other_filer_drawer_tunnel(case):
    c = other_filer_case(case)
    tid = tunnel(c, drawer=True, actor=c.actor)
    call(c.w, c.owner, "delete_tunnel", tunnel_id=tid)
    assert any(t["id"] == tid for t in call(c.w, c.actor, "list_tunnels")), (
        "Other filer's hidden drawer tunnel was deleted"
    )


def test_card228_r27c_owner_delete_keeps_encoded_other_filer_token(case):
    c = other_filer_case(case)
    visible = store_thread(c.w)
    source, rid = roomless(c, source="/x/" + visible.id + "/%74%68%72%5f" + c.room[4:] + ".md")
    call(c.w, c.owner, "delete_by_source", source_file=source, dry_run=False)
    assert survives(c, rid), "Encoded hidden token's roomless revision was deleted"
