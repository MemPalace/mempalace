"""Checkpoint visibility of builders started outside the runner (kind ``thread``).

A journaled thread start for a card that is in ``doing`` under a live claim shows up in
the checkpoint's ``jobs``; either fact alone does not. The checkpoint stays read-only.
"""

import hashlib

import pytest

from commonplace.boards.service import BoardService
from commonplace.orch import durable, store as orch_store
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_orch_durable import BOARD, OTHER_ROLE, ROLE, Env, at, config_text, lease

OTHER_BOARD = "plain"
NOTE = "T3 thread thr_example started for the card"
LIVE_UNTIL = at(3600)
NOW = at(10)


@pytest.fixture
def env(hub):
    return Env(hub)


def claim(env, principal, card, *, until=LIVE_UNTIL, board=BOARD):
    """Put ``card`` in doing under ``principal`` with a claim that is live at ``T0``."""
    BoardService(env.ctx(principal)).task_take_next(board=board, task=card)
    with env.boards_db() as conn:
        conn.execute("UPDATE tasks SET claim_until=? WHERE id=?", (until, card))


def start_thread(env, epoch, token, key, card, *, board=BOARD, role=ROLE, state="started"):
    ctx = env.ctx(env.a)
    durable.journal_intent(
        ctx, board, role, epoch, token, key, "thread", card=card, detail="builder", now=at(1)
    )
    if state is None:
        return
    durable.journal_record(
        ctx,
        board,
        role,
        epoch,
        token,
        key,
        state,
        evidence={"type": "holder_attested", "note": NOTE},
        now=at(2),
    )


def thread_jobs(env, board=BOARD, role=ROLE, now=NOW):
    jobs = durable.checkpoint(env.ctx(env.a), board, role, now=now)["jobs"]
    return [job for job in jobs if job["source"] == "journal"]


def test_journaled_thread_start_with_live_doing_assignee_is_a_checkpoint_job(env):
    epoch, token = lease(env)
    card = env.card("Build it")
    claim(env, env.b, card)
    start_thread(env, epoch, token, "t3-1", card)

    assert thread_jobs(env) == [
        {
            "id": None,
            "task_number": card,
            "state": "started",
            "outcome_code": None,
            "thread_id": None,
            "started_at": at(2),
            "updated_at": at(2),
            "step_key": "t3-1",
            "source": "journal",
            "assignee": env.b.actor,
            "claim_until": at(3600),
            "detail": "builder",
            "evidence": {
                "type": "holder_attested",
                "note": NOTE,
                "by": durable._actor_ref(env.a),
            },
        }
    ]


def test_thread_job_needs_both_the_recorded_start_and_a_live_doing_assignee(env):
    epoch, token = lease(env)
    intent_only, todo, expired, finished, voided, done_card, no_card = (
        env.card(title) for title in ("i", "t", "e", "f", "v", "d", "n")
    )
    for card in (intent_only, expired, finished, voided, done_card, no_card):
        claim(env, env.b, card)
    start_thread(env, epoch, token, "intent-only", intent_only, state=None)
    start_thread(env, epoch, token, "todo", todo)
    start_thread(env, epoch, token, "expired", expired)
    start_thread(env, epoch, token, "finished", finished)
    start_thread(env, epoch, token, "voided", voided)
    start_thread(env, epoch, token, "done-card", done_card)
    # A doing card with a live assignee but no journaled start for it.
    durable.journal_intent(
        env.ctx(env.a), BOARD, ROLE, epoch, token, "no-card", "thread", now=at(1)
    )
    durable.journal_record(
        env.ctx(env.a), BOARD, ROLE, epoch, token, "no-card", "started", now=at(2)
    )
    with env.boards_db() as conn:
        conn.execute("UPDATE tasks SET claim_until=? WHERE id=?", (at(5), expired))
    durable.journal_record(
        env.ctx(env.a), BOARD, ROLE, epoch, token, "finished", "failed", now=at(3)
    )
    durable.void_step(env.ctx(env.owner), BOARD, ROLE, "voided", now=at(3))
    BoardService(env.ctx(env.b)).task_update(done_card, status="done")

    assert thread_jobs(env) == []
    # The same expired card was listed while its claim was live.
    assert [job["step_key"] for job in thread_jobs(env, now=at(4))] == ["expired"]


def test_thread_job_is_scoped_to_its_board_and_role(env):
    text = config_text(board=OTHER_BOARD)
    orch_store.open_store(env.hub.gateway, env.ws).write(
        env.owner,
        OTHER_BOARD,
        text,
        connections=[],
        sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    other_card = env.owner_board.task_add("Elsewhere", board=OTHER_BOARD)["number"]
    claim(env, env.b, other_card, board=OTHER_BOARD)
    claim_other = durable.resume(env.ctx(env.a), OTHER_BOARD, ROLE, now=at(0))
    start_thread(
        env, claim_other["epoch"], claim_other["token"], "t3-1", other_card, board=OTHER_BOARD
    )
    role_card = env.card("Other role")
    claim(env, env.b, role_card)
    role_epoch, role_token = lease(env, role=OTHER_ROLE)
    start_thread(env, role_epoch, role_token, "t3-2", role_card, role=OTHER_ROLE)

    assert thread_jobs(env) == []
    assert [job["task_number"] for job in thread_jobs(env, board=OTHER_BOARD)] == [other_card]
    assert [job["task_number"] for job in thread_jobs(env, role=OTHER_ROLE)] == [role_card]


def test_thread_job_stays_listed_when_a_new_epoch_marks_the_step_in_doubt(env):
    epoch, token = lease(env)
    started, never_started = env.card("Started"), env.card("Never started")
    claim(env, env.b, started, until=at(9000))
    claim(env, env.b, never_started, until=at(9000))
    start_thread(env, epoch, token, "t3-1", started)
    start_thread(env, epoch, token, "t3-2", never_started, state=None)
    later = at(durable.TTL_SECONDS + 60)
    env.resume(env.a, now=later)

    jobs = thread_jobs(env, now=later)
    assert [(job["step_key"], job["state"], job["started_at"]) for job in jobs] == [
        ("t3-1", "in_doubt", at(2))
    ]


def test_checkpoint_with_thread_jobs_writes_nothing(env):
    epoch, token = lease(env)
    card = env.card()
    claim(env, env.b, card)
    start_thread(env, epoch, token, "t3-1", card)

    def snapshot():
        with env.boards_db() as conn:
            tasks = [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]
        conn = env.runner_db()
        try:
            return tasks, {
                table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
                for table in ("orch_steps", "orch_journal", "orch_leases")
            }
        finally:
            conn.close()

    before = snapshot()
    assert len(thread_jobs(env)) == 1
    assert snapshot() == before


def test_stale_open_thread_steps_do_not_crowd_out_a_live_builder(env, monkeypatch):
    monkeypatch.setattr(durable, "MAX_LIST", 2)
    epoch, token = lease(env)
    live = env.card("Live")
    claim(env, env.b, live)
    start_thread(env, epoch, token, "live", live)
    for index in range(3):
        start_thread(env, epoch, token, f"stale-{index}", env.card(f"Stale {index}"))

    assert [job["step_key"] for job in thread_jobs(env)] == ["live"]


def test_no_thread_step_means_no_extra_read():
    # Neither store is opened without a candidate step: a context or store of None
    # would raise on first use.
    steps = [{"kind": "merge", "card": 1, "voided_at": None, "state": "started"}]
    assert durable._thread_jobs(None, None, {"id": 1, "slug": BOARD}, ROLE, steps, NOW) == []
    assert durable._thread_jobs(None, None, {"id": 1, "slug": BOARD}, ROLE, [], NOW) == []
