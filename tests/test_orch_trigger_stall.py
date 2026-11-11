"""An idle board with open work must still start it: the trigger sweep never wedges.

Live failure (board ``commonplace``, 10-11 Nov 2026): orchestration was on, the accepted
config named its orchestrator role ``coordinator`` (no ``main``), and every sweep raised
``KeyError`` before ``fenced_start``. The cursor never moved, so no card on the board
was started or missed again. Released restricted-origin cards also never fired.
"""

from commonplace.boards.service import BoardService
from commonplace.orch import runner_bridge, schema, triggers
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._split_helpers import world
from tests.test_orch_runner import BOARD, config_text

T0 = "2030-01-01T00:00:00Z"


def live_shaped(machine, **fields):
    """The live board's shape: orchestrator role ``coordinator`` runs the cards."""
    text = config_text(machine, **fields)
    return (
        text.replace("[roles.main]", "[roles.coordinator]")
        .replace('parent = "main"', 'parent = "coordinator"')
        .replace('role = "builder"', 'role = "coordinator"')
    )


def configure_live_shaped(w):
    w.write(live_shaped(w.machine_id))
    triggers.arm_cursor(w.gw, w.ws, BOARD)


def test_split_start_without_main_role_starts_the_card(hub):
    w = world(hub, runner=True)
    configure_live_shaped(w)
    a = w.card()
    triggers.run(w.js, now=T0)
    rows = w.jobs_for(a)
    assert len(rows) == 1 and rows[0]["topic"] == "orchestrate"
    assert rows[0]["launch_role"] == "coordinator"
    with w.js.connection() as conn:
        message = runner_bridge.launch_fields(conn, rows[0])["message"]
    assert "orchestrator_inbox" in message and "Coordinate this project" in message
    assert w.cursor()[0] == w.max_event()


def test_unplannable_card_is_a_miss_and_later_cards_still_start(hub, monkeypatch):
    w = world(hub, on=False, runner=True)
    first, second = w.card(), w.card()
    prepare = runner_bridge.prepare

    def refuse_first(*args, **kwargs):
        if kwargs["card_row"]["id"] == first:
            raise schema.ConfigError("roles_invalid", "roles.coordinator", "missing role")
        return prepare(*args, **kwargs)

    monkeypatch.setattr(runner_bridge, "prepare", refuse_first)
    report = triggers.run(w.js, now=T0)
    assert report[BOARD] is not None
    assert [(m["card"], m["code"]) for m in w.misses()] == [(first, "config_invalid")]
    assert not w.jobs_for(first)
    assert len(w.jobs_for(second)) == 1
    triggers.run(w.js, now=T0)
    assert len(w.misses()) == 1 and len(w.jobs_for(second)) == 1


def test_origin_release_of_a_waiting_todo_card_starts_it(hub):
    w = world(hub, on=False, runner=True)
    a = w.card()
    with w.boards.store.transaction() as conn:  # a restricted-origin card waiting in todo
        conn.execute("UPDATE tasks SET restricted_origin=1, approved_by=NULL WHERE id=?", (a,))
    triggers.run(w.js, now=T0)
    assert not w.jobs_for(a)
    assert [(m["card"], m["code"]) for m in w.misses()] == [(a, "origin_not_released")]
    BoardService(w.ctx(w.owner)).task_approve(a, approve=True, reason="release for build")
    triggers.run(w.js, now=T0)
    rows = w.jobs_for(a)
    assert len(rows) == 1, "a released todo card was never started"
    assert a not in w.unstarted()


# The live board's accepted config v3 (roles, runner role, triggers), with the test
# machine id and the runtime mode the test machine grants instead of "auto".
LIVE_V3 = """schema = "commonplace.orchestrator/1"
project = "acme"

[limits]
max_agents = 20

[roles.coordinator]
kind = "orchestrator"
harness = "t3"
topic = "runner"
model_role = "main"
prompt = '''Coordinate the board. Release restricted-origin cards as soon as they appear.'''

[roles.builder]
kind = "builder"
parent = "coordinator"
harness = "t3"
topic = "build"
model_role = "worker"
prompt = '''Do build work on your assigned card only.'''

[runner]
role = "coordinator"
machine = "__MACHINE__"
project = "demo"
runtime_mode = "approval-required"

[[triggers]]
on = "card.todo"
"""


def test_live_v3_config_starts_cards_without_a_keyerror(hub, caplog):
    from tests._phase_b_helpers import World, seed

    w = World(hub, topics=("runner", "build"))
    seed(w, True)
    w.write(LIVE_V3.replace("__MACHINE__", w.machine_id))
    triggers.arm_cursor(w.gw, w.ws, BOARD)
    cards = [w.card(), w.card()]
    report = triggers.run(w.js, now=T0)
    assert "KeyError" not in caplog.text
    assert report[BOARD] is not None and w.cursor()[0] == w.max_event()
    for card in cards:
        rows = w.jobs_for(card)
        assert len(rows) == 1, w.misses()
        assert rows[0]["topic"] == "runner"
        assert rows[0]["launch_role"] == "coordinator"


def test_repeated_sweep_failure_alerts_the_owner_once(hub, monkeypatch):
    from commonplace import events

    w = world(hub, on=False, runner=True)

    def broken(*args, **kwargs):
        raise KeyError("boom")

    monkeypatch.setattr(triggers, "_sweep_board", broken)
    triggers.run(w.js, now=T0)
    assert w.boards.needs_me()["orchestrator_failing"] == []  # one failure is not a streak
    triggers.run(w.js, now=T0)
    triggers.run(w.js, now=T0)
    failing = w.boards.needs_me()["orchestrator_failing"]
    assert [(f["board"], f["failures"], f["error"]) for f in failing] == [(BOARD, 3, "KeyError")]
    alerts = [e for e in events.after(w.db, w.ws, 0) if e["kind"] == "orch.trigger.failing"]
    assert len(alerts) == 1 and alerts[0]["data"]["board"] == BOARD
    monkeypatch.undo()
    triggers.run(w.js, now=T0)
    assert w.boards.needs_me()["orchestrator_failing"] == []
