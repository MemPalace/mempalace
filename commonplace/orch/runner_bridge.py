"""Config-driven starts: ``fenced_start``, one fenced and capped ``BEGIN IMMEDIATE``.

A board whose accepted live config has ``[runner]`` is *configured*. Every start of a
card on such a board goes through ``fenced_start`` on the workspace's ``runner.sqlite3``
(the runner jobs, the config versions, the cursor and the durable journal share it):

- ``orch_card_start`` (path ``orch``): a lease holder that passes ``requester_ok``,
  with epoch and token; one ``orch_steps`` row per ``(board, role, step_key)``.
- ``person_start`` (path ``person``): ``card_start`` by a signed-in workspace owner; no
  lease and no step row.
- the trigger sweep (path ``trigger``, ``commonplace.orch.triggers``): the rebuilt
  actor of a todo event; refusals become coded miss rows and are returned, not raised.

The control snapshot (principal, grants, machine, model settings, board and card rows)
is read **before** the transaction (``Plan``, lock order as ``jobs.sweep``). Inside it,
in this order: (trigger only) the ``(cursor, gen)`` re-check as the first statement;
0. resolved model + request hash; 1. lease (orch), restore flag, accepted version still
current, ``plan`` label -> ``plan_pending``; 2. existing step (orch) or existing keyed
job (person with a ``request_key``, trigger): an exact match returns the stored job, a
mismatch is a 409; then card/grant refusals; 4. the cap; 5. ``jobs.create_in`` and (orch)
the step row, the lease re-check and renewal; (trigger) the cursor advance with the job
or the miss row. A refusal after a write rolls back to a savepoint, so nothing partial
is ever committed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from commonplace.boards.service import BoardService
from commonplace.boards.store import BoardStore, task_row
from commonplace.extensions import ToolContext, ToolError
from commonplace.projects import limits as project_limits, models
from commonplace.runner import jobs, registry
from commonplace.runner.predicate import Refused, grant_values, requester_ok, startable

from . import durable, schema, store as orch_store

MAX_REQUEST_KEY = 128
PATHS = ("orch", "person", "trigger")
MISSES_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS orch_misses (
 id INTEGER PRIMARY KEY,
 board TEXT NOT NULL,
 event_id INTEGER NOT NULL,
 card INTEGER NOT NULL,
 code TEXT NOT NULL,
 actor TEXT,
 gen INTEGER NOT NULL,
 created_at TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS orch_misses_by_board ON orch_misses(board, id)",
)
# 409 unless listed: the refusal is about the caller, not the state.
_STATUS = {
    "use_orch_card_start": 403,
    "not_allowed": 403,
    "config_mismatch": 403,
    "invalid_step_key": 400,
    "invalid_request_key": 400,
    "board_not_found": 404,
    "card_not_found": 404,
    "unknown_role": 404,
}


class StartRefused(ToolError):
    """A refused config-driven start. ``code`` is stable (it is also the trigger miss
    code), ``status`` the HTTP status for surfaces, ``details`` JSON-safe (the stored
    step or job of a 409). ``str()`` is ``"no: <message>"``."""

    def __init__(self, code: str, message: str = "", status: int | None = None, **details):
        self.code = code
        self.status = status or _STATUS.get(code, 409)
        self.details = details
        super().__init__(f"no: {message or code.replace('_', ' ')}")


@dataclass(frozen=True)
class Plan:
    """Pre-transaction control snapshot of one start."""

    board: str  # slug
    board_id: int
    card: int
    version: orch_store.Version
    config: schema.Config
    machine: dict | None
    model: models.ModelChoice | None
    cap: int
    doing: frozenset  # doing cards on the board with a conn: assignee
    plan_label: bool
    refusal: str | None  # card/grant/machine refusal, applied to new inserts only
    requested_by: str
    person: bool
    orchestration_on: bool = False
    workers: frozenset = frozenset()
    workspace_dir: str | None = None

    @property
    def role(self) -> schema.Role:
        return self.config.runner_role

    def run_record(self, step_key=None) -> dict:
        """``jobs.CONFIG_COLUMNS`` of the job this plan inserts."""
        return {
            "config_version": self.version.version,
            "config_sha256": self.version.sha256,
            "channel": self.version.channel,
            "step_key": step_key,
            "model_provider": self.model.provider if self.model else None,
            "model": self.model.model if self.model else None,
        }


@dataclass(frozen=True)
class TriggerRef:
    """The todo event a trigger start is for and the ``(cursor, gen)`` the sweep read."""

    event: int
    cursor: int
    gen: int
    actor: str | None = None


@dataclass
class Outcome:
    status: int | None = None  # 201 inserted, 200 stored job returned
    job: dict | None = None
    step: dict | None = None
    refusal: StartRefused | None = None
    dropped: bool = False  # trigger: the (cursor, gen) re-check failed


# ------------------------------------------------------------------ snapshot


def ensure_schema(conn) -> None:
    """Durable tables and ``orch_misses`` (idempotent, safe inside a transaction)."""
    durable.ensure_schema(conn)
    for statement in MISSES_SCHEMA:
        conn.execute(statement)


def _has_configs(directory) -> bool:
    """Read-only probe: does ``runner.sqlite3`` hold an ``orch_configs`` table? Never
    creates the file or a table."""
    from pathlib import Path

    from mempalace.sqlite_crypto import connect

    path = Path(directory) / orch_store.DB_NAME
    if not path.is_file():
        return False
    conn = connect(str(path), timeout=10, isolation_level=None)
    try:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orch_configs'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def configured(gateway, workspace_id: str, board: str) -> bool:
    """True when ``board`` (slug) has an accepted live config with ``[runner]``.

    A workspace that never stored a config answers False from a read-only probe, so the
    unconfigured path creates no orchestrator tables. Otherwise opening the store
    converts a pending restore marker."""
    if not _has_configs(gateway.workspace_dir(workspace_id)):
        return False
    st = orch_store.open_store(gateway, workspace_id)
    with st.connection() as conn:
        return orch_store.configured_in(conn, board)


def resolve_version(gateway, workspace_id: str, board: str, config_ref=None):
    """The accepted version a start on ``board`` runs (``OrchError`` on refusal)."""
    st = orch_store.open_store(gateway, workspace_id)
    with st.connection() as conn:
        return orch_store.resolve_in(conn, board, config_ref)


def _labels(row) -> list:
    value = row["labels"]
    try:
        labels = json.loads(value) if isinstance(value, str) else (value or [])
    except ValueError:
        return []
    return labels if isinstance(labels, list) else []


def card_refusal(gateway, machine, card_row, board_row, principal, config) -> str | None:
    """Machine, card and grant refusals for a new start, as codes."""
    if (
        machine is None
        or not machine["enabled"]
        or machine["revoked_at"]
        or machine["binding_state"] != "owner_attested"
    ):
        return "machine_unavailable"
    try:
        BoardService(ToolContext(principal=principal, gateway=gateway))._write(
            board_row, row=card_row
        )
    except ToolError as exc:
        return "origin_not_released" if "restricted-origin" in str(exc) else "not_writable"
    try:
        startable(gateway, machine, card_row, board_row)
    except Refused as exc:
        return exc.reason.replace(" ", "_")
    runner, role = config.runner, config.runner_role
    if runner.project not in grant_values(machine, "projects"):
        return "project_not_allowed"
    if role.topic not in grant_values(machine, "topics"):
        return "topic_not_allowed"
    modes = jobs.MODES
    if runner.runtime_mode not in modes or modes.index(runner.runtime_mode) > modes.index(
        machine["max_runtime_mode"]
    ):
        return "mode_not_allowed"
    return None


def prepare(
    gateway,
    workspace_id: str,
    *,
    board_row: dict,
    card_row,
    version,
    principal,
    requested_by: str,
    person: bool,
) -> Plan:
    """Read the control snapshot of one start (before ``fenced_start``)."""
    from commonplace.projects import orchestration as orch

    on = orch.enabled(gateway.db, workspace_id)
    workers = orch.workers(gateway.db, workspace_id) if on else frozenset()
    config = version.config()
    role = config.runner_role
    if config.runner is None or role is None:
        raise StartRefused("not_configured", "the board has no orchestrator runner config")
    machine = registry.get_machine(gateway.db, workspace_id, config.runner.machine)
    try:
        model = models.resolve(gateway.db, workspace_id, board_row["slug"], role.model_role)
    except ValueError:
        model = None
    with BoardStore(gateway.workspace_dir(workspace_id)).connection() as conn:
        limit = project_limits.get(conn, board_row["id"])["max_agents"]
        doing = frozenset(
            row["id"]
            for row in conn.execute(
                "SELECT id FROM tasks WHERE board_id=? AND status='doing' "
                "AND assignee LIKE 'conn:%'",
                (board_row["id"],),
            ).fetchall()
        )
    cap = config.max_agents if limit is None else min(config.max_agents, limit)
    return Plan(
        board=board_row["slug"],
        board_id=board_row["id"],
        card=card_row["id"],
        version=version,
        config=config,
        machine=dict(machine) if machine is not None else None,
        model=model,
        cap=cap,
        doing=doing,
        plan_label="plan" in _labels(card_row),
        refusal=None
        if principal is None  # trigger with an unrebuildable actor: its refusal wins
        else card_refusal(gateway, machine, card_row, board_row, principal, config),
        requested_by=requested_by,
        person=person,
        orchestration_on=on,
        workers=workers,
        workspace_dir=gateway.workspace_dir(workspace_id),
    )


def request_hash(plan: Plan, role: str) -> str:
    """Step hash: card, role, pinned config, resolved model and the launch grant."""
    data = {
        "card": plan.card,
        "role": role,
        "config_version": plan.version.version,
        "config_sha256": plan.version.sha256,
        "model_provider": plan.model.provider if plan.model else None,
        "model": plan.model.model if plan.model else None,
        "machine": plan.config.runner.machine,
        "project": plan.config.runner.project,
        "topic": plan.role.topic,
        "runtime_mode": plan.config.runner.runtime_mode,
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def request_key_for(path: str, *, role=None, step_key=None, event=None, request_key=None):
    """``orch:<role>:<step_key>`` (orch), ``trig:<event id>`` (trigger) or the
    caller's key (person). Longer than 128 characters is refused (R26)."""
    if path == "orch":
        if not isinstance(step_key, str) or not durable.STEP_KEY.fullmatch(step_key):
            raise StartRefused("invalid_step_key", "step_key must match [A-Za-z0-9._:-]{1,80}")
        key = f"orch:{role}:{step_key}"
    elif path == "trigger":
        key = f"trig:{event}"
    else:
        key = request_key
    if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= MAX_REQUEST_KEY):
        raise StartRefused("invalid_request_key", "request_key must be 1-128 characters")
    return key


# -------------------------------------------------------------- fenced start


def _cap_count(conn, plan: Plan, now: str) -> int:
    rows = conn.execute(
        "SELECT DISTINCT task_number FROM runner_jobs WHERE board_slug=? AND "
        "(state IN ('queued','leased','starting','uncertain') "
        "OR (state='started' AND started_at>?))",
        (plan.board, jobs._after(now, -jobs.STARTED_HOLD_SECONDS)),
    ).fetchall()
    return len({row[0] for row in rows} | set(plan.doing))


def _job(conn, job_id):
    row = conn.execute("SELECT * FROM runner_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


def _wrap(exc) -> StartRefused:
    """``OrchError``/``DurableError`` keep their code and status."""
    return StartRefused(
        exc.code,
        str(exc).removeprefix("no: "),
        getattr(exc, "status", None),
        **getattr(exc, "details", {}),
    )


def _fenced(conn, plan, *, path, role, step_key, epoch, token, request_key, now, clock):
    # 0. resolve: the model is pinned from the snapshot; the hash covers it.
    if plan.model is None:
        raise StartRefused("model_unresolved", f"no model is set for {plan.role.model_role}")
    digest = request_hash(plan, role)
    record = plan.run_record(step_key if path == "orch" else None)
    # 1. restore flag, lease (orch), version still accepted, plan label.
    try:
        orch_store.check_restore_in(conn)
        if path == "orch":
            durable.check_lease(
                conn, board=plan.board, role=role, epoch=epoch, token=token, now=now
            )
    except (orch_store.OrchError, durable.DurableError) as exc:
        raise _wrap(exc) from None
    current = orch_store.accepted_in(conn, plan.board, plan.version.channel)
    if current is None or (current.version, current.sha256) != (
        plan.version.version,
        plan.version.sha256,
    ):
        raise StartRefused("config_changed", "the board's config changed; retry")
    if plan.plan_label:
        raise StartRefused("plan_pending", "the card carries plan; approve the plan first")
    # 2. existing step (orch) / 2p existing keyed job (person, trigger).
    if path == "orch":
        stored = durable.step_in(conn, board=plan.board, role=role, step_key=step_key)
        if stored is not None:
            if stored["card"] == plan.card and stored["request_hash"] == digest:
                return Outcome(200, _job(conn, stored["job_id"]), stored)
            raise StartRefused(
                "step_conflict", "step_key reused with a different request", step=stored
            )
    elif request_key is not None:
        previous = conn.execute(
            "SELECT * FROM runner_jobs WHERE task_number=? AND requested_by=? AND request_key=?",
            (plan.card, plan.requested_by, request_key),
        ).fetchone()
        if previous is not None:
            if previous["request_hash"] == jobs.request_digest(
                plan.config.runner.machine,
                plan.config.runner.project,
                plan.role.topic,
                plan.config.runner.runtime_mode,
                record,
            ):
                return Outcome(200, dict(previous))
            raise StartRefused(
                "request_conflict",
                "request_key reused with different arguments",
                job=jobs.job_object(previous),
            )
    if plan.refusal is not None:
        raise StartRefused(plan.refusal, f"card is not startable: {plan.refusal}")
    # 4. cap.
    if plan.orchestration_on:
        from commonplace.projects import orchestration as orch

        with orch.board_read(plan.workspace_dir) as boards:
            used = orch.usage(
                boards,
                conn,
                board_id=plan.board_id,
                board_slug=plan.board,
                workers=plan.workers,
                now=now,
            )
            used = orch.Usage(
                used.slots,
                orch.effective_cap(boards, plan.board_id, plan.config.max_agents, on=True),
            )
            try:
                orch.admit(used, None, plan.card)
            except orch.AdmissionRefused as exc:
                raise StartRefused(exc.code, str(exc).removeprefix("no: ")) from None
    elif _cap_count(conn, plan, now) >= plan.cap:
        raise StartRefused("at_cap", f"the board is at its cap of {plan.cap} agents")
    # 5. insert.
    try:
        status, row = jobs.create_in(
            conn,
            now=now,
            task_number=plan.card,
            board_slug=plan.board,
            machine=plan.machine,
            project=plan.config.runner.project,
            topic=plan.role.topic,
            requested_by=plan.requested_by,
            runtime_mode=plan.config.runner.runtime_mode,
            request_key=request_key,
            person=plan.person and path == "person",
            config=record,
        )
    except jobs.Conflict as exc:
        raise StartRefused(jobs.conflict_code(exc), str(exc)) from None
    step = None
    if path == "orch":
        step = durable.insert_step_in(
            conn,
            board=plan.board,
            role=role,
            step_key=step_key,
            card=plan.card,
            request_hash=digest,
            epoch=epoch,
            actor=plan.requested_by,
            now=now,
            job_id=row["id"],
        )
        try:  # re-check expiry right before COMMIT, then renew
            later = clock()
            durable.check_lease(
                conn, board=plan.board, role=role, epoch=epoch, token=token, now=later
            )
            durable.renew_in(conn, board=plan.board, role=role, epoch=epoch, now=later)
        except durable.DurableError as exc:
            raise _wrap(exc) from None
    return Outcome(status, row, step)


def _advance(conn, plan, trigger) -> None:
    # Same transaction as the job or miss; the in-lock re-check made the pair current.
    conn.execute(
        "UPDATE orch_cursor SET cursor=? WHERE board=? AND gen=? AND cursor=?",
        (trigger.event, plan.board, trigger.gen, trigger.cursor),
    )


def fenced_start(
    store,
    plan: Plan,
    *,
    path: str,
    role: str | None = None,
    step_key: str | None = None,
    epoch=None,
    token=None,
    request_key: str | None = None,
    trigger: TriggerRef | None = None,
    actor_refusal: str | None = None,
    now: str | None = None,
) -> Outcome:
    """One ``BEGIN IMMEDIATE`` on ``store`` (``jobs.JobStore``); see the module doc.

    ``orch``/``person``: a refusal commits nothing and is raised (``StartRefused``)
    after the transaction. ``trigger``: the in-lock ``(cursor, gen)`` re-check runs
    first (a mismatch returns ``Outcome(dropped=True)``: no job, no miss, no advance);
    a refusal (``actor_refusal`` from the pre-transaction actor rebuild included)
    commits its miss row and the cursor advance and is returned, never raised.
    Infrastructure errors roll everything back and propagate."""
    if path not in PATHS:
        raise ValueError("invalid path")
    if (path == "trigger") != (trigger is not None):
        raise ValueError("trigger needs a TriggerRef")
    clock = (lambda: now) if now is not None else jobs._now
    now = clock()
    role = plan.config.runner.role if role is None else role
    key = request_key_for(
        path,
        role=role,
        step_key=step_key,
        event=trigger.event if trigger else None,
        request_key=request_key,
    )
    from commonplace.projects import orchestration as orch

    refusal = None
    with (
        orch.admission(store.gateway, store.workspace_id, store=store)
        if plan.orchestration_on
        else store.transaction() as conn
    ):
        if trigger is not None:
            # First statement in the lock: the pair the sweep recorded must still hold.
            if orch_store.cursor_in(conn, plan.board) != (trigger.cursor, trigger.gen):
                return Outcome(dropped=True)
        ensure_schema(conn)
        conn.execute("SAVEPOINT fenced_start")
        try:
            if actor_refusal is not None:
                raise StartRefused(actor_refusal)
            outcome = _fenced(
                conn,
                plan,
                path=path,
                role=role,
                step_key=step_key,
                epoch=epoch,
                token=token,
                request_key=key,
                now=now,
                clock=clock,
            )
        except StartRefused as exc:
            conn.execute("ROLLBACK TO fenced_start")
            refusal, outcome = exc, Outcome(refusal=exc)
        conn.execute("RELEASE fenced_start")
        if trigger is not None:
            if refusal is not None:
                conn.execute(
                    "INSERT INTO orch_misses(board,event_id,card,code,actor,gen,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        plan.board,
                        trigger.event,
                        plan.card,
                        refusal.code,
                        trigger.actor,
                        trigger.gen,
                        now,
                    ),
                )
            _advance(conn, plan, trigger)
    if refusal is not None and trigger is None:
        raise refusal
    return outcome


# --------------------------------------------------------------- start paths


def _board_row(ctx, board):
    if not isinstance(board, str) or not board:
        raise StartRefused("board_not_found", "board not found")
    with BoardStore(ctx.workspace_dir()).connection() as conn:
        row = conn.execute("SELECT * FROM boards WHERE slug=?", (board,)).fetchone()
    if row is None or row["archived"] or not ctx.principal.policy.can_read_wing(row["wing"]):
        raise StartRefused("board_not_found", "board not found")
    return dict(row)


def _card_row(ctx, board_row, card):
    if isinstance(card, bool) or not isinstance(card, (int, str)):
        raise StartRefused("card_not_found", "card not found on this board")
    try:
        with BoardStore(ctx.workspace_dir()).connection() as conn:
            row = dict(task_row(conn, card))
    except (ToolError, ValueError):
        raise StartRefused("card_not_found", "card not found on this board") from None
    if row["board_id"] != board_row["id"]:
        raise StartRefused("card_not_found", "card not found on this board")
    return row


def launch_reply(plan: Plan, outcome: Outcome) -> dict:
    job = outcome.job or {}
    return {
        "status": outcome.status,
        "job": {**jobs.job_object(job), "state": job.get("state")} if job else None,
        "step": outcome.step,
        "config": {
            "version": job.get("config_version", plan.version.version),
            "sha256": job.get("config_sha256", plan.version.sha256),
            "channel": job.get("channel", plan.version.channel),
        },
        "model": {
            "provider": job.get("model_provider", plan.model.provider if plan.model else None),
            "model": job.get("model", plan.model.model if plan.model else None),
        },
    }


def orch_card_start(
    ctx, board, role, card, step_key, epoch, token, config_ref=None, *, now=None
) -> dict:
    """``orch_card_start``: a lease holder starts the configured runner role on ``card``.

    ``role`` is the lease's role (the step owner); the job runs ``[runner].role`` with
    the pinned prompt, workflow, topic and model. Returns ``launch_reply``: ``status``
    201 (new job) or 200 (same step retried: the stored job, whoever requested it),
    ``job``, ``step``, ``config`` and ``model``. Refusals raise ``StartRefused`` (codes:
    ``invalid_step_key``, ``invalid_request_key``, ``board_not_found``,
    ``card_not_found``, ``unknown_role``, ``not_allowed``, ``not_configured``,
    ``fenced``, ``restore_pending``, ``config_changed``, ``plan_pending``,
    ``model_unresolved``, ``step_conflict``, card codes, ``at_cap``, ``active_start``,
    ``started_recently``, ``busy``) or pass ``OrchError``s of ``resolve_in`` through."""
    principal = ctx.principal
    if getattr(principal, "runner", None) is not None:
        raise StartRefused("not_allowed", "requester not allowed")
    if not isinstance(role, str) or not durable.ROLE.fullmatch(role):
        raise StartRefused("unknown_role", "role not found")
    request_key_for("orch", role=role, step_key=step_key)
    board_row = _board_row(ctx, board)
    card_row = _card_row(ctx, board_row, card)
    ws = principal.workspace_id
    store = jobs.open_store(ctx.gateway, ws)
    jobs.sweep(store, ctx.db, ws)
    version = resolve_version(ctx.gateway, ws, board, config_ref)
    if role not in version.config().roles:
        raise StartRefused("unknown_role", "role not found")
    plan = prepare(
        ctx.gateway,
        ws,
        board_row=board_row,
        card_row=card_row,
        version=version,
        principal=principal,
        requested_by=jobs.requester_ref(principal),
        person=False,
    )
    if plan.machine is None or not requester_ok(ctx.gateway, principal, plan.machine):
        raise StartRefused("not_allowed", "requester not allowed")
    outcome = fenced_start(
        store, plan, path="orch", role=role, step_key=step_key, epoch=epoch, token=token, now=now
    )
    return launch_reply(plan, outcome)


def person_start(
    ctx,
    store,
    *,
    machine,
    card_row,
    board_row,
    project,
    topic,
    runtime_mode=None,
    request_key=None,
    config_ref=None,
):
    """``card_start`` by a signed-in workspace owner on a configured board.

    The caller already checked the person owner, the machine and the card. The
    arguments must equal the config's ``[runner]`` machine/project/runtime_mode and the
    runner role's topic (``config_mismatch``, 403). Returns ``(status, job object)``."""
    principal = ctx.principal
    ws = principal.workspace_id
    version = resolve_version(ctx.gateway, ws, board_row["slug"], config_ref)
    config = version.config()
    runner = config.runner
    if (
        machine["id"] != runner.machine
        or project != runner.project
        or topic != config.runner_role.topic
        or runtime_mode not in (None, runner.runtime_mode)
    ):
        raise StartRefused(
            "config_mismatch",
            "machine, project, topic and runtime_mode come from the board's config",
        )
    plan = prepare(
        ctx.gateway,
        ws,
        board_row=board_row,
        card_row=card_row,
        version=version,
        principal=principal,
        requested_by=jobs.requester_ref(principal),
        person=True,
    )
    outcome = fenced_start(store, plan, path="person", request_key=request_key)
    return outcome.status, jobs.job_object(outcome.job)


# ------------------------------------------------------------- launch payload


def launch_fields(conn, row) -> dict:
    """Lease-payload additions of a config-driven job: ``message`` (``SPAWN_MESSAGE``,
    then the pinned role prompt and workflow steps) and ``modelSelection``. Empty for
    other jobs, and when the pinned version is gone or no longer matches its sha256
    (``starting`` then refuses ``config_invalid`` before any launch)."""
    if row["config_version"] is None:
        return {}
    from commonplace.runner.agent import SPAWN_MESSAGE

    stored = conn.execute(
        "SELECT * FROM orch_configs WHERE board=? AND version=?",
        (row["board_slug"], row["config_version"]),
    ).fetchone()
    if stored is None or stored["sha256"] != row["config_sha256"]:
        return {}
    try:
        version = orch_store._version(stored)
        config = orch_store._checked_config(version)
        text = schema.role_instructions(config, config.runner.role)
    except (orch_store.OrchError, schema.ConfigError, AttributeError):
        return {}
    message = SPAWN_MESSAGE.format(n=row["task_number"], job_id=row["id"]) + "\n\n" + text
    return {
        "message": message,
        "modelSelection": {"instanceId": row["model_provider"], "model": row["model"]},
    }


def config_problem(conn, row) -> str | None:
    """``starting`` re-check of a config-driven job's pinned version."""
    if row["config_version"] is None:
        return None
    return None if launch_fields(conn, row) else "config_invalid"
