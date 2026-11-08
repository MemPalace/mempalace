"""Task rows: the server-derived ``need`` of a card, and where it shows up."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from commonplace.boards.display import NEEDS, need, need_for
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_boards import api, env, mcp, service  # noqa: F401

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
FRESH, STALE = "2026-10-30T11:45:00Z", "2026-10-30T11:00:00Z"
ME, OTHER = "user:usr_me", "user:usr_other"


def card(**fields):
    base = {
        "status": "todo",
        "ask": None,
        "ask_to": None,
        "labels": [],
        "pr_url": None,
        "checks": None,
        "heartbeat_at": None,
        "updated_at": STALE,
    }
    return {**base, **fields}


def derive(c, me=ME, owner=True):
    c = dict(c)
    release = c.pop("person_release", None)
    return need(c, me=me, owner=owner, now=NOW, person_release=release)


ASK = {"status": "review", "ask": "Which?"}
PR = "https://github.com/140er/mempalace/pull/7"
REPORT = {"pr_state": "open", "pr_head": "a" * 40, "pr_reported_at": FRESH}


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({**ASK, "ask_to": ME}, "you"),
        ({**ASK, "ask_to": OTHER}, "idle"),
        ({**ASK, **REPORT, "ask_to": OTHER, "pr_url": PR, "checks": "passing"}, "merge"),
        ({"status": "review"}, "idle"),
        ({"labels": ["plan"]}, "plan"),
        (
            {**REPORT, "status": "doing", "pr_url": PR, "checks": "passing", "heartbeat_at": FRESH},
            "merge",
        ),
        ({"status": "doing", "pr_url": PR, "checks": "passing", "heartbeat_at": FRESH}, "working"),
        (
            {
                **REPORT,
                "status": "doing",
                "pr_url": PR,
                "checks": "passing",
                "heartbeat_at": FRESH,
                "pr_reported_at": STALE,
            },
            "working",
        ),
        (
            {
                **REPORT,
                "status": "doing",
                "pr_url": PR,
                "checks": "passing",
                "heartbeat_at": FRESH,
                "pr_state": "merged",
            },
            "working",
        ),
        ({"status": "backlog", "proposal_status": "pending", "person_release": False}, "plan"),
        ({"status": "doing", "checks": "pending", "heartbeat_at": FRESH}, "checks"),
        ({"status": "doing", "heartbeat_at": FRESH}, "working"),
        ({"status": "doing", "updated_at": FRESH}, "working"),
        ({"status": "doing", "heartbeat_at": STALE}, "idle"),
        ({"status": "todo"}, "idle"),
        ({"status": "backlog"}, "idle"),
        ({"status": "done", "pr_url": PR, "checks": "passing", "labels": ["plan"]}, "done"),
    ],
)
def test_every_state(fields, expected):
    assert derive(card(**fields)) == expected
    assert expected in NEEDS


def test_ask_without_recipient_goes_to_the_owner():
    assert derive(card(**ASK)) == "you"
    assert derive(card(**ASK), owner=False) == "idle"
    assert derive(card(**ASK, ask_to=ME), me=None, owner=True) == "idle"


def test_only_a_signed_in_person_is_you():
    def viewer(actor, person, role="owner"):
        return SimpleNamespace(actor=actor, person=person, role=role)

    to_owner, to_me = card(**ASK), card(**ASK, ask_to=ME)
    assert need_for(to_owner, viewer(ME, True), now=NOW) == "you"
    assert need_for(to_me, viewer(ME, True, "member"), now=NOW) == "you"
    assert need_for(to_owner, viewer(ME, True, "member"), now=NOW) == "idle"
    # An agent token belongs to the owner but never inherits the owner's asks.
    assert need_for(to_owner, viewer("conn:agent", False), now=NOW) == "idle"
    assert need_for(card(**ASK, ask_to="conn:agent"), viewer("conn:agent", False)) == "idle"
    assert need_for(to_owner, None, now=NOW) == "idle"


def test_precedence_follows_needs_order():
    # Each step adds the next-weaker signal; the stronger one keeps winning.
    everything = card(
        **REPORT,
        status="review",
        ask="Ship it?",
        ask_to=ME,
        labels=["plan"],
        pr_url=PR,
        checks="passing",
        heartbeat_at=FRESH,
    )
    assert derive(everything) == "you"
    assert derive({**everything, "ask_to": OTHER}) == "merge"
    assert derive({**everything, "ask_to": OTHER, "labels": []}) == "merge"
    doing = {**everything, "status": "doing", "ask": None, "ask_to": None, "labels": []}
    assert derive(doing) == "merge"
    assert derive({**doing, "checks": "pending"}) == "checks"
    assert derive({**doing, "pr_url": None}) == "working"
    assert derive({**doing, "pr_url": None, "heartbeat_at": STALE}) == "idle"
    assert derive({**doing, "status": "done"}) == "done"
    # Origin queue projections rank release between person decisions and review;
    # ordinary task rows above still derive their existing needs.
    assert NEEDS == (
        "you",
        "release",
        "plan",
        "merge",
        "checks",
        "expired",
        "working",
        "idle",
        "done",
    )


def test_failing_checks_and_pr_without_passing_checks_stay_quiet():
    assert derive(card(status="doing", pr_url=PR, checks="failing", heartbeat_at=FRESH)) == (
        "working"
    )
    assert derive(card(status="todo", pr_url=PR)) == "idle"


def test_need_in_rest_and_mcp_output(hub, env):  # noqa: F811
    token, agent, person, cookie, owner = env
    worker = service(hub, agent)
    asked = worker.ask(
        "Ship? Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    planned = owner.task_add(title="Plan", board="work", labels=["plan"])
    idle = owner.task_add(title="Later", board="work")
    building = worker.task_add(title="Build", board="work")
    worker.task_take_next(task=building["number"])
    checks = worker.task_add(title="Checks", board="work")
    worker.task_take_next(task=checks["number"])
    worker.task_update(checks["number"], pr=7, checks="pending")
    merge = worker.task_add(title="Merge", board="work")
    worker.task_take_next(task=merge["number"])
    worker.task_update(merge["number"], pr=8, checks="passing", pr_state="open", pr_head="a" * 40)
    stale = worker.task_add(title="Stale", board="work")
    worker.task_take_next(task=stale["number"])
    finished = owner.seed_card(board="work", title="Finished", status="done")
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET updated_at='2000-01-01T00:00:00Z',heartbeat_at='2000-01-01T00:00:00Z' WHERE id=?",
            (stale["number"],),
        )
    expected = {
        asked["number"]: "you",
        planned["number"]: "you",
        merge["number"]: "merge",
        checks["number"]: "checks",
        building["number"]: "working",
        idle["number"]: "idle",
        stale["number"]: "idle",
        finished["number"]: "done",
    }
    status, listed = api(hub, cookie, "GET", "/api/boards/work/cards")
    assert status == 200
    assert {c["number"]: c["need"] for c in listed["cards"]} == expected
    status, shown = api(hub, cookie, "GET", f"/api/cards/{asked['number']}")
    assert status == 200 and shown["need"] == "you"
    status, needs = api(hub, cookie, "GET", "/api/needs-me")
    assert [c["need"] for c in needs["asks"]] == ["you"]
    assert needs["ready_to_merge"] == []
    assert needs["stale"] == []

    ok, output = mcp(hub, token, "task_list", board="work", status="doing")
    assert ok
    assert f"#{building['number']} doing need:working" in output
    assert f"#{checks['number']} doing need:checks" in output
    assert f"#{merge['number']} doing need:merge" in output
    assert f"#{stale['number']} doing need:idle" in output
    # The owner's web session sees the agent's ask as theirs; the asking agent does not.
    ok, output = mcp(hub, token, "task_show", task=asked["number"])
    assert ok and output.startswith(f"#{asked['number']} review need:idle")
    status, agents = api(hub, token, "GET", f"/api/cards/{asked['number']}")
    assert status == 200 and agents["need"] == "idle"

    member = hub.db.create_user("member@example.com")
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
    member_token = hub.token(
        "member",
        user=member,
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wings=["shared"],
        trust="standard",
    )
    status, theirs = api(hub, member_token, "GET", f"/api/cards/{asked['number']}")
    assert status == 200 and theirs["need"] == "idle"
