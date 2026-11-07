"""Requester operations and MCP registration for optional machine starts."""

from commonplace.boards.service import BoardService
from commonplace.boards.store import task_row
from commonplace.extensions import WORK_PROFILES, LocalTool, ToolError, register

from . import jobs, registry
from .predicate import Refused, grant_values, requester_ok, startable

MODES = ("approval-required", "auto-accept-edits", "auto", "full-access")
STRING = {"type": "string", "minLength": 1}
TASK = {"type": ["string", "integer"]}
_registered = False


def person_owner(principal):
    return (
        principal.person
        and principal.role == "owner"
        and getattr(principal, "runner", None) is None
    )


def available(principal, gateway):
    if gateway is None or getattr(principal, "runner", None) is not None:
        return False
    return any(
        machine["enabled"]
        and not machine["revoked_at"]
        and machine["binding_state"] == "owner_attested"
        and requester_ok(gateway, principal, machine)
        for machine in registry.machines(gateway.db, principal.workspace_id)
    )


def _store(ctx):
    store = jobs.open_store(ctx.gateway, ctx.principal.workspace_id)
    jobs.sweep(store, ctx.db, ctx.principal.workspace_id)
    return store


def _card(ctx, task):
    service = BoardService(ctx)
    with service.store.connection() as conn:
        row = task_row(conn, task)
        board = conn.execute("SELECT * FROM boards WHERE id=?", (row["board_id"],)).fetchone()
        if board["archived"] or not ctx.principal.policy.can_read_wing(board["wing"]):
            raise ToolError("no: card not found")
        # Do not expire claims or project a card here: requesting a start is read-only.
        return service, dict(row), dict(board)


def _requester(ctx):
    p = ctx.principal
    if getattr(p, "runner", None) is not None or not (
        person_owner(p) or (not p.person and p.auth == "pat" and p.policy.trust == "trusted")
    ):
        raise ToolError("no: requester not allowed")


def _check_request_key(request_key):
    if request_key is not None and (
        not isinstance(request_key, str) or not 1 <= len(request_key) <= 128
    ):
        raise ToolError("no: invalid request_key")


def card_start(
    ctx,
    task,
    machine,
    project,
    topic,
    runtime_mode=None,
    request_key=None,
    config_ref=None,
):
    """On a board with an accepted live ``[runner]`` config only a signed-in workspace
    owner may call this (anyone else is told to use ``orch_card_start``); the start then
    runs the config's pinned role through the orchestrator's fenced, capped start."""
    for key, value in (("machine", machine), ("project", project), ("topic", topic)):
        if not isinstance(value, str) or not value:
            raise ToolError(f"no: {key} is required")
    if runtime_mode is not None and not isinstance(runtime_mode, str):
        raise ToolError("no: runtime mode not allowed")
    if config_ref is not None and (not isinstance(config_ref, str) or not config_ref):
        raise ToolError("no: invalid config_ref")
    from commonplace.projects import orchestration as orch

    # Fail closed on a settings read error before resolving the start.
    orch.enabled(ctx.db, ctx.principal.workspace_id)
    machine = registry.get_machine(ctx.db, ctx.principal.workspace_id, machine)
    if machine is None or not machine["enabled"] or machine["revoked_at"]:
        raise ToolError("no: machine not found")
    store = _store(ctx)
    if machine["binding_state"] != "owner_attested":
        raise ToolError("no: binding_not_attested")
    if not requester_ok(ctx.gateway, ctx.principal, machine):
        raise ToolError("no: requester not allowed")
    service, row, board = _card(ctx, task)
    try:
        service._write(board, row=row)
    except ToolError as exc:
        if "restricted-origin" in str(exc):
            raise Refused("origin_not_released") from None
        raise
    from commonplace.orch import runner_bridge

    if runner_bridge.configured(ctx.gateway, ctx.principal.workspace_id, board["slug"]):
        _check_request_key(request_key)
        if not person_owner(ctx.principal):
            raise runner_bridge.StartRefused(
                "use_orch_card_start",
                "use orch_card_start: this board's starts run from its orchestrator config",
            )
        return runner_bridge.person_start(
            ctx,
            store,
            machine=machine,
            card_row=row,
            board_row=board,
            project=project,
            topic=topic,
            runtime_mode=runtime_mode,
            request_key=request_key,
            config_ref=config_ref,
        )
    from commonplace.projects import orchestration as orch

    if orch.enabled(ctx.db, ctx.principal.workspace_id):
        refusal = orch.requester_refusal(ctx.gateway, ctx.principal, board)
        if refusal:
            raise orch.AdmissionRefused(refusal)
    if config_ref is not None:
        raise ToolError("no: config_ref needs a board with an orchestrator config")
    runtime_mode = "approval-required" if runtime_mode is None else runtime_mode
    startable(ctx.gateway, machine, row, board)
    if project not in grant_values(machine, "projects"):
        raise ToolError("no: project not allowed")
    if topic not in grant_values(machine, "topics"):
        raise ToolError("no: topic not allowed")
    if runtime_mode not in MODES or MODES.index(runtime_mode) > MODES.index(
        machine["max_runtime_mode"]
    ):
        raise ToolError("no: runtime mode not allowed")
    _check_request_key(request_key)
    status, row = jobs.create(
        store,
        machine=machine,
        requested_by=(
            "user:" + ctx.principal.user_id
            if ctx.principal.person
            else "conn:" + ctx.principal.connection_id
        ),
        person=ctx.principal.person,
        task_number=row["id"],
        board_slug=board["slug"],
        project=project,
        topic=topic,
        runtime_mode=runtime_mode,
        request_key=request_key,
    )
    return status, jobs.job_object(row, tokens=False)


def card_start_cancel(ctx, job):
    _requester(ctx)
    row = jobs.cancel(_store(ctx), job, ctx.principal)
    return {key: row[key] for key in ("state", "cancel_requested_at", "updated_at")}


def card_start_resolve(ctx, job, outcome, thread_id=None):
    if not person_owner(ctx.principal):
        raise ToolError("no: a signed-in workspace owner is required")
    if outcome not in ("started", "failed") or (
        outcome == "started" and (not isinstance(thread_id, str) or not thread_id)
    ):
        raise ToolError("no: invalid resolution")
    store = _store(ctx)
    with store.transaction() as conn, ctx.db._lock, ctx.db._tx():
        ctx.db.audit(
            action="runner.resolve",
            decision="allow",
            workspace_id=ctx.principal.workspace_id,
            user_id=ctx.principal.user_id,
            connection_id=ctx.principal.connection_id,
            auth=ctx.principal.auth,
            target_ids=(job,),
        )
        row = jobs.resolve(store, job, ctx.principal, outcome, thread_id, conn=conn)
    return {key: row[key] for key in ("state", "thread_id", "resolved_by", "resolved_at")}


def card_start_status(ctx, task):
    _requester(ctx)
    _service, row, _board = _card(ctx, task)
    return jobs.status(_store(ctx), row["id"], ctx.principal)


OPERATIONS = {
    "card_start": (
        card_start,
        {
            "task": TASK,
            "machine": STRING,
            "project": STRING,
            "topic": STRING,
            "runtime_mode": {"type": "string", "enum": list(MODES), "default": "approval-required"},
            "request_key": {**STRING, "maxLength": 128},
            "config_ref": {**STRING, "maxLength": 40},
        },
        ("task", "machine", "project", "topic"),
        "Request an attested machine to start a worker for a claimable card. On a board "
        "with an orchestrator config only a signed-in workspace owner may call it; "
        "config_ref (live or exp:<name>) picks the config version.",
    ),
    "card_start_cancel": (
        card_start_cancel,
        {"job": STRING},
        ("job",),
        "Cancel a queued start or request cancellation while starting. Uncertain starts need owner resolution.",
    ),
    "card_start_resolve": (
        card_start_resolve,
        {
            "job": STRING,
            "outcome": {"type": "string", "enum": ["started", "failed"]},
            "thread_id": STRING,
        },
        ("job", "outcome"),
        "Resolve an uncertain start as a signed-in workspace owner.",
    ),
    "card_start_status": (
        card_start_status,
        {"task": TASK},
        ("task",),
        "Read your start jobs for a card; signed-in workspace owners see all jobs.",
    ),
}


def dispatch(ctx, name, arguments):
    operation, properties, required, _description = OPERATIONS[name]
    if not isinstance(arguments, dict) or arguments.keys() - properties.keys():
        raise ToolError("no: unexpected arguments")
    if any(key not in arguments for key in required):
        raise ToolError("no: missing required arguments")
    for key, value in arguments.items():
        if key in ("request_key", "config_ref") and value is None:
            continue
        types = properties[key]["type"]
        if types == "string" and (not isinstance(value, str) or not value):
            raise ToolError(f"no: invalid {key}")
        if key == "task" and (isinstance(value, bool) or not isinstance(value, (int, str))):
            raise ToolError("no: card not found")
    try:
        return operation(ctx, **arguments)
    except (jobs.Conflict, ValueError) as exc:
        raise ToolError(f"no: {exc}") from None


def _mcp(ctx, name, arguments):
    result = dispatch(ctx, name, arguments)
    return result[1] if name == "card_start" else result


def register_tools():
    global _registered
    if _registered:
        return
    for name, (_operation, properties, required, description) in OPERATIONS.items():
        write = name != "card_start_status"
        register(
            LocalTool(
                name=name,
                description=description,
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": list(required),
                    "additionalProperties": False,
                },
                handler=lambda ctx, args, name=name: _mcp(ctx, name, args),
                scope="memory:write" if write else "memory:read",
                kind="write" if write else "read",
                profiles=WORK_PROFILES,
                available=available,
            )
        )
    _registered = True
