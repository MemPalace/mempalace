"""Every waiting reason has one plain sentence from boards.why, shown by API and app alike."""

import json
import subprocess
from types import SimpleNamespace

from commonplace.boards import why
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._phase_b_helpers import board, cap, claim, doing
from tests._split_helpers import blockers, world

PAST = "2000-01-01T00:00:00Z"


def _row(**fields):
    return {"status": "todo", "ask": None, "assignee": None, "ask_to": None, **fields}


def _wait(row=None, **kwargs):
    kwargs.setdefault("blockers", [])
    kwargs.setdefault("released", True)
    kwargs.setdefault("usage", None)
    return why.waiting(row or _row(), **kwargs)


def test_pure_precedence_and_text_for_every_code():
    full = SimpleNamespace(used=1, cap=1)
    blocked = [{"number": 7, "title": "Dependency"}]
    cases = {
        "blocked_by": _wait(blockers=blocked),
        "waits_on_person": _wait(_row(ask="Which one?")),
        "claim_expired": _wait(_row(status="doing"), claim_expired=True),
        "plan_review": _wait(released=False, plan_review=True),
        "not_released": _wait(released=False),
        "no_agent": _wait(agents=False),
        "project_limit": _wait(usage=full),
        "checks_pending": _wait(_row(status="review", checks="pending")),
    }
    assert tuple(cases) == why.CODES
    for code, reason in cases.items():
        assert reason["code"] == code and reason["text"] == why.text(reason) and reason["text"]
    assert cases["blocked_by"]["text"] == "Waiting on #7: Dependency"
    assert cases["project_limit"]["text"] == "Waiting for a free slot: 1 of 1"
    # The first applicable reason wins, in CODES order.
    assert _wait(blockers=blocked, released=False, agents=False, usage=full)["code"] == "blocked_by"
    assert _wait(released=False, agents=False, usage=full)["code"] == "not_released"
    assert _wait(agents=False, usage=full)["code"] == "no_agent"


def test_pure_no_reason_when_nothing_waits():
    assert _wait() is None and _wait(agents=None) is None and _wait(agents=True) is None
    assert _wait(_row(status="done"), blockers=[{"number": 1, "title": "x"}]) is None
    # Unknown agent availability never invents a reason; a doing card is not "no agent".
    assert _wait(_row(status="doing"), agents=False) is None
    # An unreleased card in doing or review is not waiting for release.
    assert _wait(_row(status="doing"), released=False) is None


def test_pure_notes_are_plain_and_ordered():
    summary = dict.fromkeys(why.CODES, 0)
    assert why.notes(summary) == []
    summary.update(blocked_by=1, project_limit=3, no_agent=2, claim_expired=1, checks_pending=1)
    lines = why.notes(summary, used=2, cap=2)
    assert [n["code"] for n in lines] == [
        "blocked_by",
        "claim_expired",
        "no_agent",
        "project_limit",
        "checks_pending",
    ]
    text = {n["code"]: n["text"] for n in lines}
    assert text["blocked_by"] == "1 card wait for other cards to be Done."
    assert (
        text["project_limit"]
        == "Agent limit reached (2 of 2 working): 3 cards wait for a free slot."
    )
    assert text["no_agent"] == "No agent is connected, so 2 cards cannot start."


def _show(w, card):
    return w.boards.task_show(card).get("waiting")


def test_card_claim_expired(hub):
    w = world(hub)
    a = w.card()
    doing(w, a, w.b.actor)
    assert _show(w, a) is None
    with w.boards.store.transaction() as conn:
        conn.execute("UPDATE tasks SET claim_until=? WHERE id=?", (PAST, a))
    reason = _show(w, a)
    assert reason["code"] == "claim_expired" and reason["text"] == why.text(reason)
    assert board(w)["waiting"]["claim_expired"] == 1


def test_expired_claim_is_explanatory_and_never_refuses_the_holder(hub):
    w = world(hub)
    a = w.card()
    doing(w, a, w.a.actor)
    with w.boards.store.transaction() as conn:
        conn.execute("UPDATE tasks SET claim_until=? WHERE id=?", (PAST, a))
    status, held = claim(w, a)
    assert status == 200 and held["number"] == a


def test_card_plan_review_and_release_are_told_apart(hub):
    w = world(hub)
    plan, origin = w.card(title="Plan"), w.card(title="Origin")
    with w.boards.store.transaction() as conn:
        conn.execute("UPDATE tasks SET labels='[\"plan\"]' WHERE id=?", (plan,))
        conn.execute(
            "UPDATE tasks SET restricted_origin=1,proposal_status='pending' WHERE id=?", (origin,)
        )
    assert _show(w, plan)["code"] == "plan_review"
    assert _show(w, plan)["text"] == "Waiting for plan review"
    assert _show(w, origin)["code"] == "not_released"
    summary = board(w)["waiting"]
    assert summary["plan_review"] == 1 and summary["not_released"] == 1


def test_plan_review_still_refuses_a_claim_with_its_own_code(hub):
    w = world(hub)
    plan = w.card(title="Plan")
    with w.boards.store.transaction() as conn:
        conn.execute("UPDATE tasks SET labels='[\"plan\"]' WHERE id=?", (plan,))
    status, error = claim(w, plan)
    assert status >= 400 and error["code"] == "plan_review"
    assert error["error"].endswith("Waiting for plan review")


def test_card_no_agent_connected(hub):
    w = world(hub)
    a = w.card()
    assert _show(w, a) is None
    with w.db._lock:
        w.db._conn.execute("UPDATE connections SET revoked_at=? WHERE workspace_id=?", (PAST, w.ws))
    reason = _show(w, a)
    assert reason["code"] == "no_agent" and reason["text"] == why.text(reason)
    assert board(w)["waiting"]["no_agent"] == 1
    assert any("No agent is connected" in n["text"] for n in board(w)["waiting_notes"])


def test_card_checks_pending(hub):
    w = world(hub)
    a = w.card()
    doing(w, a, w.b.actor)
    with w.boards.store.transaction() as conn:
        conn.execute("UPDATE tasks SET checks='pending' WHERE id=?", (a,))
    reason = _show(w, a)
    assert reason["code"] == "checks_pending" and reason["text"] == "Waiting for checks to finish"


def test_card_blocked_person_limit_still_report_through_the_shared_text(hub):
    w = world(hub)
    dep, b, person, limited = (w.card(title=t) for t in ("Dep", "B", "Person", "Limited"))
    blockers(w, b, dep)
    with w.boards.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='review',restricted_origin=1,proposal_status='pending',"
            "ask='Decision?',ask_options='[\"Yes\",\"No\"]',ask_to=? WHERE id=?",
            (w.owner.actor, person),
        )
    cap(w, 1)
    doing(w, w.card(title="Busy"), w.b.actor)
    seen = {n: _show(w, n) for n in (b, person, limited)}
    assert [seen[n]["code"] for n in (b, person, limited)] == [
        "blocked_by",
        "waits_on_person",
        "project_limit",
    ]
    assert all(r["text"] == why.text(r) for r in seen.values())


def test_board_list_notes_explain_why_few_agents_are_working(hub):
    w = world(hub)
    cap(w, 1)
    doing(w, w.card(title="Busy"), w.b.actor)
    w.card(title="Waits one")
    w.card(title="Waits two")
    row = board(w)
    assert row["used"] == 1 and row["max_agents"] == 1 and row["waiting"]["project_limit"] == 2
    assert row["waiting_notes"] == [
        {
            "code": "project_limit",
            "count": 2,
            "text": "Agent limit reached (1 of 1 working): 2 cards wait for a free slot.",
        }
    ]


def test_orchestration_off_adds_no_waiting_fields(hub):
    w = world(hub, on=False)
    a = w.card()
    assert "waiting" not in w.boards.task_show(a)
    assert "waiting_notes" not in board(w) and "waiting" not in board(w)


def _ui():
    run = subprocess.run(
        ["node", "tests/board_why.mjs"], capture_output=True, text=True, check=True
    )
    return json.loads(run.stdout)


def test_app_shows_the_server_sentence_for_every_reason():
    ui = _ui()
    assert len(ui["tags"]) == len(why.CODES)
    assert ui["tags"][0] == "Waiting on #7: Dependency"
    assert ui["tags"][5] == why.text({"code": "no_agent"})
    assert ui["tags"][6] == "Waiting for a free slot: 1 of 1"
    assert ui["details"] == [True] * len(why.CODES)
    assert ui["detailsPlain"] is False
    assert ui["available"] is True and ui["availableWhenWaiting"] is False


def test_app_board_strip_lists_notes_and_escapes_text():
    ui = _ui()
    assert "<b>" not in ui["escaped"] and "&lt;b&gt;x" in ui["escaped"]
    assert ui["strip"].count("<li ") == 2 and 'data-why="no_agent"' in ui["strip"]
    assert "Agent limit reached (1 of 1 working): 2 cards wait for a free slot." in ui["strip"]
    assert ui["stripEmpty"] == ["", "", ""]
    assert ui["inBoard"] is True and ui["noStripInBoard"] is False
