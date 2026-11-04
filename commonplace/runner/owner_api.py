"""Person-owner HTTP routes for grants and binding attestations."""

import json
import logging

from commonplace.control_db import ControlError
from commonplace.extensions import ToolError
from commonplace.http_util import (
    log_failure,
    path_parts,
    read_json,
    require_principal,
    send_json,
)
from commonplace.web.agents import _read_wings

from . import jobs, registry

LOG = logging.getLogger(__name__)


def _view(row):
    result = dict(row)
    for key in registry.JSON_KEYS:
        if result[key] is not None:
            result[key] = json.loads(result[key])
    result.update(machine_id=result["id"], enabled=bool(result["enabled"]), verified=False)
    return result


def _wings(gateway, owner, data):
    if "wings" not in data:
        return data
    wings = data["wings"]
    if not isinstance(wings, list) or not 1 <= len(wings) <= 8:
        raise ValueError("wings must contain 1 to 8 entries")
    try:
        wings = _read_wings(gateway, owner, {"read_wings": wings})
    except ValueError:
        raise ValueError("choose existing nonprivate workspace wings without patterns") from None
    return {**data, "wings": wings}


def register(gateway):
    def manage(h):
        owner = require_principal(gateway, h)
        if owner is None:
            return
        if not owner.person or owner.role != "owner":
            send_json(h, 403, {"error": "a signed-in workspace owner is required"})
            return
        parts = path_parts(h, "/api/runners")
        method = h.command
        if method == "GET" and not parts:
            send_json(
                h,
                200,
                {
                    "machines": [
                        _view(row) for row in registry.machines(gateway.db, owner.workspace_id)
                    ]
                },
            )
            return
        if not (
            (not parts and method == "POST")
            or (len(parts) == 1 and method in ("PATCH", "DELETE"))
            or (len(parts) == 2 and method == "POST" and parts[1] in ("attest", "rotate"))
        ):
            send_json(h, 404, {"error": "not found"})
            return
        if method != "DELETE":
            try:
                data = read_json(h)
            except ValueError:
                return
        try:
            if not parts:
                result = registry.enroll(gateway.db, owner, _wings(gateway, owner, data))
                status = 201
            elif method == "PATCH":
                result = _view(
                    registry.patch(gateway.db, owner, parts[0], _wings(gateway, owner, data))
                )
                status = 200
            elif method == "DELETE":
                result = registry.revoke(gateway.db, owner, parts[0])
                status = 200
                # Authentication is already disabled even if workspace cleanup fails.
                try:
                    jobs.sweep(
                        jobs.open_store(gateway, owner.workspace_id), gateway.db, owner.workspace_id
                    )
                except Exception as exc:
                    log_failure(LOG, "Runner revoke cleanup deferred", exc)
            elif parts[1] == "attest":
                result = registry.attest(gateway.db, owner, parts[0], data)
                status = 200
            else:
                if set(data) != {"credential"} or data["credential"] not in ("machine", "worker"):
                    raise ValueError("credential must be machine or worker")
                result = registry.rotate(gateway.db, owner, parts[0], data["credential"])
                status = 200
        except registry.NotFound as exc:
            send_json(h, 404, {"error": str(exc)})
            return
        except registry.Conflict as exc:
            send_json(h, 409, {"error": str(exc)})
            return
        except (ValueError, ControlError) as exc:
            send_json(h, 400, {"error": str(exc)})
            return
        except ToolError:
            send_json(h, 503, {"error": "workspace wings are unavailable; try again"})
            return
        send_json(h, status, result)

    for method in ("GET", "POST"):
        gateway.add_route(method, "/api/runners", manage)
    for method in ("PATCH", "POST", "DELETE"):
        gateway.add_prefix_route(method, "/api/runners/", manage)
