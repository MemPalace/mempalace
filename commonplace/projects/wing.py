"""The complete owner own-wing flow shared by REST and MCP."""

from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext
from commonplace.projects import seats


def own_wing(gateway, principal, board):
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    result = service.board_own_wing(board)
    result.update(
        seats.reconcile(
            gateway.db, principal.workspace_id, result["board"], result["wing"], principal.user_id
        )
    )
    detached = result.pop("_detached")
    if result["changed"] or detached or result["unbound"] or result["revoked"]:
        gateway.db.audit(
            action="project.wing.own",
            decision="ok",
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
            connection_id=principal.connection_id,
            auth=principal.auth,
            wing=result["wing"],
            target_ids=(result["board"],),
        )
    return result
