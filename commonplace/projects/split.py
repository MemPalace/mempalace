"""Opt-in inbox and idempotent child keys; the hub never plans or creates children."""

import json
import re
from contextlib import contextmanager
from pathlib import Path

from commonplace.boards.store import CardRefused, now, require_visible, task_number, task_row
from commonplace.projects import orchestration as orch
from mempalace.sqlite_crypto import connect, row_factory

KEY = re.compile(r"split:(\d+):([a-z0-9-]{1,32})")


def keys(labels):
    return [
        KEY.fullmatch(label) for label in labels if isinstance(label, str) and KEY.fullmatch(label)
    ]


def is_root(row):
    labels = (
        json.loads(row["labels"] or "[]") if isinstance(row["labels"], str) else row["labels"] or []
    )
    return row["parent_number"] is None and not keys(labels)


def authorize(principal, board):
    if not principal.policy.has_scope("memory:read") or not principal.policy.can_read_wing(
        board["wing"]
    ):
        raise CardRefused("board_not_found", status=404)
    if not (
        (principal.person and principal.role == "owner")
        or (
            not principal.person
            and principal.auth == "pat"
            and principal.actor == board["coordinator"]
        )
    ):
        raise CardRefused("not_allowed", status=403)


def require_on(gateway, principal):
    if not orch.enabled(gateway.db, principal.workspace_id):
        raise CardRefused("orchestration_off", status=409)


def parent_for(conn, board, principal, parent, labels):
    parsed = keys(labels)
    if (
        any(label.startswith("split:") and KEY.fullmatch(label) is None for label in labels)
        or len(parsed) > 1
    ):
        raise CardRefused("invalid_split_key", status=400)
    if not parsed:
        return None
    if parent is None or int(parsed[0][1]) != task_number(parent):
        raise CardRefused("split_parent_mismatch", status=400)
    row = task_row(conn, parent)
    if row["board_id"] != board["id"] or not is_root(row):
        raise CardRefused("invalid_split_parent", status=400)
    authorize(principal, board)
    return row["id"]


def check_key(conn, parent: int, labels) -> int | None:
    parsed = keys(labels)
    if not parsed:
        return None
    key = parsed[0].group(0)
    for child in conn.execute("SELECT id,labels FROM tasks WHERE parent_number=?", (parent,)):
        if key in json.loads(child["labels"] or "[]"):
            return child["id"]
    return None


def check_label_update(conn, board, principal, row, labels):
    previous = [label for label in json.loads(row["labels"] or "[]") if label.startswith("split:")]
    current = [label for label in labels if label.startswith("split:")]
    if previous == current:
        return
    if set(previous) - set(current):
        raise CardRefused("split_key_immutable", status=400)
    parent = parent_for(conn, board, principal, row["parent_number"], labels)
    if parent is not None:
        existing = check_key(conn, parent, labels)
        if existing is not None and existing != row["id"]:
            raise CardRefused("split_key_exists", number=existing)


def eligible(conn, board, seat_actor):
    marked = {r[0] for r in conn.execute("SELECT task_id FROM card_splits")}
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM tasks WHERE board_id=? AND status IN ('backlog','todo','doing','review') ORDER BY id",
            (board["id"],),
        )
        if is_root(r) and r["created_by"] != seat_actor and r["id"] not in marked
    ]


def inbox(gateway, principal, board) -> list[dict]:
    require_on(gateway, principal)
    with orch.board_read(gateway.workspace_dir(principal.workspace_id)) as conn:
        b = conn.execute("SELECT * FROM boards WHERE slug=? AND archived=0", (board,)).fetchone()
        if b is None:
            raise CardRefused("board_not_found", status=404)
        authorize(principal, b)
        results = []
        for row in eligible(conn, b, b["coordinator"]):
            children = [
                {"number": c["id"], "title": c["title"], "status": c["status"], "key": k.group(0)}
                for c in conn.execute(
                    "SELECT * FROM tasks WHERE parent_number=? ORDER BY id", (row["id"],)
                )
                for k in keys(json.loads(c["labels"] or "[]"))
            ]
            results.append(
                {
                    "number": row["id"],
                    "board": board,
                    "title": row["title"],
                    "body": row["body"],
                    "status": row["status"],
                    "keyed_children": children,
                }
            )
        return results


def mark(gateway, principal, task, state) -> dict:
    require_on(gateway, principal)
    if not principal.policy.has_scope("memory:write"):
        raise CardRefused("not_allowed", status=403)
    if state not in ("split", "kept"):
        raise CardRefused("invalid_split_state", status=400)
    from commonplace.boards.service import BoardService
    from commonplace.extensions import ToolContext

    service = BoardService(ToolContext(principal, gateway))
    with service.transaction() as conn:
        row = task_row(conn, task)
        require_visible(row, principal)
        board = conn.execute(
            "SELECT * FROM boards WHERE id=? AND archived=0", (row["board_id"],)
        ).fetchone()
        if board is None:
            raise CardRefused("board_not_found", status=404)
        authorize(principal, board)
        if not is_root(row):
            raise CardRefused("invalid_split_parent", status=400)
        inserted = conn.execute(
            "INSERT OR IGNORE INTO card_splits VALUES (?,?,?,?)",
            (row["id"], state, principal.actor, now()),
        ).rowcount
        if inserted:
            service._set(conn, row, {}, release_origin=False)
            service._log(conn, row["id"], "edit", choice="split", text=state)
            service._emit(conn, row["id"], "card.updated")
        stored = conn.execute("SELECT * FROM card_splits WHERE task_id=?", (row["id"],)).fetchone()
        return {
            "number": row["id"],
            "state": stored["state"],
            "by": stored["by"],
            "at": stored["at"],
        }


@contextmanager
def runner_read(directory):
    path = Path(directory) / "runner.sqlite3"
    if not path.is_file():
        yield None
        return
    conn = connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10, isolation_level=None)
    conn.row_factory = row_factory
    try:
        yield conn
    finally:
        conn.close()


def attempts(conn, card):
    if (
        conn is None
        or not conn.execute("SELECT 1 FROM sqlite_master WHERE name='orch_misses'").fetchone()
    ):
        return []
    return conn.execute(
        "SELECT * FROM orch_misses WHERE card=? AND event_id<0 ORDER BY id", (card,)
    ).fetchall()


def stuck(conn, card):
    prior = attempts(conn, card)
    return len(prior) >= 3 and prior[-1]["code"] != "split_started"


def context(gateway, principal, board=None):
    require_on(gateway, principal)
    with orch.board_read(gateway.workspace_dir(principal.workspace_id)) as conn:
        boards = conn.execute(
            "SELECT * FROM boards WHERE archived=0 AND (? IS NULL OR slug=?)", (board, board)
        ).fetchall()
    notes = []
    for b in boards:
        try:
            authorize(principal, b)
        except CardRefused:
            continue
        cards = inbox(gateway, principal, b["slug"])
        with runner_read(gateway.workspace_dir(principal.workspace_id)) as runner:
            stalled = [c["number"] for c in cards if stuck(runner, c["number"])]
        notes.append(
            f"{b['slug']}: {len(cards)} unsplit cards; call orchestrator_inbox(board={b['slug']})."
            + (" split stuck: " + ", ".join(f"#{n}" for n in stalled) if stalled else "")
        )
    return "\n".join(notes)


def retry_due(conn, row, at, *, expected=None):
    """No retry after a job started or three attempts; gaps are 10/30/90 minutes."""
    from commonplace.runner import jobs

    prior = attempts(conn, row["id"])
    n = len(prior) + 1
    if n > 3 or (expected is not None and expected != n):
        return False
    since = prior[-1]["created_at"] if prior else row["created_at"]
    if jobs._after(since, (10, 30, 90)[n - 1] * 60) > at:
        return False
    return not conn.execute(
        "SELECT 1 FROM runner_jobs WHERE task_number=? AND (state IN ('queued','leased','starting','uncertain','started') OR started_at IS NOT NULL)",
        (row["id"],),
    ).fetchone()
