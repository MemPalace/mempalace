"""Rooms and topics naming a thread reveal it only to readers of that thread."""

import json
import logging
import re
import secrets
import threading
from types import SimpleNamespace

import pytest

from commonplace import sessions
from commonplace.handoff import mirror_access, provenance_refs
from commonplace.handoff.models import ThreadMessage
from tests._commonplace_helpers import hub_fixture  # noqa: F401


def deep_encoded(identifier, layers=4):
    """Each layer hides a fullwidth percent sign behind one more decode/NFKC round."""
    value = "\uff05" + "74" + identifier[1:]
    for _ in range(layers):
        value = value.replace("\uff05", "\uff05EF\uff05BC\uff0585")
    return value


UNPROVABLE = {"undecodable", "deep_encoded"}
TAB, NEWLINE, ZWSP = "\t", "\n", "\u200b"
# form -> (stored source from "probe-<hex>", room from the stored thread).
# Filing now refuses every one of these sources except the canonical control F;
# rows stored before that are matched by the resolver's own extraction.
LEGACY_FORMS = {
    "mcp_source_r7_a_xmcp_suffix": (
        lambda p: "xmcp:" + p,
        lambda t: "mcp:" + probe_id(t) + "_notes",
    ),
    "mcp_source_r7_b_xmcp_variant": (
        lambda p: "xmcp:" + p + "#sha256:0123abcd",
        lambda t: "mcp:" + probe_id(t),
    ),
    "mcp_source_r7_c_xmcp_variant_room": (
        lambda p: "xmcp:" + p,
        lambda t: "mcp:" + probe_id(t) + "#sha256:0123abcd",
    ),
    "mcp_source_r7_d_space_suffix": (
        lambda p: " mcp:" + p,
        lambda t: "mcp:" + probe_id(t) + "_notes",
    ),
    "mcp_source_r7_e_encoded_suffix": (
        lambda p: "mcp:" + p + "%2Ftail",
        lambda t: "mcp:" + probe_id(t) + "/tail_notes",
    ),
    "mcp_source_r7_f_canonical_encoded_room": (
        lambda p: "mcp:" + p + "/tail",
        lambda t: "mcp:" + probe_id(t) + "%2Ftail_notes",
    ),
    "mcp_source_r7_g_trailing_tab": (
        lambda p: "mcp:" + p + TAB,
        lambda t: "mcp:" + probe_id(t),
    ),
    "mcp_source_r7_h_trailing_newline": (
        lambda p: "mcp:" + p + NEWLINE,
        lambda t: "mcp:" + probe_id(t),
    ),
    "mcp_source_r7_i_encoded_variant": (
        lambda p: "mcp:" + p + "%23sha256%3A0123abcd",
        lambda t: "mcp:" + probe_id(t),
    ),
    "mcp_source_r7_o_zero_width_namespace": (
        lambda p: "m" + ZWSP + "cp:" + p,
        lambda t: "mcp:" + probe_id(t),
    ),
    "mcp_source_r7_r1_upper_marker": (
        lambda p: "mcp:" + p + "#SHA256:0123abcd",
        lambda t: "mcp:" + probe_id(t),
    ),
    "mcp_source_r7_v_leading_tab": (
        lambda p: TAB + "mcp:" + p,
        lambda t: "mcp:" + probe_id(t),
    ),
    "mcp_source_r7_x_encoded_tab": (
        lambda p: "mcp:" + p + "%09",
        lambda t: "mcp:" + probe_id(t),
    ),
}
SOURCE_CHARS = {
    "space": " ",
    "slash": "/",
    "query": "?",
    "hash": "#",
    "amp": "&",
    "dquote": '"',
    "squote": "'",
    "lt": "<",
    "gt": ">",
    "backslash": "\\",
}
FULLWIDTH_THR = "\uff54\uff48\uff52"
FORMS = {
    "bare": lambda t: t.id,
    "app_path": lambda t: f"/app/threads/{t.id}",
    "app_url": lambda t: f"https://example.test/app/threads/{t.id}/messages/3",
    "t3_link": lambda t: f"t3-thread://v1/{t.id}",
    "mcp_prefix": lambda t: f"mcp:{t.id}",
    "percent": lambda t: "/app/threads/" + t.id.replace("_", "%5F"),
    "underscore_suffix": lambda t: f"{t.id}_notes",
    "underscore_prefix": lambda t: f"notes_{t.id}",
    "letter_suffix": lambda t: f"{t.id}g",
    "letter_prefix": lambda t: f"x{t.id}",
    "fullwidth": lambda t: FULLWIDTH_THR + t.id[3:],
    "upper_case": lambda t: t.id.upper(),
    "mcp_source": lambda t: t.source_thread_id,
    # Stored sources containing each character the old token scan stopped at;
    # the room is the whole source, so only the full value resolves it.
    **{f"mcp_source_{name}": (lambda t: t.source_thread_id) for name in SOURCE_CHARS},
    "mcp_source_underscore": lambda t: f"{t.source_thread_id}_notes",
    "mcp_source_letter": lambda t: f"{t.source_thread_id}x",
    # The stored namespace in another spelling; rooms resolve it the same way.
    "mcp_source_upper_namespace": lambda t: t.source_thread_id,
    "mcp_source_mixed_namespace": lambda t: "MCP:" + t.source_thread_id[4:],
    "mcp_source_fullwidth_namespace": lambda t: "mcp:" + t.source_thread_id[4:],
    "mcp_source_fullwidth_room": lambda t: "\uff4d\uff43\uff50\uff1a" + t.source_thread_id[4:],
    # A bare stored "mcp:" is a prefix of every mcp: room, so it fails closed.
    "mcp_source_bare_namespace": lambda t: "mcp:probe-" + t.id[4:],
    # Mixed-width stored namespaces (ASCII "m" with fullwidth letters or colon).
    "mcp_source_ascii_m_fullwidth_cp": lambda t: "mcp:" + probe_id(t),
    "mcp_source_ascii_mc_fullwidth_p": lambda t: "mcp:" + probe_id(t),
    "mcp_source_ascii_m_fullwidth_cp_colon": lambda t: "mcp:" + probe_id(t),
    # A stored source ending inside the hash marker still prefixes the room.
    "mcp_source_trailing_hash": lambda t: "mcp:" + probe_id(t) + "#sha256:0123abcd",
    # A fullwidth number sign in the stored variant marker is a variant after NFKC.
    "mcp_source_fullwidth_hash_variant": lambda t: "mcp:" + probe_id(t),
    # Sources whose mcp: is not the leading namespace; the room equals the source.
    "mcp_source_xmcp": lambda t: t.source_thread_id,
    "mcp_source_leading_space": lambda t: t.source_thread_id,
    "mcp_source_leading_nbsp": lambda t: t.source_thread_id,
    "mcp_source_percent_namespace": lambda t: t.source_thread_id,
    # The room names the same extracted mcp: remainder as a non-leading source.
    "mcp_source_xmcp_canonical_room": lambda t: "mcp:" + probe_id(t),
    # Legacy stored sources (filed before IDs were checked), resolved by key.
    **{form: room for form, (_, room) in LEGACY_FORMS.items()},
    # A percent sign behind NFKC: decoding must repeat until the value is stable.
    "double_encoded": lambda t: "%EF%BC%85" + "74" + t.id[1:],
    # Still changing after the decode/NFKC round bound: hidden from every reader.
    "deep_encoded": lambda t: deep_encoded(t.id),
    # Undecodable values are hidden from every reader: no thread can be proved.
    "undecodable": lambda t: f"/app/threads/%FF{t.id}",
}
# Room-name validation refuses slashes, colons and percent signs on write.
WRITABLE = [
    "bare",
    "underscore_suffix",
    "underscore_prefix",
    "letter_suffix",
    "letter_prefix",
    "fullwidth",
    "upper_case",
]
DRAWER = "thread-room-probe"
TEXT = "Shared exact words about the plan"
DIARY_TEXT = "Diary words about the plan"


def secret(thread, form):
    """The identity that must not reach a non-reader, in any spelling."""
    if form == "mcp_source_bare_namespace":
        return "probe-" + thread.id[4:]
    if form.startswith("mcp_source"):
        # The ASCII-safe part: JSON escapes quotes and backslashes in the rest.
        return re.search(r"probe-[0-9a-f]+", thread.source_thread_id)[0]
    return thread.id[4:].upper() if form == "upper_case" else thread.id[4:]


def probe_id(thread):
    return re.search(r"probe-[0-9a-f]+", thread.source_thread_id)[0]


# Stored source spellings, built from "probe-<hex>".
SOURCE_SPELLINGS = {
    "mcp_source_ascii_m_fullwidth_cp": lambda p: "m\uff43\uff50:" + p,
    "mcp_source_ascii_mc_fullwidth_p": lambda p: "mc\uff50:" + p,
    "mcp_source_ascii_m_fullwidth_cp_colon": lambda p: "m\uff43\uff50\uff1a" + p,
    "mcp_source_trailing_hash": lambda p: "mcp:" + p + "#",
    "mcp_source_fullwidth_hash_variant": lambda p: "mcp:" + p + "\uff03sha256:abc",
    "mcp_source_xmcp": lambda p: "xmcp:" + p,
    "mcp_source_leading_space": lambda p: " mcp:" + p,
    "mcp_source_leading_nbsp": lambda p: "\u00a0mcp:" + p,
    "mcp_source_percent_namespace": lambda p: "%6dcp:" + p,
    "mcp_source_xmcp_canonical_room": lambda p: "xmcp:" + p,
    **{form: source for form, (source, _) in LEGACY_FORMS.items()},
}
NAMESPACE_SOURCES = {
    "mcp_source_upper_namespace": "MCP:",
    "mcp_source_mixed_namespace": "Mcp:",
    "mcp_source_fullwidth_namespace": "\uff4d\uff43\uff50:",
    "mcp_source_fullwidth_room": "mcp:",
}


def store_thread(hub, form, *, actor, wing, source=None):
    probe = f"mcp:probe-{secrets.token_hex(6)}"
    char = SOURCE_CHARS.get(form.removeprefix("mcp_source_"))
    if form in SOURCE_SPELLINGS:
        source = source or SOURCE_SPELLINGS[form](probe[4:])
    elif form in NAMESPACE_SOURCES:
        source = source or NAMESPACE_SOURCES[form] + probe[4:]
    elif form == "mcp_source_bare_namespace":
        source = source or "mcp:"
    source = source or (
        probe + char + "tail"
        if form.startswith("mcp_source") and char is not None
        else probe
        if form.startswith("mcp_source")
        else f"capture:{actor}:room-{secrets.token_hex(6)}"
    )
    return hub.gateway.handoff.store_for(hub.workspace).store(
        hub.workspace,
        source,
        [ThreadMessage(0, "user", "message", "Thread exact words", "Thread exact words")],
        actor=actor,
        wing=wing,
    )


def member(hub):
    uid = hub.db.create_user("thread-room-member@example.test")
    hub.db.add_member(hub.workspace, uid, "member", actor=hub.admin)
    link = sessions.create_login_link(hub.db, "thread-room-member@example.test", hub.workspace)
    return {"Cookie": f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, link)}"}


def owner(hub):
    link = sessions.create_login_link(hub.db, hub.db.user(hub.admin)["email"], hub.workspace)
    return {"Cookie": f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, link)}"}


@pytest.fixture(autouse=True)
def palace_files(config, monkeypatch, tmp_path):
    """Tunnels and hallways resolve their files from the test palace."""
    from mempalace import hallways, palace_graph

    monkeypatch.setattr(palace_graph, "MempalaceConfig", lambda: config)
    monkeypatch.setattr(hallways, "_get_hallway_file", lambda *a, **k: str(tmp_path / "h.json"))
    monkeypatch.setattr(hallways, "_legacy_hallway_file", lambda: str(tmp_path / "legacy.json"))


SCOPES = ["memory:read", "memory:write", "memory:delete", "private:read", "private:write"]


def agent(hub, name, scopes, wings=("*",)):
    """An owner-role agent; thread reads depend only on its scopes, wings and filer."""
    uid = hub.db.create_user(f"{name}@example.test")
    hub.db.add_member(hub.workspace, uid, "owner", actor=hub.admin)
    token = hub.token(
        name,
        user=uid,
        profile="full",
        scopes=scopes,
        read_wings=list(wings),
        write_wings=list(wings),
        trust="trusted",
    )
    principal = hub.db.authenticate_token(token)
    assert principal.policy.sees_all_wings is (list(wings) == ["*"])
    return uid, token, principal


@pytest.fixture
def blind(hub):
    """Sees every wing but cannot read any thread."""
    return agent(hub, "thread-blind", SCOPES)


@pytest.fixture(autouse=True)
def media_route(monkeypatch):
    """Let unified media/code search run on the test palace and embedder."""
    from mempalace import embedding
    from mempalace.config import MempalaceConfig
    from mempalace.searcher import media_search

    class MediaConfig(MempalaceConfig):
        @property
        def embedding_model(self):
            return "embeddinggemma2"

    monkeypatch.setattr(media_search, "MempalaceConfig", MediaConfig)
    original = embedding.get_embedding_function

    class CodeQueries:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def embed_code_query(self, input):
            return self.inner.embed_query(input)

    monkeypatch.setattr(embedding, "get_embedding_function", lambda: CodeQueries(original()))


@pytest.fixture
def reader(hub):
    """Sees every wing and reads its own threads."""
    return agent(hub, "thread-reader", [*SCOPES, "threads:read"])


def hidden_thread(hub, blind, form):
    thread = store_thread(hub, form, actor=hub.admin, wing="private_chats")
    assert not hub.gateway.handoff.recall.can_read(blind[2], thread.id)
    return thread


def readable_thread(hub, reader, form):
    thread = store_thread(hub, form, actor=reader[0], wing="shared")
    assert hub.gateway.handoff.recall.can_read(reader[2], thread.id)
    return thread


def seed(collection, config, room):
    """One room value in drawers, a diary topic, a tunnel and a hallway."""
    from mempalace import hallways, palace_graph

    collection.add(
        ids=[DRAWER, "probe-other", "probe-general", "probe-other-general", "probe-diary"],
        documents=[
            TEXT,
            "Other plan words",
            "General words",
            "Other general words",
            DIARY_TEXT,
        ],
        metadatas=[
            {"wing": "shared", "room": room},
            {"wing": "other", "room": room},
            {"wing": "shared", "room": "general"},
            {"wing": "other", "room": "general"},
            {
                "wing": "shared",
                "room": "diary",
                "hall": "hall_diary",
                "topic": room,
                "type": "diary_entry",
                "agent": "probe",
                "filed_at": "2026-11-10T00:00:00Z",
                "date": "2026-11-10",
            },
        ],
    )
    palace_graph._save_tunnels(
        [
            {
                "id": "probe-tunnel",
                "source": {"wing": "shared", "room": room},
                "target": {"wing": "other", "room": "general"},
                "label": "probe",
            }
        ],
        config,
    )
    hallways._save_hallways(
        [
            {
                "id": "probe-hallway",
                "wing": "shared",
                "rooms": [room],
                "label": "probe",
                "co_occurrence_count": 2,
            }
        ],
        config,
    )


def rest(hub, headers, path):
    return hub.raw(None, b"", method="GET", path=path, headers=headers)


def mcp(hub, token, name, **args):
    response = hub.rpc(token, "tools/call", {"name": name, "arguments": args})
    return json.dumps(response, ensure_ascii=False)


READS = {
    "get_drawer": ("mempalace_get_drawer", {"drawer_id": DRAWER}),
    "get_drawers": ("mempalace_get_drawers", {"drawer_ids": [DRAWER]}),
    "search": ("mempalace_search", {"query": "plan"}),
    "search_cli": ("mempalace_search", {"query": "plan", "cli_compatible": True}),
    "list_drawers": ("mempalace_list_drawers", {"wing": "shared"}),
    "list_rooms": ("mempalace_list_rooms", {"wing": "shared"}),
    "taxonomy": ("mempalace_get_taxonomy", {}),
    "status": ("mempalace_status", {}),
    "list_tunnels": ("mempalace_list_tunnels", {}),
    "follow_tunnels": ("mempalace_follow_tunnels", {"wing": "other", "room": "general"}),
    "traverse": ("mempalace_traverse", {"start_room": "general"}),
    "find_tunnels": ("mempalace_find_tunnels", {}),
    "graph_stats": ("mempalace_graph_stats", {}),
    "list_hallways": ("mempalace_list_hallways", {}),
    "check_duplicate": ("mempalace_check_duplicate", {"content": TEXT, "threshold": 0.1}),
    "diary_read": ("mempalace_diary_read", {"agent_name": "probe", "wing": "shared"}),
}


@pytest.mark.parametrize("route", sorted(READS))
@pytest.mark.parametrize("form", sorted(FORMS))
def test_thread_blind_reader_never_receives_thread_identity(
    hub, collection, config, blind, form, route
):
    thread = hidden_thread(hub, blind, form)
    seed(collection, config, FORMS[form](thread))
    name, args = READS[route]
    raw = mcp(hub, blind[1], name, **args)
    assert secret(thread, form) not in raw, raw


@pytest.mark.parametrize("route", sorted(READS))
@pytest.mark.parametrize(
    "form",
    [
        "bare",
        "app_path",
        "underscore_suffix",
        "fullwidth",
        "upper_case",
        "mcp_source",
        "mcp_source_slash",
        "mcp_source_upper_namespace",
        "mcp_source_fullwidth_namespace",
        "mcp_source_bare_namespace",
        "mcp_source_ascii_m_fullwidth_cp",
        "mcp_source_trailing_hash",
        "mcp_source_fullwidth_hash_variant",
        "mcp_source_xmcp",
        "mcp_source_percent_namespace",
        "mcp_source_xmcp_canonical_room",
        "mcp_source_r7_a_xmcp_suffix",
        "mcp_source_r7_e_encoded_suffix",
        "mcp_source_r7_g_trailing_tab",
        "double_encoded",
    ],
)
def test_thread_reader_keeps_room_on_every_route(hub, collection, config, reader, form, route):
    thread = readable_thread(hub, reader, form)
    seed(collection, config, FORMS[form](thread))
    name, args = READS[route]
    raw = mcp(hub, reader[1], name, **args)
    assert secret(thread, form) in raw, raw


@pytest.mark.parametrize("route", ["/api/memory/" + DRAWER, "/api/memory/search?q=plan"])
@pytest.mark.parametrize("form", sorted(FORMS))
def test_member_rest_never_receives_thread_identity(hub, collection, config, blind, form, route):
    headers = member(hub)
    thread = hidden_thread(hub, blind, form)
    seed(collection, config, FORMS[form](thread))
    _, raw = rest(hub, headers, route)
    assert secret(thread, form) not in raw, raw


WRITES = {
    "add_drawer": lambda r: ("mempalace_add_drawer", {"wing": "shared", "room": r, "content": "x"}),
    "update_drawer": lambda r: (
        "mempalace_update_drawer",
        {"drawer_id": "probe-general", "room": r},
    ),
    "checkpoint_item": lambda r: (
        "mempalace_checkpoint",
        {"items": [{"wing": "shared", "room": r, "content": "Checkpoint words"}]},
    ),
    "checkpoint_diary": lambda r: (
        "mempalace_checkpoint",
        {"items": [], "diary": {"agent_name": "probe", "entry": "x", "topic": r}},
    ),
    "create_tunnel": lambda r: (
        "mempalace_create_tunnel",
        {"source_wing": "shared", "source_room": r, "target_wing": "other", "target_room": "x"},
    ),
    "diary_write": lambda r: (
        "mempalace_diary_write",
        {"agent_name": "probe", "entry": "x", "topic": r, "wing": "shared"},
    ),
}


@pytest.mark.parametrize("route", sorted(WRITES))
@pytest.mark.parametrize("form", WRITABLE)
def test_thread_blind_writer_cannot_file_into_thread_room(
    hub, collection, config, blind, form, route
):
    thread = hidden_thread(hub, blind, form)
    seed(collection, config, "general")
    name, args = WRITES[route](FORMS[form](thread))
    raw = mcp(hub, blind[1], name, **args)
    assert "Room not found" in raw, raw


@pytest.mark.parametrize("form", sorted(FORMS))
def test_thread_blind_writer_cannot_delete_thread_room_drawer(hub, collection, config, blind, form):
    thread = hidden_thread(hub, blind, form)
    seed(collection, config, FORMS[form](thread))
    raw = mcp(hub, blind[1], "mempalace_delete_drawer", drawer_id=DRAWER)
    assert collection.get(ids=[DRAWER])["ids"] == [DRAWER], raw


@pytest.mark.parametrize("form", sorted(set(FORMS) - UNPROVABLE))
def test_owner_session_reads_thread_room_unchanged(hub, collection, config, form):
    thread = store_thread(hub, form, actor=hub.admin, wing="private_chats")
    room = FORMS[form](thread)
    seed(collection, config, room)
    headers = owner(hub)
    status, raw = rest(hub, headers, f"/api/memory/{DRAWER}")
    assert status == 200, raw
    assert json.loads(raw)["topic"] == room
    status, raw = rest(hub, headers, "/api/memory/search?q=plan")
    assert status == 200, raw
    assert room in [hit["topic"] for hit in json.loads(raw)["results"]]


@pytest.mark.parametrize("room", ["general", "thr_notes", "thr_deadline", "mcp-notes"])
def test_ordinary_rooms_unchanged(hub, collection, config, blind, room):
    seed(collection, config, room)
    status, raw = rest(hub, member(hub), f"/api/memory/{DRAWER}")
    assert status == 200, raw
    assert json.loads(raw)["topic"] == room
    got = mcp(hub, blind[1], "mempalace_get_drawer", drawer_id=DRAWER)
    assert json.loads(json.loads(got)["result"]["content"][0]["text"])["room"] == room


def test_room_references_names_every_form():
    class Thread:
        id = "thr_" + "ab12" * 4
        source_thread_id = "mcp:probe-1"

    for form, make in FORMS.items():
        if form in UNPROVABLE:
            assert provenance_refs.room_references(make(Thread)) is None, form
            continue
        names = provenance_refs.room_references(make(Thread))
        if form == "mcp_source_bare_namespace":
            assert any(n.startswith("mcp:") for n in names), form
        elif form.startswith("mcp_source"):
            assert any(n.startswith(Thread.source_thread_id) for n in names), form
        else:
            assert Thread.id in names, form
    for ordinary in ("general", "thr_notes", "thr_deadline", "thr_abcdefg", None):
        assert provenance_refs.room_references(ordinary) == (), ordinary
    # Values that cannot be decoded name a thread nobody can prove readable.
    for undecodable in (
        "%FF" + Thread.id,
        "thr%252525255Fab12",
        "thr\u200b_ab12",  # zero-width space
    ):
        assert provenance_refs.room_references(undecodable) is None, undecodable
    # The old thread_ids alias is gone on purpose: a tuple cannot say
    # "undecodable", so returning () would let a caller show such a value.
    # Callers decide visibility through mirror_access.private_thread_reference.
    assert not hasattr(mirror_access, "thread_ids")
    # A source ID is the whole remaining value, whatever characters it holds,
    # and an upper-case thread ID names the stored lower-case thread.
    assert provenance_refs.room_references("mcp:github.com/o/r") == ("mcp:github.com/o/r",)
    assert provenance_refs.room_references("THR_" + "AB12" * 4) == ("thr_" + "ab12" * 4,)


@pytest.mark.parametrize("route", sorted(READS))
def test_partial_source_family_reader_never_receives_source(hub, collection, config, reader, route):
    """An mcp: room is shown only to readers of every thread filed under its source."""
    source = f"mcp:probe-{secrets.token_hex(6)}"
    uid, token, principal = partial = agent(
        hub, "family-reader", ["memory:read", "threads:read"], wings=["shared", "other"]
    )
    readable = store_thread(hub, "mcp_source", actor=uid, wing="shared", source=source)
    private = store_thread(
        hub,
        "mcp_source",
        actor=hub.admin,
        wing="private_chats",
        source=source + "#sha256:" + "0" * 64,
    )
    recall = hub.gateway.handoff.recall
    assert recall.can_read(principal, readable.id)
    assert not recall.can_read(principal, private.id)
    seed(collection, config, source)
    name, args = READS[route]
    raw = mcp(hub, partial[1], name, **args)
    assert source[4:] not in raw, raw


def visible_diary_entry(collection):
    collection.add(
        ids=["probe-diary-visible"],
        documents=["Visible diary words"],
        metadatas=[
            {
                "wing": "shared",
                "room": "diary",
                "hall": "hall_diary",
                "topic": "general",
                "type": "diary_entry",
                "agent": "probe",
                "filed_at": "2026-11-10T01:00:00Z",
                "date": "2026-11-10",
            }
        ],
    )


def payload(raw):
    return json.loads(json.loads(raw)["result"]["content"][0]["text"])


SEARCH_COUNTERS = {
    "total_before_filter",
    "filtered_by_max_distance",
    "date_filter_pool_truncated",
    "candidates_truncated",
    "distinct_results_truncated",
}


@pytest.mark.parametrize("signal", ["diary", "diary_window", "search", "duplicate"])
@pytest.mark.parametrize("form", sorted(FORMS))
def test_counts_describe_only_visible_rows(hub, collection, config, blind, form, signal):
    thread = hidden_thread(hub, blind, form)
    seed(collection, config, FORMS[form](thread))
    if signal.startswith("diary"):
        visible_diary_entry(collection)
        args = {"agent_name": "probe", "wing": "shared"}
        if signal == "diary_window":
            args["last_n"] = 1  # The hidden entry is older than the window.
        body = payload(mcp(hub, blind[1], "mempalace_diary_read", **args))
        assert [entry["topic"] for entry in body["entries"]] == ["general"], body
        assert body["total"] == 1 and body["showing"] == 1, body
    elif signal == "search":
        body = payload(mcp(hub, blind[1], "mempalace_search", query="plan"))
        assert not SEARCH_COUNTERS & set(body), body
    else:
        # The only exact matches sit in the hidden room and the hidden diary topic.
        body = payload(
            mcp(hub, blind[1], "mempalace_check_duplicate", content=TEXT, threshold=0.95)
        )
        assert body["matches"] == [] and body["is_duplicate"] is False, body


def test_gate_recounts_diary_entries_it_drops(hub, blind):
    thread = hidden_thread(hub, blind, "bare")
    entry = {"date": "2026-11-10", "timestamp": "t", "content": "words"}
    page = {
        "agent": "probe",
        "entries": [{**entry, "topic": thread.id}, {**entry, "topic": "general"}],
        "total": 5,
        "showing": 2,
    }
    gated = mirror_access.gate(hub.gateway, blind[2], "diary_read", page)
    assert gated == {**page, "entries": [page["entries"][1]], "total": 4, "showing": 1}
    only_hidden = {**page, "entries": page["entries"][:1], "total": 1, "showing": 1}
    gated = mirror_access.gate(hub.gateway, blind[2], "diary_read", only_hidden)
    assert gated == {"agent": "probe", "entries": [], "message": "No diary entries yet."}


def test_client_thread_scope_never_reaches_the_worker(hub, collection, config, blind):
    """A client's {"probe": true} is dropped: it receives entries, never topics."""
    seed(collection, config, "general")
    visible_diary_entry(collection)
    body = payload(
        mcp(
            hub,
            blind[1],
            "mempalace_diary_read",
            agent_name="probe",
            wing="shared",
            thread_scope={"probe": True},
        )
    )
    assert "topics" not in body and "source" not in body, body
    assert [entry["topic"] for entry in body["entries"]] == ["general", "general"], body


def test_diary_scope_is_server_only_in_the_worker_schema():
    from mempalace import mcp_server

    tool = mcp_server.TOOLS["mempalace_diary_read"]
    entry = mcp_server._tool_catalog_entry("mempalace_diary_read", tool)
    assert "thread_scope" not in entry["inputSchema"]["properties"]
    # Still declared, so the argument whitelist accepts the gateway's scope.
    assert "thread_scope" in tool["input_schema"]["properties"]


def test_diary_read_stays_on_the_store_its_probe_used(hub, collection, monkeypatch):
    """The exclusion runs on the probe's store; a changed store is refused."""
    from mempalace import mcp_server

    visible_diary_entry(collection)
    read = mcp_server.tool_diary_read
    probe = read("probe", wing="shared", thread_scope={"probe": True})
    assert probe.get("topics") == ["general"], probe
    assert probe.get("source") in ("sqlite", "collection"), probe
    scope = {"exclude_topics": ["general"], "source": probe.get("source")}
    assert read("probe", wing="shared", thread_scope=scope)["entries"] == []
    monkeypatch.setattr(mcp_server, "_sqlite_diary_read", lambda *a, **k: None)
    pinned = {"exclude_topics": [], "source": "sqlite"}
    refused = read("probe", wing="shared", thread_scope=pinned)
    assert refused.get("probe_fault") is True and "entries" not in refused, refused


def failing_probe(hub, monkeypatch, outcome):
    """Replace only the gateway's topic probe with ``outcome`` (raised or returned)."""
    workers = hub.gateway.mediator.workers
    original = workers.call_tool

    def call_tool(principal, name, args):
        if args.get("thread_scope") == {"probe": True}:
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return original(principal, name, args)

    monkeypatch.setattr(workers, "call_tool", call_tool)


def diary_member(hub):
    uid = hub.db.create_user("diary-member@example.test")
    hub.db.add_member(hub.workspace, uid, "member", actor=hub.admin)
    token = hub.token(
        "diary-member", user=uid, profile="full", scopes=["memory:read"], read_wings=["shared"]
    )
    assert hub.db.authenticate_token(token).role != "owner"
    return token


@pytest.mark.parametrize("who", ["owner", "member"])
@pytest.mark.parametrize("fault", ["raised", "malformed"])
def test_probe_fault_answers_an_error_and_logs_its_class(
    hub, collection, config, blind, monkeypatch, caplog, who, fault
):
    """Never an unscoped read, never a silent empty diary: an explicit error."""
    seed(collection, config, "general")
    visible_diary_entry(collection)
    token = blind[1] if who == "owner" else diary_member(hub)
    outcome = RuntimeError("probe unavailable") if fault == "raised" else {"topics": "x"}
    failing_probe(hub, monkeypatch, outcome)
    with caplog.at_level(logging.WARNING, logger="commonplace.gateway"):
        body = payload(mcp(hub, token, "mempalace_diary_read", agent_name="probe", wing="shared"))
    assert set(body) == {"error"}, body
    kind = "RuntimeError" if fault == "raised" else "malformed probe reply"
    assert f"Diary topic probe failed ({kind})" in caplog.text
    assert "probe unavailable" not in caplog.text


def test_probe_error_is_returned_not_read_unscoped(hub, collection, config, blind, monkeypatch):
    """A probe error never falls through to a read whose total counts hidden entries."""
    thread = hidden_thread(hub, blind, "bare")
    seed(collection, config, thread.id)  # The hidden entry is older than the window.
    visible_diary_entry(collection)
    error = {"error": "Failed to read diary entries"}
    failing_probe(hub, monkeypatch, error)
    raw = mcp(hub, blind[1], "mempalace_diary_read", agent_name="probe", wing="shared", last_n=1)
    assert payload(raw) == error, raw


@pytest.mark.parametrize("paused", [True, False], ids=["paused", "unavailable"])
def test_unavailable_memory_is_not_reported_as_an_empty_diary(
    hub, collection, config, blind, monkeypatch, paused
):
    from commonplace.gateway import WorkerPaused, WorkerUnavailable

    visible_diary_entry(collection)
    failing_probe(hub, monkeypatch, WorkerPaused(30) if paused else WorkerUnavailable())
    response = hub.rpc(
        blind[1],
        "tools/call",
        {"name": "mempalace_diary_read", "arguments": {"agent_name": "probe", "wing": "shared"}},
    )
    assert "result" not in response, response
    assert "unavailable" in response["error"]["message"], response
    if paused:
        assert response["error"]["data"] == {"retry_after_s": 30}, response


# Routes that can return a diary entry's words. check_duplicate looks for the entry itself.
DIARY_TEXT_READS = {
    "search": ("mempalace_search", {"query": "plan"}),
    "search_cli": ("mempalace_search", {"query": "plan", "cli_compatible": True}),
    "search_media": ("mempalace_search", {"query": "plan", "include_media": True}),
    "search_code": ("mempalace_search", {"query": "plan", "query_task": "code"}),
    "check_duplicate": ("mempalace_check_duplicate", {"content": DIARY_TEXT, "threshold": 0.9}),
    "get_drawer": ("mempalace_get_drawer", {"drawer_id": "probe-diary"}),
    "get_drawers": ("mempalace_get_drawers", {"drawer_ids": ["probe-diary"]}),
    "list_drawers": ("mempalace_list_drawers", {"wing": "shared"}),
    "diary_read": ("mempalace_diary_read", {"agent_name": "probe", "wing": "shared"}),
}
DIARY_TEXT_FORMS = ["bare", "app_path", "upper_case", "mcp_source", "mcp_source_slash"]


@pytest.mark.parametrize("route", sorted(DIARY_TEXT_READS))
@pytest.mark.parametrize("form", DIARY_TEXT_FORMS)
def test_hidden_topic_diary_words_are_never_returned(hub, collection, config, blind, form, route):
    """Hiding an entry hides its words, not only the thread its topic names."""
    thread = hidden_thread(hub, blind, form)
    seed(collection, config, FORMS[form](thread))
    name, args = DIARY_TEXT_READS[route]
    raw = mcp(hub, blind[1], name, **args)
    assert DIARY_TEXT not in raw, raw
    if route == "check_duplicate":
        body = payload(raw)
        assert body["matches"] == [] and body["is_duplicate"] is False, body


@pytest.mark.parametrize("route", sorted(DIARY_TEXT_READS))
@pytest.mark.parametrize("form", ["bare", "mcp_source_slash"])
def test_thread_reader_keeps_diary_words(hub, collection, config, reader, form, route):
    thread = readable_thread(hub, reader, form)
    seed(collection, config, FORMS[form](thread))
    name, args = DIARY_TEXT_READS[route]
    raw = mcp(hub, reader[1], name, **args)
    assert DIARY_TEXT in raw, raw


def test_member_rest_search_never_returns_hidden_topic_diary_words(hub, collection, config, blind):
    headers = member(hub)
    thread = hidden_thread(hub, blind, "bare")
    seed(collection, config, thread.id)
    status, raw = rest(hub, headers, "/api/memory/search?q=plan")
    assert status == 200 and DIARY_TEXT not in raw, raw


def diary_entry(collection, identifier, topic, stamp):
    collection.add(
        ids=[identifier],
        documents=[f"Diary entry {identifier}"],
        metadatas=[
            {
                "wing": "shared",
                "room": "diary",
                "hall": "hall_diary",
                "topic": topic,
                "type": "diary_entry",
                "agent": "probe",
                "filed_at": stamp,
                "date": "2026-11-10",
            }
        ],
    )


def test_too_many_diary_topics_is_named_and_fails_closed(
    hub, collection, config, blind, monkeypatch
):
    from mempalace import mcp_server

    seed(collection, config, "general")
    diary_entry(collection, "probe-diary-a", "topic-a", "2026-11-10T02:00:00Z")
    diary_entry(collection, "probe-diary-b", "topic-b", "2026-11-10T03:00:00Z")
    monkeypatch.setattr(mcp_server, "_DIARY_TOPIC_LIMIT", 2)
    body = payload(mcp(hub, blind[1], "mempalace_diary_read", agent_name="probe", wing="shared"))
    assert body == {
        "error": "This agent has too many diary topics to check (more than 2);"
        " diary entries are not shown"
    }, body


def test_collection_pinned_read_is_refused_when_sqlite_would_serve(hub, collection, monkeypatch):
    """A store change refuses the read in both directions."""
    from mempalace import mcp_server

    visible_diary_entry(collection)
    read = mcp_server.tool_diary_read
    assert read("probe", wing="shared", thread_scope={"probe": True}).get("source") == "sqlite"
    scope = {"exclude_topics": [], "source": "collection"}
    refused = read("probe", wing="shared", thread_scope=scope)
    assert refused.get("probe_fault") is True and "entries" not in refused, refused


def test_plain_read_does_not_decode_diary_metadata(hub, collection, monkeypatch):
    """An empty hidden-topic list leaves the SQLite page path unchanged."""
    from mempalace import mcp_server
    from mempalace.backends import chroma_sqlite

    visible_diary_entry(collection)
    decoded = []
    original = chroma_sqlite._decoded_rows

    def counting(conn, row_ids, *args, **kwargs):
        decoded.append(kwargs.get("metadata_only", False))
        return original(conn, row_ids, *args, **kwargs)

    monkeypatch.setattr(chroma_sqlite, "_decoded_rows", counting)
    read = mcp_server.tool_diary_read
    page = read("probe", wing="shared", thread_scope={"exclude_topics": [], "source": "sqlite"})
    assert [entry["topic"] for entry in page["entries"]] == ["general"], page
    assert decoded == [False], decoded  # Only the page itself is hydrated.


def handoff_service(side_effect):
    from commonplace.handoff.service import HandoffService

    return HandoffService(
        control_db=SimpleNamespace(_lock=threading.Lock()),
        handoff_db=SimpleNamespace(set_document_store=lambda _: None),
        settings=SimpleNamespace(),
        store_for=side_effect,
        events=SimpleNamespace(deliver_pending=side_effect),
    )


class ReachedSideEffect(Exception):
    """The thread ID got past validation."""


def reached(*args, **kwargs):
    raise ReachedSideEffect()


def filer(hub):
    token = hub.token("thread-filer", profile="full", scopes=["handoff:write"], trust="trusted")
    return hub.db.authenticate_token(token)


def outcome(call):
    try:
        call()
    except Exception as error:  # noqa: BLE001 - the outcome is the assertion
        return error
    return None


REFUSED_FORMS = sorted(set(LEGACY_FORMS) - {"mcp_source_r7_f_canonical_encoded_room"})


@pytest.mark.parametrize("form", REFUSED_FORMS)
def test_filing_refuses_non_canonical_thread_ids(hub, form):
    from commonplace.handoff.models import HandoffRequest, ThreadMeta

    source = LEGACY_FORMS[form][0]("probe-" + secrets.token_hex(6))
    request = HandoffRequest(ThreadMeta(source), "[]", transcript_format="messages")
    error = outcome(lambda: handoff_service(reached).run(filer(hub), request))
    assert type(error) is ValueError and str(error).startswith("thread_id "), error


@pytest.mark.parametrize("form", REFUSED_FORMS)
def test_successor_refuses_non_canonical_thread_ids(hub, form):
    from commonplace.handoff.models import SuccessorRef

    source = LEGACY_FORMS[form][0]("probe-" + secrets.token_hex(6))
    ref = SuccessorRef(agent_ref="t3:successor", thread_id=source)
    error = outcome(lambda: handoff_service(reached).register_successor(filer(hub), "hof_x", ref))
    assert type(error) is ValueError and str(error).startswith("thread_id "), error


@pytest.mark.parametrize(
    "thread_id",
    [
        "mcp:source-id",
        "mcp:3f2c9a1e-7b4d-4c2a-9e1f-0a1b2c3d4e5f",
        "3f2c9a1e-7b4d-4c2a-9e1f-0a1b2c3d4e5f",
        "MCP:x",
        "mcp:x/tail",
        "session.2026-11-11@host:1",
        "mcp:x#sha256:" + "0" * 64,
    ],
)
def test_filing_accepts_canonical_thread_ids(hub, thread_id):
    """T3 thread IDs ([A-Za-z0-9][A-Za-z0-9._:@-]*) and their mcp: forms pass."""
    from commonplace.handoff.models import HandoffRequest, ThreadMeta

    request = HandoffRequest(ThreadMeta(thread_id), "[]", transcript_format="messages")
    error = outcome(lambda: handoff_service(reached).run(filer(hub), request))
    assert type(error) is ReachedSideEffect, error


def statement_counter(hub):
    store = hub.gateway.handoff.store_for(hub.workspace)
    statements = []
    store._conn.set_trace_callback(statements.append)
    return store, statements


def test_batch_room_checks_issue_a_bounded_number_of_queries(hub, collection, config, blind):
    """500 drawers in one delete call: one reader, cached indexed lookups."""
    seed(collection, config, "general")
    ids = [f"bulk-{i}" for i in range(500)]
    collection.add(
        ids=ids,
        documents=[f"Bulk words {i}" for i in range(500)],
        metadatas=[{"wing": "shared", "room": f"room-{i % 5}"} for i in range(500)],
    )
    store, statements = statement_counter(hub)
    try:
        raw = mcp(hub, blind[1], "mempalace_delete_drawers", drawer_ids=ids)
    finally:
        store._conn.set_trace_callback(None)
    assert collection.get(ids=ids[:1])["ids"] == [], raw[:400]
    assert len(statements) <= 30, (len(statements), statements[:10])


def test_long_room_probes_no_more_prefixes_than_the_longest_stored_key(hub, monkeypatch):
    """A room near the length cap must not probe one prefix per character."""
    stored = store_thread(hub, "mcp_source", actor=hub.admin, wing="private_chats")
    store = hub.gateway.handoff.store_for(hub.workspace)
    probed = []
    original = store._keyed_threads

    def record(workspace_id, column, values):
        probed.extend(values)
        return original(workspace_id, column, values)

    monkeypatch.setattr(store, "_keyed_threads", record)
    room = stored.source_thread_id + "_" + "x" * 16000
    rows = store.by_source_prefix(hub.workspace, room)
    assert [row.id for row in rows] == [stored.id]
    assert len(probed) <= len(stored.source_thread_id), len(probed)
    assert sum(map(len, probed)) < 4 * len(stored.source_thread_id) ** 2


def test_prefix_lookup_in_a_workspace_without_stored_keys_probes_nothing(hub, monkeypatch):
    store = hub.gateway.handoff.store_for(hub.workspace)
    probed = []
    monkeypatch.setattr(store, "_keyed_threads", lambda *args: probed.append(args) or [])
    assert store.by_source_prefix(hub.workspace, "mcp:" + "y" * 5000) == []
    assert probed == []


def test_checkpoint_item_count_is_bounded(hub, blind):
    items = [{"wing": "shared", "room": "general", "content": f"x{i}"} for i in range(501)]
    raw = mcp(hub, blind[1], "mempalace_checkpoint", items=items)
    assert "at most 500 items" in raw, raw[:400]


def test_source_lookup_failure_hides_every_non_thread_room(
    hub, collection, config, blind, monkeypatch
):
    from commonplace.handoff.thread_store import ThreadStore

    source = "probe-" + secrets.token_hex(6)
    store_thread(hub, "bare", actor=hub.admin, wing="private_chats", source=source)
    seed(collection, config, source)

    reached = []

    def failed(self, workspace_id, value):
        reached.append(value)
        raise RuntimeError("lookup unavailable")

    monkeypatch.setattr(ThreadStore, "by_source_value", failed)
    raw = mcp(hub, blind[1], "mempalace_list_rooms", wing="shared")
    assert reached, "the failing lookup was never reached"
    assert source not in raw and "general" not in raw, raw[:400]


def test_unloaded_store_under_deadline_hides_source_equal_room(hub):
    """A workspace with threads whose store is not open cannot be read inside a
    request deadline: the room must stay hidden, never shown as naming no thread."""
    from commonplace.rules import checks

    source = "probe-" + secrets.token_hex(6)
    store_thread(hub, "bare", actor=hub.admin, wing="private_chats", source=source)
    ctx = hub.gateway.handoff
    ctx._stores.pop(hub.workspace, None)
    assert ctx.has_threads(hub.workspace)
    principal = SimpleNamespace(workspace_id=hub.workspace)
    with checks.deadline(5):
        access = mirror_access._ResponseAccess(hub.gateway, principal)
        assert not access.exact_source_readable(source, [source])
        assert ctx._stores.get(hub.workspace) is None


def test_colonless_source_equal_room_is_hidden(hub, collection, config, blind):
    source = "probe-" + secrets.token_hex(6)
    store_thread(hub, "bare", actor=hub.admin, wing="private_chats", source=source)
    seed(collection, config, source)
    raw = mcp(hub, blind[1], "mempalace_list_rooms", wing="shared")
    assert source not in raw, raw[:400]
