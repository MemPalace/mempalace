"""What the card panel's "Start agent" control may offer, for one card.

Read-only and owner-only, like ``card_start`` itself. It resolves the same things the start
would: the board's accepted ``[runner]`` config when there is one, otherwise the attested
machines this owner may use and the projects and topics each one allows. Anything the start
would refuse for the card (not Todo, blocked, outside the machine's wings...) comes back as
a ``refusal`` code, so the panel never offers a choice that the server will turn down."""

from commonplace.extensions import ToolError
from commonplace.orch import runner_bridge
from commonplace.redact import redact

from . import registry
from .predicate import Refused, grant_values, requester_ok, startable
from .tools import _card, person_owner

# The card cannot be started whatever the setup is: the panel shows no control at all.
NOT_CLAIMABLE = frozenset(
    {"not_todo", "assigned", "proposal_open", "waiting_on_person", "blocked_by"}
)


def _machine_ok(gateway, principal, machine) -> bool:
    return bool(
        machine["enabled"]
        and not machine["revoked_at"]
        and machine["binding_state"] == "owner_attested"
        and requester_ok(gateway, principal, machine)
    )


def _public(machine) -> dict:
    return {"id": machine["id"], "label": redact(machine["label"])[0]}


def _card_refusal(gateway, machine, row, board) -> str | None:
    try:
        startable(gateway, machine, row, board)
    except Refused as exc:
        return exc.reason.replace(" ", "_")
    return None


def _configured(ctx, row, board) -> dict:
    """A board with an accepted ``[runner]`` config: the start is the config's own."""
    ws = ctx.principal.workspace_id
    config = runner_bridge.resolve_version(ctx.gateway, ws, board["slug"]).config()
    machine = registry.get_machine(ctx.gateway.db, ws, config.runner.machine)
    return {
        "configured": True,
        "machine": None if machine is None else _public(machine),
        "project": config.runner.project,
        "topic": config.runner_role.topic,
        "refusal": runner_bridge.card_refusal(
            ctx.gateway, machine, row, board, ctx.principal, config
        ),
    }


def _free(ctx, row, board) -> dict:
    """A board without a config: the owner picks the machine, project and topic."""
    offers, refusals = [], []
    for machine in registry.machines(ctx.gateway.db, ctx.principal.workspace_id):
        if not _machine_ok(ctx.gateway, ctx.principal, machine):
            continue
        refusal = _card_refusal(ctx.gateway, machine, row, board)
        if refusal:
            refusals.append(refusal)
            continue
        # The start checks project and topic against the machine's grants, so offer only
        # those; a machine with no project or no topic cannot run anything.
        projects = list(grant_values(machine, "projects"))
        topics = list(grant_values(machine, "topics"))
        if projects and topics:
            offers.append(
                {
                    **_public(machine),
                    "projects": projects,
                    "topics": topics,
                    "max_runtime_mode": machine["max_runtime_mode"],
                }
            )
    refusal = None
    if not offers:
        # A reason about the card (blocked, assigned...) wins over a setup reason.
        card_reasons = [r for r in refusals if r in NOT_CLAIMABLE]
        refusal = (card_reasons or refusals or ["no_machine"])[0]
    return {"configured": False, "machines": offers, "refusal": refusal}


def start_options(ctx, task) -> dict:
    """The answer of ``GET /api/card-starts/options?task=<n>``.

    ``refusal`` is ``None`` when a start can be requested now, otherwise a stable code."""
    if not person_owner(ctx.principal):
        raise ToolError("no: only a workspace owner can start an agent from a card")
    _service, row, board = _card(ctx, task)
    if row["status"] not in ("todo", "doing"):
        return {"configured": None, "refusal": "not_todo"}
    if runner_bridge.configured(ctx.gateway, ctx.principal.workspace_id, board["slug"]):
        return _configured(ctx, row, board)
    return _free(ctx, row, board)
