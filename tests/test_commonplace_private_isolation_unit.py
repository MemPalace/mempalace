"""Synthetic read seam units; integrated acceptance runs belong to the parent."""

import json
import threading
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from commonplace import continuity_read, sync_sessions
from commonplace.continuity_schema import StreamRow, archive_segments, ensure_schema
from commonplace.control_db import ControlDB, Principal
from commonplace.handoff import mcp_tools, recall
from commonplace.handoff.models import HandoffRequest, ThreadMessage, ThreadMeta
from commonplace.handoff.service import HandoffService
from commonplace.handoff.thread_store import ThreadStore
from commonplace.handoff.transcripts import parse
from commonplace.sessions import person_policy


def native(text, source="claude-code"):
    if source == "codex":
        record = {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": text},
        }
    else:
        record = {"type": "user", "sessionId": "S", "message": {"role": "user", "content": text}}
    return json.dumps(record, ensure_ascii=False) + "\r\n"


def segment(text, source="claude-code", *, snapshot=False, start=0):
    rows = [
        ThreadMessage(
            0, "system", "other", "", text, name="" if snapshot else "capture.raw", at="then"
        )
    ] + parse(text, source, require_recognized=False)
    return [replace(row, seq=start + i) for i, row in enumerate(rows)]


def archive(rows):
    return b"".join(piece.raw for piece in archive_segments(rows))


@pytest.fixture
def account(tmp_path):
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    owner = db.create_user("owner@example.test")
    member = db.create_user("member@example.test")
    ws = db.create_workspace("synthetic shared", owner)
    db.add_member(ws, member, "member", actor=owner)
    ensure_schema(db)
    store = ThreadStore(str(tmp_path / "threads.sqlite3"))
    principal = Principal(
        owner,
        "owner@example.test",
        ws,
        "",
        "web",
        "web",
        "session",
        "owner",
        person_policy("owner"),
        True,
    )
    private = continuity_read.PrivateReader(db, lambda _: store)
    ctx = SimpleNamespace(
        db=db,
        store=store,
        ws=ws,
        owner=principal,
        member=replace(principal, user_id=member, role="member"),
        private=private,
        reader=recall.Recall(lambda _: store, private=private),
    )
    try:
        yield ctx
    finally:
        store.close()
        db.close()


def put(a, identity, rows, *, parent="", wing="private_chats"):
    return a.store.store(
        a.ws, identity, rows, parent_stored_id=parent, wing=wing, actor=a.owner.actor
    )


def group(a, holder, *, user=None, name="group", source="claude-code", native_id="S"):
    with a.db._lock:
        a.db._conn.execute(
            "INSERT INTO sync_groups VALUES (?,?,?,?,?,?)",
            (name, a.ws, user or a.owner.user_id, source, native_id, holder.id),
        )


def stream(a, own, *, group_id="group", user=None, **fields):
    values = asdict(
        StreamRow(
            workspace_id=a.ws,
            stream_id=own.source_thread_id,
            user_id=user or a.owner.user_id,
            connection_id="",
            kind=fields.pop("kind", "capture"),
            group_id=group_id,
            own_stored_id=own.id,
            **fields,
        )
    )
    with a.db._lock:
        a.db._conn.execute(
            f"INSERT INTO sync_streams ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )


def marker(a, identity, holder):
    return put(
        a,
        identity,
        [ThreadMessage(0, "system", "other", "", '{"v":1}', name="capture.base")],
        parent=holder.id,
    )


def test_filer_lookup_is_locked_and_includes_revoked_connections():
    lock = threading.RLock()

    def execute(sql, args):
        assert lock._is_owned()
        assert sql == "SELECT user_id FROM connections WHERE id=?"
        assert args == ("conn_old",)
        return SimpleNamespace(fetchone=lambda: {"user_id": "usr_owner"})

    db = SimpleNamespace(_lock=lock, _conn=SimpleNamespace(execute=execute))
    stored = SimpleNamespace(source_thread_id="capture:conn_old:claude-code:S")
    assert continuity_read.filing_user(db, stored) == "usr_owner"


@pytest.mark.parametrize("identity", ["user", "connection", "fork", "import", "unknown"])
def test_member_cannot_read_another_filers_private_threads(account, identity):
    a = account
    identities = {
        "user": f"capture:{a.owner.user_id}:claude-code:S",
        "fork": f"capture-fork:{a.owner.user_id}:abc",
        "import": f"import-private:{a.owner.user_id}:claude-code:S",
        "unknown": "old-unresolvable",
    }
    if identity == "connection":
        connection, _ = a.db.create_token(
            workspace_ref=a.ws,
            user_ref=a.owner.user_id,
            name="synthetic-capture",
            client="claude-code",
            actor=a.owner.user_id,
            policy=person_policy("owner"),
        )
        with a.db._lock:
            a.db._conn.execute("UPDATE connections SET revoked_at='then' WHERE id=?", (connection,))
        identities[identity] = f"capture:{connection}:claude-code:S"
    stored = put(a, identities[identity], segment(native("unchanged")))
    with pytest.raises(recall.ThreadNotFound, match="thread not found"):
        a.reader.thread(a.member, stored.id)
    assert a.reader.get(a.owner, stored.id) == a.store.messages(stored.id)


def test_read_narrows_only_and_public_readable_behavior_stays_unchanged(account):
    a = account
    stored = put(a, "ordinary", segment(native("public")), wing="threads")
    assert a.reader.thread(a.member, stored.id) == stored
    private = put(a, f"capture:{a.owner.user_id}:claude-code:S", segment(native("private")))
    assert recall.readable(a.member, private) == recall._readable(a.member, private) is True
    assert not a.private.readable(a.member, private)
    blocked = replace(a.owner, policy=replace(a.owner.policy, scopes=()))
    with pytest.raises(recall.ThreadNotFound):
        a.reader.thread(blocked, private.id)
    with pytest.raises(recall.ThreadNotFound):
        a.reader.thread(replace(a.owner, workspace_id="other"), private.id)


@pytest.mark.parametrize("source", ["claude-code", "codex"])
def test_marker_alias_fork_cut_and_mcp_raw_are_verbatim(account, source):
    a = account
    first, second, third = [native(word, source) for word in ("één", "two", "three")]
    holder_rows = segment(first, source, snapshot=True)
    holder_rows += segment(second + third, source, start=len(holder_rows))
    holder = put(a, f"capture:{a.owner.user_id}:{source}:S", holder_rows)
    group(a, holder, source=source)
    alias = marker(a, f"capture:{a.owner.user_id}:{source}:alias", holder)
    stream(a, alias, offset=len((first + second).encode()))
    assert a.reader.get(a.owner, alias.id) == holder_rows
    assert a.reader.thread(a.owner, alias.id).message_count == len(holder_rows)
    assert a.private.view(holder) is None

    changed = native("changed", source)
    fork_rows = [ThreadMessage(0, "system", "other", "", '{"v":1}', name="capture.base")]
    fork_rows += segment(changed, source, start=1)
    fork = put(a, f"capture-fork:{a.owner.user_id}:abc", fork_rows, parent=holder.id)
    fork_alias = marker(a, f"capture:{a.owner.user_id}:{source}:fork", holder)
    stream(
        a,
        fork_alias,
        offset=len((first + second + changed).encode()),
        fork_offset=len((first + second).encode()),
        fork_stored_id=fork.id,
    )
    for stored in (alias, fork_alias, fork, holder):
        with pytest.raises(recall.ThreadNotFound):
            a.reader.thread(a.member, stored.id)
    expected = (first + second + changed).encode()
    for stored in (fork, fork_alias):
        rows = a.reader.get(a.owner, stored.id)
        assert archive(rows) == expected
        assert [row.seq for row in rows] == list(range(len(rows)))
        assert a.reader.get(a.owner, stored.id, 1, 3) == rows[1:4]
        assert a.reader.thread(a.owner, stored.id).message_count == len(rows)
        result = mcp_tools._get(
            SimpleNamespace(recall=a.reader), a.owner, {"thread_id": stored.id, "include_raw": True}
        )
        assert [row["raw"] for row in result["messages"]] == [row.raw for row in rows]
        assert a.store.messages(stored.id) == (
            fork_rows if stored == fork else a.store.messages(alias.id)
        )
    assert a.store.messages(holder.id) == holder_rows
    assert [t.id for t in a.reader.chain(a.owner, fork_alias.id)] == [fork_alias.id, holder.id]


@pytest.mark.parametrize("continued,forked", [(False, False), (True, False), (True, True)])
def test_legacy_keeps_own_rows_and_cuts_continuation_at_both_ends(account, continued, forked):
    a = account
    F, G, J, K = [native(word) for word in ("old", "middle", "end", "later")]
    holder = put(a, f"capture:{a.owner.user_id}:claude-code:S", segment(F + G + J + K))
    group(a, holder)
    own_rows = segment(F, snapshot=True)
    legacy = put(a, f"capture:{a.owner.user_id}:claude-code:legacy", own_rows)
    fields = {"offset": len((F + G + J if continued else F).encode()), "legacy": 1}
    expected = F + G + J if continued else F
    if forked:
        fork_rows = [ThreadMessage(0, "system", "other", "", '{"v":1}', name="capture.base")]
        changed = native("forked")
        fork_rows += segment(changed, start=1)
        fork = put(a, f"capture-fork:{a.owner.user_id}:def", fork_rows, parent=holder.id)
        fields.update(fork_stored_id=fork.id, fork_offset=len((F + G).encode()))
        expected = F + G + changed
    stream(a, legacy, **fields)
    rows = a.reader.get(a.owner, legacy.id)
    assert rows[: len(own_rows)] == own_rows
    assert archive(rows) == expected.encode()
    assert [row.seq for row in rows] == list(range(len(rows)))
    assert a.reader.get(a.owner, legacy.id, 1, 3) == rows[1:4]
    assert a.store.messages(legacy.id) == own_rows
    if not continued:
        assert a.private.view(legacy) is None
    with pytest.raises(recall.ThreadNotFound):
        a.reader.thread(a.member, legacy.id)


def test_alias_cache_observes_holder_growth_and_range_clamp(account):
    a = account
    rows = segment(native("initial"))
    holder = put(a, f"capture:{a.owner.user_id}:claude-code:S", rows)
    group(a, holder)
    alias = marker(a, f"capture:{a.owner.user_id}:claude-code:alias", holder)
    stream(a, alias, offset=1)
    assert a.reader.get(a.owner, alias.id) == rows
    grown = rows + segment("".join(native(str(i)) for i in range(205)), start=len(rows))
    a.store.append(holder.id, grown)
    assert a.reader.thread(a.owner, alias.id).message_count == len(grown)
    assert a.reader.get(a.owner, alias.id, 2, 999) == grown[2:202]
    assert a.reader.get(a.owner, alias.id, 5, 4) == []
    for i in range(20):
        with a.db._lock:
            a.db._conn.execute("UPDATE sync_streams SET offset=?", (i,))
        a.private.view(alias)
    assert len(a.private._cache) == 16


@pytest.mark.parametrize(
    "kind,legacy,listed,expected",
    [
        ("import", 0, 7, 7),
        ("import", 1, 7, None),
        ("capture", 0, 7, None),
        ("import", 0, None, None),
    ],
)
def test_listed_messages_reads_only_control_rows(account, kind, legacy, listed, expected):
    a = account
    holder = put(a, f"import-private:{a.owner.user_id}:claude-code:S", segment(native("first")))
    group(a, holder)
    alias = marker(a, f"import-private:{a.owner.user_id}:claude-code:alias", holder)
    for own in (holder, alias):
        stream(a, own, kind=kind, legacy=legacy, listed_messages=listed)
    a.store.messages = lambda *args: pytest.fail("listing read holder bytes")
    assert continuity_read.listed_messages(a.db, holder) == expected
    assert continuity_read.listed_messages(a.db, alias) == expected
    assert continuity_read.listed_messages(a.db, replace(alias, id="thr_absent")) is None
    assert continuity_read.listed_messages(a.db, replace(alias, workspace_id="other")) is None


def test_sessions_route_is_person_only_and_filters_caller_and_workspace(account):
    a = account
    holder = put(a, f"capture:{a.owner.user_id}:claude-code:S", segment(native("één")))
    group(a, holder)
    stream(a, holder, legacy=1)
    alias = marker(a, f"capture:{a.owner.user_id}:claude-code:alias", holder)
    stream(a, alias, fork_stored_id="thr_fork")
    other = put(a, f"capture:{a.member.user_id}:claude-code:other", segment(native("private")))
    group(a, other, user=a.member.user_id, name="other")
    stream(a, other, group_id="other", user=a.member.user_id)
    with a.db._lock:
        a.db._conn.execute(
            "INSERT INTO sync_groups VALUES ('elsewhere','ws_elsewhere',?,'codex','S','thr_hidden')",
            (a.owner.user_id,),
        )
    routes = {}
    ctx = SimpleNamespace(
        control_db=a.db,
        store_for=lambda _: a.store,
        gateway=SimpleNamespace(
            principal_for=lambda _: ctx.principal,
            add_route=lambda method, path, handle: routes.update({(method, path): handle}),
        ),
    )
    sync_sessions.register(ctx)
    handle = routes[("GET", "/api/sync/sessions")]
    for principal, status in [
        (None, 401),
        (replace(a.owner, person=False), 403),
        (a.owner, 200),
        (a.member, 200),
    ]:
        ctx.principal = principal
        replies = []
        handle(SimpleNamespace(_send_json=lambda *args, replies=replies: replies.append(args)))
        assert replies[0][0] == status
        if status == 200:
            rows = replies[0][1]["sessions"]
            assert len(rows) == 1
            assert rows[0]["holder_stored_id"] == (holder.id if principal == a.owner else other.id)
            assert set(rows[0]) == {
                "source",
                "native_id",
                "holder_stored_id",
                "bytes",
                "streams",
                "forks",
                "legacy",
            }
            if principal == a.owner:
                assert rows[0]["bytes"] == len(native("één").encode())
                assert (rows[0]["streams"], rows[0]["forks"], rows[0]["legacy"]) == (2, 1, 1)


@pytest.mark.parametrize(
    "identity", ["import-private:usr_a:S", "capture:usr_a:claude-code:S", "capture-fork:usr_a:abc"]
)
def test_reserved_namespaces_fail_before_any_side_effect(account, identity):
    def forbidden(*args, **kwargs):
        pytest.fail("reserved namespace reached a side effect")

    service = HandoffService(
        control_db=SimpleNamespace(),
        handoff_db=SimpleNamespace(set_document_store=lambda _: None),
        settings=SimpleNamespace(),
        store_for=forbidden,
        events=SimpleNamespace(deliver_pending=forbidden),
    )
    request = HandoffRequest(ThreadMeta(identity), "[]", transcript_format="messages")
    with pytest.raises(ValueError, match="^thread_id uses a reserved namespace$"):
        service.run(account.owner, request)
