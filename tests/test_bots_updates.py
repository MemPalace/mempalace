"""Updates between turns: events on a bot's work reach its thread once, only for its owner."""

import logging
import threading
from pathlib import Path

import pytest

from commonplace import events, sessions
from commonplace.bots import store
from tests._bots_helpers import bots_hub_fixture  # noqa: F401
from tests.test_commonplace_boards import service

CONTEXT = "The implementation is complete and the affected tests have passed."
TITLE = "Tidy the Zebra crossing"
QUESTION = "Should the zebra crossing ship now or wait for the next release?"
OPTIONS = ["Ship now", "Wait"]
REPLY = "On it: I read your note,\n  and will file it today — café included."


class Scene:
    """A project with the owner's bots, an orchestrator agent and a worker agent."""

    def __init__(self, hub):
        self.hub = hub
        self.admin = hub.hub.admin
        self.owner = service(hub.hub, hub.person(self.admin))
        self.coordinator = service(hub.hub, hub.principal(hub.t1))
        self.worker = service(hub.hub, hub.principal(hub.t2))
        self.store = hub.store()
        self.bot = self.store.create(self.admin, "Filer")["id"]
        self.other_bot = self.store.create(self.admin, "Bystander")["id"]

    def card(self, bot=None, title=TITLE):
        number = self.owner.task_add(title=title, board="work")["number"]
        self.link(number, bot)
        return number

    def link(self, number, bot=None, user=None):
        user = user or self.admin
        bot = bot or self.bot
        message = self.store.add_user_message(bot, user, "file this", f"req-{bot[:6]}-{number}")
        self.store.work_add(bot, user, "work", number, message["id"])

    def ask(self, number):
        self.worker.task_update(number, status="doing")
        self.worker.task_update(
            number,
            status="review",
            question=QUESTION,
            context=CONTEXT,
            options=OPTIONS,
            person_reason="Only the owner can decide this",
        )

    def ready(self, number):
        self.worker.task_update(number, status="doing")
        self.worker.task_update(number, status="done", context=CONTEXT)

    def events(self, bot=None, user=None):
        found = self.store.messages(bot or self.bot, user or self.admin)
        return [m for m in found if m["role"] == "event"]

    def forward(self, text="please look at this", bot=None):
        posted = self.owner.space_post("work", text)["id"]
        message = self.store.add_user_message(
            bot or self.bot, self.admin, text, f"fw-{posted:04d}0"
        )
        self.store.forward_add(bot or self.bot, self.admin, "work", posted, message["id"])
        return posted


@pytest.fixture(name="scene")
def scene_fixture(bots_hub):
    scene = Scene(bots_hub)
    bots_hub.set_coordinator = lambda: bots_hub.request(
        bots_hub.owner, "PATCH", "/api/boards/work", {"coordinator": "a1"}
    )
    assert bots_hub.set_coordinator()[0] == 200
    with scene.owner.store.connection() as conn:
        conn.execute("UPDATE boards SET done_needs_review=1 WHERE slug='work'")
    return scene


def test_a_question_for_the_person_posts_one_event_with_a_decision_block(scene):
    number = scene.card()
    scene.ask(number)
    (event,) = scene.events()
    assert event["text"] == f"A question about “{TITLE}” needs you."
    assert event["blocks"] == [{"type": "decision", "card": number}]
    assert scene.events(scene.other_bot) == []


def test_a_card_ready_for_review_and_then_done_posts_one_event_each(scene):
    number = scene.card()
    scene.ready(number)
    scene.owner.decide(number, choice="Accept the completed work", then="done")
    ready, done = scene.events()
    assert ready["text"] == f"“{TITLE}” is ready for you to look at."
    assert done["text"] == f"“{TITLE}” is done."
    assert ready["blocks"] == done["blocks"] == [{"type": "work", "card": number}]


def test_progress_that_needs_nobody_posts_nothing(scene):
    number = scene.card()
    scene.worker.task_update(number, status="doing")
    scene.worker.task_comment(number, "halfway there")
    assert scene.events() == []


def test_the_same_event_twice_and_a_second_event_for_one_transition_add_one_message(scene):
    number = scene.card()
    scene.ready(number)
    db, workspace = scene.hub.hub.db, scene.hub.hub.workspace
    found = [e for e in events.after(db, workspace, 0) if e["data"].get("number") == number]
    assert {e["kind"] for e in found} >= {"card.review_needed"}
    for event in found + found:
        events.notify(event)
    events.emit(db, workspace, "card.moved", data=dict(found[-1]["data"]))
    assert len(scene.events()) == 1


def test_a_card_that_is_asked_again_after_an_answer_posts_again(scene):
    number = scene.card()
    scene.ask(number)
    scene.owner.decide(number, choice=OPTIONS[0])
    scene.worker.task_update(number, status="doing")
    scene.ask(number)
    assert [e["text"] for e in scene.events()] == [f"A question about “{TITLE}” needs you."] * 2


def test_only_the_bot_that_filed_the_card_gets_the_event(scene):
    mine, theirs = scene.card(), scene.card(scene.other_bot, title="Another job")
    scene.ready(mine)
    assert [e["blocks"][0]["card"] for e in scene.events()] == [mine]
    assert scene.events(scene.other_bot) == []
    scene.ready(theirs)
    assert [e["blocks"][0]["card"] for e in scene.events(scene.other_bot)] == [theirs]
    assert len(scene.events()) == 1


def test_another_persons_bot_linked_to_another_card_gets_nothing(scene):
    hub = scene.hub
    member = hub.member_user
    bot = scene.store.create(member, "Member bot")["id"]
    other_card = scene.owner.task_add(title="Member job", board="work")["number"]
    scene.link(other_card, bot, user=member)
    mine = scene.card()
    scene.ready(mine)
    assert scene.events(bot, member) == []
    assert len(scene.events()) == 1
    scene.ready(other_card)
    assert len(scene.events(bot, member)) == 1
    assert len(scene.events()) == 1  # the owner's bot never filed the member's card


def test_a_person_who_lost_access_to_the_item_gets_nothing(scene):
    hub = scene.hub
    member = hub.member_user
    bot = scene.store.create(member, "Member bot")["id"]
    number = scene.owner.task_add(title="Member job", board="work")["number"]
    scene.link(number, bot, user=member)
    db = hub.hub.db
    with db._lock:
        db._conn.execute(
            "UPDATE users SET disabled_at='2026-01-01T00:00:00Z' WHERE id=?", (member,)
        )
    scene.ready(number)
    assert scene.events(bot, member) == []
    row = scene.store._connection().execute("SELECT last_word FROM bot_work WHERE bot_id=?", (bot,))
    assert row.fetchone()["last_word"] is None


def test_a_card_on_an_archived_project_is_not_announced(scene):
    number = scene.card()
    scene.ready(number)
    assert len(scene.events()) == 1
    other = scene.card(title="Second")
    scene.hub.archive_board("work")
    db = scene.hub.hub.db
    events.emit(
        db,
        scene.hub.hub.workspace,
        "card.moved",
        data={"board": "work", "number": other, "status": "done"},
    )
    assert len(scene.events()) == 1


def test_an_orchestrator_reply_to_a_forwarded_post_arrives_verbatim_and_marked(scene):
    post = scene.forward()
    reply = scene.coordinator.space_post("work", REPLY)["id"]
    assert reply > post
    (event,) = scene.events()
    assert event["text"] == REPLY
    assert event["blocks"] == [{"type": "orchestrator"}]
    assert scene.events(scene.other_bot) == []
    again = [e for e in events.after(scene.hub.hub.db, scene.hub.hub.workspace, 0)]
    for item in again:
        events.notify(item)
    scene.coordinator.space_post("work", "A later message that answers nothing new")
    assert len(scene.events()) == 1


def test_only_the_first_coordinator_reply_after_the_post_answers_it(scene):
    scene.forward()
    scene.coordinator.space_post("work", "first")
    scene.coordinator.space_post("work", "second")
    assert [e["text"] for e in scene.events()] == ["first"]


def test_a_reply_from_an_agent_that_is_not_the_coordinator_is_ignored(scene):
    scene.forward()
    scene.worker.space_post("work", "I am only a worker")
    assert scene.events() == []
    scene.coordinator.space_post("work", "coordinator here")
    assert [e["text"] for e in scene.events()] == ["coordinator here"]


def test_an_unrelated_chat_post_gets_no_message(scene):
    scene.owner.space_post("work", "not forwarded by any bot")
    scene.coordinator.space_post("work", "answer to the person, not to a bot")
    assert scene.events() == []


def test_a_reply_posted_before_the_forward_does_not_answer_it(scene):
    scene.coordinator.space_post("work", "old news")
    scene.forward()
    assert scene.events() == []


def test_a_card_no_bot_filed_costs_no_store_write(scene, monkeypatch):
    writes = []
    original = store.BotStore._tx

    def spy(self, *, write=True):
        writes.append(write)
        return original(self, write=write)

    monkeypatch.setattr(store.BotStore, "_tx", spy)
    number = scene.owner.task_add(title="Nobody's bot filed this", board="work")["number"]
    scene.ready(number)
    scene.owner.decide(number, choice="Accept the completed work", then="done")
    scene.coordinator.space_post("work", "hello")
    assert writes
    assert not any(writes)


def test_a_workspace_without_bots_creates_no_store_file(scene, bots_hub):
    principal = sessions.person_principal(
        bots_hub.hub.db, bots_hub.other_user, bots_hub.other_workspace
    )
    other = service(bots_hub.hub, principal)
    other.board_add(slug="mine", wing="shared")
    other.task_add(title="Solo", board="mine")
    path = Path(bots_hub.hub.supervisor.workspace_dir(bots_hub.other_workspace))
    assert not (path / "bots.sqlite3").exists()


def test_the_listener_swallows_a_store_failure_and_the_card_write_succeeds(
    scene, monkeypatch, caplog
):
    number = scene.card()

    def broken(self, *args, **kwargs):
        raise RuntimeError(f"disk said: {TITLE}")

    monkeypatch.setattr(store.BotStore, "work_changed", broken)
    with caplog.at_level(logging.ERROR):
        scene.ready(number)
    assert scene.owner.task_show(number)["status"] == "review"
    assert any("bot update failed" in r.getMessage() for r in caplog.records)
    assert all(TITLE not in r.getMessage() for r in caplog.records)
    monkeypatch.undo()


def test_a_card_event_that_is_not_in_the_log_is_ignored(scene):
    number = scene.card()
    scene.ready(number)
    fake = {
        "seq": 10**6,
        "at": "2026-01-01T00:00:00Z",
        "workspace_id": scene.hub.hub.workspace,
        "kind": "card.moved",
        "subject": f"#{number}",
        "actor": "x",
        "data": {"board": "work", "number": number},
    }
    scene.owner.decide(number, choice="Accept the completed work", then="done")
    before = len(scene.events())
    events.notify(fake)
    assert len(scene.events()) == before


def test_roster_state_and_unread_follow_the_events(scene):
    hub = scene.hub

    def row():
        return next(b for b in hub.bots(hub.owner)["bots"] if b["id"] == scene.bot)

    assert (row()["state"], row()["unread"]) == ("idle", 0)
    number = scene.card()
    scene.worker.task_update(number, status="doing")
    assert (row()["state"], row()["unread"]) == ("working", 0)
    scene.ask(number)
    assert (row()["state"], row()["unread"]) == ("needs_you", 1)
    assert row()["last"]["text"].startswith("A question about")
    scene.owner.decide(number, choice=OPTIONS[0])
    scene.worker.task_update(number, status="doing")
    assert (row()["state"], row()["unread"]) == ("working", 1)
    newest = scene.events()[-1]["id"]
    assert hub.post(hub.owner, f"/api/bots/{scene.bot}/read", {"upto": newest})[0] == 200
    scene.ready(number)
    scene.owner.decide(number, choice="Accept the completed work", then="done")
    assert (row()["state"], row()["unread"]) == ("idle", 2)
    body = hub.get(hub.owner, f"/api/bots/{scene.bot}/messages")[1]
    assert body["bot"]["state"] == "idle"
    assert [m["role"] for m in body["messages"]][-1] == "event"
    assert body["messages"][-1]["blocks"][0]["title"] == TITLE


def test_working_state_ignores_cards_other_bots_filed(scene):
    hub = scene.hub
    number = scene.card(scene.other_bot)
    scene.worker.task_update(number, status="doing")
    states = {b["id"]: b["state"] for b in hub.bots(hub.owner)["bots"]}
    assert (states[scene.bot], states[scene.other_bot]) == ("idle", "working")


def test_logs_hold_no_titles_or_reply_text(scene, caplog):
    with caplog.at_level(logging.DEBUG):
        number = scene.card()
        scene.ask(number)
        scene.forward()
        scene.coordinator.space_post("work", REPLY)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert TITLE not in text and "café" not in text and QUESTION not in text
    assert scene.events()


def test_a_title_is_shown_redacted_and_never_interpreted(scene):
    secret = "sk-" + "a1B2c3D4" * 5
    number = scene.card(title=f"Fix {secret}\n\n<b>bold</b> {{title}} %s")
    scene.ready(number)
    (event,) = scene.events()
    assert secret not in event["text"]
    assert "\n" not in event["text"]
    assert "<b>bold</b> {title} %s" in event["text"]


def test_an_existing_bots_file_gains_the_new_columns(scene, tmp_path):
    conn = scene.store._connection()
    conn.execute("DROP TABLE bot_work")
    conn.execute("DROP TABLE bot_forwards")
    conn.executescript(
        "CREATE TABLE bot_work (bot_id TEXT NOT NULL, board TEXT NOT NULL, card INTEGER NOT NULL,"
        " message_id INTEGER NOT NULL, PRIMARY KEY (bot_id, board, card));"
        "CREATE TABLE bot_forwards (bot_id TEXT NOT NULL, board TEXT NOT NULL,"
        " space_message_id INTEGER NOT NULL, message_id INTEGER NOT NULL,"
        " PRIMARY KEY (bot_id, board, space_message_id));"
    )
    scene.store.close()
    reopened = scene.hub.store()

    def names(table):
        return {r["name"] for r in reopened._connection().execute(f"PRAGMA table_info({table})")}

    assert {"last_word", "last_seq"} <= names("bot_work")
    assert "answered_by" in names("bot_forwards")


def test_a_stale_handler_after_a_newer_one_is_a_no_op_and_the_marker_stays(scene):
    number = scene.card()
    args = (scene.bot, scene.admin, "work", number)
    assert scene.store.work_changed(*args, 6, "done", "done text") is not None
    assert scene.store.work_changed(*args, 5, "ready", "ready text") is None
    assert [e["text"] for e in scene.events()] == ["done text"]
    row = scene.store._connection().execute("SELECT * FROM bot_work WHERE bot_id=?", (scene.bot,))
    assert row.fetchone()["last_word"] == "done"
    assert scene.store.work_changed(*args, 6, "ready", "again") is None  # redelivery of seq 6


def test_a_fast_review_then_done_posts_both_in_order_whoever_writes_first(scene):
    number = scene.card()
    args = (scene.bot, scene.admin, "work", number)
    assert scene.store.work_changed(*args, 5, "ready", "ready text")
    assert scene.store.work_changed(*args, 6, "done", "done text")
    assert [e["text"] for e in scene.events()] == ["ready text", "done text"]


def test_two_threads_racing_on_one_event_post_one_message(scene):
    number = scene.card()
    start, results = threading.Barrier(8), []

    def handle():
        start.wait()
        results.append(
            scene.store.work_changed(scene.bot, scene.admin, "work", number, 9, "ready", "once")
        )

    threads = [threading.Thread(target=handle) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([r for r in results if r]) == 1
    assert [e["text"] for e in scene.events()] == ["once"]


def test_a_forwarded_post_is_answered_once_and_never_taken_over(scene):
    post = scene.forward()
    args = (scene.bot, scene.admin, "work")
    first = scene.store.forward_answered(*args, post + 1, [post], "first")
    assert first and first["text"] == "first"
    assert scene.store.forward_answered(*args, post + 1, [post], "first") is None
    assert scene.store.forward_answered(*args, post + 2, [post], "later") is None
    assert [e["text"] for e in scene.events()] == ["first"]


def test_replies_handled_out_of_order_still_go_to_the_right_post(scene):
    hub = scene.hub.hub
    events.remove_listener(hub.gateway.bots_listener)  # deliver by hand, in the wrong order
    try:
        first = scene.forward("one")
        reply_one = scene.coordinator.space_post("work", "answer one")["id"]
        second = scene.forward("two")
        reply_two = scene.coordinator.space_post("work", "answer two")["id"]
        assert first < reply_one < second < reply_two
        found = {
            e["data"]["message_id"]: e
            for e in events.after(hub.db, hub.workspace, 0, 500)
            if e["kind"] == "space_replied"
        }
        hub.gateway.bots_listener(found[reply_two])
        assert [e["text"] for e in scene.events()] == ["answer two"]  # the post "one" is not taken
        hub.gateway.bots_listener(found[reply_one])
        hub.gateway.bots_listener(found[reply_one])
        assert [e["text"] for e in scene.events()] == ["answer two", "answer one"]
    finally:
        events.add_listener(hub.gateway.bots_listener)


def test_a_reply_that_arrives_before_the_earlier_one_for_the_same_post_is_skipped(scene):
    hub = scene.hub.hub
    events.remove_listener(hub.gateway.bots_listener)
    try:
        scene.forward()
        reply_one = scene.coordinator.space_post("work", "first")["id"]
        reply_two = scene.coordinator.space_post("work", "second")["id"]
        found = {
            e["data"]["message_id"]: e
            for e in events.after(hub.db, hub.workspace, 0, 500)
            if e["kind"] == "space_replied"
        }
        hub.gateway.bots_listener(found[reply_two])
        assert scene.events() == []
        hub.gateway.bots_listener(found[reply_one])
        hub.gateway.bots_listener(found[reply_two])
        assert [e["text"] for e in scene.events()] == ["first"]
    finally:
        events.add_listener(hub.gateway.bots_listener)


def test_a_person_with_no_unfinished_filed_work_costs_no_board_read(scene, monkeypatch):
    hub = scene.hub
    from commonplace.boards.service import BoardService

    reads = []
    original = BoardService.task_list
    monkeypatch.setattr(
        BoardService, "task_list", lambda self, *a, **k: reads.append(k) or original(self, *a, **k)
    )
    hub.bots(hub.owner)
    assert reads == []
    number = scene.card()
    scene.ready(number)
    scene.owner.decide(number, choice="Accept the completed work", then="done")
    reads.clear()
    states = {b["id"]: b["state"] for b in hub.bots(hub.owner)["bots"]}
    assert reads == [] and states[scene.bot] == "idle"
    unfinished = scene.card()
    reads.clear()
    hub.bots(hub.owner)
    assert len(reads) == 1 and unfinished
