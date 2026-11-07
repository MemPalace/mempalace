"""One actor-independent waiting reason, shared by views, claims and tags."""

import json
import subprocess

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._phase_b_helpers import board, cap, doing
from tests._split_helpers import blockers, take, world


def test_r15_blockers_first(hub):
    w = world(hub)
    a, b = w.card(title="Dependency"), w.card()
    blockers(w, b, a)
    with w.boards.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET restricted_origin=1,proposal_status='pending',assignee=? WHERE id=?",
            (w.owner.actor, b),
        )
    cap(w, 1)
    doing(w, w.card(), w.b.actor)
    reason = w.boards.task_show(b).get("waiting")
    assert reason and reason["code"] == "blocked_by" and reason["blockers"][0]["number"] == a  # RED


def test_r16_limit_and_summary(hub):
    w = world(hub)
    cap(w, 1)
    doing(w, w.card(), w.b.actor)
    b = w.card()
    reason = w.boards.task_show(b).get("waiting")
    assert reason and (reason["code"], reason["used"], reason["cap"]) == (
        "project_limit",
        1,
        1,
    )  # RED
    assert board(w)["waiting"]["project_limit"] == 1


@pytest.mark.parametrize("kind", ["waits_on_person", "not_released"])
def test_r17_person_and_release(hub, kind):
    w = world(hub)
    b = w.card()
    with w.boards.store.transaction() as conn:
        if kind == "waits_on_person":
            conn.execute(
                "UPDATE tasks SET status='review',restricted_origin=1,proposal_status='pending',ask='Decision?',ask_options='[\"Yes\",\"No\"]',ask_to=? WHERE id=?",
                (w.owner.actor, b),
            )
        else:
            conn.execute(
                "UPDATE tasks SET restricted_origin=1,proposal_status='pending' WHERE id=?", (b,)
            )
    cap(w, 1)
    doing(w, w.card(), w.b.actor)
    reason = w.boards.task_show(b).get("waiting")
    assert reason and reason["code"] == kind  # RED


@pytest.mark.parametrize("kind", ["blocked_by", "project_limit"])
def test_r18_claim_uses_shared_text(hub, kind):
    w = world(hub)
    b = w.card()
    if kind == "blocked_by":
        blockers(w, b, w.card(title="Dependency"))
    else:
        cap(w, 1)
        doing(w, w.card(), w.b.actor)
    reason = w.boards.task_show(b).get("waiting")
    status, error = take(w, b)
    assert status >= 400 and reason and error["error"] == reason["text"]  # RED
    from commonplace.boards import why

    assert error["error"] == why.text(reason)


def test_r21_waiting_tags():
    run = subprocess.run(
        ["node", "tests/board_why.mjs"], capture_output=True, text=True, check=True
    )
    assert json.loads(run.stdout) == [
        "Waiting on #7: Dependency",
        "Waiting for a free slot: 1 of 1",
    ]  # RED
