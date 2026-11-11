"""A small goals tree per workspace, and the goal chain a card carries into agent context.

The owner sets the tree; a card may point at one goal. ``chain_block`` is the single
place that turns a card's goal into agent-visible text, inside a fixed character budget:
the card's own goal is never dropped, the top-level goals go first."""

import json
import re
import sqlite3

from commonplace.extensions import ToolError

# Additive and idempotent: old databases gain the tables on the next open.
GOALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
 id INTEGER PRIMARY KEY,
 parent_id INTEGER REFERENCES goals(id),
 title TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '',
 archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
 created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS goals_by_parent ON goals(parent_id);
CREATE TABLE IF NOT EXISTS card_goals (
 task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
 goal_id INTEGER NOT NULL REFERENCES goals(id),
 set_by TEXT NOT NULL, set_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS card_goals_by_goal ON card_goals(goal_id);
"""

TITLE_LIMIT, DETAIL_LIMIT = 100, 400
MAX_DEPTH, MAX_GOALS = 6, 200
# Characters of goal text a card's context may carry, header and markers included.
BUDGET = 1200
HEADER = "goals (top-level first, the last is this card's goal):"


def require_owner(principal):
    if not principal.person or principal.role != "owner":
        raise ToolError("no: only a signed-in workspace owner can set goals")


CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text(value, name, limit, *, required=False):
    """Single-line text: whitespace runs collapse, other control characters are refused,
    so a goal's rendered size is bounded by its length limit (quotes and backslashes
    at most double it)."""
    if not isinstance(value, str) or CONTROL.search(value):
        raise ToolError(f"no: goal {name} must be plain text")
    value = " ".join(value.split())
    if len(value) > limit or (required and not value):
        raise ToolError(
            f"no: goal {name} must be {'nonempty ' if required else ''}text of at most {limit} characters"
        )
    return value


def _goal(conn, goal_id):
    if type(goal_id) is not int or goal_id < 1 or goal_id >= 2**63:
        raise ToolError("no: goal not found")
    row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
    if row is None:
        raise ToolError("no: goal not found")
    return row


def _ancestors(conn, goal_id):
    """Rows from the goal up to its top-level goal; stops on a cycle in damaged data."""
    rows, seen = [], set()
    while goal_id is not None and goal_id not in seen and len(rows) <= MAX_DEPTH * 4:
        seen.add(goal_id)
        row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if row is None:
            break
        rows.append(row)
        goal_id = row["parent_id"]
    return rows


def _height(conn, goal_id):
    """Levels at and below the goal."""
    deepest, level = 1, [goal_id]
    while level:
        level = [
            r[0]
            for gid in level
            for r in conn.execute("SELECT id FROM goals WHERE parent_id=?", (gid,))
        ]
        deepest += bool(level)
        if deepest > MAX_DEPTH * 2:
            break
    return deepest


def put(conn, actor, now, *, goal=None, title=None, detail=None, parent=None, archived=None):
    """Create a goal (no ``goal``) or change one; ``parent`` 0 makes it top-level."""
    if goal is None:
        title = _text(title, "title", TITLE_LIMIT, required=True)
        if conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] >= MAX_GOALS:
            raise ToolError(f"no: at most {MAX_GOALS} goals")
        row = None
    else:
        row = _goal(conn, goal)
        title = row["title"] if title is None else _text(title, "title", TITLE_LIMIT, required=True)
    detail = (
        (row["detail"] if row else "") if detail is None else _text(detail, "detail", DETAIL_LIMIT)
    )
    if archived is not None and type(archived) is not bool:
        raise ToolError("no: archived must be true or false")
    parent_id = row["parent_id"] if row else None
    if parent is not None:
        if type(parent) is not int or parent < 0:
            raise ToolError("no: parent must be a goal number, or 0 for top-level")
        parent_id = parent or None
    if parent_id is not None:
        above = _ancestors(conn, _goal(conn, parent_id)["id"])
        if row is not None and any(a["id"] == row["id"] for a in above):
            raise ToolError("no: a goal cannot sit under itself")
        if len(above) + (_height(conn, row["id"]) if row else 1) > MAX_DEPTH:
            raise ToolError(f"no: goals nest at most {MAX_DEPTH} deep")
    hidden = row["archived"] if archived is None and row is not None else archived
    stamp = (parent_id, title, detail, int(bool(hidden)), now)
    if row is None:
        number = conn.execute(
            "INSERT INTO goals(parent_id,title,detail,archived,updated_at,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (*stamp, actor, now),
        ).lastrowid
    else:
        number = row["id"]
        conn.execute(
            "UPDATE goals SET parent_id=?,title=?,detail=?,archived=?,updated_at=? WHERE id=?",
            (*stamp, number),
        )
    return view(conn, number)


def view(conn, goal_id):
    row = _goal(conn, goal_id)
    return {
        "id": row["id"],
        "parent": row["parent_id"],
        "title": row["title"],
        "detail": row["detail"],
        "archived": bool(row["archived"]),
        "cards": conn.execute(
            "SELECT COUNT(*) FROM card_goals WHERE goal_id=?", (row["id"],)
        ).fetchone()[0],
    }


def tree(conn):
    """Every goal, parents before children, ordered by number within a level."""
    rows = conn.execute("SELECT id FROM goals ORDER BY id").fetchall()
    views = {r["id"]: view(conn, r["id"]) for r in rows}
    out, seen = [], set()

    def walk(parent, depth):
        for item in views.values():
            if item["parent"] == parent and item["id"] not in seen:
                seen.add(item["id"])
                out.append({**item, "depth": depth})
                walk(item["id"], depth + 1)

    walk(None, 0)
    return out


def point(conn, actor, now, task_id, goal):
    """Point a card at a goal, or clear it with ``goal`` None."""
    if goal is None:
        conn.execute("DELETE FROM card_goals WHERE task_id=?", (task_id,))
        return None
    row = _goal(conn, goal)
    held = conn.execute("SELECT goal_id FROM card_goals WHERE task_id=?", (task_id,)).fetchone()
    if row["archived"] and not (held and held[0] == row["id"]):
        raise ToolError("no: goal is archived")
    conn.execute(
        "INSERT INTO card_goals(task_id,goal_id,set_by,set_at) VALUES (?,?,?,?) "
        "ON CONFLICT(task_id) DO UPDATE SET goal_id=excluded.goal_id,"
        "set_by=excluded.set_by,set_at=excluded.set_at",
        (task_id, row["id"], actor, now),
    )
    return row["id"]


def _line(item):
    return (
        "- "
        + json.dumps(item["title"], ensure_ascii=False)
        + (": " + json.dumps(item["detail"], ensure_ascii=False) if item["detail"] else "")
    )


def render(chain, omitted=0):
    lines = [HEADER]
    if omitted:
        lines.append(f"- (+{omitted} higher goal{'s' if omitted != 1 else ''} omitted)")
    lines += [_line(item) for item in chain]
    return "\n".join(lines)


def chain_block(conn, task_id, budget=BUDGET):
    """The card's goal chain, or None for a card without a goal.

    ``chain`` runs top-level first and ends with the card's own goal. Higher goals are
    dropped from the top down until the rendered ``text`` fits ``budget``; the card's own
    goal always stays. ``omitted`` counts the dropped ones."""
    try:
        held = conn.execute("SELECT goal_id FROM card_goals WHERE task_id=?", (task_id,)).fetchone()
        if held is None:
            return None
        rows = list(reversed(_ancestors(conn, held[0])))
    except sqlite3.Error:
        # A database without the tables behaves like a card without a goal.
        return None
    if not rows:
        return None
    chain = [{"id": r["id"], "title": r["title"], "detail": r["detail"]} for r in rows]
    omitted = 0
    while omitted < len(chain) - 1 and len(render(chain[omitted:], omitted)) > budget:
        omitted += 1
    kept = chain[omitted:]
    return {"chain": kept, "omitted": omitted, "text": render(kept, omitted)}
