"""MCP tools for orchestrator configs, the durable lease and journal, and config-driven starts.

Every tool goes through :func:`dispatch`, which REST (``rest.py``) shares, so both
surfaces validate arguments, check authority and answer refusals the same way.

Authority (nothing new; the stores enforce it again):

- read (``orch_config_get``, ``orch_config_render``, ``orch_config_diff``,
  ``orch_checkpoint``, ``orch_restore_status``): read access to the board's wing.
- propose (``orch_config_propose``): write access to the board's wing; untrusted
  connections may propose, and the proposer and its trust are stamped on the version.
- accept, direct write, reject, retire an experiment, force a lease, void a step,
  acknowledge a restore: ``store.orch_owner`` only (a signed-in owner; the CLI acts as
  the owner through the data directory). An agent token with role owner is refused.
- lease, journal, gates, ``orch_card_start``: a principal that passes the runner's
  ``requester_ok`` for the config's ``[runner].machine``, plus epoch and token.

Untrusted connections only see the read tools and ``orch_config_propose``
(``trusted_only``); runner children see none (``available``). A configuring live accept
leaves the trigger cursor arming; the surface arms it right after the commit.

Events (``commonplace.events``) carry refs only: board, version ref, channel, sha256,
role, step key. Never a config body, prompt, token or note.
"""

from __future__ import annotations

import json
import logging

from commonplace.extensions import WORK_PROFILES, LocalTool, ToolError, register

logger = logging.getLogger("commonplace.orch")

STRING = {"type": "string", "minLength": 1}
BOARD = {"type": "string", "minLength": 1, "maxLength": 64}
ROLE = {"type": "string", "minLength": 1, "maxLength": 32}
VERSION = {"type": "integer", "minimum": 1}
CHANNEL = {"type": "string", "minLength": 1, "maxLength": 40}
EPOCH = {"type": "integer", "minimum": 1}
TOKEN = {"type": "string", "minLength": 1, "maxLength": 200}
STEP_KEY = {"type": "string", "minLength": 1, "maxLength": 80}
SHA256 = {"type": "string", "minLength": 64, "maxLength": 64}
CARD = {"type": "integer", "minimum": 1}
TEXT = {"type": "string", "minLength": 1, "maxLength": 128 * 1024}
SOURCE_REF = {"type": "string", "minLength": 1, "maxLength": 200}
MAX_VERSIONS = 100
LEASE = {"board": BOARD, "role": ROLE, "epoch": EPOCH, "token": TOKEN}
_registered = False


class SurfaceError(ToolError):
    """A refusal raised by the surface itself (bad arguments, unknown board)."""

    def __init__(self, code: str, message: str, status: int):
        self.code = code
        self.status = status
        self.details = {}
        super().__init__(f"no: {message} ({code})")


def available(principal, gateway) -> bool:
    """Runner children (worker sessions started by a machine) never see these tools."""
    return gateway is not None and getattr(principal, "runner", None) is None


# ------------------------------------------------------------------ helpers


def _store(ctx):
    from . import store

    return store.open_store(ctx.gateway, ctx.principal.workspace_id)


def _board(ctx, board, *, write=False) -> dict:
    """The board row; 404 unless the caller reads its wing, 403 to propose without write."""
    from commonplace.boards.store import BoardStore

    from . import store

    store._board(board)
    with BoardStore(ctx.workspace_dir()).connection() as conn:
        row = conn.execute("SELECT * FROM boards WHERE slug=?", (board,)).fetchone()
    policy = ctx.principal.policy
    if row is None or row["archived"] or not policy.can_read_wing(row["wing"]):
        raise SurfaceError("board_not_found", "board not found", 404)
    if write and not policy.can_write_wing(row["wing"]):
        raise SurfaceError("not_allowed", "proposing needs write access to the board's wing", 403)
    return dict(row)


def _owner(ctx) -> None:
    """Owner-only actions are refused before any store is opened."""
    from . import store

    store.require_owner(ctx.principal)


def _connection_names(ctx) -> list:
    rows = ctx.db.connections(ctx.principal.workspace_id)
    return [row["name"] for row in rows if not row["revoked_at"]]


def _emit(ctx, kind: str, subject: str = "", **data) -> None:
    """Refs-only event after the store committed; an emission failure never fails the call."""
    from commonplace import events
    from commonplace.http_util import log_failure

    try:
        events.emit(
            ctx.db,
            ctx.principal.workspace_id,
            kind,
            subject=subject,
            actor=ctx.principal.actor,
            data=data,
        )
    except Exception as exc:
        log_failure(logger, "orchestrator event emission failed", exc)


def _arm(ctx, board: str) -> bool:
    """Arm the board's NULL trigger cursor now; the sweep self-heals if this fails."""
    from commonplace.http_util import log_failure

    from . import triggers

    try:
        return triggers.arm_cursor(ctx.gateway, ctx.principal.workspace_id, board)
    except Exception as exc:
        log_failure(logger, "orchestrator cursor arming failed", exc)
        return False


def _version_ref(version) -> dict:
    return {
        "board": version.board,
        "version": version.ref,
        "channel": version.channel,
        "sha256": version.sha256,
        "status": version.status,
    }


def _accepted(ctx, result) -> dict:
    from . import schema

    version = result.version
    cursor = result.cursor
    if result.arm_needed and _arm(ctx, version.board):
        cursor = "armed"
    _emit(ctx, "orch.config.accepted", version.board, **_version_ref(version))
    return {
        "accepted": version.ref,
        "board": version.board,
        "channel": version.channel,
        "sha256": version.sha256,
        "previous": result.previous.ref if result.previous else None,
        "cursor": cursor,
        "diff": schema.format_diff(result.changes),
    }


# ------------------------------------------------------------------- configs


def config_get(ctx, board, version=None, channel="live"):
    _board(ctx, board)
    st = _store(ctx)
    if version is not None:
        return {"version": st.get(board, version).public(body=True)}
    accepted = st.accepted(board, channel)
    return {
        "board": board,
        "channel": channel,
        "accepted": accepted.public(body=True) if accepted else None,
        "versions": [row.public() for row in st.versions(board)[:MAX_VERSIONS]],
    }


def config_render(ctx, board, version=None, channel="live"):
    from . import schema, store

    _board(ctx, board)
    st = _store(ctx)
    if version is None:
        found = st.accepted(board, channel)
        if found is None:
            raise store.OrchError("not_found", "no accepted version on that channel")
    else:
        found = st.get(board, version)
    try:
        config = schema.parse(found.body)
    except schema.ConfigError as exc:
        raise store._config_error(exc) from None
    return {"board": board, "version": found.ref, "markdown": schema.render(config)}


def config_diff(ctx, board, old, new):
    from . import schema

    _board(ctx, board)
    changes = _store(ctx).diff(board, old, new)
    return {"board": board, "old": f"v{old}", "new": f"v{new}", "diff": schema.format_diff(changes)}


def config_propose(ctx, board, text, channel="live", sha256=None, source_ref=None):
    _board(ctx, board, write=True)
    version = _store(ctx).propose(
        ctx.principal,
        board,
        text,
        connections=_connection_names(ctx),
        channel=channel,
        sha256=sha256,
        source_ref=source_ref,
    )
    _emit(ctx, "orch.config.proposed", board, **_version_ref(version))
    return version.public()


def config_accept(ctx, board, version, sha256):
    _owner(ctx)
    _board(ctx, board)
    result = _store(ctx).accept(
        ctx.principal, board, version, sha256=sha256, connections=_connection_names(ctx)
    )
    return _accepted(ctx, result)


def config_write(ctx, board, text, sha256, channel="live", source_ref=None):
    _owner(ctx)
    _board(ctx, board)
    result = _store(ctx).write(
        ctx.principal,
        board,
        text,
        connections=_connection_names(ctx),
        sha256=sha256,
        channel=channel,
        source_ref=source_ref,
    )
    return _accepted(ctx, result)


def config_reject(ctx, board, version):
    _owner(ctx)
    _board(ctx, board)
    row = _store(ctx).reject(ctx.principal, board, version)
    _emit(ctx, "orch.config.rejected", board, **_version_ref(row))
    return row.public()


def exp_retire(ctx, board, name):
    _owner(ctx)
    _board(ctx, board)
    rows = _store(ctx).retire(ctx.principal, board, f"exp:{name}")
    _emit(ctx, "orch.experiment.retired", board, board=board, channel=f"exp:{name}")
    return [row.public() for row in rows]


# ------------------------------------------------------------------- restore


def restore_status(ctx):
    state = _store(ctx).restore_state()
    return {
        "restore_pending": state.flagged,
        "after": state.after,
        "ack_text": state.ack_text,
        "restored_at": state.restored_at,
        "acked_by": state.acked_by,
        "acked_at": state.acked_at,
    }


def restore_ack(ctx, text):
    _owner(ctx)
    reset = _store(ctx).restore_ack(ctx.principal, text)
    arming = [{"board": board, "gen": gen, "armed": _arm(ctx, board)} for board, gen in reset]
    _emit(ctx, "orch.restore.acked", "", boards=len(arming))
    return {"acknowledged": True, "arming": arming}


# ------------------------------------------------------------- lease, journal


def resume(ctx, board, role, force=False):
    from . import durable

    if force:
        _owner(ctx)
    return durable.resume(ctx, board, role, force=bool(force))


def heartbeat(ctx, board, role, epoch, token):
    from . import durable

    return durable.heartbeat(ctx, board, role, epoch, token)


def release(ctx, board, role, epoch, token):
    from . import durable

    return durable.release(ctx, board, role, epoch, token)


def force_release(ctx, board, role):
    from . import durable

    _owner(ctx)
    _board(ctx, board)
    result = durable.force_release(ctx, board, role)
    if result["released"]:
        _emit(ctx, "orch.lease.forced", board, board=board, role=role, epoch=result["epoch"])
    return result


def card_start(ctx, board, role, card, step_key, epoch, token, config_ref=None):
    from . import runner_bridge

    return runner_bridge.orch_card_start(
        ctx, board, role, card, step_key, epoch, token, config_ref=config_ref
    )


def journal_intent(ctx, board, role, epoch, token, step_key, kind, card=None, detail=None):
    from . import durable

    return durable.journal_intent(
        ctx, board, role, epoch, token, step_key, kind, card=card, detail=detail
    )


def journal_record(ctx, board, role, epoch, token, step_key, state, evidence=None, code=None):
    from . import durable

    return durable.journal_record(
        ctx, board, role, epoch, token, step_key, state, evidence=evidence, code=code
    )


def gate_open(ctx, board, role, epoch, token, step_key, repo, pr, head, card=None):
    from . import durable

    return durable.gate_open(
        ctx, board, role, epoch, token, step_key, repo=repo, pr=pr, head=head, card=card
    )


def gate_record(ctx, board, role, epoch, token, step_key, verdict):
    from . import durable

    return durable.gate_record(ctx, board, role, epoch, token, step_key, verdict)


def void_step(ctx, board, role, step_key):
    from . import durable

    _owner(ctx)
    _board(ctx, board)
    step = durable.void_step(ctx, board, role, step_key)
    _emit(ctx, "orch.step.voided", board, board=board, role=role, step_key=step_key)
    return step


def checkpoint(ctx, board, role):
    from . import durable

    return durable.checkpoint(ctx, board, role)


# ---------------------------------------------------------------- registry

# Each entry: handler, properties, required keys, kind, trusted_only, owner_only, text.
OPERATIONS = {
    "orch_config_get": (
        config_get,
        {"board": BOARD, "version": VERSION, "channel": CHANNEL},
        ("board",),
        "read",
        False,
        False,
        "Read an orchestrator config version (with its TOML), or the accepted version of "
        "a channel (default live) and the board's version list.",
    ),
    "orch_config_render": (
        config_render,
        {"board": BOARD, "version": VERSION, "channel": CHANNEL},
        ("board",),
        "read",
        False,
        False,
        "Render a config version (default: the accepted live one) as Markdown.",
    ),
    "orch_config_diff": (
        config_diff,
        {"board": BOARD, "old": VERSION, "new": VERSION},
        ("board", "old", "new"),
        "read",
        False,
        False,
        "Diff two config versions of a board.",
    ),
    "orch_config_propose": (
        config_propose,
        {
            "board": BOARD,
            "text": TEXT,
            "channel": CHANNEL,
            "sha256": SHA256,
            "source_ref": SOURCE_REF,
        },
        ("board", "text"),
        "write",
        False,
        False,
        "Propose an orchestrator config (TOML) for a board you can write; channel live or "
        "exp:<name>. The workspace owner accepts it by its sha256.",
    ),
    "orch_config_accept": (
        config_accept,
        {"board": BOARD, "version": VERSION, "sha256": SHA256},
        ("board", "version", "sha256"),
        "write",
        True,
        True,
        "Accept a proposed config whose text has this sha256 (signed-in workspace owner "
        "only). Returns the diff.",
    ),
    "orch_config_write": (
        config_write,
        {
            "board": BOARD,
            "text": TEXT,
            "sha256": SHA256,
            "channel": CHANNEL,
            "source_ref": SOURCE_REF,
        },
        ("board", "text", "sha256"),
        "write",
        True,
        True,
        "Write and accept a config at once (signed-in workspace owner only).",
    ),
    "orch_config_reject": (
        config_reject,
        {"board": BOARD, "version": VERSION},
        ("board", "version"),
        "write",
        True,
        True,
        "Reject a proposed config (signed-in workspace owner only).",
    ),
    "orch_exp_retire": (
        exp_retire,
        {"board": BOARD, "name": {"type": "string", "minLength": 1, "maxLength": 32}},
        ("board", "name"),
        "write",
        True,
        True,
        "Close the experiment channel exp:<name> (signed-in workspace owner only).",
    ),
    "orch_restore_status": (
        restore_status,
        {},
        (),
        "read",
        False,
        False,
        "Show whether a restore needs acknowledging and the exact text to type.",
    ),
    "orch_restore_ack": (
        restore_ack,
        {"text": {"type": "string", "minLength": 1, "maxLength": 200}},
        ("text",),
        "write",
        True,
        True,
        "Acknowledge a restore with the exact text from orch_restore_status (signed-in "
        "workspace owner only).",
    ),
    "orch_resume": (
        resume,
        {"board": BOARD, "role": ROLE, "force": {"type": "boolean"}},
        ("board", "role"),
        "write",
        True,
        False,
        "Claim the orchestrator lease of a board role and read its checkpoint. The token "
        "is shown once; force (owner only) takes a held lease.",
    ),
    "orch_heartbeat": (
        heartbeat,
        dict(LEASE),
        tuple(LEASE),
        "write",
        True,
        False,
        "Renew your orchestrator lease.",
    ),
    "orch_release": (
        release,
        dict(LEASE),
        tuple(LEASE),
        "write",
        True,
        False,
        "Give up your orchestrator lease.",
    ),
    "orch_force_release": (
        force_release,
        {"board": BOARD, "role": ROLE},
        ("board", "role"),
        "write",
        True,
        True,
        "End the current lease of a board role (signed-in workspace owner only).",
    ),
    "orch_card_start": (
        card_start,
        {
            **LEASE,
            "card": CARD,
            "step_key": STEP_KEY,
            "config_ref": CHANNEL,
        },
        (*LEASE, "card", "step_key"),
        "write",
        True,
        False,
        "As the lease holder, start the config's runner role on a card. The same step key "
        "returns the stored job; config_ref is live or exp:<name>.",
    ),
    "orch_journal_intent": (
        journal_intent,
        {
            **LEASE,
            "step_key": STEP_KEY,
            "kind": {
                "type": "string",
                "enum": ["merge", "thread", "handoff", "card", "hold", "next"],
            },
            "card": CARD,
            "detail": {"type": "string", "maxLength": 2000},
        },
        (*LEASE, "step_key", "kind"),
        "write",
        True,
        False,
        "Write a journal step before its effect. The same key returns the stored step.",
    ),
    "orch_journal_record": (
        journal_record,
        {
            **LEASE,
            "step_key": STEP_KEY,
            "state": {
                "type": "string",
                "enum": ["started", "waiting", "done", "failed", "in_doubt", "needs_person"],
            },
            "evidence": {"type": "object"},
            "code": {"type": "string", "minLength": 1, "maxLength": 64},
        },
        (*LEASE, "step_key", "state"),
        "write",
        True,
        False,
        "Move a journal step to a new state with evidence (runner_job, card_state or "
        "holder_attested).",
    ),
    "orch_gate_open": (
        gate_open,
        {
            **LEASE,
            "step_key": STEP_KEY,
            "repo": {"type": "string", "minLength": 3, "maxLength": 201},
            "pr": {"type": "integer", "minimum": 1},
            "head": {"type": "string", "minLength": 40, "maxLength": 64},
            "card": CARD,
        },
        (*LEASE, "step_key", "repo", "pr", "head"),
        "write",
        True,
        False,
        "Reserve the one gate attempt for a pull request head.",
    ),
    "orch_gate_record": (
        gate_record,
        {
            **LEASE,
            "step_key": STEP_KEY,
            "verdict": {"type": "string", "enum": ["pass", "fail", "error", "budget"]},
        },
        (*LEASE, "step_key", "verdict"),
        "write",
        True,
        False,
        "Record the gate verdict; every verdict is final for that head.",
    ),
    "orch_void_step": (
        void_step,
        {"board": BOARD, "role": ROLE, "step_key": STEP_KEY},
        ("board", "role", "step_key"),
        "write",
        True,
        True,
        "Void a journal step and its gate (signed-in workspace owner only).",
    ),
    "orch_checkpoint": (
        checkpoint,
        {"board": BOARD, "role": ROLE},
        ("board", "role"),
        "read",
        False,
        False,
        "Read a board role's checkpoint: open steps, in-doubt work, jobs, misses and "
        "unstarted todo cards.",
    ),
}

_TYPES = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "object": dict,
}


def _check(name, key, value, spec) -> None:
    kind = spec["type"]
    if kind == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
        ok = ok and value >= spec.get("minimum", value)
    else:
        ok = isinstance(value, _TYPES[kind])
    if ok and kind == "string":
        ok = spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", len(value))
        ok = ok and ("enum" not in spec or value in spec["enum"])
    if not ok:
        raise SurfaceError("invalid_argument", f"invalid {key}", 400)


def validate(name: str, arguments) -> dict:
    """Arguments checked against the tool's properties; ``None`` drops an optional one."""
    _handler, properties, required, *_rest = OPERATIONS[name]
    if not isinstance(arguments, dict) or arguments.keys() - properties.keys():
        raise SurfaceError("invalid_argument", "unexpected arguments", 400)
    clean = {key: value for key, value in arguments.items() if value is not None}
    missing = [key for key in required if key not in clean]
    if missing:
        raise SurfaceError("invalid_argument", "missing " + ", ".join(missing), 400)
    for key, value in clean.items():
        _check(name, key, value, properties[key])
    return clean


def dispatch(ctx, name: str, arguments) -> object:
    """Validate and run one operation. Refusals raise ``ToolError`` subclasses with
    ``code``, ``status`` (HTTP) and ``details`` (``OrchError``, ``DurableError``,
    ``StartRefused``, ``SurfaceError``)."""
    clean = validate(name, arguments)
    return OPERATIONS[name][0](ctx, **clean)


def error_text(exc: ToolError) -> str:
    """``no: ...`` plus a JSON line with the stable code and the refusal's details (stored
    step, holder, job), when the refusal has a code."""
    text = str(exc)
    if not text.startswith("no:"):
        text = "no: " + text
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        extra = {"code": code, **(getattr(exc, "details", None) or {})}
        text += "\n" + json.dumps(extra, sort_keys=True, default=str)
    return text


def _mcp(ctx, name, arguments):
    try:
        return dispatch(ctx, name, arguments)
    except ToolError as exc:
        raise ToolError(error_text(exc)) from None


def register_tools() -> None:
    global _registered
    if _registered:
        return
    for name, (_h, properties, required, kind, trusted, owner, text) in OPERATIONS.items():
        register(
            LocalTool(
                name=name,
                description=text,
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": list(required),
                    "additionalProperties": False,
                },
                handler=lambda ctx, args, name=name: _mcp(ctx, name, args),
                scope="memory:write" if kind == "write" else "memory:read",
                kind=kind,
                profiles=WORK_PROFILES,
                trusted_only=trusted,
                owner_only=owner,
                available=available,
            )
        )
    _registered = True
