"""Bounded verbatim project search under the board-read rule."""

from datetime import datetime

from commonplace import redact
from commonplace.boards.service import BoardService
from commonplace.chat import store
from commonplace.extensions import ToolContext, ToolError


def predicates(words, actor=None, since=None):
    """SQL predicates for a source projected as text, actor and at."""
    clauses, values = [], []
    for word in words:
        clauses.append("text LIKE ? ESCAPE '\\'")
        values.append(
            "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
    if actor is not None:
        clauses.append("actor=?")
        values.append(actor)
    if since is not None:
        clauses.append("julianday(at)>=julianday(?)")
        values.append(since)
    return " AND ".join(clauses), values


def _result(kind, board, row):
    return {
        "kind": kind,
        "id": row["id"],
        "board": board,
        "at": row["at"],
        "actor": row["actor"],
        "model": row["model"],
        "number": row["number"],
        "field": row["field"],
        "text": redact.redact(row["text"])[0],
    }


def handle(gateway, principal, slug: str, params: dict[str, str]) -> tuple[int, dict]:
    if not principal.person:
        return 403, {"error": "Sign in to view chat"}
    if not principal.policy.has_scope("memory:read"):
        return 403, {"error": "Forbidden"}
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    with service.store.connection() as conn:
        try:
            board = service._board(conn, slug)
        except ToolError:
            return 404, {"error": "Board not found"}
        query = params.get("q", "")
        words = query.split()
        source = params.get("source")
        since = params.get("since")
        try:
            if since is not None:
                datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            return 400, {"error": "Invalid search"}
        if (
            not 1 <= len(words) <= 8
            or len(query) > 200
            or any(len(word) > 100 for word in words)
            or source not in (None, "chat", "reply", "card")
        ):
            return 400, {"error": "Invalid search"}
        where, values = predicates(words, params.get("from"), since)
        results, truncated = [], False
        if source in (None, "chat"):
            rows = conn.execute(
                "SELECT * FROM (SELECT id,text,created_at AS at,"
                "CASE WHEN stamp_connection IS NULL THEN 'user:'||stamp_user "
                "ELSE 'conn:'||stamp_connection END AS actor,"
                "NULL AS model,NULL AS number,'text' AS field,created_at,stamp_user,stamp_connection,stamp_trust,flags "
                "FROM space_messages WHERE board_id=?) "
                f"WHERE {where} ORDER BY julianday(at) DESC,id DESC LIMIT 21",
                (board["id"], *values),
            ).fetchall()
            # Keep the same actor projection as the board chat page.
            for row in rows[:20]:
                results.append(
                    _result("chat", board["slug"], {**row, "actor": service._message(row)["actor"]})
                )
            truncated |= len(rows) > 20
        if source in (None, "card"):
            # task_list(all_statuses=True) returns every task on a readable board.
            # Read that set directly: task_list also expires claims, which search must not do.
            rows = conn.execute(
                "WITH visible AS (SELECT * FROM tasks WHERE board_id=?), fields AS ("
                "SELECT id,id AS number,title AS text,'title' AS field,created_at AS at,"
                "CASE WHEN stamp_connection IS NULL THEN 'user:'||stamp_user "
                "ELSE 'conn:'||stamp_connection END AS actor,NULL AS model FROM visible UNION ALL "
                "SELECT id,id,body,'body',updated_at,updated_by,NULL FROM visible UNION ALL "
                "SELECT id,id,ask,'ask',updated_at,updated_by,NULL FROM visible UNION ALL "
                "SELECT e.id,t.id,e.text,e.kind,e.created_at,"
                "CASE WHEN e.stamp_connection IS NULL THEN 'user:'||e.stamp_user "
                "ELSE 'conn:'||e.stamp_connection END,NULL FROM task_events e "
                "JOIN visible t ON t.id=e.task_id WHERE e.kind IN ('comment','handoff','ask','decide')) "
                f"SELECT * FROM fields WHERE {where} "
                "ORDER BY julianday(at) DESC,id DESC,number DESC,field LIMIT 21",
                (board["id"], *values),
            ).fetchall()
            results.extend(_result("card", board["slug"], row) for row in rows[:20])
            truncated |= len(rows) > 20
        if source in (None, "reply"):
            replies = store.open_replies(gateway, principal.workspace_id).search(
                board=board["slug"],
                for_user=principal.user_id,
                words=words,
                actor=params.get("from"),
                since=since,
            )
            results.extend(_result("reply", board["slug"], row) for row in replies[:20])
            truncated |= len(replies) > 20
    results.sort(
        key=lambda row: (
            datetime.fromisoformat(row["at"].replace("Z", "+00:00")).timestamp(),
            row["id"],
        ),
        reverse=True,
    )
    return 200, {"results": results, "truncated": truncated}
