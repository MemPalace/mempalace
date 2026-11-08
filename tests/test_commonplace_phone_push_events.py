"""Public-route notification coverage; paired evidence is run after integration."""

import importlib
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit

from commonplace import events, sessions
from commonplace.policy import Policy
from tests.phone_push_harness import (
    agent,
    api,
    cli,
    clock,
    enable,
    fresh as fresh,
    hub as hub,
    off,
    on,
    positive_get,
    receiver as receiver,
    remote as remote,
    revoke,
    rows,
    s1_counter,
    signup as signup,
)

QUESTION = "Which approach should we use for this change?"
CONTEXT = "Both approaches are ready; choose one so work can continue."
OPTIONS = ["Continue", "Revise"]


def worker(hub, owner, name="worker", *, parent=None):
    result = agent(hub, owner, name, parent=parent, unrestricted=parent is None)
    result.name = name
    return result


def call(hub, owner, method, path, data=None, *, actor=None, expected=200):
    code, body = api(hub, owner, method, path, data, token=actor.token if actor else None)
    assert code == expected, (code, body)
    return body


def create(hub, owner, *, actor=None, **fields):
    return call(
        hub,
        owner,
        "POST",
        "/api/boards/commonplace/cards",
        {"title": "Synthetic work item", **fields},
        actor=actor,
        expected=201,
    )


def show(hub, owner, number):
    return call(hub, owner, "GET", f"/api/cards/{number}")


def update(hub, owner, number, *, actor=None, **fields):
    card = show(hub, owner, number)
    return call(
        hub,
        owner,
        "PATCH",
        f"/api/cards/{number}",
        {"version": card["version"], **fields},
        actor=actor,
    )


def ask(hub, owner, number, *, actor=None, target="owner"):
    return call(
        hub,
        owner,
        "POST",
        f"/api/cards/{number}/handoff",
        {"to": target, "note": QUESTION, "context": CONTEXT, "options": OPTIONS},
        actor=actor,
    )


def reopen(hub, owner, number, agent):
    update(hub, owner, number, status="todo")
    return call(
        hub,
        owner,
        "POST",
        f"/api/cards/{number}/handoff",
        {"to": f"conn:{agent.name}", "note": "Continue work on this item"},
    )


def decide(hub, owner, number, *, then="return"):
    card = show(hub, owner, number)
    return call(
        hub,
        owner,
        "POST",
        f"/api/cards/{number}/decide",
        {"choice": OPTIONS[0], "then": then, "version": card["version"]},
    )


def comment(hub, owner, number, *, actor=None):
    return call(
        hub,
        owner,
        "POST",
        f"/api/cards/{number}/comments",
        {"text": "A synthetic progress note"},
        actor=actor,
    )


def needs(hub, owner):
    return call(hub, owner, "GET", "/api/needs-me")


def state(hub, number=None, category=None):
    return [
        row
        for row in rows(hub, "phone_push_state")
        if (number is None or row["number"] == number)
        and (category is None or row["category"] == category)
    ]


def deliveries(hub, number=None, category=None):
    return [
        row
        for row in rows(hub, "phone_push_deliveries")
        if (number is None or row["number"] == number)
        and (category is None or row["category"] == category)
    ]


def bodies(hub):
    return [request.body.decode() for request in hub.ntfy.requests]


def cursor(hub):
    found = [row for row in rows(hub, "meta") if row["key"] == "phone_push_seq"]
    assert len(found) == 1, found
    return int(found[0]["value"])


def latest(hub):
    with hub.db._lock:
        return int(hub.db._conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0])


def shifted(iso, minutes):
    return (datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ") + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def wake_once(monkeypatch, dispatcher, start):
    """Run a real daemon wake and join it, with a single bounded completion signal."""
    drained = threading.Event()
    original = dispatcher.drain

    def drain():
        try:
            return original()
        finally:
            drained.set()

    monkeypatch.setattr(dispatcher, "drain", drain)
    start(dispatcher)
    try:
        assert drained.wait(5), "dispatcher wake did not finish"
    finally:
        dispatcher.stop()
    assert not dispatcher.thread.is_alive()


def test_t2_categories_claim_and_click(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    agent = worker(hub, owner)
    card = create(hub, owner, actor=agent)
    number = card["number"]
    ask(hub, owner, number, actor=agent)
    backlog = create(hub, owner, actor=agent, status="backlog")
    pr_card = create(hub, owner, actor=agent, claim=True)
    update(
        hub,
        owner,
        pr_card["number"],
        actor=agent,
        pr="https://example.test/project/repo/pull/7",
        pr_state="open",
        pr_head="a" * 40,
        checks="passing",
    )
    assert dispatcher.reconcile()
    pending = deliveries(hub)
    assert len(pending) == 1, pending
    assert pending[0]["status"] == "pending" and pending[0]["attempt"] == 0
    dispatcher.drain()
    assert bodies(hub) == [f"#{number} needs you"]
    request = hub.ntfy.requests[0]
    assert request.headers["Priority"] == "high"
    assert re.fullmatch(r"https://cp\.example/s/[0-9A-Za-z]{7}", request.headers["Click"])
    accepted = deliveries(hub)
    assert len(accepted) == 1
    assert accepted[0]["status"] == "accepted" and accepted[0]["attempt"] == 1
    report = needs(hub, owner)
    expected = {
        c["number"] for key in ("asks", "done_review", "backlog_from_agents") for c in report[key]
    }
    assert expected == {number}
    assert backlog["number"] not in expected and pr_card["number"] not in expected
    assert {r["number"] for r in state(hub, category="needs_you")} == expected


def test_t3_blocked_uses_staleness_not_checks(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    agent = worker(hub, owner)
    stuck = create(hub, owner, actor=agent, claim=True)
    number = stuck["number"]
    update(hub, owner, number, actor=agent, checks="failing")
    clock(monkeypatch, shifted(stuck["claim_until"], -15))
    dispatcher.reconcile()
    dispatcher.sweep()
    dispatcher.drain()
    assert deliveries(hub) == [] and bodies(hub) == []
    clock(monkeypatch, shifted(stuck["claim_until"], -1))
    dispatcher.sweep()
    assert deliveries(hub) == []
    clock(monkeypatch, shifted(stuck["claim_until"], 1))
    dispatcher.sweep()
    dispatcher.drain()
    assert bodies(hub) == [f"#{number} is blocked"]
    dispatcher.sweep()
    dispatcher.drain()
    assert len(hub.ntfy.requests) == 1


def test_t4_done_only_for_named_person_and_other_actors(hub, signup, monkeypatch):
    """The board's done_needs_review flag defaults to 0 (boards/schema.sql)."""
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    agent = worker(hub, owner)
    related = create(hub, owner, assignee=f"conn:{agent.name}")
    unrelated = create(hub, owner, actor=agent)
    own = create(hub, owner, status="doing")
    # A second person named on a card in the same workspace is not the recipient.
    second, _ = signup[2](email="second@example.test")
    me = hub.me(owner)
    cli(
        "--data",
        os.path.dirname(hub.db.path),
        "member",
        "add",
        me["workspace"],
        "second@example.test",
        "--role",
        "owner",
        "--by",
        me["user"],
    )
    assert hub.me(second)["user"] != me["user"]
    second_here = login(hub, "second@example.test", me["workspace"])
    foreign = create(hub, second_here, assignee=f"conn:{agent.name}")
    dispatcher.reconcile()
    update(hub, owner, related["number"], actor=agent, status="done")
    update(hub, owner, unrelated["number"], actor=agent, status="done")
    update(hub, owner, own["number"], status="done")
    update(hub, owner, foreign["number"], actor=agent, status="done")
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub) == [f"#{related['number']} is done"]
    assert len(state(hub, related["number"], "done")) == 1
    assert len(state(hub, own["number"], "done")) == 1
    assert not state(hub, unrelated["number"], "done")
    assert not state(hub, foreign["number"], "done")


def test_t5_dedupe_comments_and_separate_reentries(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    agent = worker(hub, owner)
    card = create(hub, owner, actor=agent)
    number = card["number"]
    ask(hub, owner, number, actor=agent)
    dispatcher.reconcile()
    dispatcher.drain()
    assert len(state(hub, number, "needs_you")) == 1
    assert bodies(hub) == [f"#{number} needs you"]
    comment(hub, owner, number, actor=agent)
    comment(hub, owner, number, actor=agent)
    dispatcher.reconcile()
    dispatcher.drain()
    assert len(state(hub, number, "needs_you")) == 1
    assert len(hub.ntfy.requests) == 1
    decide(hub, owner, number)
    dispatcher.reconcile()
    assert not state(hub, number, "needs_you")
    ask(hub, owner, number, actor=agent)
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub).count(f"#{number} needs you") == 2
    related = create(hub, owner, assignee=f"conn:{agent.name}")
    done = related["number"]
    dispatcher.reconcile()
    update(hub, owner, done, actor=agent, status="done")
    dispatcher.reconcile()
    dispatcher.drain()
    reopen(hub, owner, done, agent)
    dispatcher.reconcile()
    update(hub, owner, done, actor=agent, status="done")
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub).count(f"#{done} is done") == 2


def test_t5b_leave_and_reenter_within_one_batch(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    agent = worker(hub, owner)
    card = create(hub, owner, actor=agent)
    number = card["number"]
    ask(hub, owner, number, actor=agent)
    dispatcher.reconcile()
    dispatcher.drain()
    before = state(hub, number, "needs_you")
    assert len(before) == 1 and "entered_seq" in before[0]
    decide(hub, owner, number)
    ask(hub, owner, number, actor=agent)
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub).count(f"#{number} needs you") == 2
    after = state(hub, number, "needs_you")
    assert len(after) == 1 and after[0]["entered_seq"] > before[0]["entered_seq"]
    related = create(hub, owner, assignee=f"conn:{agent.name}")
    done = related["number"]
    dispatcher.reconcile()
    update(hub, owner, done, actor=agent, status="done")
    dispatcher.reconcile()
    dispatcher.drain()
    reopen(hub, owner, done, agent)
    update(hub, owner, done, actor=agent, status="done")
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub).count(f"#{done} is done") == 2


def test_t6_opt_in_snapshot_and_cursor_replay(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    agent = worker(hub, owner)
    x = create(hub, owner, actor=agent)["number"]
    ask(hub, owner, x, actor=agent)
    y = create(hub, owner, assignee=f"conn:{agent.name}")["number"]
    update(hub, owner, y, actor=agent, status="done")
    lagging = cursor(hub)
    assert lagging < latest(hub)
    on(hub, owner)
    assert cursor(hub) == lagging
    z = create(hub, owner, actor=agent)["number"]
    ask(hub, owner, z, actor=agent)
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub) == [f"#{z} needs you"]
    assert len(state(hub, x, "needs_you")) == 1
    assert not state(hub, y)
    comment(hub, owner, x, actor=agent)
    dispatcher.reconcile()
    dispatcher.drain()
    before_state, before_deliveries = rows(hub, "phone_push_state"), deliveries(hub)
    # Deliberate cursor replay tests notification dedupe, never board data setup.
    with hub.db._lock, hub.db._tx() as conn:
        conn.execute("UPDATE meta SET value='0' WHERE key='phone_push_seq'")
    dispatcher.reconcile()
    dispatcher.reconcile()
    dispatcher.drain()
    assert rows(hub, "phone_push_state") == before_state
    assert deliveries(hub) == before_deliveries
    assert bodies(hub) == [f"#{z} needs you"]
    reopen(hub, owner, y, agent)
    update(hub, owner, y, actor=agent, status="done")
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub) == [f"#{z} needs you", f"#{y} is done"]


def test_t6b_lagging_cursor_cannot_backfill_done(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    agent = worker(hub, owner)
    number = create(hub, owner, assignee=f"conn:{agent.name}")["number"]
    update(hub, owner, number, actor=agent, status="done")
    done_seq = latest(hub)
    on(hub, owner)
    subscriptions = rows(hub, "phone_push_subscriptions")
    assert len(subscriptions) == 1
    assert subscriptions[0]["from_seq"] >= done_seq > cursor(hub)
    dispatcher.reconcile()
    dispatcher.drain()
    assert not deliveries(hub, number) and not state(hub, number) and not bodies(hub)
    new = create(hub, owner, actor=agent)["number"]
    ask(hub, owner, new, actor=agent)
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub) == [f"#{new} needs you"]


def test_t11_stopped_catchup_and_foreign_database_guard(hub, signup, fresh, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    other, mailbox = fresh()
    other_owner = other.signin(mailbox, "other@example.test")
    positive_get(other, other_owner)
    module = importlib.import_module("commonplace.phone_push")
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    agent = worker(hub, owner)
    module.stop(hub.gateway)
    number = create(hub, owner, actor=agent)["number"]
    ask(hub, owner, number, actor=agent)
    module.install(hub.gateway)
    dispatcher = hub.gateway.phone_push
    assert dispatcher is not None
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub) == [f"#{number} needs you"]
    before = cursor(hub)
    woke = threading.Event()
    monkeypatch.setattr(dispatcher, "wake", woke.set)
    events.add_listener(dispatcher.on_event)
    try:
        create(other, other_owner)
        with other.db._lock:
            event = events._row(
                other.db._conn.execute("SELECT * FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            )
        dispatcher.on_event(event)
        assert not woke.is_set() and cursor(hub) == before
        second = create(hub, owner, actor=agent)["number"]
        ask(hub, owner, second, actor=agent)
        assert woke.is_set()
        assert cursor(hub) == before  # Callback only wakes; evaluation is on the daemon.
        dispatcher.reconcile()
        dispatcher.drain()
        assert bodies(hub) == [f"#{number} needs you", f"#{second} needs you"]
    finally:
        events.remove_listener(dispatcher.on_event)
        module.stop(other.gateway)


def test_t11b_one_wake_drains_fifteen_hundred_stopped_events(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    module = importlib.import_module("commonplace.phone_push.dispatcher")
    start = module.PushDispatcher.start
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    # This synthetic gateway has no webhooks to dispatch during backlog setup.
    hub.gateway.webhooks.stop()
    agent = worker(hub, owner)
    number = create(hub, owner, actor=agent)["number"]
    ask(hub, owner, number, actor=agent)
    # Review entry emits two events and a decision emits one. Two real sessions
    # can fill the same 1,500-event backlog with fewer HTTP round trips, while
    # every write still passes normal auth, version, policy and rate guards.
    second, _ = signup[2]()
    assert second != owner
    assert hub.me(second)["user"] == hub.me(owner)["user"]
    cards = [create(hub, cookie) for cookie in (owner, second)]
    comment(hub, owner, number, actor=agent)

    def reviews(item):
        cookie, card = item
        for _ in range(249):
            card = call(
                hub,
                cookie,
                "PATCH",
                f"/api/cards/{card['number']}",
                {
                    "version": card["version"],
                    "status": "review",
                    "question": QUESTION,
                    "options": OPTIONS,
                    "context": CONTEXT,
                },
            )
            assert card["status"] == "review"
            card = call(
                hub,
                cookie,
                "POST",
                f"/api/cards/{card['number']}/decide",
                {"version": card["version"], "choice": OPTIONS[0], "then": "todo"},
            )
            assert card["status"] == "todo"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(reviews, zip((owner, second), cards)))
    target = latest(hub)
    assert target - cursor(hub) >= 1500
    # The initial board also contributes one space_created event.
    assert target - cursor(hub) == 1501
    wake_once(monkeypatch, dispatcher, start)
    assert cursor(hub) == target
    assert bodies(hub) == [f"#{number} needs you"]


def test_t15_entering_actor_survives_later_owner_comment(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    agent = worker(hub, owner)
    number = create(hub, owner, actor=agent)["number"]
    own = create(hub, owner, status="doing")["number"]
    dispatcher.reconcile()
    ask(hub, owner, number, actor=agent)
    comment(hub, owner, number)
    comment(hub, owner, own, actor=agent)
    update(hub, owner, own, status="done")
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub) == [f"#{number} needs you"]
    assert len(state(hub, own, "done")) == 1
    assert not deliveries(hub, own)


def fail_once(monkeypatch):
    module = importlib.import_module("commonplace.phone_push.evaluate")
    original = module.current
    failed = False

    def current(*args):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("synthetic evaluation failure")
        return original(*args)

    monkeypatch.setattr(module, "current", current)


def test_t16_evaluation_failure_rolls_back_entire_batch(hub, signup, monkeypatch, caplog):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    agent = worker(hub, owner)
    number = create(hub, owner, actor=agent)["number"]
    ask(hub, owner, number, actor=agent)
    before = cursor(hub)
    fail_once(monkeypatch)
    caplog.clear()
    assert not dispatcher.reconcile()
    assert cursor(hub) == before
    assert not rows(hub, "phone_push_state") and not deliveries(hub)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == ["phone push evaluation failed"]
    assert dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub) == [f"#{number} needs you"]


def test_t16b_sweep_failure_and_ended_row_cleanup_without_reconcile(
    hub, signup, monkeypatch, caplog
):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    agent = worker(hub, owner)
    stuck = create(hub, owner, actor=agent, claim=True)
    clock(monkeypatch, shifted(stuck["claim_until"], -1))
    dispatcher.reconcile()
    before = cursor(hub)
    clock(monkeypatch, shifted(stuck["claim_until"], 1))
    fail_once(monkeypatch)
    caplog.clear()
    dispatcher.sweep()
    assert not state(hub) and not deliveries(hub)
    assert cursor(hub) == before
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "phone push evaluation failed"
    ]
    dispatcher.sweep()
    dispatcher.drain()
    assert bodies(hub) == [f"#{stuck['number']} is blocked"]
    clock(monkeypatch, shifted(stuck["claim_until"], -1))
    question = create(hub, owner, actor=agent)["number"]
    ask(hub, owner, question, actor=agent)
    done = create(hub, owner, status="doing")["number"]
    dispatcher.reconcile()
    assert len(state(hub, question, "needs_you")) == 1
    queued = deliveries(hub, question)
    assert len(queued) == 1 and queued[0]["status"] == "pending"
    before = cursor(hub)
    decide(hub, owner, question)
    update(hub, owner, done, status="done")
    assert before < latest(hub)
    dispatcher.sweep()
    assert cursor(hub) == before
    assert not state(hub, question) and not state(hub, done)
    ended = deliveries(hub, question)
    assert len(ended) == 1 and ended[0]["status"] == "cancelled"


def login(hub, email, workspace):
    link = cli(
        "--data", os.path.dirname(hub.db.path), "login-link", email, "--workspace", workspace
    )
    parsed = urlsplit(link)
    code, headers, body = hub.request(parsed.path + "?" + parsed.query)
    assert code == 302, (code, body)
    assert "Set-Cookie" in headers
    return headers["Set-Cookie"].split(";", 1)[0]


def test_t20_multi_owner_done_and_nonentering_edits(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    agent = worker(hub, owner)
    old = create(hub, owner, assignee=f"conn:{agent.name}")["number"]
    update(hub, owner, old, actor=agent, status="done")
    second, _ = signup[2](email="second@example.test")
    assert hub.me(second)["user"] != hub.me(owner)["user"]
    me = hub.me(owner)
    cli(
        "--data",
        os.path.dirname(hub.db.path),
        "member",
        "add",
        me["workspace"],
        "second@example.test",
        "--role",
        "owner",
        "--by",
        me["user"],
    )
    second = login(hub, "second@example.test", me["workspace"])
    on(hub, owner)
    number = create(hub, owner)["number"]
    own = create(hub, owner)["number"]
    dispatcher.reconcile()
    ask(hub, second, number, target=f"user:{me['user']}")
    decide(hub, second, number, then="done")
    ask(hub, owner, own)
    decide(hub, owner, own, then="done")
    comment(hub, owner, old, actor=agent)
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub) == [f"#{number} is done"]
    assert len(state(hub, own, "done")) == 1 and not deliveries(hub, own)
    assert not state(hub, old) and not deliveries(hub, old)
    # Completed cards are frozen for agents; owners can still edit their titles.
    code, body = api(
        hub,
        owner,
        "PATCH",
        f"/api/cards/{old}",
        {"version": show(hub, owner, old)["version"], "title": "Changed title"},
        token=agent.token,
    )
    assert code == 403, body
    update(hub, second, old, title="Changed title")
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub) == [f"#{number} is done"]
    assert not state(hub, old) and not deliveries(hub, old)


def proposal(hub, owner, name):
    person = sessions.session_principal(hub.db, sessions.cookie_value(owner))
    parent_name = name + "-parent"
    cid, token = hub.db.create_token(
        name=parent_name,
        client="generic",
        actor=person.user_id,
        user_ref=person.user_id,
        workspace_ref=person.workspace_id,
        policy=Policy.build(
            profile="full",
            trust="standard",
            scopes=["memory:read", "memory:write"],
            read_wings=["shared"],
            write_wings=["shared"],
        ),
    )
    parent = SimpleNamespace(id=cid, token=token, name=parent_name)
    call(hub, owner, "PATCH", "/api/boards/commonplace", {"coordinator": "conn:" + parent_name})
    parent_card = create(hub, owner, actor=parent)
    child = worker(hub, owner, name + "-child", parent=parent.id)
    card = create(hub, owner, actor=child, parent=parent_card["number"])
    assert card["proposal_status"] == "pending"
    return parent, child, card["number"]


def listed(report):
    return {
        c["number"]
        for key in ("asks", "done_review", "backlog_from_agents", "stale")
        for c in report[key]
    }


def test_t21_eventless_needs_you_revoke_reconcile_and_sweep(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    parent, child, number = proposal(hub, owner, "first")
    on(hub, owner)
    dispatcher.reconcile()
    assert number not in listed(needs(hub, owner))
    assert not state(hub, number)
    before = latest(hub)
    revoke(hub, owner, parent)
    assert latest(hub) == before
    dispatcher.reconcile()
    dispatcher.sweep()
    dispatcher.drain()
    recovery = next(c for c in needs(hub, owner)["approvals"] if c["number"] == number)
    assert recovery["approval_kind"] == "origin" and recovery["actions"] == ["approve"]
    assert bodies(hub) == [f"#{number} needs you"]
    assert hub.ntfy.requests[0].headers["Priority"] == "high"
    dispatcher.sweep()
    dispatcher.drain()
    assert len(hub.ntfy.requests) == 1
    parent2, child2, second = proposal(hub, owner, "second")
    dispatcher.reconcile()
    assert second not in listed(needs(hub, owner)) and not state(hub, second)
    revoke(hub, owner, parent2)
    comment(hub, owner, second, actor=child2)
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub).count(f"#{second} needs you") == 1
    dispatcher.sweep()
    dispatcher.drain()
    assert bodies(hub).count(f"#{second} needs you") == 1
    parent3, child3, third = proposal(hub, owner, "third")
    dispatcher.reconcile()
    assert third not in listed(needs(hub, owner)) and not state(hub, third)
    revoke(hub, owner, parent3)
    # Another card's pending event prevents a sweep from bypassing event actors.
    comment(hub, owner, number, actor=child)
    assert cursor(hub) < latest(hub)
    dispatcher.sweep()
    assert not state(hub, third) and not deliveries(hub, third)
    dispatcher.reconcile()
    dispatcher.sweep()
    dispatcher.drain()
    assert bodies(hub).count(f"#{third} needs you") == 1


def test_t22a_one_needs_me_scan_per_subscription_per_pass(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    agent = worker(hub, owner)
    numbers = []
    for _ in range(5):
        number = create(hub, owner, actor=agent)["number"]
        ask(hub, owner, number, actor=agent)
        numbers.append(number)
    counter = s1_counter(monkeypatch)
    assert dispatcher.reconcile()
    assert counter.calls == 1
    assert {s["number"] for s in state(hub, category="needs_you")} == set(numbers)
    second, _ = signup[2](email="second@example.test")
    on(hub, second)
    counter.calls = 0
    dispatcher.sweep()
    assert counter.calls == 2


def test_t23_turn_off_during_evaluation_creates_no_rows(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    agent = worker(hub, owner)
    number = create(hub, owner, actor=agent)["number"]
    ask(hub, owner, number, actor=agent)
    module = importlib.import_module("commonplace.phone_push.evaluate")
    original = module.current

    def current(*args):
        cur = original(*args)
        off(hub, owner)
        return cur

    with monkeypatch.context() as patch:
        patch.setattr(module, "current", current)
        assert dispatcher.reconcile()
    assert cursor(hub) == latest(hub)
    assert not state(hub) and not deliveries(hub)
    on(hub, owner)
    second = create(hub, owner, actor=agent)["number"]
    ask(hub, owner, second, actor=agent)
    dispatcher.reconcile()
    dispatcher.drain()
    assert bodies(hub) == [f"#{second} needs you"]


def test_head_only_ineligible_subscription_does_not_abort_others(hub, signup, monkeypatch, caplog):
    """Head-only eligibility unit: a missing owner is skipped in both passes."""
    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    second, _ = signup[2](email="second@example.test")
    on(hub, second)
    first_agent = worker(hub, owner, "first-worker")
    second_agent = worker(hub, second, "second-worker")
    first = create(hub, owner, actor=first_agent)["number"]
    ask(hub, owner, first, actor=first_agent)
    other = create(hub, second, actor=second_agent)["number"]
    ask(hub, second, other, actor=second_agent)
    module = importlib.import_module("commonplace.phone_push.eligibility")
    original = module.owner_principal
    first_uid = hub.me(owner)["user"]
    monkeypatch.setattr(
        module,
        "owner_principal",
        lambda db, uid, ws: None if uid == first_uid else original(db, uid, ws),
    )
    caplog.clear()
    assert dispatcher.reconcile()
    dispatcher.sweep()
    dispatcher.drain()
    assert bodies(hub) == [f"#{other} needs you"]
    subs = rows(hub, "phone_push_subscriptions")
    invalid = [s for s in subs if s["user_id"] == first_uid]
    assert len(invalid) == 1
    assert not [s for s in state(hub) if s["subscription_id"] == invalid[0]["id"]]
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "phone push subscription is ineligible"
    ]


def test_real_thread_sweeps_and_stops(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    module = importlib.import_module("commonplace.phone_push.dispatcher")
    start = module.PushDispatcher.start
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    dispatcher.sweep_seconds = 0.02
    swept, drained = threading.Event(), threading.Event()
    sweep, drain = dispatcher.sweep, dispatcher.drain
    calls = []

    def observed_sweep():
        sweep()
        calls.append(1)
        swept.set()

    def observed_drain():
        result = drain()
        if swept.is_set():
            drained.set()
            # Park after exactly one sweep until stop wakes this real worker.
            dispatcher._stop.wait(2)
        return result

    monkeypatch.setattr(dispatcher, "sweep", observed_sweep)
    monkeypatch.setattr(dispatcher, "drain", observed_drain)
    start(dispatcher)
    try:
        assert drained.wait(1), "periodic sweep did not run"
        assert dispatcher.thread.is_alive()
        assert dispatcher.on_event in events._listeners
    finally:
        dispatcher.stop()
    assert calls == [1]
    assert not dispatcher.thread.is_alive()
    assert dispatcher.on_event not in events._listeners
