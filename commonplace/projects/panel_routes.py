"""REST routes for the project panel: GET and PUT /api/projects/<slug>/panel.

Anyone who can read the project reads its panel. Setting or clearing it takes the project's
board-write authority (a connection or person that may change the board), so a read-only or
add-cards-only connection is refused. The write is rules-checked like any other agent write.
"""

from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import read_json, require_principal, send_json
from commonplace.projects import panel
from commonplace.rules.enforce import RulesRefresh, rest_write

TOOL = "project_panel_set"


def _refuse(h, exc):
    message = str(exc).removeprefix("no: ")
    missing = message == "board not found"
    send_json(
        h,
        404 if missing else 403,
        {"error": "Board not found" if missing else message, "code": "not_allowed"},
    )


def handle(gateway, h, parts, action):
    principal = require_principal(gateway, h)
    if principal is None:
        return
    slug = parts[0]
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    try:
        with service.store.connection() as conn:
            board = service._board(conn, slug)
            if action == "panel_view":
                send_json(h, 200, panel.get(conn, board["id"]) or {"panel": None})
                return
        try:
            body = read_json(h)
        except ValueError:
            return
        with rest_write(ToolContext(principal, gateway), h, TOOL, body):
            if set(body) != {"panel"}:
                raise panel.PanelInvalid('the body must be exactly {"panel": <panel> or null}')
            service._write(board)
            with service.store.transaction() as conn:
                stored = panel.put(conn, board["id"], body["panel"], principal.actor)
    except RulesRefresh as exc:
        send_json(h, 428, exc.payload)
        return
    except panel.PanelInvalid as exc:
        send_json(h, exc.status, {"error": str(exc), "code": exc.code})
        return
    except ToolError as exc:
        _refuse(h, exc)
        return
    send_json(h, 200, stored or {"panel": None})
