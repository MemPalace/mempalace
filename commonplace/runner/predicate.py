"""Read-only authority checks shared by request and lease validation."""

import json

from commonplace.boards import task_types
from commonplace.boards.service import BoardService, _waiting_on_person
from commonplace.extensions import ToolContext, ToolError


class Refused(ToolError):
    """A card cannot be started for the given reason."""

    def __init__(self, reason):
        self.reason = reason
        message = (
            "worker unavailable"
            if reason == "worker unavailable"
            else f"card is not startable: {reason}"
        )
        super().__init__(f"no: {message}")


def grant_values(machine, key):
    """Accept the registry's decoded grants and persisted JSON rows."""
    value = machine[key]
    return json.loads(value) if isinstance(value, str) else value


def requester_ok(gateway, principal, machine) -> bool:
    if (
        principal is None
        or getattr(principal, "runner", None) is not None
        or principal.workspace_id != machine["workspace_id"]
    ):
        return False
    if principal.person:
        return principal.role == "owner"
    live = gateway.db.authenticate_connection(principal.connection_id)
    return bool(
        live is not None
        and live.workspace_id == machine["workspace_id"]
        and getattr(live, "runner", None) is None
        and live.auth == "pat"
        and live.policy.trust == "trusted"
        and live.connection_id in grant_values(machine, "requesters")
    )


def startable(gateway, machine, row, board) -> None:
    worker = gateway.db.authenticate_connection(machine["worker_connection_id"])
    if worker is None or worker.workspace_id != machine["workspace_id"]:
        raise Refused("worker unavailable")
    service = BoardService(ToolContext(worker, gateway), store=False)
    from commonplace.boards.store import active_blockers
    from commonplace.projects import orchestration as orch

    if orch.enabled(gateway.db, machine["workspace_id"]):
        with orch.board_read(gateway.workspace_dir(machine["workspace_id"])) as conn:
            if active_blockers(conn, row["id"]):
                raise Refused("blocked_by")
    if _waiting_on_person(row):
        raise Refused("waiting_on_person")
    own_claim = row["status"] == "doing" and row["assignee"] == worker.actor
    if row["status"] != "todo" and not own_claim:
        raise Refused("not_todo")
    if row["assignee"] not in (None, worker.actor):
        raise Refused("assigned")
    if row["proposal_status"] in ("pending", "rejected"):
        raise Refused("proposal_open")
    if not service._claimable_origin(row):
        raise Refused("origin_not_released")
    if not task_types.read_eligible(service.ctx.workspace_dir(), row["task_type"], worker):
        raise Refused("task_type_not_allowed")
    try:
        service._write(board, row=row, claiming=True)
    except ToolError:
        raise Refused("outside_worker_wing") from None
    if board["wing"] != worker.policy.write_wing:
        raise Refused("outside_worker_wing")
