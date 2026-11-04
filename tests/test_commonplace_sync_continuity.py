"""A native session keeps each stored record once across transport identities."""

from dataclasses import replace

from commonplace import capture, plans
from tests.commonplace_sync_fixtures import (
    PART_KEYS,
    accepted,
    actors,
    appended,
    counts,
    hub as hub,
    item,
    machines,
    parts,
    raw,
    reached,
    records,
    reset_reached as reset_reached,
    sha,
)


def test_import_then_live_exactly_once(hub):
    h = hub()
    p, token, grant = actors(h)
    F, G = records("S"), records("S", 1, "g")
    accepted(h.imp("claude-code", [item("S", raw(F))], token=grant))
    imported = h.import_thread(p, "S")
    h.at(901)
    live = appended(h.push(token, "claude-code", "S", raw(F + G)))
    o = h.oracle(p, F + G)
    assert counts(o, F) == [1] * len(F)  # COUNTED:L1
    reached("L1")
    assert [o.parsed[r.key] for r in F + G] == [1] * len(F + G)
    assert o.archive_bytes == len(raw(F + G))
    assert h.read(p, live["stored_thread_id"]) == h.read(p, imported) == raw(F + G)


def test_live_then_repeat_grown_and_multipart_import(hub):
    h = hub()
    p, token, grant = actors(h)
    F, G, H = records("S"), records("S", 1, "g"), records("S", 1, "h")
    appended(h.push(token, "claude-code", "S", raw(F + G)))
    h.at(901)
    accepted(h.imp("claude-code", [item("S", raw(F))], token=grant))
    o = h.oracle(p, F)
    assert counts(o, F) == [1] * len(F)  # COUNTED:L2
    reached("L2")
    h.at(1802)
    accepted(h.imp("claude-code", [item("S", raw(F + G))], token=grant))
    grown = [r for r in h.imports(p) if r["conflict_of"] is not None]
    assert len(grown) == 1
    submissions = parts("S-mp", raw(F + G + H), len(raw(F)))
    h.at(2703)
    first = accepted(h.imp("claude-code", [submissions[0]], token=grant))
    assert set(first) == PART_KEYS
    h.at(3604)
    last = accepted(h.imp("claude-code", [submissions[1]], token=grant))
    assert set(last) == PART_KEYS
    assert last["session_state"] == "complete"
    assert counts(h.oracle(p, F + G + H), F + G + H) == [1] * len(F + G + H)
    assert h.read(p, h.import_thread(p, "S-mp")) == raw(F + G + H)


def test_replacement_pat_after_revoke(hub):
    h = hub()
    p = h.signup()
    h.enable_capture(p)
    token, connection = h.pat(p, client="claude-code")
    F, G = records("S"), records("S", 1, "g")
    first = appended(h.push(token, "claude-code", "S", raw(F)))
    assert h.revoke(p, connection).status == 200
    assert h.push(token, "claude-code", "S", raw(F)).status == 401
    replacement, _ = h.pat(p, client="claude-code")
    h.at(30)
    second = appended(h.push(replacement, "claude-code", "S", raw(F + G)))
    assert second["stored_thread_id"] != first["stored_thread_id"]
    o = h.oracle(p, F)
    assert counts(o, F) == [1] * len(F)  # COUNTED:L4
    reached("L4")
    assert h.read(p, second["stored_thread_id"]) == raw(F + G)


def test_two_machines_interleaved_and_divergent(hub):
    h = hub()
    p, F, G, e, _, _, _ = machines(h)
    F_same = F[:-1]
    o = h.oracle(p, F + G + [e])
    assert counts(o, F_same) == [1] * len(F_same)  # COUNTED:L5
    reached("L5")
    assert counts(o, [e, F[-1], *G]) == [1] * (2 + len(G))


def test_agent_suffix_and_wrong_thread_id_group_by_content(hub):
    h = hub()
    p, token, grant = actors(h)
    A = records("S", agent="agent-a")
    appended(h.push(token, "claude-code", "wrong", raw(A)))
    h.at(901)
    accepted(h.imp("claude-code", [item("S:agent:agent-a", raw(A))], token=grant))
    o = h.oracle(p, A)
    assert counts(o, A) == [1] * len(A)  # COUNTED:L6
    reached("L6")
    plain = records("S", 1, "plain")
    h.at(1802)
    appended(h.push(token, "claude-code", "S", raw(plain)))
    listed = h.sessions(p)
    assert listed.status == 200
    assert {r["native_id"] for r in listed.json["sessions"]} == {"S", "S:agent:agent-a"}


def test_non_observer_capture_and_quota_uniform(hub, monkeypatch):
    zero = replace(plans.FREE, sync_interval_seconds=0)
    monkeypatch.setattr(plans, "FREE", zero)
    monkeypatch.setitem(plans.PLANS, "free", zero)
    h = hub()
    p, owner, _ = actors(h)
    F, novel = records("S"), records("other")
    appended(h.push(owner, "claude-code", "S", raw(F)))
    x, _ = h.pat(p, client="claude-code")
    y, _ = h.pat(p, client="claude-code")
    covered = appended(h.push(x, "claude-code", "S", raw(F)))
    new = appended(h.push(y, "claude-code", "other", raw(novel)))
    assert set(covered) == set(new)
    assert covered["offset"] == len(raw(F))
    assert new["offset"] == len(raw(novel))
    assert covered["prefix_sha256"] == sha(raw(F))
    assert new["prefix_sha256"] == sha(raw(novel))
    assert covered["added_messages"] == new["added_messages"] == 3
    x2, _ = h.pat(p, client="claude-code")
    y2, _ = h.pat(p, client="claude-code")
    tiny = replace(zero, storage_bytes=1)
    monkeypatch.setattr(plans, "FREE", tiny)
    monkeypatch.setitem(plans.PLANS, "free", tiny)
    assert h.push(x2, "claude-code", "S", raw(F)).status == 507
    assert h.push(y2, "claude-code", "novel", raw(records("novel"))).status == 507
    for token, sid, data in ((x2, "S", raw(F)), (y2, "novel", raw(records("novel")))):
        # Set the limit one byte above the base-compatible admission bound.
        # Recompute after each write, since SQLite and WAL sizes can grow.
        directory = capture.workspace_path(h.root, p.workspace)
        path = capture.thread_path(directory)
        sizes = [
            candidate.stat().st_size if candidate.exists() else 0
            for candidate in (
                path,
                path.with_name(path.name + "-wal"),
                path.with_name(path.name + "-shm"),
            )
        ]
        store = h.store(p)
        with store._lock:
            pages = store._conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = store._conn.execute("PRAGMA page_size").fetchone()[0]
        text = data.decode()
        messages = [
            capture.ThreadMessage(
                0, "system", "other", "", text, name="capture.raw", at=h.now.isoformat()
            ),
            *capture.parse(text, "claude-code", require_recognized=True),
        ]
        admission = (
            plans.StorageMeter._walk(directory)
            - sum(sizes)
            + max(sizes[0], pages * page_size)
            + sizes[1]
            + max(sizes[2], 32768)
            + 32
            + pages * (page_size + 24)
            + 256 * 1024
            + 8
            * sum(
                len(value.encode())
                for message in messages
                for value in (
                    message.raw,
                    message.text,
                    message.role,
                    message.kind,
                    message.name,
                    message.call_id,
                    message.at,
                )
            )
        )
        above = replace(zero, storage_bytes=admission + 1)
        monkeypatch.setattr(plans, "FREE", above)
        monkeypatch.setitem(plans.PLANS, "free", above)
        assert h.push(token, "claude-code", sid, data).status == 200
