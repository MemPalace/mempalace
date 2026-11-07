"""Independent persisted seeds and non-raising call adapters for admission tests."""

from commonplace.boards.service import BoardService
from commonplace.boards.store import BoardStore
from commonplace.extensions import ToolError
from tests.test_orch_runner import World as World

SEED_DDL = """CREATE TABLE IF NOT EXISTS orchestration_settings (
 workspace_id TEXT PRIMARY KEY,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 updated_by TEXT NOT NULL,
 updated_at TEXT NOT NULL
)"""


def seed(w, on=True):
    with w.db._lock:
        w.db._conn.execute(SEED_DDL)
        w.db._conn.execute(
            "INSERT OR REPLACE INTO orchestration_settings VALUES (?,?,?,?)",
            (w.ws, int(on), w.owner.user_id, "2030-01-01T00:00:00Z"),
        )


def cap(w, value):
    board_id = w.board_row()["id"]
    with w.boards.store.transaction() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS project_limits (board_id INTEGER PRIMARY KEY, max_agents INTEGER)"
        )
        conn.execute("INSERT OR REPLACE INTO project_limits VALUES (?,?)", (board_id, value))


def call(fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
        if result is None:
            return 204, None
        return result if isinstance(result, tuple) else (200, result)
    except (ToolError, ValueError) as exc:
        return getattr(exc, "status", 409), {
            "error": str(exc),
            "code": getattr(exc, "code", str(exc)),
        }


def claim(w, card, actor=None):
    return call(BoardService(w.ctx(actor or w.a)).task_take_next, board="acme", task=card)


def doing(w, card, actor):
    with w.boards.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='doing',assignee=?,claim_until=NULL WHERE id=?", (actor, card)
        )


def board(w):
    return next(row for row in w.boards.board_list() if row["slug"] == "acme")


def count(w):
    return board(w).get("used", -1)


def trusted_seat(w):
    from commonplace.projects import connect

    _, payload = connect.connect(w.gw, w.owner, "acme", allow_shared=True)
    cid = payload["connection"]["id"]
    # A trusted seat seed reaches the board-create path even before the route exists.
    import json

    with w.db._lock:
        policy = json.loads(
            w.db._conn.execute("SELECT policy FROM connections WHERE id=?", (cid,)).fetchone()[0]
        )
        policy["trust"] = "trusted"
        w.db._conn.execute("UPDATE connections SET policy=? WHERE id=?", (json.dumps(policy), cid))
    return w.gw.authenticate("Bearer " + payload["token"]), payload


def machine_two(w):
    from commonplace.runner import registry

    data = registry.enroll(
        w.db,
        w.owner,
        {"label": "runner-two", "projects": ["demo"], "topics": ["worker"], "wings": ["shared"]},
    )
    registry.attest(
        w.db,
        w.owner,
        data["machine_id"],
        {
            "topics": ["worker"],
            "provider_instance_id": "worker",
            "worker_connection_id": data["worker_connection_id"],
        },
    )
    return registry.get_machine(w.db, w.ws, data["machine_id"])


def raw_state(w, card, state):
    with BoardStore(w.gw.workspace_dir(w.ws)).transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status=?,assignee=NULL,claim_until=NULL WHERE id=?", (state, card)
        )
