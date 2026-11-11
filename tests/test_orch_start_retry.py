"""A todo start refused for a transient reason is retried; a permanent refusal is not.

Everything runs on the real trigger path: cards are created through the board service,
``triggers.run`` sweeps them and ``fenced_start`` starts or refuses them. Time is the
``now`` the sweep is given, so the backoff is exact and nothing sleeps.
"""

from commonplace.orch import start_retry, triggers
from commonplace.runner import jobs
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._phase_b_helpers import cap
from tests._split_helpers import world
from tests.test_orch_runner import BOARD

T0 = "2030-01-01T00:00:00Z"


def at(seconds):
    return jobs._after(T0, seconds)


def retries(w):
    with w.js.connection() as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='orch_start_retries'"
        ).fetchone():
            return []
    return w.rows("SELECT * FROM orch_start_retries ORDER BY event_id")


def codes(w):
    return [(m["card"], m["code"]) for m in w.misses()]


def full_board(hub):
    """A board of one agent whose slot is taken: the second card is refused ``at_cap``."""
    w = world(hub, on=False, runner=True)
    cap(w, 1)
    first, second = w.card(), w.card()
    triggers.run(w.js, now=T0)
    assert len(w.jobs_for(first)) == 1 and not w.jobs_for(second)
    assert codes(w) == [(second, "at_cap")]
    return w, first, second


def test_at_cap_refusal_is_retried_after_its_backoff_and_then_starts(hub):
    w, first, second = full_board(hub)
    [row] = retries(w)
    assert (row["card"], row["state"], row["attempts"], row["last_code"]) == (
        second,
        "pending",
        0,
        "at_cap",
    )
    assert row["next_at"] == at(start_retry.BACKOFF[0])
    cursor = w.cursor()[0]
    assert cursor == w.max_event()  # the refusal still advanced the cursor

    report = triggers.run(w.js, now=at(start_retry.BACKOFF[0] - 1))
    assert report[BOARD] == [] and not w.jobs_for(second)  # not due yet

    cap(w, 2)  # a slot frees up
    report = triggers.run(w.js, now=at(start_retry.BACKOFF[0]))
    [result] = report[BOARD]
    assert (result["card"], result["retry"], result["miss"]) == (second, 1, None)
    [job] = w.jobs_for(second)
    assert result["job"] == job["id"]
    [row] = retries(w)
    assert (row["state"], row["attempts"], row["job_id"]) == ("started", 1, job["id"])
    assert w.cursor()[0] == cursor
    assert codes(w) == [(second, "at_cap")]  # the success adds no miss

    triggers.run(w.js, now=at(10_000))
    assert len(w.jobs_for(second)) == 1 and retries(w) == [row]  # nothing left to retry


def test_a_still_refused_retry_backs_off_further_and_records_every_miss(hub):
    w, first, second = full_board(hub)
    t1 = start_retry.BACKOFF[0]
    triggers.run(w.js, now=at(t1))
    [row] = retries(w)
    assert (row["state"], row["attempts"], row["last_code"]) == ("pending", 1, "at_cap")
    assert row["next_at"] == at(t1 + start_retry.BACKOFF[1])
    assert codes(w) == [(second, "at_cap")] * 2

    triggers.run(w.js, now=at(t1 + start_retry.BACKOFF[1] - 1))
    assert retries(w) == [row]  # inside the backoff nothing is attempted or recorded
    assert len(w.misses()) == 2


def test_retries_are_bounded_and_end_with_a_retry_exhausted_miss(hub):
    w, first, second = full_board(hub)
    when = 0
    for n, delay in enumerate(start_retry.BACKOFF, start=1):
        when += delay
        triggers.run(w.js, now=at(when))
        [row] = retries(w)
        assert row["attempts"] == n
    assert row["state"] == "exhausted" and row["next_at"] is None
    assert codes(w) == [(second, "at_cap")] * (1 + len(start_retry.BACKOFF)) + [
        (second, "retry_exhausted")
    ]
    before = w.misses()
    cap(w, 5)  # even with room again, an exhausted start is not tried again
    triggers.run(w.js, now=at(when + 100_000))
    assert w.misses() == before and not w.jobs_for(second)


def test_permanent_refusal_is_never_retried(hub):
    w = world(hub, on=False, runner=True)
    card = w.card()
    with w.boards.store.transaction() as conn:  # restricted origin, not released
        conn.execute("UPDATE tasks SET restricted_origin=1, approved_by=NULL WHERE id=?", (card,))
    triggers.run(w.js, now=T0)
    assert codes(w) == [(card, "origin_not_released")]
    assert retries(w) == []
    for later in (60, 3600, 100_000):
        triggers.run(w.js, now=at(later))
    assert codes(w) == [(card, "origin_not_released")] and not w.jobs_for(card)
    assert retries(w) == []


def test_a_retry_refused_permanently_ends_the_retries(hub):
    w, first, second = full_board(hub)
    w.update(second, status="backlog")  # the card left todo while the start waited
    triggers.run(w.js, now=at(start_retry.BACKOFF[0]))
    [row] = retries(w)
    assert (row["state"], row["attempts"], row["last_code"]) == ("refused", 1, "not_todo")
    assert codes(w) == [(second, "at_cap"), (second, "not_todo")]
    cap(w, 5)
    triggers.run(w.js, now=at(100_000))
    assert not w.jobs_for(second) and len(w.misses()) == 2  # refused stays refused
    assert [r["state"] for r in retries(w)] == ["refused"]


def test_a_stale_retry_cannot_double_count_or_double_start(hub):
    w, first, second = full_board(hub)
    cap(w, 2)
    triggers.run(w.js, now=at(start_retry.BACKOFF[0]))
    [job] = w.jobs_for(second)
    # A concurrent sweep that read the row as pending before the start committed:
    with w.js.connection() as conn:
        assert start_retry.due(conn, BOARD, at(100_000)) == []
        assert not start_retry.claimable(conn, BOARD, retries(w)[0]["event_id"], 1)
    assert w.jobs_for(second) == [job]
