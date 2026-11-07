"""Person-owner configuration of workspace-local connection task labels."""

from pathlib import Path

from commonplace.boards import task_types
from commonplace.boards.store import BoardStore
from commonplace.extensions import ToolError
from commonplace.http_util import path_parts, read_json, require_principal, send_json

PREFIX = "/api/task-types/connections/"


def install(gateway):
    def manage(h):
        principal = require_principal(gateway, h)
        if principal is None:
            return
        if not principal.person or principal.role != "owner":
            send_json(h, 403, {"error": "Sign in as a workspace owner to manage task types"})
            return
        parts = path_parts(h, PREFIX)
        if len(parts) != 1:
            send_json(h, 404, {"error": "Connection not found"})
            return
        # Authenticate the exact stable ID, never the name-or-ID convenience lookup.
        holder = gateway.db.authenticate_connection(parts[0])
        if holder is None or holder.workspace_id != principal.workspace_id:
            send_json(h, 404, {"error": "Connection not found"})
            return
        values = None
        if h.command == "POST":
            try:
                data = read_json(h)
            except ValueError:
                return
            try:
                if not isinstance(data, dict) or set(data) != {"allowed_types"}:
                    raise ToolError("no: use allowed_types only")
                values = task_types.canonical(data["allowed_types"])
            except ToolError as exc:
                send_json(h, 400, {"error": str(exc).removeprefix("no: ")})
                return
        directory = gateway.workspace_dir(principal.workspace_id)
        if h.command == "GET" and not (Path(directory) / "boards.sqlite3").exists():
            send_json(h, 200, {"allowed_types": []})
            return
        store = BoardStore(directory, create=h.command == "POST")
        if h.command == "POST":
            with store.transaction() as conn:
                conn.execute(
                    "DELETE FROM connection_task_types WHERE connection_id=?",
                    (holder.connection_id,),
                )
                conn.executemany(
                    "INSERT INTO connection_task_types(connection_id,task_type) VALUES (?,?)",
                    [(holder.connection_id, label) for label in values],
                )
        else:
            with store.connection() as conn:
                labels = task_types.allowed(conn, holder.connection_id)
                if labels is None:
                    send_json(h, 503, {"error": "Task types are unavailable"})
                    return
                values = sorted(labels)
        send_json(h, 200, {"allowed_types": values})

    gateway.add_prefix_route("GET", PREFIX, manage)
    gateway.add_prefix_route("POST", PREFIX, manage)
