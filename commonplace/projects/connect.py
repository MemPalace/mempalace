"""Attach an agent to a project's automatic orchestrator seat."""

import secrets

from commonplace import control_db, redact, urls
from commonplace.boards.service import BoardService, shared_wing
from commonplace.control_db import ControlError
from commonplace.extensions import ToolContext, ToolError
from commonplace.policy import Policy
from commonplace.projects.copy import OWN_WING_EFFECT, SHARED_EXPLAIN
from commonplace.projects.seats import active_connection, ensure_main, reconcile
from commonplace.projects.types import SEAT_CLIENT, SEAT_PREFIX, TOKEN_DAYS, ProjectDenied
from commonplace.web.agents import connection_view


def connect(
    gateway, principal, board: str, *, replace: bool = False, allow_shared: bool = False
) -> tuple[int, dict]:
    if not principal.person or principal.role != "owner":
        raise ProjectDenied("Sign in as a workspace owner to connect an agent")
    if type(replace) is not bool or type(allow_shared) is not bool:
        raise ValueError("Connect options must be booleans")

    db = gateway.db
    ws = principal.workspace_id
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    with service.store.connection() as conn:
        project = service._board(conn, board)
    board, wing = project["slug"], project["wing"]
    policy = Policy.build(
        profile="standard",
        scopes=("memory:read", "memory:write"),
        read_wings=[wing],
        write_wing=wing,
        trust="standard",
    )
    # Decide consent on the effective grant: every spelling of "shared" counts.
    if not allow_shared and shared_wing(wing):
        return 409, {
            "error": "shared_wing",
            "explanation": SHARED_EXPLAIN,
            "fix": {
                "label": "Give this project its own memory section",
                "method": "POST",
                "path": f"/api/projects/{board}/wing",
                "effect": OWN_WING_EFFECT,
            },
            "alternative": {
                "label": "Allow this agent to read and write all shared memory",
                "method": "POST",
                "path": f"/api/projects/{board}/orchestrator/connect",
                "body": {"allow_shared": True},
            },
            "message": (
                "This project uses the shared memory wing. Connecting gives the agent "
                "read and write access to all shared memory."
            ),
        }

    reconcile(db, ws, board, wing, principal.user_id)
    seat = ensure_main(db, ws, board)
    coordinator = project["coordinator"]
    own = active_connection(db, ws, seat.connection_id)
    if (
        not replace
        and coordinator
        and (own is None or coordinator != "conn:" + own["name"])
        and coordinator.startswith("conn:")
        and active_connection(db, ws, coordinator.removeprefix("conn:")) is not None
    ):
        return 409, {"error": "replaces current orchestrator"}

    minted = False
    with db._lock:
        row = active_connection(db, ws, seat.connection_id)
        if row is not None:
            cid, name = row["id"], row["name"]
            token = db.rotate_token(
                cid, actor=principal.user_id, workspace_ref=ws, expires_days=TOKEN_DAYS
            )
        else:
            name = seat.name
            # Revoked names remain reserved by the connections table's unique key.
            for attempt in range(4):
                if not db._conn.execute(
                    "SELECT 1 FROM connections WHERE workspace_id=? AND name=?", (ws, name)
                ).fetchone():
                    break
                if attempt == 3:
                    return 409, {"error": "Could not allocate an orchestrator name; try again"}
                name = f"{SEAT_PREFIX}{board}:{secrets.token_hex(3)}"
            cid, token = db.create_token(
                workspace_ref=ws,
                user_ref=principal.user_id,
                actor=principal.user_id,
                name=name,
                client=SEAT_CLIENT,
                expires_days=TOKEN_DAYS,
                policy=policy,
            )
            try:
                claimed = db._conn.execute(
                    "UPDATE project_orchestrators SET connection_id=?, name=?, updated_at=? "
                    "WHERE workspace_id=? AND board=? AND role='main' AND connection_id IS ?",
                    (cid, name, control_db._iso(control_db._now()), ws, board, seat.connection_id),
                ).rowcount
            except Exception:
                db.revoke(cid, actor=principal.user_id, workspace_ref=ws)
                raise
            if claimed:
                minted = True
            else:
                # Another Connect claimed the seat after our initial read. Keep
                # its identity and rotate it, discarding our unused connection.
                db.revoke(cid, actor=principal.user_id, workspace_ref=ws)
                winner = db._conn.execute(
                    "SELECT connection_id FROM project_orchestrators "
                    "WHERE workspace_id=? AND board=? AND role='main'",
                    (ws, board),
                ).fetchone()
                row = active_connection(db, ws, winner["connection_id"]) if winner else None
                if row is None:
                    return 409, {"error": "The orchestrator connection changed; try again"}
                cid, name = row["id"], row["name"]
                token = db.rotate_token(
                    cid, actor=principal.user_id, workspace_ref=ws, expires_days=TOKEN_DAYS
                )

    # Board writes acquire a separate store lock and emit events: never hold
    # the control database lock while entering this path.
    try:
        configured = service.board_set(board, "conn:" + name)
    except Exception as exc:
        # Never leave a freshly minted credential that nobody holds.
        if minted:
            with db._lock:
                db.revoke(cid, actor=principal.user_id, workspace_ref=ws)
                db._conn.execute(
                    "UPDATE project_orchestrators SET connection_id=?, name=?, updated_at=? "
                    "WHERE workspace_id=? AND board=? AND role='main' AND connection_id IS ?",
                    (seat.connection_id, seat.name, seat.updated_at, ws, board, cid),
                )
        if not isinstance(exc, (ToolError, ControlError)):
            raise
        return 409, {"error": "Could not connect the project's orchestrator"}

    db.audit(
        action="project.orchestrator.connect",
        decision="ok",
        workspace_id=ws,
        user_id=principal.user_id,
        connection_id=cid,
        auth=principal.auth,
        wing=wing,
        target_ids=(project["id"], cid),
    )
    connection = connection_view(db.connection(cid, ws))
    connection["name"] = redact.redact(connection["name"])[0]
    result = {
        "connection": connection,
        "token": token,
        "mcp_url": urls.AGENT_URL + "/mcp",
        "board": board,
        "wing": wing,
        "setup": {"webhook_add": {"events": "space_message", "board": board}},
    }
    from commonplace.projects import orchestration

    if orchestration.enabled(db, ws):
        result["setup"]["webhook_add"]["events"] = "space_message,card.created"
    if "warning" in configured:
        result["warning"] = configured["warning"]
    return (201 if minted else 200), result


def _demote_in(db, principal, cid):
    import json

    from commonplace.runner import registry

    # Demotion must also clean revoked seats and machines, without validating peers.
    row = db.connection(cid, principal.workspace_id)
    policy = json.loads(row["policy"])
    policy["trust"] = "standard"
    db._conn.execute("UPDATE connections SET policy=? WHERE id=?", (json.dumps(policy), cid))
    for machine in registry.machines(db, principal.workspace_id):
        if cid in json.loads(machine["requesters"]):
            registry.patch_in(db, principal, machine["id"], {}, remove_requester=cid)


def demote_workspace_in(db, principal):
    rows = db._conn.execute(
        "SELECT connection_id FROM project_orchestrators WHERE workspace_id=? AND connection_id IS NOT NULL",
        (principal.workspace_id,),
    ).fetchall()
    for row in rows:
        _demote_in(db, principal, row["connection_id"])


def trust(gateway, principal, board, body):
    import json

    from commonplace.projects import orchestration as orch
    from commonplace.runner import registry

    if not principal.person or principal.role != "owner":
        raise ProjectDenied("Only workspace owners can trust an orchestrator")
    demoting = isinstance(body, dict) and body == {"trusted": False}
    if not demoting and (
        not isinstance(body, dict)
        or set(body) != {"confirm", "machines"}
        or body["confirm"] is not True
        or not isinstance(body["machines"], list)
        or not body["machines"]
        or any(not isinstance(m, str) for m in body["machines"])
    ):
        raise ValueError("Confirm trust and select machines")
    db = gateway.db
    with db._lock, db._tx():
        if not orch.enabled(db, principal.workspace_id):
            raise orch.AdmissionRefused("orchestration_off")
        seat = ensure_main(db, principal.workspace_id, board)
        if seat.connection_id is None:
            raise registry.Conflict("Connect the seat first")
        if demoting:
            _demote_in(db, principal, seat.connection_id)
        else:
            registry.seat_trust_in(db, principal, seat.connection_id, True)
            for ref in dict.fromkeys(body["machines"]):
                machine = registry._active(db, principal, ref)
                if not machine["enabled"]:
                    raise registry.Conflict("machine is disabled")
                requesters = list(
                    dict.fromkeys([*json.loads(machine["requesters"]), seat.connection_id])
                )
                registry.patch_in(db, principal, machine["id"], {"requesters": requesters})
        db.audit(
            action="project.orchestrator.trust",
            decision="allow",
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
            connection_id=seat.connection_id,
            auth=principal.auth,
            target_ids=(seat.connection_id,),
        )
    return {"trusted": not demoting}
