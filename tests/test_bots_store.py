"""Bot storage: append-only messages, idempotent sends, one reply, memory tombstones, reopen."""

import sqlite3
from types import SimpleNamespace

import pytest

from commonplace.bots import store as bots_store
from commonplace.bots.store import BotStore, Conflict, Invalid, Limit, Missing

REQ = "request-0001"


@pytest.fixture(name="bots")
def bots_fixture(tmp_path):
    supervisor = SimpleNamespace(workspace_dir=lambda workspace: str(tmp_path / workspace))
    opened = []

    def open_store():
        opened.append(BotStore(supervisor, "ws1"))
        return opened[-1]

    bots = open_store()
    bots.reopen = open_store
    bots.path = tmp_path / "ws1" / "bots.sqlite3"
    yield bots
    for each in opened:
        each.close()


def bot(bots, user="u1", name="Docs"):
    return bots.create(user, name)["id"]


def raw(bots, sql, *args):
    conn = sqlite3.connect(bots.path)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def test_defaults_are_one_general_and_one_per_board_and_never_recreated(bots):
    bots.ensure_defaults("u1", [("work", "Work"), ("garden", "Garden")])
    bots.ensure_defaults("u1", [("work", "Work"), ("garden", "Garden")])
    roster = bots.roster("u1")
    assert [(b["kind"], b["board"]) for b in roster] == [
        ("general", None),
        ("project", "work"),
        ("project", "garden"),
    ]
    work = next(b for b in roster if b["board"] == "work")
    bots.update(work["id"], "u1", archived=True)
    bots.ensure_defaults("u1", [("work", "Work"), ("garden", "Garden")])
    assert [b["board"] for b in bots.roster("u1")] == [None, "garden"]
    assert bots.roster("u2") == []


def test_create_rename_archive_and_unarchive(bots):
    created = bots.create("u1", " Notes ", job="Keep notes", instructions="Be short")
    assert (created["name"], created["job"], created["kind"], created["board"]) == (
        "Notes",
        "Keep notes",
        "custom",
        None,
    )
    renamed = bots.update(created["id"], "u1", name="Ideas", job="")
    assert (renamed["name"], renamed["job"], renamed["instructions"]) == ("Ideas", "", "Be short")
    assert bots.update(created["id"], "u1", archived=True)["archived"] is True
    assert bots.roster("u1") == []
    assert bots.roster("u1", created["id"])[0]["archived"] is True
    assert bots.update(created["id"], "u1", archived=False)["archived"] is False
    assert [b["name"] for b in bots.roster("u1")] == ["Ideas"]


def test_live_bots_are_bounded_per_person_and_archiving_frees_a_slot(bots):
    ids = [bots.create("u1", f"Bot {i}")["id"] for i in range(bots_store.MAX_LIVE_BOTS)]
    with pytest.raises(Limit):
        bots.create("u1", "One too many")
    bots.create("u2", "Someone else")
    bots.update(ids[0], "u1", archived=True)
    again = bots.create("u1", "Fits now")
    with pytest.raises(Limit):
        bots.update(ids[0], "u1", archived=False)
    assert again["name"] == "Fits now"


@pytest.mark.parametrize(
    "call",
    [
        lambda b: b.create("u1", ""),
        lambda b: b.create("u1", "   "),
        lambda b: b.create("u1", "x" * 61),
        lambda b: b.create("u1", "ok", job="x" * 201),
        lambda b: b.create("u1", "ok", instructions="x" * 4001),
        lambda b: b.create("u1", 7),
        lambda b: b.update(bot(b), "u1", archived="yes"),
        lambda b: b.add_user_message(bot(b), "u1", "", REQ),
        lambda b: b.add_user_message(bot(b), "u1", "x" * 8001, REQ),
        lambda b: b.add_user_message(bot(b), "u1", "hi", "short"),
        lambda b: b.add_user_message(bot(b), "u1", "hi", "bad id with spaces"),
        lambda b: b.add_user_message(bot(b), "u1", "hi", "x" * 65),
        lambda b: b.memory_add(bot(b), "u1", ""),
        lambda b: b.memory_add(bot(b), "u1", "x" * 2001),
        lambda b: b.messages(bot(b), "u1", limit=101),
        lambda b: b.messages(bot(b), "u1", limit=0),
        lambda b: b.messages(bot(b), "u1", after=1, before=2),
        lambda b: b.mark_read(bot(b), "u1", -1),
    ],
)
def test_bounds_are_enforced(bots, call):
    with pytest.raises(Invalid):
        call(bots)


def test_bounds_accept_their_limits(bots):
    b = bots.create("u1", "n" * 60, job="j" * 200, instructions="i" * 4000)["id"]
    assert bots.add_user_message(b, "u1", "x" * 8000, "r" * 64)["text"] == "x" * 8000
    assert bots.memory_add(b, "u1", "m" * 2000)["text"] == "m" * 2000
    assert bots.messages(b, "u1", limit=100)


def test_messages_are_append_only(bots):
    b = bot(bots)
    message = bots.add_user_message(b, "u1", "hello", REQ)
    for sql in (
        "UPDATE bot_messages SET text='changed' WHERE id=?",
        "DELETE FROM bot_messages WHERE id=?",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            raw(bots, sql, message["id"])
    assert bots.messages(b, "u1")[0]["text"] == "hello"


def test_user_message_is_idempotent_per_request_id_and_conflicts_on_other_text(bots):
    b = bot(bots)
    first = bots.add_user_message(b, "u1", "plan the week", REQ)
    assert bots.add_user_message(b, "u1", "plan the week", REQ) == first
    with pytest.raises(Conflict):
        bots.add_user_message(b, "u1", "plan another week", REQ)
    assert len(bots.messages(b, "u1")) == 1
    other = bot(bots, name="Other")
    assert bots.add_user_message(other, "u1", "plan another week", REQ)["id"] != first["id"]


def test_one_reply_per_user_message(bots):
    b = bot(bots)
    ask = bots.add_user_message(b, "u1", "hi", REQ)
    reply = bots.add_bot_message(b, "u1", ask["id"], "hello", blocks=[{"type": "setup"}])
    assert (reply["role"], reply["reply_to"], reply["state"]) == ("bot", ask["id"], "ok")
    assert reply["blocks"] == [{"type": "setup"}]
    with pytest.raises(Conflict):
        bots.add_bot_message(b, "u1", ask["id"], "again")
    with pytest.raises(Invalid):
        bots.add_bot_message(b, "u1", reply["id"], "reply to a reply")
    with pytest.raises(Invalid):
        bots.add_bot_message(b, "u1", 999, "no such message")
    with pytest.raises(Invalid):
        bots.add_bot_message(b, "u1", ask["id"], "x", state="weird")
    assert bots.reply_to(b, "u1", ask["id"])["text"] == "hello"
    assert bots.message_for_request(b, "u1", REQ)["id"] == ask["id"]


def test_event_messages_and_paging(bots):
    b = bot(bots)
    ids = [bots.add_user_message(b, "u1", f"m{i}", f"request-{i:04d}")["id"] for i in range(5)]
    event = bots.add_event_message(b, "u1", "Work is ready", [{"type": "work", "card": 3}])
    assert event["role"] == "event"
    assert [m["id"] for m in bots.messages(b, "u1")] == [*ids, event["id"]]
    assert [m["id"] for m in bots.messages(b, "u1", limit=2)] == [ids[-1], event["id"]]
    assert [m["id"] for m in bots.messages(b, "u1", before=ids[2], limit=2)] == ids[:2]
    assert [m["id"] for m in bots.messages(b, "u1", after=ids[1], limit=2)] == ids[2:4]
    assert bots.messages(b, "u1", after=event["id"]) == []


def test_unread_counts_bot_and_event_messages_after_the_read_mark(bots):
    b = bot(bots)
    ask = bots.add_user_message(b, "u1", "hi", REQ)
    assert bots.roster("u1")[0]["unread"] == 0
    reply = bots.add_bot_message(b, "u1", ask["id"], "hello")
    event = bots.add_event_message(b, "u1", "update")
    assert bots.roster("u1")[0]["unread"] == 2
    assert bots.mark_read(b, "u1", reply["id"]) == reply["id"]
    assert bots.roster("u1")[0]["unread"] == 1
    assert bots.mark_read(b, "u1", 1) == reply["id"]
    assert bots.mark_read(b, "u1", 10**9) == event["id"]
    assert bots.roster("u1")[0]["unread"] == 0


def test_roster_orders_by_last_activity_with_unused_bots_last(bots):
    first, second, unused = bot(bots, name="A"), bot(bots, name="B"), bot(bots, name="C")
    bots.add_user_message(first, "u1", "one", "request-0001")
    bots.add_user_message(second, "u1", "two", "request-0002")
    roster = bots.roster("u1")
    assert [b["id"] for b in roster] == [second, first, unused]
    assert roster[0]["last"]["text"] == "two" and roster[2]["last"] is None


def test_action_journal_started_then_finished_once(bots):
    b = bot(bots)
    bots.action_start(b, "u1", REQ, 1, "create_work", "abc")
    with pytest.raises(Conflict):
        bots.action_start(b, "u1", REQ, 1, "create_work", "abc")
    assert bots.actions(b, "u1", REQ)[0]["state"] == "started"
    assert bots.action_finish(b, "u1", REQ, 1, "ok", [{"type": "work", "card": 1}])
    assert not bots.action_finish(b, "u1", REQ, 1, "refused")
    [row] = bots.actions(b, "u1", REQ)
    assert (row["state"], row["blocks"]) == ("ok", [{"type": "work", "card": 1}])


def test_pending_resolves_once_by_compare_and_set(bots):
    b = bot(bots)
    pending = bots.pending_create(b, "u1", "approve_work", {"ref": 4, "version": 2}, "Approve #4")
    assert pending["state"] == "open" and pending["args"] == {"ref": 4, "version": 2}
    assert bots.pending_resolve(b, "u1", pending["id"], "done")
    assert not bots.pending_resolve(b, "u1", pending["id"], "done")
    assert not bots.pending_resolve(b, "u1", pending["id"], "cancelled")
    assert bots.pending_get(b, "u1", pending["id"])["state"] == "done"
    second = bots.pending_create(b, "u1", "stop_work", {}, "Stop")
    assert bots.pending_resolve(b, "u1", second["id"], "cancelled")


def test_an_expired_pending_cannot_be_confirmed(bots):
    b = bot(bots)
    pending = bots.pending_create(b, "u1", "stop_work", {}, "Stop", ttl_s=-1)
    assert not bots.pending_resolve(b, "u1", pending["id"], "done")
    assert bots.pending_get(b, "u1", pending["id"])["state"] == "expired"


def test_work_links_are_idempotent(bots):
    b = bot(bots)
    ask = bots.add_user_message(b, "u1", "file it", REQ)
    bots.work_add(b, "u1", "work", 7, ask["id"])
    bots.work_add(b, "u1", "work", 7, ask["id"])
    bots.work_add(b, "u1", "work", 8, ask["id"])
    assert bots.work_list(b, "u1") == [
        {"board": "work", "card": 7, "message_id": ask["id"]},
        {"board": "work", "card": 8, "message_id": ask["id"]},
    ]


def test_forwards_and_filed_work_are_found_by_board_and_number(bots):
    first, second = bot(bots, name="A"), bot(bots, name="B", user="u2")
    m1 = bots.add_user_message(first, "u1", "tell them", "request-0001")["id"]
    m2 = bots.add_user_message(second, "u2", "tell them", "request-0002")["id"]
    bots.forward_add(first, "u1", "work", 41, m1)
    bots.forward_add(first, "u1", "work", 41, m1)
    bots.forward_add(second, "u2", "work", 41, m2)
    bots.forward_add(second, "u2", "garden", 41, m2)
    assert bots.forward_list(first, "u1") == [
        {"board": "work", "space_message_id": 41, "message_id": m1}
    ]
    assert bots.bots_for_post("work", 41) == [
        {"bot_id": first, "user_id": "u1", "message_id": m1},
        {"bot_id": second, "user_id": "u2", "message_id": m2},
    ]
    assert bots.bots_for_post("work", 42) == [] and len(bots.bots_for_post("garden", 41)) == 1
    bots.work_add(first, "u1", "work", 7, m1)
    assert bots.bots_for_card("work", 7) == [{"bot_id": first, "user_id": "u1", "message_id": m1}]
    assert bots.bots_for_card("garden", 7) == [] and bots.bots_for_card("work", 8) == []
    with pytest.raises(Missing):
        bots.forward_add(first, "u2", "work", 43, m1)
    with pytest.raises(Missing):
        bots.forward_list(first, "u2")


def test_memory_removal_is_a_tombstone(bots):
    b = bot(bots)
    keep = bots.memory_add(b, "u1", "prefers short answers")
    drop = bots.memory_add(b, "u1", "lives in a boat")
    assert bots.memory_remove(b, "u1", drop["id"])
    assert not bots.memory_remove(b, "u1", drop["id"])
    assert not bots.memory_remove(b, "u1", 999)
    assert [m["id"] for m in bots.memory_list(b, "u1")] == [keep["id"]]
    conn = sqlite3.connect(bots.path)
    try:
        row = conn.execute(
            "SELECT text,removed_at FROM bot_memory WHERE id=?", (drop["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "lives in a boat" and row[1]
    with pytest.raises(sqlite3.DatabaseError):
        raw(bots, "DELETE FROM bot_memory WHERE id=?", keep["id"])
    with pytest.raises(sqlite3.DatabaseError):
        raw(bots, "UPDATE bot_memory SET text='x' WHERE id=?", keep["id"])


def test_every_method_refuses_another_persons_bot(bots):
    b = bot(bots)
    ask = bots.add_user_message(b, "u1", "private", REQ)
    note = bots.memory_add(b, "u1", "private note")
    pending = bots.pending_create(b, "u1", "stop_work", {}, "Stop")
    calls = [
        lambda: bots.get(b, "u2"),
        lambda: bots.update(b, "u2", name="Mine now"),
        lambda: bots.roster("u2", b),
        lambda: bots.mark_read(b, "u2", 1),
        lambda: bots.add_user_message(b, "u2", "hi", "request-9999"),
        lambda: bots.add_bot_message(b, "u2", ask["id"], "hi"),
        lambda: bots.add_event_message(b, "u2", "hi"),
        lambda: bots.messages(b, "u2"),
        lambda: bots.message_for_request(b, "u2", REQ),
        lambda: bots.reply_to(b, "u2", ask["id"]),
        lambda: bots.action_start(b, "u2", REQ, 9, "t", "h"),
        lambda: bots.action_finish(b, "u2", REQ, 1, "ok"),
        lambda: bots.actions(b, "u2", REQ),
        lambda: bots.pending_create(b, "u2", "t", {}, "l"),
        lambda: bots.pending_get(b, "u2", pending["id"]),
        lambda: bots.pending_resolve(b, "u2", pending["id"], "done"),
        lambda: bots.work_add(b, "u2", "work", 1, 1),
        lambda: bots.work_list(b, "u2"),
        lambda: bots.memory_add(b, "u2", "x"),
        lambda: bots.memory_list(b, "u2"),
        lambda: bots.memory_remove(b, "u2", note["id"]),
        lambda: bots.get("no-such-bot", "u1"),
    ]
    for call in calls:
        with pytest.raises(Missing):
            call()
    assert bots.get(b, "u1")["name"] == "Docs"
    assert [m["id"] for m in bots.memory_list(b, "u1")] == [note["id"]]
    assert bots.pending_get(b, "u1", pending["id"])["state"] == "open"


def test_data_survives_closing_and_reopening_the_store(bots):
    b = bot(bots)
    ask = bots.add_user_message(b, "u1", "remember me", REQ)
    bots.add_bot_message(b, "u1", ask["id"], "I will", model="local/model")
    note = bots.memory_add(b, "u1", "likes tea")
    bots.mark_read(b, "u1", ask["id"])
    bots.close()
    again = bots.reopen()
    [row] = again.roster("u1")
    assert (row["name"], row["unread"], row["last"]["text"]) == ("Docs", 1, "I will")
    assert [m["text"] for m in again.messages(b, "u1")] == ["remember me", "I will"]
    assert again.messages(b, "u1")[1]["model"] == "local/model"
    assert again.add_user_message(b, "u1", "remember me", REQ)["id"] == ask["id"]
    assert [m["id"] for m in again.memory_list(b, "u1")] == [note["id"]]


def test_padded_text_is_bounded_by_its_raw_length_and_stored_verbatim(bots):
    b = bot(bots)
    for padded in (" " + "x" * 8000, "x" * 8000 + "\n"):
        with pytest.raises(Invalid):
            bots.add_user_message(b, "u1", padded, REQ)
    with pytest.raises(Invalid):
        bots.memory_add(b, "u1", " " + "m" * 2000)
    with pytest.raises(Invalid):
        bots.add_user_message(b, "u1", "  \n ", REQ)
    inside = "  " + "x" * 7998
    assert bots.add_user_message(b, "u1", inside, REQ)["text"] == inside
    note = "\t" + "m" * 1999
    assert bots.memory_add(b, "u1", note)["text"] == note
    assert bots.messages(b, "u1")[0]["text"] == inside


def test_text_that_cannot_be_stored_is_invalid_not_a_crash(bots):
    b = bot(bots)
    with pytest.raises(Invalid):
        bots.memory_add(b, "u1", "lone \ud800 surrogate")
    with pytest.raises(Invalid):
        bots.add_user_message(b, "u1", "lone \udfff surrogate", REQ)


def test_names_keep_their_stored_bound_when_padded(bots):
    assert bots.create("u1", " " + "n" * 60)["name"] == "n" * 60


def test_an_expired_pending_cannot_be_cancelled_either(bots):
    b = bot(bots)
    pending = bots.pending_create(b, "u1", "stop_work", {}, "Stop", ttl_s=-1)
    assert not bots.pending_resolve(b, "u1", pending["id"], "cancelled")
    assert bots.pending_get(b, "u1", pending["id"])["state"] == "expired"
    assert not bots.pending_resolve(b, "u1", pending["id"], "done")


def test_memory_ids_outside_the_integer_range_are_simply_unknown(bots):
    b = bot(bots)
    assert not bots.memory_remove(b, "u1", 2**63)
    assert not bots.memory_remove(b, "u1", -1)
