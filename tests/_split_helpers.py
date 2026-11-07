"""Persisted seeds and honest error adapters for paired card-contract tests."""

from commonplace.boards.service import BoardService
from commonplace.extensions import ToolError
from tests._phase_b_helpers import World, seed

DDL = """
CREATE TABLE IF NOT EXISTS task_blockers (
 task_id INTEGER NOT NULL REFERENCES tasks(id),
 blocker_id INTEGER NOT NULL REFERENCES tasks(id),
 created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(task_id, blocker_id)
);
CREATE TABLE IF NOT EXISTS card_splits (
 task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
 state TEXT NOT NULL CHECK(state IN ('split','kept')),
 by TEXT NOT NULL, at TEXT NOT NULL
);
"""


def world(hub, *, on=True, runner=False):
    w = World(hub, topics=("worker", "orchestrate"))
    seed(w, on)
    if runner:
        w.configure()
    return w


def invoke(fn, *args, **kwargs):
    try:
        return 200, fn(*args, **kwargs)
    except (ToolError, ValueError) as exc:
        return getattr(exc, "status", 400), {
            "error": str(exc).removeprefix("no: "),
            "code": getattr(exc, "code", "invalid_arguments"),
            **getattr(exc, "details", {}),
        }


def tool(w, name, principal=None, **args):
    service = BoardService(w.ctx(principal or w.owner))
    fn = getattr(service, name, None)
    if fn is None:
        return 404, {"error": "tool not found"}
    return invoke(fn, **args)


def blockers(w, task, *ids):
    with w.boards.store.connection() as conn:
        conn.executescript(DDL)
    with w.boards.store.transaction() as conn:
        conn.executemany(
            "INSERT INTO task_blockers VALUES (?,?,?,?)",
            [(task, n, w.owner.actor, "2030-01-01T00:00:00Z") for n in ids],
        )


def age(w, card, at="2030-01-01T00:00:00Z"):
    with w.boards.store.transaction() as conn:
        conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (at, card))


def take(w, task=None):
    return invoke(BoardService(w.ctx(w.a)).task_take_next, board="acme", task=task)
