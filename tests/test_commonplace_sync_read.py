"""Pure filer boundaries and public continuity read views."""

import inspect
import threading
from dataclasses import replace
from types import SimpleNamespace

from commonplace.control_db import Principal
from commonplace.handoff import recall
from commonplace.handoff.models import HandoffRequest, StoredThread, ThreadMeta
from commonplace.handoff.service import HandoffService
from commonplace.sessions import person_policy
from tests.commonplace_sync_fixtures import (
    hub as hub,
    machines,
    raw,
    reached,
    reset_reached as reset_reached,
)


def person():
    return Principal(
        user_id="usr_a",
        user_email="a@example.test",
        workspace_id="ws_same",
        connection_id="",
        connection="web:a@example.test",
        client="web",
        auth="session",
        role="owner",
        policy=person_policy("owner"),
        person=True,
    )


def test_private_capture_filer_only_pure():
    owner = person()
    member = replace(owner, user_id="usr_b", user_email="b@example.test", role="member")
    THREADS = [
        StoredThread(
            id=f"thr_a{i}",
            workspace_id="ws_same",
            source_thread_id=identity,
            message_count=0,
            chunk_count=0,
            sha256="a" * 64,
            wing="private_chats",
        )
        for i, identity in enumerate(
            (
                "capture:usr_a:claude-code:S",
                "capture-fork:usr_a:abc",
                "capture:conn_a1:claude-code:S",
            )
        )
    ]
    by_id = {t.id: t for t in THREADS}
    store = SimpleNamespace(get=by_id.get)

    def store_for(_):
        return store

    def execute(sql, args=()):
        row = {"user_id": "usr_a"} if args and args[0] == "conn_a1" else None
        return SimpleNamespace(fetchone=lambda: row)

    fake_db = SimpleNamespace(
        _lock=threading.RLock(),
        _conn=SimpleNamespace(execute=execute),
        connection=lambda *_: {"user_id": "usr_a"},
    )
    if "private" in inspect.signature(recall.Recall).parameters:
        from commonplace.continuity_read import PrivateReader

        reader = recall.Recall(store_for, private=PrivateReader(fake_db, store_for))
    else:
        reader = recall.Recall(store_for)

    def missing(principal, thread):
        try:
            reader.thread(principal, thread.id)
        except recall.ThreadNotFound as error:
            return error
        return None

    caught = [missing(member, t) for t in THREADS]
    assert all(c is not None for c in caught)  # COUNTED:L17
    reached("L17")
    assert [reader.thread(owner, t.id).id for t in THREADS] == [t.id for t in THREADS]


def test_handoff_cannot_squat_capture_namespaces_pure():
    calls = []

    class BoundaryReached(Exception):
        pass

    def deliver_pending(*, workspace_id):
        calls.append(("deliver_pending", workspace_id))
        raise BoundaryReached("synthetic boundary reached")

    def boundary(*args, **kwargs):
        calls.append(("boundary",))
        raise BoundaryReached("synthetic boundary reached")

    service = HandoffService(
        control_db=SimpleNamespace(),
        handoff_db=SimpleNamespace(set_document_store=lambda _: None, begin=boundary),
        settings=SimpleNamespace(),
        store_for=boundary,
        events=SimpleNamespace(deliver_pending=deliver_pending),
    )
    caught = []
    for identity in ("capture:usr_x:claude-code:S", "capture-fork:usr_x:abc"):
        request = HandoffRequest(
            meta=ThreadMeta(thread_id=identity, client="test"),
            transcript='[{"role":"user","content":"synthetic boundary"}]',
            transcript_format="messages",
        )
        try:
            service.run(person(), request)
        except (ValueError, BoundaryReached) as error:
            caught.append(error)
    assert calls == []  # COUNTED:L18
    reached("L18")
    assert len(caught) == 2
    assert all(
        type(e) is ValueError and str(e) == "thread_id uses a reserved namespace" for e in caught
    )


def test_sessions_listing_and_whole_reads(hub):
    h = hub()
    p, F, G, e, a, b, c = machines(h)
    listed = h.sessions(p)
    assert listed.status == 200  # COUNTED:R1
    reached("R1")
    assert set(listed.json) == {"sessions"}
    assert len(listed.json["sessions"]) == 1
    group = listed.json["sessions"][0]
    assert set(group) == {
        "source",
        "native_id",
        "holder_stored_id",
        "bytes",
        "streams",
        "forks",
        "legacy",
    }
    assert group["source"] == "claude-code" and group["native_id"] == "S"
    assert group["holder_stored_id"] == a["stored_thread_id"]
    assert group["bytes"] == len(raw(F + G))
    assert group["streams"] == 3 and group["forks"] == 1 and group["legacy"] == 0
    forks = [t for t in h.stored_threads(p) if t.source_thread_id.startswith("capture-fork:")]
    assert len(forks) == 1
    token, _ = h.pat(p, client="claude-code")
    assert h.sessions(token).status == 403
    edited = raw(F[:-1] + [e])
    assert h.read(p, forks[0].id) == edited
    assert h.read(p, c["stored_thread_id"]) == edited
    assert h.read(p, b["stored_thread_id"]) == raw(F + G)
