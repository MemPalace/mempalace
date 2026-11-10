"""Store/control crash recovery and continued legacy import listing regressions."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from commonplace import capture, continuity as core, imports
from commonplace.handoff.recall import Recall
from commonplace.handoff.thread_store import ThreadConflict
from commonplace.import_grant import grant_id
from commonplace.import_parts import append_part
from commonplace.sessions import person_policy
from tests.test_commonplace_continuity_unit import (
    all_archives,
    descriptor,
    env as env,
    native,
    push,
    seed,
    snapshot,
    streams,
)


@pytest.mark.parametrize("kind", ["capture", "snapshot", "part"])
def test_fix5_retry_adopts_orphan_between_store_and_control_commits(env, monkeypatch, kind):
    e = env
    first, suffix = native(word="unique-first"), native(word="unique-suffix")
    core.backfill(e.db, e.store, e.ws, e.user)  # Startup migration already finished.
    sid = grant_id(e.user, "claude-code", "multipart")
    part = descriptor(first + suffix, first)

    def submit():
        if kind == "snapshot":
            return snapshot(e, first)
        if kind == "part":
            return push(e, first, stream=sid, part=part)
        return push(e, first)

    original = e.db._tx

    @contextmanager
    def crash_after_store_commit():
        with original() as conn:
            yield conn
            raise RuntimeError("injected failure before control commit")

    with monkeypatch.context() as patch:
        patch.setattr(e.db, "_tx", crash_after_store_commit)
        with pytest.raises(RuntimeError, match="before control commit"):
            submit()
    assert not streams(e)
    assert all_archives(e) == first
    stored = {
        thread.id: e.store.messages(thread.id)
        for prefix in ("capture:", "import-private:")
        for thread in e.store.list_source_range(e.ws, prefix, limit=10)
    }
    try:
        submit()  # Exact replay from offset zero after process restart.
    except ThreadConflict as exc:
        raise AssertionError("retry refused the orphaned holder") from exc
    push(e, first + suffix, stream="capture:conn-second:claude-code:second")
    raw = all_archives(e)
    assert (raw.count(first), raw.count(suffix)) == (1, 1)
    assert len(streams(e)) == 2
    for thread_id, previous in stored.items():
        assert e.store.messages(thread_id)[: len(previous)] == previous


def test_fix6_legacy_multipart_listing_grows_by_own_submission_formula(env):
    e = env
    first, suffix, extra = native(), native(word="second"), native(word="extra")
    sid = grant_id(e.user, "claude-code", "multipart")
    result = append_part(
        e.store,
        e.ws,
        "claude-code",
        "multipart",
        "claude-code",
        first.decode(),
        descriptor(first + suffix, first),
        actor="synthetic",
        maximum=None,
        source_id=sid,
        wing=capture.PRIVATE_WING,
    )
    original = e.store.messages(result["stored_thread_id"])
    seed(e, first + suffix + extra, name="longer", capture_=True)
    core.backfill(e.db, e.store, e.ws, e.user)
    assert streams(e)[sid]["legacy"] == 1
    push(
        e,
        suffix,
        before=first,
        stream=sid,
        part=descriptor(first + suffix, suffix, offset=len(first), index=1),
    )
    routes = {}
    principal = SimpleNamespace(
        person=True, user_id=e.user, workspace_id=e.ws, policy=person_policy("owner")
    )
    ctx = SimpleNamespace(
        control_db=e.db,
        store_for=lambda ws: e.store,
        recall=Recall(lambda ws: e.store),
        gateway=SimpleNamespace(
            principal_for=lambda h: principal,
            add_route=lambda method, path, handler: routes.__setitem__((method, path), handler),
        ),
    )
    imports.register(ctx)
    replies = []
    handler = SimpleNamespace(path="/api/imports", _send_json=lambda *args: replies.append(args))
    routes["GET", "/api/imports"](handler)
    assert replies[0][0] == 200
    assert replies[0][1]["imports"][0]["stored_messages"] == 2 * 2 + 2 - 1
    assert e.store.messages(result["stored_thread_id"]) == original


@pytest.mark.parametrize("orphan_kind", ["capture", "snapshot", "part"])
@pytest.mark.parametrize("second_kind", ["capture", "t3"])
def test_fix2_other_identity_adopts_orphan_before_original_retry(
    env, monkeypatch, orphan_kind, second_kind
):
    e = env
    first, suffix = native(word="orphan-first"), native(word="later-suffix")
    connection, _ = e.db.create_token(
        workspace_ref=e.ws,
        user_ref=e.user,
        name="first",
        client="claude-code",
        policy=person_policy("owner"),
        actor=e.user,
    )
    other, _ = e.db.create_token(
        workspace_ref=e.ws,
        user_ref=e.user,
        name="second",
        client="claude-code",
        policy=person_policy("owner"),
        actor=e.user,
    )
    core.backfill(e.db, e.store, e.ws, e.user)
    sid = f"capture:{connection}:claude-code:first"
    if orphan_kind == "part":
        sid = grant_id(e.user, "claude-code", "multipart-orphan")

    def submit():
        if orphan_kind == "snapshot":
            return snapshot(e, first, name="snapshot-orphan")
        return push(
            e,
            first,
            stream=sid,
            part=descriptor(first + suffix, first) if orphan_kind == "part" else None,
        )

    original = e.db._tx

    @contextmanager
    def crash():
        with original() as conn:
            yield conn
            raise RuntimeError("injected control rollback")

    with monkeypatch.context() as patch:
        patch.setattr(e.db, "_tx", crash)
        with pytest.raises(RuntimeError, match="control rollback"):
            submit()
    assert not streams(e) and all_archives(e) == first
    stored = {
        thread.id: e.store.messages(thread.id)
        for prefix in ("capture:", "import-private:")
        for thread in e.store.list_source_range(e.ws, prefix, limit=10)
    }
    try:
        if second_kind == "t3":
            snapshot(e, first, name="t3-copy", source="t3")
        else:
            push(e, first, stream=f"capture:{other}:claude-code:second")
        submit()
    except ThreadConflict as exc:
        raise AssertionError("compatible orphan recovery must accept both identities") from exc
    assert all_archives(e).count(first) == 1
    assert len(streams(e)) == 2
    assert e.conn.execute("SELECT count(*) FROM sync_groups").fetchone()[0] == 1
    for thread_id, previous in stored.items():
        assert e.store.messages(thread_id) == previous
    # Divergent bytes stay in their own archive; adopting the orphan never rewrites it.
    divergent = native(word="different-content")
    result = snapshot(e, divergent, name="divergent", source="t3")
    assert result is None
    assert all_archives(e) == first


@pytest.mark.parametrize("marker_table", ["missing", "unmarked"])
def test_fix3_preupgrade_legacy_replay_keeps_count_until_new_bytes(env, marker_table):
    e = env
    first, second, third, extra = (native(word=w) for w in ("first", "second", "third", "extra"))
    whole = first + second + third
    sid = grant_id(e.user, "claude-code", "old-multipart")
    result = append_part(
        e.store,
        e.ws,
        "claude-code",
        "old-multipart",
        "claude-code",
        first.decode(),
        descriptor(whole, first),
        actor="synthetic",
        maximum=None,
        source_id=sid,
        wing=capture.PRIVATE_WING,
    )
    original = e.store.messages(result["stored_thread_id"])
    seed(e, whole + extra, name="longer", capture_=True)
    core.backfill(e.db, e.store, e.ws, e.user)
    push(
        e,
        second,
        before=first,
        stream=sid,
        part=descriptor(whole, second, offset=len(first), index=1),
    )
    # Simulate the exact pre-upgrade control/store state: a part accepted after demotion.
    with e.db._lock:
        if marker_table == "missing":
            e.conn.execute("DROP TABLE sync_import_submissions")
        else:
            e.conn.execute("DELETE FROM sync_import_submissions WHERE stream_id=?", (sid,))
    assert streams(e)[sid]["legacy"] == 1
    assert streams(e)[sid]["listed_messages"] == 5

    routes, replies = {}, []
    principal = SimpleNamespace(
        person=True, user_id=e.user, workspace_id=e.ws, policy=person_policy("owner")
    )
    ctx = SimpleNamespace(
        control_db=e.db,
        store_for=lambda ws: e.store,
        recall=Recall(lambda ws: e.store),
        gateway=SimpleNamespace(
            principal_for=lambda h: principal,
            add_route=lambda method, path, handler: routes.__setitem__((method, path), handler),
        ),
    )
    imports.register(ctx)

    def count():
        replies.clear()
        routes["GET", "/api/imports"](
            SimpleNamespace(path="/api/imports", _send_json=lambda *args: replies.append(args))
        )
        assert replies[0][0] == 200
        return replies[0][1]["imports"][0]["stored_messages"]

    assert count() == len(original) - 1 == 2
    push(
        e,
        second,
        before=first,
        stream=sid,
        part=descriptor(whole, second, offset=len(first), index=1),
    )
    assert count() == 2
    table_exists = e.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_import_submissions'"
    ).fetchone()
    if marker_table == "missing":
        assert table_exists is None
    else:
        assert (
            e.conn.execute(
                "SELECT 1 FROM sync_import_submissions WHERE workspace_id=? AND stream_id=?",
                (e.ws, sid),
            ).fetchone()
            is None
        )
    push(
        e,
        third,
        before=first + second,
        stream=sid,
        part=descriptor(whole, third, offset=len(first + second), index=2),
    )
    assert count() == 2 * 3 + 3 - 1
    assert e.conn.execute(
        "SELECT 1 FROM sync_import_submissions WHERE workspace_id=? AND stream_id=?",
        (e.ws, sid),
    ).fetchone()
    assert e.store.messages(result["stored_thread_id"]) == original


@pytest.mark.parametrize("second_kind", ["capture", "t3"])
def test_fix3_orphan_search_pages_past_controlled_archives(env, monkeypatch, second_kind):
    e = env
    monkeypatch.setattr(core, "BACKFILL_PAGE", 2)
    connections = [
        e.db.create_token(
            workspace_ref=e.ws,
            user_ref=e.user,
            name=name,
            client="claude-code",
            policy=person_policy("owner"),
            actor=e.user,
        )[0]
        for name in ("first", "second")
    ]
    connection, other = connections
    core.backfill(e.db, e.store, e.ws, e.user)
    for index in range(2):
        snapshot(e, native(sid=f"import-{index}"), name=f"controlled-{index}")
        push(
            e,
            native(sid=f"capture-{index}"),
            stream=f"capture:{connection}:claude-code:a-controlled-{index}",
        )
    first = native(word="orphan-after-controlled-pages")
    sid = f"capture:{connection}:claude-code:z-orphan"
    original = e.db._tx

    @contextmanager
    def crash():
        with original() as conn:
            yield conn
            raise RuntimeError("injected control rollback")

    with monkeypatch.context() as patch:
        patch.setattr(e.db, "_tx", crash)
        with pytest.raises(RuntimeError, match="control rollback"):
            push(e, first, stream=sid)
    orphan = e.store.by_source(e.ws, sid)
    previous = e.store.messages(orphan.id)
    assert sid not in streams(e)
    if second_kind == "t3":
        snapshot(e, first, name="t3-copy", source="t3")
    else:
        push(e, first, stream=f"capture:{other}:claude-code:second")
    assert all_archives(e).count(first) == 1
    group = e.conn.execute(
        "SELECT holder_stored_id FROM sync_groups WHERE native_id=?", ("S",)
    ).fetchall()
    assert [row[0] for row in group] == [orphan.id]
    assert e.store.messages(orphan.id) == previous


def test_fix3_controlled_probe_uses_stream_indexes(env):
    e = env
    core.backfill(e.db, e.store, e.ws, e.user)
    probe = getattr(core, "_CONTROLLED_PROBE", None)
    assert probe is not None, "the controlled-archive probe is not a single indexed query"
    plan = " | ".join(
        row[-1]
        for row in e.db._conn.execute(
            "EXPLAIN QUERY PLAN " + probe, (e.ws, "x", e.ws, "x", e.ws, "x")
        ).fetchall()
    )
    assert "sync_streams_own" in plan and "sync_streams_fork" in plan, plan
