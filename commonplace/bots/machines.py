"""Machines a bot may use for a person: list them, start or cancel an agent on one.

This module is the only thing bot code knows about machines. Providers plug in through
``register_provider``; their machines are merged and each machine names its own
capabilities. The built-in provider sits on the runner: it calls the runner's own functions
(``registry``, ``card_start``, ``card_start_status``, ``card_start_cancel``, the start options)
and never re-implements or widens their authority checks. A refusal keeps the runner's
stable code and carries a plain sentence a person can act on.
"""

import logging
import threading

from commonplace.extensions import ToolContext, ToolError
from commonplace.orch import runner_bridge
from commonplace.redact import redact
from commonplace.runner import jobs, registry, start_options, tools
from commonplace.runner.predicate import Refused as CardRefused, grant_values, requester_ok

LOG = logging.getLogger(__name__)

TRUSTS = ("owner_attested", "unbound")
LIVE = frozenset(("queued", "leased", "starting"))
QUIET = frozenset(("not_allowed", "card_not_found"))

SENTENCES = {
    "no_machine": "No machine is ready to start this task right now.",
    "machine_not_found": "I can't find that machine among the ones you may use.",
    "not_attested": "That machine has not been attested by the workspace owner yet, so it can't start agents.",
    "not_allowed": "Only workspace owners can start agents on machines today.",
    "card_not_found": "I can't find that task.",
    "not_todo": "That task isn't waiting to start: only a task in the to-do state can be started.",
    "assigned": "That task is already assigned, so an agent can't take it.",
    "blocked_by": "That task is blocked by other open work; finish or unblock that first.",
    "waiting_on_person": "That task is waiting for a person's answer, so an agent can't start it yet.",
    "proposal_open": "That task has an open proposal; settle it before starting an agent.",
    "origin_not_released": "That task comes from a source that hasn't been released for agents, so it can't be started.",
    "task_type_not_allowed": "Agents aren't allowed to take this type of task.",
    "outside_worker_wing": "That machine isn't allowed to work in this project's area. An owner can change what it may use.",
    "worker_unavailable": "That machine's worker isn't available right now.",
    "project_not_allowed": "That machine isn't allowed to work on this project. An owner can change what it may use.",
    "topic_not_allowed": "That machine isn't allowed to run this kind of work. An owner can change what it may use.",
    "mode_not_allowed": "That machine isn't allowed to run at the requested level.",
    "active_start": "An agent is already queued or starting on that task.",
    "started_recently": "An agent was started on that task recently.",
    "busy": "That machine's queue is full or its hourly limit is reached; try again later.",
    "request_conflict": "That start request was already used with different settings.",
    "invalid_request": "That start request is not valid.",
    "start_not_found": "I can't find that start.",
    "already_finished": "That agent has already launched or finished, so it can't be stopped from here.",
    "uncertain": "It isn't clear whether that agent launched; a workspace owner has to resolve it.",
    "changed": "That start changed while I was working on it; check its status and try again.",
    "machine_not_configured": "That machine has no project or kind of work set up yet. An owner can fix that in the runner settings.",
    "unsupported": "Running commands on machines is not available yet.",
}
# Runner refusal texts ("no: <text>" or a Conflict message) that have no code of their own.
TEXT_CODES = {
    "machine not found": "machine_not_found",
    "binding_not_attested": "not_attested",
    "requester not allowed": "not_allowed",
    "a signed-in workspace owner is required": "not_allowed",
    "only the requester or a workspace owner may cancel": "not_allowed",
    "card not found": "card_not_found",
    "project not allowed": "project_not_allowed",
    "topic not allowed": "topic_not_allowed",
    "runtime mode not allowed": "mode_not_allowed",
    "invalid request_key": "invalid_request",
    "start not found": "start_not_found",
    "start is already finished": "already_finished",
    "start outcome is uncertain; a workspace owner must resolve it": "uncertain",
    "start changed": "changed",
}


class Refused(Exception):
    """A request a provider turned down. ``code`` is stable; ``text`` is a plain sentence."""

    def __init__(self, code, text=""):
        self.code = code
        self.text = text or SENTENCES.get(code) or f"The request was refused ({code})."
        super().__init__(self.text)


class Unsupported(Exception):
    """No provider offers this capability."""

    code = "unsupported"

    def __init__(self, text=""):
        self.text = text or SENTENCES["unsupported"]
        super().__init__(self.text)


# ---------------------------------------------------------------- the runner provider


def _refusal(exc) -> Refused:
    """The runner's refusal as a Refused with its stable code kept."""
    if isinstance(exc, CardRefused):
        code = exc.reason.replace(" ", "_")
    elif getattr(exc, "code", None):
        code = exc.code
    elif isinstance(exc, jobs.Conflict) and str(exc) in jobs.CONFLICT_CODES:
        code = jobs.conflict_code(exc)
    else:
        code = TEXT_CODES.get(str(exc).removeprefix("no: "), "refused")
    return Refused(code)


def _can_use(principal) -> bool:
    return bool(
        principal.person
        and principal.role == "owner"
        and getattr(principal, "runner", None) is None
    )


def _last_seen(db, machine):
    """When the machine's own credential last reached the hub, or None if never recorded."""
    with db._lock:
        row = db._conn.execute(
            "SELECT last_used_at FROM connections WHERE id=? AND workspace_id=?",
            (machine["machine_connection_id"], machine["workspace_id"]),
        ).fetchone()
    return (row["last_used_at"] or None) if row else None


def _view(db, machine) -> dict:
    attested = machine["binding_state"] == "owner_attested"
    return {
        "id": machine["id"],
        "label": redact(machine["label"])[0],
        "last_seen": _last_seen(db, machine),
        "capabilities": {"start_agent": attested, "run_command": False},
        "grants": {
            "projects": list(grant_values(machine, "projects")),
            "topics": list(grant_values(machine, "topics")),
            "wings": list(grant_values(machine, "wings")),
            "max_runtime_mode": machine["max_runtime_mode"],
        },
        "trust": "owner_attested" if attested else "unbound",
        "verified": False,
    }


class RunnerProvider:
    """Every enrolled, non-revoked machine the person may start on under the runner's rules."""

    name = "runner"

    def _rows(self, gateway, principal):
        return [
            m
            for m in registry.machines(gateway.db, principal.workspace_id)
            if m["enabled"] and not m["revoked_at"] and requester_ok(gateway, principal, m)
        ]

    def machines(self, gateway, principal):
        return [_view(gateway.db, m) for m in self._rows(gateway, principal)]

    def why_none(self, gateway, principal) -> str:
        if not _can_use(principal):
            return "Only workspace owners can use machines today; ask an owner to start the work."
        live = [
            m for m in registry.machines(gateway.db, principal.workspace_id) if not m["revoked_at"]
        ]
        if not live:
            return "No machine is set up yet. A workspace owner can add one in the runner settings."
        return "Every enrolled machine is switched off right now."

    def _choose(self, ctx, number, machine):
        """The registry row, project and topic for a start on a board without its own config."""
        if machine is None:
            options = start_options.start_options(ctx, number)
            offers = options.get("machines") or []
            if not offers:
                raise Refused(options.get("refusal") or "no_machine")
            if len(offers) > 1:
                labels = ", ".join(offer["label"] for offer in offers)
                raise Refused(
                    "machine_needed",
                    f"More than one machine can take this task; say which one: {labels}.",
                )
            machine = offers[0]["id"]
        row = registry.get_machine(ctx.db, ctx.principal.workspace_id, machine)
        if row is None or not row["enabled"] or row["revoked_at"]:
            raise Refused("machine_not_found")
        return row

    def start_agent(self, gateway, principal, card, machine, request_key):
        ctx = ToolContext(principal, gateway)
        try:
            _service, _row, board = tools._card(ctx, card)
            if runner_bridge.configured(gateway, principal.workspace_id, board["slug"]):
                # The board's accepted config fills project and topic (and the machine).
                args = {"machine": machine}
            else:
                row = self._choose(ctx, card, machine)
                projects, topics = grant_values(row, "projects"), grant_values(row, "topics")
                if not projects or not topics:
                    raise Refused("machine_not_configured")
                # The board's own name when the machine allows it, else the owner's first grant.
                project = board["slug"] if board["slug"] in projects else projects[0]
                args = {"machine": row["id"], "project": project, "topic": topics[0]}
            _status, job = tools.card_start(ctx, card, request_key=request_key, **args)
            state = next(
                (
                    s["state"]
                    for s in tools.card_start_status(ctx, card)
                    if s["job"] == job["job_id"]
                ),
                "queued",
            )
        except (ToolError, jobs.Conflict, ValueError) as exc:
            raise _refusal(exc) from None
        return {"job": job["job_id"], "state": state}

    def start_status(self, gateway, principal, card):
        ctx = ToolContext(principal, gateway)
        try:
            rows = tools.card_start_status(ctx, card)
        except ToolError as exc:
            code = _refusal(exc).code
            # Not allowed or not visible is the normal answer for a reader who may not use machines.
            if code not in QUIET:
                LOG.warning("start status failed: %s", code)
            return []
        return [
            {key: row[key] for key in ("job", "machine_label", "state", "updated_at")}
            for row in rows
        ]

    def cancel_start(self, gateway, principal, job):
        try:
            result = tools.card_start_cancel(ToolContext(principal, gateway), job)
        except (ToolError, jobs.Conflict, ValueError) as exc:
            raise _refusal(exc) from None
        return {"state": result["state"]}


# ---------------------------------------------------------------- merged providers

_providers: dict = {}
_lock = threading.Lock()


def register_provider(provider) -> None:
    """Add a provider, replacing one of the same ``name``. Providers are merged."""
    with _lock:
        _providers[provider.name] = provider


def unregister_provider(name) -> None:
    with _lock:
        _providers.pop(name, None)


def providers() -> list:
    with _lock:
        return list(_providers.values())


register_provider(RunnerProvider())


def _clean(machine) -> dict:
    """Only the documented fields: a provider cannot leak anything else through this API."""
    caps = machine.get("capabilities") or {}
    grants = machine.get("grants") or {}
    return {
        "id": str(machine["id"]),
        "label": redact(str(machine.get("label") or machine["id"]))[0],
        "last_seen": machine.get("last_seen") or None,
        "capabilities": {
            "start_agent": caps.get("start_agent") is True,
            "run_command": caps.get("run_command") is True,
        },
        "grants": {
            "projects": list(grants.get("projects") or []),
            "topics": list(grants.get("topics") or []),
            "wings": list(grants.get("wings") or []),
            "max_runtime_mode": grants.get("max_runtime_mode"),
        },
        "trust": machine.get("trust") if machine.get("trust") in TRUSTS else "unbound",
        "verified": False,
    }


def _listed(gateway, principal) -> list:
    """(provider, machine) for every machine every provider offers this person."""
    found = []
    for provider in providers():
        try:
            found.extend((provider, _clean(m)) for m in provider.machines(gateway, principal))
        except Exception:  # one broken provider must not hide the others
            LOG.warning("machine provider %s failed", provider.name, exc_info=True)
    return found


def _owner(found, ref):
    for key in ("id", "label"):
        for provider, machine in found:
            if machine[key] == ref:
                return provider
    return None


def list_machines(gateway, principal) -> list:
    return [machine for _provider, machine in _listed(gateway, principal)]


def why_none(gateway, principal) -> str:
    """A plain sentence for an empty list; empty text when there are machines."""
    if list_machines(gateway, principal):
        return ""
    reasons = []
    for provider in providers():
        text = provider.why_none(gateway, principal)
        if text and text not in reasons:
            reasons.append(text)
    return " ".join(reasons) or SENTENCES["no_machine"]


def can_run_command(gateway, principal) -> bool:
    return any(m["capabilities"]["run_command"] for _p, m in _listed(gateway, principal))


def start_agent(gateway, principal, card, machine=None, request_key=None) -> dict:
    found = _listed(gateway, principal)
    if machine is not None:
        provider = _owner(found, machine)
        if provider is None:
            raise Refused("machine_not_found")
    else:
        capable = {p.name: p for p, m in found if m["capabilities"]["start_agent"]}
        if not capable:
            raise Refused("no_machine", why_none(gateway, principal) or SENTENCES["no_machine"])
        if len(capable) > 1:
            labels = ", ".join(m["label"] for _p, m in found if m["capabilities"]["start_agent"])
            raise Refused(
                "machine_needed",
                f"More than one machine can take this task; say which one: {labels}.",
            )
        provider = next(iter(capable.values()))
    return provider.start_agent(gateway, principal, card, machine, request_key)


def start_status(gateway, principal, card) -> list:
    rows = []
    for provider in providers():
        rows.extend(provider.start_status(gateway, principal, card))
    return sorted(rows, key=lambda row: row.get("updated_at") or "")


def cancel_start(gateway, principal, job) -> dict:
    for provider in providers():
        try:
            return provider.cancel_start(gateway, principal, job)
        except Refused as exc:
            if exc.code != "start_not_found":
                raise
    raise Refused("start_not_found")


def run_command(gateway, principal, machine, command):
    """Nothing runs commands on develop; a provider offering it must say so per machine."""
    provider = _owner(_listed(gateway, principal), machine)
    found = [
        m
        for p, m in _listed(gateway, principal)
        if p is provider and machine in (m["id"], m["label"])
    ]
    if provider is None or not found or not found[0]["capabilities"]["run_command"]:
        raise Unsupported()
    return provider.run_command(gateway, principal, machine, command)
