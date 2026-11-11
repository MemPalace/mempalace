"""Owner REST for the workspace goals tree; all rules live in ``BoardService``."""

from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import read_json, require_principal, send_json

PATH, CARD_PATH = "/api/goals", "/api/goals/card"
FIELDS = {"goal", "title", "detail", "parent", "archived"}


def install(gateway):
    def handle(h):
        principal = require_principal(gateway, h)
        if principal is None:
            return
        service = BoardService(ToolContext(principal=principal, gateway=gateway))
        post = h.command == "POST"
        if post:
            if not principal.person or principal.role != "owner":
                send_json(h, 403, {"error": "Sign in as a workspace owner to set goals"})
                return
            try:
                data = read_json(h)
            except ValueError:
                return
        try:
            if not post:
                result = {"goals": service.goal_list()}
            elif h.path.split("?")[0] == CARD_PATH:
                if not isinstance(data, dict) or set(data) - {"task", "goal"} or "task" not in data:
                    raise ToolError("no: use task and goal only")
                result = service.card_goal(**data)
            else:
                if not isinstance(data, dict) or set(data) - FIELDS:
                    raise ToolError("no: unknown goal field")
                result = service.goal_set(**data)
        except ToolError as exc:
            send_json(
                h, 404 if "not found" in str(exc) else 400, {"error": str(exc).removeprefix("no: ")}
            )
            return
        send_json(h, 200, result)

    gateway.add_route("GET", PATH, handle)
    gateway.add_route("POST", PATH, handle)
    gateway.add_route("POST", CARD_PATH, handle)
