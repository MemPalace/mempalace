"""No-progress wake (#386): an idle board with open work wakes its orchestrator.

The hub measures progress, not liveness: open actionable work and no card done for N
minutes emits exactly one ``board.stalled`` wake, then backs off; a done card restarts
the timer; a board whose open cards all wait on a blocker or a person is never nudged.
"""

import pytest

from commonplace import events
from commonplace.boards import service as board_service, store as board_store
from commonplace.orch import triggers
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._phase_b_helpers import doing
from tests._split_helpers import blockers, world
from tests.test_orch_runner import BOARD


def at(minute: int) -> str:
    return f"2030-01-01T{minute // 60:02d}:{minute % 60:02d}:00Z"


@pytest.fixture
def clock(monkeypatch):
    """Board writes are stamped at ``clock.minute``; sweeps pass ``at(minute)``."""

    class Clock:
        minute = 0

        def sweep(self, w, minute):
            self.minute = minute
            triggers.run(w.js, now=at(minute))

    c = Clock()
    for module in (board_store, board_service):
        monkeypatch.setattr(module, "now", lambda: at(c.minute))
    return c


def wakes(w):
    return [e for e in events.after(w.db, w.ws, 0, limit=1000) if e["kind"] == "board.stalled"]


def test_idle_board_with_open_work_gets_exactly_one_wake(hub, clock):
    w = world(hub, on=False, runner=True)
    a, b = w.card(), w.card()
    doing(w, a, w.a.actor)  # an agent card in doing, one unstarted todo card
    clock.sweep(w, 10)
    assert wakes(w) == []
    clock.sweep(w, 21)
    got = wakes(w)
    assert len(got) == 1
    data = got[0]["data"]
    assert data["board"] == BOARD and data["doing"] == [a] and data["todo"] == [b]
    assert "No card finished in the last 20 minutes" in data["summary"]
    clock.sweep(w, 30)  # backoff: no second wake inside another N minutes
    assert len(wakes(w)) == 1
    clock.sweep(w, 42)
    assert len(wakes(w)) == 2 and wakes(w)[-1]["data"]["streak"] == 2


def test_a_done_card_restarts_the_timer(hub, clock):
    w = world(hub, on=False, runner=True)
    a, b = w.card(), w.card()
    clock.minute = 15
    doing(w, a, w.owner.actor)
    w.update(a, status="done")
    clock.sweep(w, 30)  # 15 minutes after the done card
    assert wakes(w) == []
    clock.sweep(w, 36)
    assert len(wakes(w)) == 1 and wakes(w)[0]["data"]["todo"] == [b]


def test_only_blocked_or_person_cards_never_wake(hub, clock):
    w = world(hub, on=False, runner=True)
    blocker = w.card(status="backlog")
    waiting = w.card()
    blockers(w, waiting, blocker)
    held_by_person = w.card()
    doing(w, held_by_person, w.owner.actor)
    clock.sweep(w, 120)
    assert wakes(w) == []


def test_three_unanswered_wakes_reach_the_owner_and_off_switch(hub, clock):
    w = world(hub, on=False, runner=True)
    w.card()
    for minute in (21, 42, 63):
        clock.sweep(w, minute)
    stalled = w.boards.needs_me().get("orchestrator_stalled")
    assert stalled and [(s["board"], s["nudges"]) for s in stalled] == [(BOARD, 3)]
    from commonplace.orch import stall

    stall.set_minutes(w.st, BOARD, 0)
    clock.sweep(w, 200)
    assert len(wakes(w)) == 3
    with pytest.raises(ValueError):
        stall.set_minutes(w.st, BOARD, -1)
