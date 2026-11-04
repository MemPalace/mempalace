"""Synthetic head-only continuity, adoption, and crash recovery units."""

import gc
import hashlib
import json
import threading
import weakref
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from commonplace import capture, continuity as core, continuity_schema as schema, plans
from commonplace.control_db import ControlDB
from commonplace.handoff.api import _RequestError
from commonplace.handoff.thread_store import (
    ThreadConflict,
    ThreadStorageLimit,
    ThreadStore,
    ThreadStoreV2,
    canonical_sha256,
)
from commonplace.import_grant import grant_id
from commonplace.import_parts import append_part
from commonplace.import_sources import parse_import
from mempalace.dual_read import SampledReads

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def native(sid="S", word="first", size=0):
    return (
        json.dumps(
            dict(type="user", sessionId=sid, message=dict(role="user", content=word + "x" * size)),
            ensure_ascii=False,
        )
        + "\n"
    ).encode()


def descriptor(whole, raw, *, offset=0, index=0):
    return dict(
        version=1,
        session_bytes=len(whole),
        session_sha256=sha(whole),
        index=index,
        offset=offset,
        prefix_sha256=sha(whole[:offset]),
        sha256=sha(raw),
        final=offset + len(raw) == len(whole),
    )


@pytest.fixture(params=["v1", "v2", "sampled"])
def env(request, tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    monkeypatch.setattr(plans, "_now", lambda: NOW)
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    user = db.create_user("synthetic@example.test")
    ws = db.create_workspace("synthetic", user, personal=True)
    schema.ensure_schema(db)
    current = (ThreadStore if request.param == "v1" else ThreadStoreV2)(":memory:")
    previous = ThreadStore(":memory:")
    store = (
        SampledReads(
            current, previous, reads=("get", "messages", "by_source", "parent_of"), sample_every=1
        )
        if request.param == "sampled"
        else current
    )
    yield SimpleNamespace(db=db, conn=db._conn, user=user, ws=ws, store=store, current=current)
    current.close()
    previous.close()
    db.close()


def identity(e, name="a"):
    return f"capture:{e.user}:claude-code:{name}"


def push(
    e,
    raw,
    *,
    name="a",
    before=b"",
    maximum=None,
    gate=lambda: None,
    part=None,
    source="claude-code",
    stream=None,
):
    with e.db._lock, e.db._tx() as conn:
        return core.stream_push(
            conn,
            e.store,
            ws=e.ws,
            user_id=e.user,
            connection_id="",
            kind="import" if part else "capture",
            stream_id=stream or identity(e, name),
            source=source,
            native_source="claude-code",
            data=raw,
            offset=len(before),
            prefix=sha(before),
            actor="synthetic",
            maximum=maximum,
            gate=gate,
            part=part,
        )


def snapshot(e, raw, *, name="S", source="claude-code", maximum=None):
    text = raw.decode()
    messages = parse_import(source, text)
    with e.db._lock, e.db._tx() as conn:
        return core.import_item(
            conn,
            e.store,
            ws=e.ws,
            user_id=e.user,
            source=source,
            fmt="auto",
            thread_id=name,
            transcript=text,
            messages=messages,
            actor="synthetic",
            maximum=maximum,
        )


def rows(e):
    return e.conn.execute("SELECT * FROM sync_streams ORDER BY stream_id").fetchall()


def streams(e):
    return {row["stream_id"]: row for row in rows(e)}


def archive(e, stored):
    return b"".join(s.raw for s in schema.archive_segments(e.store.messages(stored)))


def all_archives(e):
    return b"".join(
        archive(e, r.id)
        for prefix in ("capture:", "import-private:", "capture-fork:")
        for r in e.store.list_source_range(e.ws, prefix, limit=1000)
    )


def seed(e, raw, *, name="S", source="claude-code", capture_=False, owner=None):
    owner = owner or e.user
    sid = f"capture:{owner}:{source}:{name}" if capture_ else grant_id(owner, source, name)
    if capture_:
        result = capture.append_capture(
            e.store,
            e.ws,
            sid,
            source,
            raw.decode(),
            0,
            sha(b""),
            actor="synthetic",
            maximum=None,
            interval=0,
        )
        return e.store.get(result["stored_thread_id"])
    return e.store.store(
        e.ws, sid, parse_import(source, raw.decode()), actor="synthetic", wing=capture.PRIVATE_WING
    )


def test_covered_extension_and_own_cursors(env):
    e = env
    f, g = native(), native(word="second")
    first = push(e, f)
    second = push(e, f + g, name="b")
    covered = push(e, g, before=f)
    assert first["stored_thread_id"] != second["stored_thread_id"]
    assert archive(e, first["stored_thread_id"]) == f + g
    assert archive(e, second["stored_thread_id"]) == b""
    assert covered["offset"] == len(f + g)
    assert covered["added_messages"] == 1
    assert all_archives(e) == f + g
    assert all(r["last_sync"] == NOW.isoformat() for r in rows(e))
    with e.db._lock:
        current = core.stream_cursor(
            e.conn, e.store, ws=e.ws, stream_id=identity(e, "b"), own_identity=identity(e, "b")
        )
    assert current["prefix_sha256"] == sha(f + g)


@pytest.mark.parametrize("edit", [0, 1, 2])
def test_fork_starts_at_first_differing_record(env, edit):
    e = env
    records = [native(word=str(i)) for i in range(3)]
    original = b"".join(records)
    push(e, original)
    replacement = native(word="edited")
    candidate = b"".join(records[:edit]) + replacement
    forked = push(e, candidate, name="fork")
    row = streams(e)[identity(e, "fork")]
    boundary = len(b"".join(records[:edit]))
    assert row["fork_offset"] == boundary
    assert archive(e, row["fork_stored_id"]) == replacement
    marker = e.store.messages(row["fork_stored_id"], 0, 0)[0]
    assert json.loads(marker.raw) == {"v": 1, "offset": boundary}
    assert core._own_bytes(e.conn, e.store, row) == candidate
    assert forked["prefix_sha256"] == sha(candidate)
    for line in records[:edit]:
        assert all_archives(e).count(line) == 1
    suffix = native(word="fork suffix")
    extended = push(e, suffix, name="fork", before=candidate)
    assert extended["offset"] == len(candidate + suffix)
    assert core._own_bytes(e.conn, e.store, streams(e)[identity(e, "fork")]) == candidate + suffix


def test_fork_first_new_record_starts_at_own_offset(env):
    e = env
    f, g, novel = native(), native(word="second"), native(word="different")
    push(e, f)
    push(e, f, name="b")
    push(e, g, before=f)
    push(e, novel, name="b", before=f)
    assert streams(e)[identity(e, "b")]["fork_offset"] == len(f)


def test_own_duplicate_bypasses_gate_interval_and_quota(env, monkeypatch):
    e = env
    f = native()
    push(e, f)
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")

    def forbidden():
        raise AssertionError("duplicate admitted")

    duplicate = push(e, f, maximum=1, gate=forbidden)
    assert duplicate["status"] == "duplicate"
    with pytest.raises(ThreadConflict):
        push(e, native(word="changed"), maximum=1)
    with pytest.raises(_RequestError) as caught:
        push(e, native(word="suffix"), before=f)
    assert caught.value.status == 429


def test_grouped_post_write_quota_failure_rolls_back(env, monkeypatch):
    e = env
    f = native()
    first = push(e, f)
    previous = e.store.messages(first["stored_thread_id"])
    check = e.current._check_storage_budget
    calls = []

    def fail_after_write(maximum, messages=()):
        check(maximum, messages)
        calls.append(bool(messages))
        if not messages:
            raise ThreadStorageLimit()

    monkeypatch.setattr(e.current, "_check_storage_budget", fail_after_write)
    with pytest.raises(ThreadStorageLimit):
        push(e, native(word="suffix"), before=f)
    assert calls == [True, False]
    assert e.store.messages(first["stored_thread_id"]) == previous
    assert rows(e)[0]["offset"] == len(f)


def test_quota_is_uniform_for_covered_and_new(env):
    e = env
    f = native()
    push(e, f)
    for maximum in (0, 1, 1000, 10_000, 100_000):
        for name, body in (("covered", f), ("novel", native("other"))):
            with pytest.raises(ThreadStorageLimit):
                push(e, body, name=name, maximum=maximum)
    assert len(rows(e)) == 1


def test_snapshots_keep_first_identity_and_distinct_digest(env):
    e = env
    f, g = native(), native(word="second")
    push(e, f)
    assert snapshot(e, f)["status"] == "accepted"
    sid = grant_id(e.user, "claude-code", "S")
    assert streams(e)[sid]["listed_messages"] == 1
    snapshot(e, f + g)
    digest = canonical_sha256(parse_import("claude-code", (f + g).decode()))
    grown = f"{sid}#sha256:{digest}"
    assert streams(e)[grown]["listed_messages"] == 2
    assert e.store.by_source(e.ws, grown)
    assert snapshot(e, f + g)["status"] == "accepted"
    assert len(rows(e)) == 3
    assert all_archives(e) == f + g
    with pytest.raises(ThreadStorageLimit):
        snapshot(e, f + g, maximum=1)


def test_import_holder_listing_does_not_grow(env):
    e = env
    f, g = native(), native(word="second")
    snapshot(e, f)
    sid = grant_id(e.user, "claude-code", "S")
    own = e.store.by_source(e.ws, sid)
    push(e, f + g)
    assert streams(e)[sid]["listed_messages"] == 1
    assert archive(e, own.id) == f + g


def test_live_tail_and_divergent_snapshots_stay_ungrouped(env):
    e = env
    f = native()
    assert snapshot(e, f + b'{"type":') is None
    assert not rows(e)
    assert not e.conn.execute("SELECT * FROM sync_groups").fetchall()
    push(e, f)
    assert snapshot(e, native(word="different")) is None
    assert len(rows(e)) == 1
    assert all(
        "#tail:" not in r.source_thread_id
        for r in e.store.list_source_range(e.ws, "capture:", limit=100)
    )


def test_live_tail_at_base_identity_uses_digest_for_complete_snapshot(env):
    e = env
    f = native()
    seed(e, f + b'{"type":')
    snapshot(e, f)
    sid = grant_id(e.user, "claude-code", "S")
    digest = canonical_sha256(parse_import("claude-code", f.decode()))
    assert list(streams(e)) == [f"{sid}#sha256:{digest}"]


def test_identical_snapshot_adopts_existing_holder_without_rewriting(env):
    e = env
    f = native()
    stored = seed(e, f)
    original = e.store.messages(stored.id)
    assert snapshot(e, f)["status"] == "accepted"
    assert len(rows(e)) == 1
    assert rows(e)[0]["own_stored_id"] == stored.id
    assert e.conn.execute("SELECT holder_stored_id FROM sync_groups").fetchone()[0] == stored.id
    assert e.store.messages(stored.id) == original


def test_parts_extend_shared_bytes_and_keep_submission_count(env):
    e = env
    f, g = native(), native(word="second")
    whole = f + g
    push(e, f)
    sid = grant_id(e.user, "claude-code", "multipart")
    p0 = descriptor(whole, f)
    p1 = descriptor(whole, g, offset=len(f), index=1)
    first = push(e, f, stream=sid, part=p0)
    assert first["status"] == "accepted"
    assert first["source_bytes"] == len(f)
    assert streams(e)[sid]["listed_messages"] == 2
    final = push(e, g, stream=sid, before=f, part=p1)
    assert final["session_state"] == "complete"
    assert streams(e)[sid]["listed_messages"] == 5
    assert all_archives(e) == whole
    assert push(e, g, stream=sid, before=f, part=p1, maximum=1)["status"] == "accepted"
    with pytest.raises(ThreadConflict):
        push(e, g, stream=sid, before=f, part=dict(p1, session_sha256="a" * 64))


def test_incomplete_final_part_has_no_side_effects(env):
    e = env
    raw = native() + b'{"unfinished":'
    sid = grant_id(e.user, "claude-code", "multipart")
    with pytest.raises(ValueError):
        push(e, raw, stream=sid, part=descriptor(raw, raw))
    assert not rows(e)
    assert not e.conn.execute("SELECT * FROM sync_groups").fetchall()
    assert not e.store.list_source_range(e.ws, "capture-fork:", limit=10)


def test_complete_unterminated_record_groups_with_later_terminator(env):
    e = env
    f, g = native().rstrip(b"\n"), native(word="second")
    snapshot(e, f)
    push(e, f + b"\n" + g)
    holder = e.conn.execute("SELECT holder_stored_id FROM sync_groups").fetchone()[0]
    assert archive(e, holder) == f + b"\n" + g
    assert all_archives(e).count(f) == 1
    assert sum(m.text == "first" for m in e.store.messages(holder)) == 1


def test_own_marker_after_store_commit_rebuilds_row_without_appending(env):
    e = env
    f, g = native(), native(word="second")
    push(e, f)
    result = push(e, f + g, name="b")
    marker_rows = e.store.messages(result["stored_thread_id"])
    with e.db._lock:
        e.conn.execute("DELETE FROM sync_streams WHERE stream_id=?", (identity(e, "b"),))
    replay = push(e, f + g, name="b")
    assert replay["status"] == "appended"
    assert replay["stored_thread_id"] == result["stored_thread_id"]
    assert e.store.messages(result["stored_thread_id"]) == marker_rows
    assert all_archives(e) == f + g
    assert len(rows(e)) == 2


def test_fork_store_commit_before_control_commit_is_covered_on_retry(env):
    e = env
    f, g = native(), native(word="second")
    push(e, f + g)
    candidate = f + native(word="edited")
    result = push(e, candidate, name="b")
    fork_row = streams(e)[identity(e, "b")]
    before = e.store.messages(fork_row["fork_stored_id"])
    with e.db._lock:
        e.conn.execute("DELETE FROM sync_streams WHERE stream_id=?", (identity(e, "b"),))
    replay = push(e, candidate, name="b")
    assert replay["stored_thread_id"] == result["stored_thread_id"]
    assert e.store.messages(fork_row["fork_stored_id"]) == before


def test_backfill_holder_switch_keeps_rows_and_lists_legacy(env):
    e = env
    f, g = native(), native(word="second")
    short = seed(e, f, name="a")
    long = seed(e, f + g, name="b")
    t3 = seed(e, f, name="c", source="t3")
    captured = seed(e, f, name="d", capture_=True)
    previous = {r.id: e.store.messages(r.id) for r in (short, long, t3, captured)}
    core.backfill(e.db, e.store, e.ws, e.user)
    group = e.conn.execute("SELECT * FROM sync_groups").fetchone()
    assert group["holder_stored_id"] == long.id
    assert len(rows(e)) == 4
    assert sum(r["legacy"] for r in rows(e)) == 3
    assert {r.id: e.store.messages(r.id) for r in (short, long, t3, captured)} == previous
    assert streams(e)[short.source_thread_id]["listed_messages"] == short.message_count - 1
    assert streams(e)[t3.source_thread_id]["listed_messages"] == t3.message_count - 1


@pytest.mark.parametrize("outcome", ["covered", "extends", "diverges"])
def test_adopted_legacy_capture_continues_without_mutating_own_rows(env, outcome):
    e = env
    f, g, h = native(), native(word="second"), native(word="third")
    holder = seed(e, f + g, name="a")
    legacy = seed(e, f, name="legacy", capture_=True)
    saved = e.store.messages(legacy.id)
    core.backfill(e.db, e.store, e.ws, e.user)
    suffix = g if outcome == "covered" else g + h if outcome == "extends" else h
    result = push(e, suffix, stream=legacy.source_thread_id, before=f)
    assert result["status"] == "appended"
    assert result["stored_thread_id"] == legacy.id
    assert result["prefix_sha256"] == sha(f + suffix)
    assert e.store.messages(legacy.id) == saved
    own = streams(e)[legacy.source_thread_id]
    assert own["legacy"] == 1
    assert core._own_bytes(e.conn, e.store, own) == f + suffix
    assert archive(e, holder.id) == (f + g + h if outcome == "extends" else f + g)


def test_adopted_capture_holder_extends_in_base_layout(env):
    e = env
    f, g = native(), native(word="second")
    holder = seed(e, f, name="holder", capture_=True)
    saved = e.store.messages(holder.id)
    core.backfill(e.db, e.store, e.ws, e.user)
    push(e, g, before=f, stream=holder.source_thread_id)
    after = e.store.messages(holder.id)
    assert after[: len(saved)] == saved
    assert after[len(saved)].name == "capture.raw"
    assert after[len(saved)].raw.encode() == g
    assert after[len(saved)].at == NOW.isoformat()


def test_divergent_legacy_thread_uses_base_at_any_offset(env):
    """LD: a no-row pre-upgrade capture never enters the grouped path."""
    e = env
    f, changed, g = native(), native(word="changed"), native(word="suffix")
    seed(e, f)
    legacy = seed(e, changed, name="legacy", capture_=True)
    core.backfill(e.db, e.store, e.ws, e.user)
    assert legacy.source_thread_id not in streams(e)
    saved = e.store.messages(legacy.id)
    duplicate = push(e, changed, stream=legacy.source_thread_id)
    assert duplicate["status"] == "duplicate"
    assert duplicate["stored_thread_id"] == legacy.id
    result = push(e, g, before=changed, stream=legacy.source_thread_id)
    assert result["status"] == "appended"
    assert legacy.source_thread_id not in streams(e)
    assert len(rows(e)) == 1
    assert e.store.messages(legacy.id)[: len(saved)] == saved
    assert e.store.messages(legacy.id)[len(saved)].name == "capture.raw"
    with e.db._lock:
        assert core.stream_cursor(
            e.conn,
            e.store,
            ws=e.ws,
            stream_id=legacy.source_thread_id,
            own_identity=legacy.source_thread_id,
        ) == capture.cursor(e.store, e.store.get(legacy.id))
    assert result["prefix_sha256"] == sha(changed + g)


def test_backfill_partial_parts_resume_and_keep_listed_count(env):
    e = env
    f, g = native(), native(word="second")
    sid = grant_id(e.user, "claude-code", "multipart")
    first = descriptor(f + g, f)
    second = descriptor(f + g, g, offset=len(f), index=1)
    result = append_part(
        e.store,
        e.ws,
        "claude-code",
        "multipart",
        "claude-code",
        f.decode(),
        first,
        actor="synthetic",
        maximum=None,
        source_id=sid,
        wing=capture.PRIVATE_WING,
    )
    original = e.store.messages(result["stored_thread_id"])
    core.backfill(e.db, e.store, e.ws, e.user)
    assert streams(e)[sid]["listed_messages"] == len(original) - 1
    push(e, g, before=f, stream=sid, part=second)
    assert streams(e)[sid]["listed_messages"] == len(original) - 1 + 3
    assert e.store.messages(result["stored_thread_id"])[: len(original)] == original
    assert push(e, f, stream=sid, part=first, maximum=1)["status"] == "accepted"


def test_backfill_skips_live_tail_digest_and_prefix_neighbours(env, monkeypatch):
    e = env
    f = native()
    included = seed(e, f)
    seed(e, f, owner=e.user + "0")
    seed(e, f, owner=e.user + ";x")
    seed(e, f + b'{"unfinished":', name="tail")
    seed(e, f, name="S#sha256:" + "a" * 64)
    monkeypatch.setattr(core, "BACKFILL_PAGE", 2)
    core.backfill(e.db, e.store, e.ws, e.user)
    assert list(streams(e)) == [included.source_thread_id]
    assert e.conn.execute("SELECT done FROM sync_backfill").fetchone()[0] == 1


def test_backfill_pages_commit_once_and_release_lock_for_other_requests(env, monkeypatch):
    """BF-pages: five rows form three short, independently committed pages."""
    e = env
    for i in range(5):
        seed(e, native(str(i)), name=str(i))
    monkeypatch.setattr(core, "BACKFILL_PAGE", 2)
    original_tx = e.db._tx
    original_messages = e.store.messages
    commits = []
    first_committed, settings_done = threading.Event(), threading.Event()

    def messages(*args, **kwargs):
        assert not e.db._lock._is_owned()
        return original_messages(*args, **kwargs)

    @contextmanager
    def tx():
        with original_tx() as conn:
            yield conn
        commits.append(len(rows(e)))
        if len(commits) == 1:
            first_committed.set()

    original_list = e.store.list_source_range

    def listing(*args, **kwargs):
        assert not e.db._lock._is_owned()
        if first_committed.is_set():
            assert settings_done.wait(5)
        return original_list(*args, **kwargs)

    def settings():
        assert first_committed.wait(5)
        with e.db._lock, original_tx() as conn:
            conn.execute("CREATE TABLE synthetic_settings (enabled INTEGER)")
            conn.execute("INSERT INTO synthetic_settings VALUES (1)")
        settings_done.set()

    thread = threading.Thread(target=settings)
    thread.start()
    monkeypatch.setattr(e.db, "_tx", tx)
    monkeypatch.setattr(e.store, "messages", messages)
    monkeypatch.setattr(e.store, "list_source_range", listing)
    try:
        core.backfill(e.db, e.store, e.ws, e.user)
    finally:
        thread.join(5)
    assert not thread.is_alive()
    assert commits == [2, 4, 5]
    assert settings_done.is_set()
    # A completed cursor never lists the archive again.
    monkeypatch.setattr(
        e.store, "list_source_range", lambda *a, **k: pytest.fail("completed backfill listed")
    )
    core.backfill(e.db, e.store, e.ws, e.user)


def test_backfill_crash_resumes_after_last_committed_page(env, monkeypatch):
    e = env
    stored = [seed(e, native(str(i)), name=str(i)) for i in range(5)]
    monkeypatch.setattr(core, "BACKFILL_PAGE", 2)
    original_list = e.store.list_source_range
    original_tx = e.db._tx
    committed = []

    @contextmanager
    def tx():
        with original_tx() as conn:
            yield conn
        committed.append(len(rows(e)))

    def fail(*args, **kwargs):
        if committed:
            raise RuntimeError("synthetic disconnect")
        return original_list(*args, **kwargs)

    monkeypatch.setattr(e.db, "_tx", tx)
    monkeypatch.setattr(e.store, "list_source_range", fail)
    with pytest.raises(RuntimeError, match="synthetic disconnect"):
        core.backfill(e.db, e.store, e.ws, e.user)
    assert len(rows(e)) == 2
    cursor = e.conn.execute("SELECT * FROM sync_backfill").fetchone()
    assert (cursor["after_source"], cursor["after_id"]) == (
        stored[1].source_thread_id,
        stored[1].id,
    )
    listings = []

    def resume(*args, **kwargs):
        listings.append(kwargs.get("after"))
        return original_list(*args, **kwargs)

    monkeypatch.setattr(e.store, "list_source_range", resume)
    core.backfill(e.db, e.store, e.ws, e.user)
    assert listings[0] == (stored[1].source_thread_id, stored[1].id)
    assert len(rows(e)) == 5
    assert committed == [2, 4, 5]


def test_backfill_rejects_owned_lock(env):
    with env.db._lock:
        with pytest.raises(RuntimeError, match="no control lock"):
            core.backfill(env.db, env.store, env.ws, env.user)


def test_backfill_rechecks_holder_after_unlocked_read(env, monkeypatch):
    """BF-holder: committed holder switches invalidate a whole read page."""
    e = env
    f, g, h = native(), native(word="second"), native(word="different")
    push(e, f)
    old_holder = e.conn.execute("SELECT holder_stored_id FROM sync_groups").fetchone()[0]
    candidate = seed(e, f + g, name="candidate")
    alternate = e.store.store(
        e.ws,
        "alternate",
        parse_import("claude-code", (f + h).decode()),
        actor="synthetic",
        wing=capture.PRIVATE_WING,
    )
    original_tx = e.db._tx
    attempts, read_ids, control_reads = [], [], []
    original_messages = e.store.messages

    def messages(stored, *args, **kwargs):
        assert not e.db._lock._is_owned()
        read_ids.append(stored)
        return original_messages(stored, *args, **kwargs)

    def trace(sql):
        if sql.startswith("SELECT * FROM sync_groups") or sql.startswith(
            "SELECT * FROM sync_backfill"
        ):
            control_reads.append((e.db._lock._is_owned(), e.conn.in_transaction))
            # Step-1 reads are outside a transaction; step 2 holds the tx.
            attempts.append((e.conn.in_transaction, sql))

    @contextmanager
    def tx():
        if not any(in_tx for in_tx, _ in attempts):
            other = ControlDB(e.db.path)
            try:
                with other._lock, other._tx() as conn:
                    conn.execute("UPDATE sync_groups SET holder_stored_id=?", (alternate.id,))
            finally:
                other.close()
        with original_tx() as conn:
            yield conn

    e.conn.set_trace_callback(trace)
    monkeypatch.setattr(e.db, "_tx", tx)
    monkeypatch.setattr(e.store, "messages", messages)
    try:
        core.backfill(e.db, e.store, e.ws, e.user)
    finally:
        e.conn.set_trace_callback(None)
    assert control_reads and all(owned for owned, _ in control_reads)
    assert any(not in_tx for _, in_tx in control_reads)
    assert any(in_tx for _, in_tx in control_reads)
    assert old_holder in read_ids and alternate.id in read_ids
    assert read_ids.count(candidate.id) == 2
    assert candidate.source_thread_id not in streams(e)
    assert e.conn.execute("SELECT holder_stored_id FROM sync_groups").fetchone()[0] == alternate.id


def test_backfill_byte_pages_and_release_page_objects(env, monkeypatch):
    """BF-bytes: byte caps cross on the last thread and always admit one."""
    e = env
    for i in range(5):
        seed(e, native(str(i), size=500), name=str(i))
    seed(e, native("large", size=5000), name="large")
    monkeypatch.setattr(core, "BACKFILL_PAGE_BYTES", 1024)
    original_tx, original_messages = e.db._tx, e.store.messages
    sizes, refs = [], []

    class PageRows(list):
        pass

    def messages(*args, **kwargs):
        value = PageRows(original_messages(*args, **kwargs))
        refs.append(weakref.ref(value))
        return value

    @contextmanager
    def tx():
        before = len(rows(e))
        with original_tx() as conn:
            yield conn
        sizes.append(len(rows(e)) - before)

    monkeypatch.setattr(e.store, "messages", messages)
    monkeypatch.setattr(e.db, "_tx", tx)
    core.backfill(e.db, e.store, e.ws, e.user)
    # The final large thread crosses its own page's budget, never a zero page.
    assert sizes == [2, 2, 2]
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_backfill_oversized_thread_is_one_page_and_holder_read_once(env, monkeypatch):
    e = env
    large = native(size=5000)
    seed(e, large, name="a")
    for name in ("b", "c", "d"):
        seed(e, large, name=name)
    monkeypatch.setattr(core, "BACKFILL_PAGE_BYTES", 1024)
    original_tx, original_messages = e.db._tx, e.store.messages
    pages, reads = [], []

    def messages(stored, *args, **kwargs):
        reads.append(stored)
        return original_messages(stored, *args, **kwargs)

    @contextmanager
    def tx():
        with original_tx() as conn:
            yield conn
        pages.append(Counter(reads))
        reads.clear()

    monkeypatch.setattr(e.db, "_tx", tx)
    monkeypatch.setattr(e.store, "messages", messages)
    core.backfill(e.db, e.store, e.ws, e.user)
    assert len(pages) == 4
    assert all(max(count.values()) == 1 for count in pages)
    assert len(rows(e)) == 4


def test_concurrent_same_suffix_serializes_and_gate_runs_once(env):
    e = env
    f, g = native(), native(word="suffix")
    push(e, f)
    in_gate, second_started, release = threading.Event(), threading.Event(), threading.Event()
    results, errors, admissions = [], [], []

    def gate():
        admissions.append(1)
        in_gate.set()
        assert release.wait(5)

    def first():
        try:
            results.append(push(e, g, before=f, gate=gate))
        except Exception as error:
            errors.append(error)

    def second():
        second_started.set()
        try:
            results.append(push(e, g, before=f, gate=gate))
        except Exception as error:
            errors.append(error)

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    try:
        assert in_gate.wait(5)
        two.start()
        assert second_started.wait(5)
    finally:
        release.set()
        one.join(5)
        if two.ident:
            two.join(5)
    assert not one.is_alive() and not two.is_alive()
    assert not errors
    assert [r["status"] for r in results] == ["appended", "duplicate"]
    assert admissions == [1]
    assert all_archives(e) == f + g


def test_backfill_waits_for_other_threads_control_transaction(env):
    e = env
    started, release, attempting, finished = (threading.Event() for _ in range(4))
    errors = []

    def writer():
        with e.db._lock, e.db._tx():
            started.set()
            assert release.wait(5)

    def adopter():
        attempting.set()
        try:
            core.backfill(e.db, e.store, e.ws, e.user)
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    one, two = threading.Thread(target=writer), threading.Thread(target=adopter)
    one.start()
    try:
        assert started.wait(5)
        two.start()
        assert attempting.wait(5)
    finally:
        release.set()
        one.join(5)
        if two.ident:
            two.join(5)
    assert finished.is_set()
    assert not errors
    assert e.conn.execute("SELECT done FROM sync_backfill").fetchone()[0] == 1


def test_backfill_cursor_cas_loss_retries_only_uncommitted_page(env, monkeypatch):
    e = env
    for i in range(4):
        seed(e, native(str(i)), name=str(i))
    monkeypatch.setattr(core, "BACKFILL_PAGE", 2)
    real_tx = e.db._tx
    committed, injected = [], []

    @contextmanager
    def tx():
        if committed == [2] and not injected:
            injected.append(1)
            # A second caller advances the cursor while this page is being
            # read. Its real transaction completes before this one starts.
            with real_tx() as conn:
                conn.execute("UPDATE sync_backfill SET after_id=after_id || '-changed'")
        with real_tx() as conn:
            yield conn
        committed.append(len(rows(e)))

    monkeypatch.setattr(e.db, "_tx", tx)
    core.backfill(e.db, e.store, e.ws, e.user)
    assert injected == [1]
    assert committed == [2, 4]
    assert len(rows(e)) == 4


def test_backfill_rechecks_thread_message_count_before_join(env, monkeypatch):
    e = env
    f, g = native(), native(word="suffix")
    stored = seed(e, f, name="a", capture_=True)
    real_tx, original_messages = e.db._tx, e.store.messages
    injected, read_ids, mutating = [], [], []

    def messages(stored_id, *args, **kwargs):
        if not mutating:
            assert not e.db._lock._is_owned()
        read_ids.append(stored_id)
        return original_messages(stored_id, *args, **kwargs)

    @contextmanager
    def tx():
        if not injected:
            injected.append(1)
            mutating.append(1)
            capture.append_capture(
                e.store,
                e.ws,
                stored.source_thread_id,
                "claude-code",
                g.decode(),
                len(f),
                sha(f),
                actor="synthetic",
                maximum=None,
                interval=0,
            )
            mutating.clear()
        with real_tx() as conn:
            yield conn

    monkeypatch.setattr(e.db, "_tx", tx)
    monkeypatch.setattr(e.store, "messages", messages)
    core.backfill(e.db, e.store, e.ws, e.user)
    assert read_ids.count(stored.id) >= 2
    assert rows(e)[0]["offset"] == len(f + g)


def test_part_finality_requires_a_recognized_native_record(env):
    e = env
    raw = b'{"sessionId":"S"}\n'
    sid = grant_id(e.user, "claude-code", "multipart")
    with pytest.raises(ThreadConflict):
        push(e, raw, stream=sid, part=descriptor(raw, raw))
    assert not rows(e)


def test_store_commit_before_control_update_is_covered_on_holder_retry(env):
    e = env
    f, g = native(), native(word="suffix")
    first = push(e, f)
    old = dict(rows(e)[0])
    push(e, g, before=f)
    with e.db._lock:
        core._save_stream(e.conn, **old)
    saved = e.store.messages(first["stored_thread_id"])
    replay = push(e, g, before=f)
    assert replay["status"] == "appended"
    assert e.store.messages(first["stored_thread_id"]) == saved
    assert rows(e)[0]["offset"] == len(f + g)


def test_missing_own_identity_outside_private_wing_conflicts(env):
    e = env
    f = native()
    e.store.store(e.ws, identity(e), parse_import("claude-code", f.decode()), actor="synthetic")
    with pytest.raises(ThreadConflict):
        push(e, f)
    assert not rows(e)


def test_continuity_survives_store_migration_round_trip(tmp_path, monkeypatch):
    from commonplace.handoff.thread_store_migrate import migrate_v1, migrate_v2

    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    monkeypatch.setattr(plans, "_now", lambda: NOW)
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    user = db.create_user("migration@example.test")
    ws = db.create_workspace("synthetic", user, personal=True)
    schema.ensure_schema(db)
    v1, v2, restored = (
        tmp_path / name for name in ("threads.sqlite3", "threads.v2.sqlite3", "restored.sqlite3")
    )
    e = SimpleNamespace(db=db, conn=db._conn, user=user, ws=ws, store=ThreadStore(str(v1)))
    f, g, h = native(), native(word="suffix"), native(word="last")
    try:
        push(e, f + g)
        candidate = f + native(word="edited")
        push(e, candidate, name="fork")
        e.store.close()
        migrate_v1(str(v1), str(v2))
        e.store = ThreadStoreV2(str(v2))
        assert core._own_bytes(e.conn, e.store, streams(e)[identity(e, "fork")]) == candidate
        push(e, h, before=f + g)
        e.store.close()
        migrate_v2(str(v2), str(restored))
        e.store = ThreadStore(str(restored))
        assert core._own_bytes(e.conn, e.store, streams(e)[identity(e)]) == f + g + h
        assert core._own_bytes(e.conn, e.store, streams(e)[identity(e, "fork")]) == candidate
    finally:
        e.store.close()
        db.close()
