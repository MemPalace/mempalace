"""Durable orchestrator: lease, journal, evidence, gates, checkpoint and resume.

Everything lives in the workspace's ``runner.sqlite3`` next to the config versions
(``commonplace.orch.store``) and the runner's jobs, so one ``BEGIN IMMEDIATE`` covers a
lease check, a journal write and a job insert.

Lease: one per ``(board, role)``. ``resume`` claims it when none exists, after a release,
after expiry, or by owner force, and returns a fresh token exactly once. Only
``sha256(token + incarnation)`` is stored, so a restore (new incarnation) kills every
old token. Each claim increments the epoch. The lease lasts ``TTL_SECONDS`` and is
renewed by every fenced write and by ``heartbeat``; expiry only fences, it never moves
cards or jobs. A lost token means waiting for expiry or an owner force.

Authority: claiming a lease and writing the journal need a principal that passes the
runner's ``requester_ok`` for the board's configured ``[runner].machine``, plus the
current epoch and token for writes. Forcing a lease and voiding a step need
``store.orch_owner``. Reading a checkpoint needs read access to the board's wing.

Journal (holder only): steps keyed by ``(board, role, step_key)`` with states
``intent -> started -> waiting -> done | failed | in_doubt`` and
``in_doubt -> done | failed | needs_person`` (``needs_person -> done | failed`` only with
hub-verified evidence). Unvoided runner-start steps follow their job
(``JOB_STEP_STATE``). At each new epoch, steps of an external kind still open from an older epoch become ``in_doubt``:
the hub never re-issues an external effect.

Evidence: ``runner_job`` (the hub checks that the job belongs to this step, card and
board), ``card_state`` (the hub reads the card; ``done`` needs the card in ``done``) and
``holder_attested`` (stored, never acted on).

Gates: one attempt per ``(board, repo, PR, head)`` across roles, whatever its verdict
(``gate_pass``, ``gate_fail``, ``gate_error``, ``gate_budget``), until the owner voids
the gate's step. The verdict is attested by the lease holder. Runner-start void
permanently retires the key while preserving its history and linked job. Only
queued/leased jobs are cancelled atomically with void; launched jobs are untouched.
A new attempt needs a new key and ordinary admission.

Checkpoint: derived from the journal, the jobs and the board, never stored: open steps,
``in_doubt``, ``needs_person``, holds, ``next``, jobs, gates, misses, the read-only card
classes ``held`` / ``expired`` / ``not_held_by_caller`` and the unstarted todo cards.
``resume`` additionally heartbeats the caller's own held cards; it never claims a card.

Clocks: every public function takes ``now`` (``"YYYY-MM-DDTHH:MM:SSZ"``); ``None`` means
the current UTC time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from commonplace.boards.store import BoardStore
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import log_failure
from commonplace.runner import jobs, registry
from commonplace.runner.predicate import requester_ok

from . import store as orch_store

logger = logging.getLogger(__name__)

TTL_SECONDS = 1800
STEP_KEY = re.compile(r"[A-Za-z0-9._:-]{1,80}\Z")
ROLE = re.compile(r"[a-z0-9-]{1,32}\Z")
CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}\Z")
HEAD = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
MAX_TEXT = 2000
MAX_JOB_ID = 64  # runner job ids are ``job_`` + 16 hex digits
MAX_LIST = 200

STATES = ("intent", "started", "waiting", "done", "failed", "in_doubt", "needs_person")
OPEN_STATES = ("intent", "started", "waiting")
TERMINAL_STATES = ("done", "failed")
TRANSITIONS = {
    "intent": frozenset({"started", "waiting", "done", "failed", "in_doubt"}),
    "started": frozenset({"waiting", "done", "failed", "in_doubt"}),
    "waiting": frozenset({"done", "failed", "in_doubt"}),
    "in_doubt": frozenset({"done", "failed", "needs_person"}),
    "needs_person": frozenset({"done", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
}
# runner_start rows are written by fenced_start only; gate rows by gate_open only.
KINDS = ("runner_start", "gate", "merge", "thread", "handoff", "card", "hold", "next")
EXTERNAL_KINDS = frozenset({"gate", "merge", "thread", "handoff", "card"})
JOB_STEP_STATE = {
    "queued": "waiting",
    "leased": "waiting",
    "starting": "waiting",
    "started": "done",
    "uncertain": "in_doubt",
    "failed": "failed",
    "cancelled": "failed",
}
ACTIVE_JOB_STATES = ("queued", "leased", "starting", "uncertain")
EVIDENCE_TYPES = ("runner_job", "card_state", "holder_attested")
VERIFIED_EVIDENCE = frozenset({"runner_job", "card_state"})
GATE_STATES = ("gate_pass", "gate_fail", "gate_error", "gate_budget")

SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS orch_leases (
 board TEXT NOT NULL,
 role TEXT NOT NULL,
 epoch INTEGER NOT NULL CHECK (epoch >= 1),
 holder TEXT NOT NULL,
 holder_actor TEXT NOT NULL,
 token_hash TEXT,
 claimed_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 released_at TEXT,
 released_by TEXT,
 PRIMARY KEY (board, role)
)""",
    """CREATE TABLE IF NOT EXISTS orch_steps (
 id INTEGER PRIMARY KEY,
 board TEXT NOT NULL,
 role TEXT NOT NULL,
 step_key TEXT NOT NULL,
 kind TEXT NOT NULL CHECK (kind IN
 ('runner_start','gate','merge','thread','handoff','card','hold','next')),
 card INTEGER,
 request_hash TEXT,
 state TEXT NOT NULL CHECK (state IN
 ('intent','started','waiting','done','failed','in_doubt','needs_person')),
 code TEXT,
 detail TEXT,
 evidence TEXT,
 job_id TEXT,
 created_epoch INTEGER NOT NULL,
 epoch INTEGER NOT NULL,
 voided_at TEXT,
 voided_by TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE (board, role, step_key)
)""",
    """CREATE TRIGGER IF NOT EXISTS orch_steps_immutable
 BEFORE UPDATE OF board, role, step_key, kind, card, request_hash ON orch_steps
BEGIN SELECT RAISE(ABORT, 'orch_steps identity is immutable'); END""",
    """CREATE TABLE IF NOT EXISTS orch_gates (
 id INTEGER PRIMARY KEY,
 board TEXT NOT NULL,
 repo TEXT NOT NULL,
 pr INTEGER NOT NULL CHECK (pr >= 1),
 head TEXT NOT NULL,
 role TEXT NOT NULL,
 step_key TEXT NOT NULL,
 state TEXT NOT NULL CHECK (state IN
 ('open','gate_pass','gate_fail','gate_error','gate_budget')),
 attested_by TEXT,
 decided_at TEXT,
 created_at TEXT NOT NULL,
 voided_at TEXT,
 voided_by TEXT
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS orch_gates_one_attempt
 ON orch_gates(board, repo, pr, head) WHERE voided_at IS NULL""",
    """CREATE TABLE IF NOT EXISTS orch_journal (
 id INTEGER PRIMARY KEY,
 board TEXT NOT NULL,
 role TEXT NOT NULL,
 step_key TEXT,
 epoch INTEGER,
 event TEXT NOT NULL,
 state TEXT,
 code TEXT,
 actor TEXT NOT NULL,
 at TEXT NOT NULL
)""",
    """CREATE TRIGGER IF NOT EXISTS orch_journal_no_update BEFORE UPDATE ON orch_journal
BEGIN SELECT RAISE(ABORT, 'orch_journal is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS orch_journal_no_delete BEFORE DELETE ON orch_journal
BEGIN SELECT RAISE(ABORT, 'orch_journal is append-only'); END""",
)
SCHEMA = ";\n".join(SCHEMA_STATEMENTS) + ";\n"


class DurableError(ToolError):
    """A refusal. ``code`` is stable, ``status`` is the HTTP status for surfaces and
    ``details`` is JSON-safe (a stored step, the lease holder, ...). ``str()`` is
    ``"no: <message>"`` and never contains a token."""

    def __init__(self, code: str, status: int = 409, message: str = "", **details):
        self.code = code
        self.status = status
        self.details = details
        super().__init__(f"no: {message or code.replace('_', ' ')}")


# ------------------------------------------------------------------ small helpers


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _after(now: str, seconds: int) -> str:
    return (datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _clock(now):
    if now is None:
        return _now()
    if not isinstance(now, str):
        raise ValueError("now must be a UTC timestamp string")
    datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
    return now


def hash_token(token: str, incarnation: str) -> str:
    """What the lease row stores: ``sha256(token + incarnation)``, lowercase hex."""
    return hashlib.sha256((token + incarnation).encode("utf-8")).hexdigest()


def job_step_state(job_state: str) -> str:
    """Step state a runner job state maps to (``JOB_STEP_STATE``)."""
    return JOB_STEP_STATE[job_state]


def ensure_schema(conn) -> None:
    """Create the durable tables (idempotent). Safe inside an open transaction."""
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)


def _int(value, name, *, minimum=1):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise DurableError("invalid_" + name, 400, f"{name} must be an integer >= {minimum}")
    return value


def _text(value, name):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise DurableError("invalid_" + name, 400, f"{name} must be 1-{MAX_TEXT} characters")
    return value


def _check_step_key(step_key):
    if not isinstance(step_key, str) or not STEP_KEY.fullmatch(step_key):
        raise DurableError("invalid_step_key", 400, "step_key must match [A-Za-z0-9._:-]{1,80}")
    return step_key


def _check_role(role):
    if not isinstance(role, str) or not ROLE.fullmatch(role):
        raise DurableError("unknown_role", 404, "role not found")
    return role


def _code(code):
    if code is None:
        return None
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise DurableError("invalid_code", 400, "code must match [a-z][a-z0-9_]{0,63}")
    return code


def _actor_ref(principal) -> str:
    if principal.person:
        return "user:" + principal.user_id
    if principal.auth == "local":
        return "local:" + principal.user_id
    return "conn:" + principal.connection_id


def _log(conn, board, role, step_key, epoch, event, actor, now, state=None, code=None):
    conn.execute(
        "INSERT INTO orch_journal(board,role,step_key,epoch,event,state,code,actor,at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (board, role, step_key, epoch, event, state, code, actor, now),
    )


# ------------------------------------------------- connection-level helpers
# Callers hold a connection on runner.sqlite3, usually inside BEGIN IMMEDIATE.


def lease_in(conn, board: str, role: str) -> dict | None:
    """The lease row of ``(board, role)`` (without the token hash), or ``None``."""
    row = conn.execute(
        "SELECT * FROM orch_leases WHERE board=? AND role=?", (board, role)
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data.pop("token_hash")
    return data


def _lease_live(row, now) -> bool:
    return row["token_hash"] is not None and row["released_at"] is None and row["expires_at"] > now


def check_lease(
    conn,
    *,
    board: str,
    role: str,
    epoch,
    token,
    now: str,
    incarnation: str | None = None,
) -> dict:
    """Fencing check for a holder write: raise ``DurableError('fenced', 409)`` unless the
    lease of ``(board, role)`` exists, is not released, has this ``epoch``, matches
    ``token`` under the current incarnation and ``expires_at > now``. Returns the lease
    row (without the token hash). Call it inside the write transaction, again right
    before COMMIT when time may have passed, then ``renew_in``."""
    if incarnation is None:
        incarnation = orch_store.incarnation_in(conn)
    row = conn.execute(
        "SELECT * FROM orch_leases WHERE board=? AND role=?", (board, role)
    ).fetchone()
    if (
        row is None
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or not isinstance(token, str)
        or row["epoch"] != epoch
        or row["token_hash"] is None
        or row["released_at"] is not None
        or not hmac.compare_digest(row["token_hash"], hash_token(token, incarnation))
        or row["expires_at"] <= now
    ):
        raise DurableError(
            "fenced", 409, "lease is not current; call orch_resume", board=board, role=role
        )
    data = dict(row)
    data.pop("token_hash")
    return data


def renew_in(conn, *, board: str, role: str, epoch: int, now: str) -> str:
    """Extend the lease to ``now + TTL_SECONDS`` (fenced writes renew); returns
    ``expires_at``. Call after ``check_lease`` in the same transaction."""
    expires = _after(now, TTL_SECONDS)
    changed = conn.execute(
        "UPDATE orch_leases SET expires_at=? WHERE board=? AND role=? AND epoch=? "
        "AND token_hash IS NOT NULL AND released_at IS NULL",
        (expires, board, role, epoch),
    ).rowcount
    if changed != 1:
        raise DurableError("fenced", 409, "lease is not current; call orch_resume")
    return expires


def drop_leases_in(conn, *, now: str) -> int:
    """Release every lease (restore). Returns the number of leases dropped."""
    return conn.execute(
        "UPDATE orch_leases SET token_hash=NULL, released_at=?, released_by='restore' "
        "WHERE token_hash IS NOT NULL",
        (now,),
    ).rowcount


def step_in(conn, *, board: str, role: str, step_key: str) -> dict | None:
    """The stored step (public shape, see ``step_public``) or ``None``."""
    row = conn.execute(
        "SELECT * FROM orch_steps WHERE board=? AND role=? AND step_key=?",
        (board, role, step_key),
    ).fetchone()
    return step_public(row) if row is not None else None


def insert_step_in(
    conn,
    *,
    board: str,
    role: str,
    step_key: str,
    card: int,
    request_hash: str,
    epoch: int,
    actor: str,
    now: str,
    job_id: str | None = None,
) -> dict:
    """fenced_start step 5: insert a ``runner_start`` step (state ``waiting``) with its
    immutable card and request hash. ``UNIQUE(board, role, step_key)`` raises
    ``sqlite3.IntegrityError`` on a duplicate (callers look up first with ``step_in``)."""
    _check_step_key(step_key)
    conn.execute(
        "INSERT INTO orch_steps(board,role,step_key,kind,card,request_hash,state,job_id,"
        "created_epoch,epoch,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            board,
            role,
            step_key,
            "runner_start",
            card,
            request_hash,
            "waiting",
            job_id,
            epoch,
            epoch,
            now,
            now,
        ),
    )
    _log(conn, board, role, step_key, epoch, "start", actor, now, state="waiting")
    return step_in(conn, board=board, role=role, step_key=step_key)


def link_job_in(conn, *, board: str, role: str, step_key: str, job_id: str, now: str) -> dict:
    """Attach the job created by ``jobs.create_in`` to its (unlinked) step."""
    changed = conn.execute(
        "UPDATE orch_steps SET job_id=?, updated_at=? WHERE board=? AND role=? AND step_key=? "
        "AND kind='runner_start' AND job_id IS NULL",
        (job_id, now, board, role, step_key),
    ).rowcount
    if changed != 1:
        raise DurableError("step_exists", 409, "step already has a job")
    return step_in(conn, board=board, role=role, step_key=step_key)


def step_public(row) -> dict:
    data = dict(row)
    evidence = data.pop("evidence")
    data["evidence"] = json.loads(evidence) if evidence else None
    data["voided"] = data["voided_at"] is not None
    data.pop("id", None)
    return data


# ------------------------------------------------------------- context helpers


def _open(ctx):
    """Open the workspace's runner.sqlite3: runner jobs, config store (converts a
    restore marker) and the durable tables."""
    workspace = ctx.principal.workspace_id
    jobs.open_store(ctx.gateway, workspace)
    st = orch_store.open_store(ctx.gateway, workspace)
    with st.connection() as conn:
        ensure_schema(conn)
    return st


def _board(ctx, board):
    if not isinstance(board, str) or not board:
        raise DurableError("board_not_found", 404, "board not found")
    with BoardStore(ctx.workspace_dir()).connection() as conn:
        row = conn.execute("SELECT * FROM boards WHERE slug=?", (board,)).fetchone()
    if row is None or row["archived"] or not ctx.principal.policy.can_read_wing(row["wing"]):
        raise DurableError("board_not_found", 404, "board not found")
    return dict(row)


def _snapshot(ctx, st, board, role):
    """Pre-transaction control snapshot: live config with [runner], role, machine and
    the caller's requester authority (lock order: control reads before runner.sqlite3)."""
    principal = ctx.principal
    if getattr(principal, "runner", None) is not None:
        raise DurableError("not_allowed", 403, "requester not allowed")
    _check_role(role)
    with st.connection() as conn:
        version = orch_store.accepted_in(conn, board)
    if version is None:
        raise DurableError("not_configured", 409, "board has no orchestrator config")
    try:
        config = version.config()
    except ValueError:
        raise DurableError("config_invalid", 409, "live config does not parse") from None
    if not config.has_runner:
        raise DurableError("not_configured", 409, "board has no orchestrator config")
    if role not in config.roles:
        raise DurableError("unknown_role", 404, "role not found")
    machine = registry.get_machine(ctx.db, principal.workspace_id, config.runner.machine)
    if machine is None or machine["revoked_at"]:
        raise DurableError("not_allowed", 403, "requester not allowed")
    if not requester_ok(ctx.gateway, principal, machine):
        raise DurableError("not_allowed", 403, "requester not allowed")
    return version


def _same_version(conn, board, version):
    current = orch_store.accepted_in(conn, board)
    if current is None or (current.version, current.sha256) != (version.version, version.sha256):
        raise DurableError("config_changed", 409, "live config changed; retry")


def _holder(ctx, board, role):
    """Pre-transaction part shared by every holder write."""
    _check_role(role)
    _board(ctx, board)
    st = _open(ctx)
    version = _snapshot(ctx, st, board, role)
    return st, version


def _fence(conn, board, role, epoch, token, now):
    check_lease(conn, board=board, role=role, epoch=epoch, token=token, now=now)
    return renew_in(conn, board=board, role=role, epoch=epoch, now=now)


def _card_row(ctx, board, card):
    with BoardStore(ctx.workspace_dir()).connection() as conn:
        row = conn.execute(
            "SELECT t.id, t.status, t.version, b.slug FROM tasks t "
            "JOIN boards b ON b.id=t.board_id WHERE t.id=?",
            (card,),
        ).fetchone()
    if row is None or row["slug"] != board:
        raise DurableError("card_not_found", 404, "card not found on this board")
    return dict(row)


# -------------------------------------------------------------------- lease API


def resume(ctx: ToolContext, board: str, role: str, *, force: bool = False, now=None) -> dict:
    """``orch_resume``: claim the lease of ``(board, role)`` and return the checkpoint.

    Returns ``{"board", "role", "epoch", "token", "expires_at", "checkpoint"}``; the
    token is shown only here. Refusals: ``board_not_found`` (404), ``not_configured``,
    ``unknown_role`` (404), ``not_allowed`` (403), ``owner_only`` (403, force),
    ``restore_pending``, ``config_changed``, ``held`` (409, details: holder, epoch,
    expires_at)."""
    now = _clock(now)
    principal = ctx.principal
    board_row = _board(ctx, board)
    st = _open(ctx)
    version = _snapshot(ctx, st, board, role)
    if force:
        orch_store.require_owner(principal)
    actor = _actor_ref(principal)
    token = secrets.token_urlsafe(32)
    with st.transaction() as conn:
        orch_store.check_restore_in(conn)
        _same_version(conn, board, version)
        incarnation = orch_store.incarnation_in(conn)
        row = conn.execute(
            "SELECT * FROM orch_leases WHERE board=? AND role=?", (board, role)
        ).fetchone()
        if row is not None and _lease_live(row, now) and not force:
            raise DurableError(
                "held",
                409,
                "lease is held; wait for expiry or an owner force",
                holder=row["holder"],
                epoch=row["epoch"],
                expires_at=row["expires_at"],
            )
        epoch = (row["epoch"] if row is not None else 0) + 1
        expires = _after(now, TTL_SECONDS)
        conn.execute(
            "INSERT INTO orch_leases(board,role,epoch,holder,holder_actor,token_hash,claimed_at,"
            "expires_at,released_at,released_by) VALUES (?,?,?,?,?,?,?,?,NULL,NULL) "
            "ON CONFLICT(board, role) DO UPDATE SET epoch=excluded.epoch, holder=excluded.holder,"
            " holder_actor=excluded.holder_actor, token_hash=excluded.token_hash,"
            " claimed_at=excluded.claimed_at, expires_at=excluded.expires_at,"
            " released_at=NULL, released_by=NULL",
            (
                board,
                role,
                epoch,
                actor,
                principal.actor,
                hash_token(token, incarnation),
                now,
                expires,
            ),
        )
        _log(conn, board, role, None, epoch, "force" if force else "claim", actor, now)
        _mark_in_doubt(conn, board, role, epoch, actor, now)
        _sync_jobs(conn, board, role, epoch, actor, now)
    cards = _card_classes(ctx, board_row, principal, now, heartbeat=True)
    checkpoint = _checkpoint(ctx, st, board_row, role, now, cards)
    return {
        "board": board,
        "role": role,
        "epoch": epoch,
        "token": token,
        "expires_at": expires,
        "checkpoint": checkpoint,
    }


def heartbeat(ctx: ToolContext, board: str, role: str, epoch: int, token: str, *, now=None):
    """``orch_heartbeat``: renew the lease. Returns ``{"epoch", "expires_at"}``;
    ``fenced`` when the lease is not current."""
    now = _clock(now)
    st, _version = _holder(ctx, board, role)
    with st.transaction() as conn:
        expires = _fence(conn, board, role, epoch, token, now)
    return {"epoch": epoch, "expires_at": expires}


def release(ctx: ToolContext, board: str, role: str, epoch: int, token: str, *, now=None):
    """Give the lease up (the next ``resume`` claims at once). Returns ``{"epoch"}``."""
    now = _clock(now)
    st, _version = _holder(ctx, board, role)
    with st.transaction() as conn:
        check_lease(conn, board=board, role=role, epoch=epoch, token=token, now=now)
        conn.execute(
            "UPDATE orch_leases SET token_hash=NULL, released_at=?, released_by=? "
            "WHERE board=? AND role=? AND epoch=?",
            (now, _actor_ref(ctx.principal), board, role, epoch),
        )
        _log(conn, board, role, None, epoch, "release", _actor_ref(ctx.principal), now)
    return {"epoch": epoch, "released": True}


def force_release(ctx: ToolContext, board: str, role: str, *, now=None) -> dict:
    """Owner force: end the current lease so the next ``resume`` claims a new epoch.
    Returns ``{"epoch", "released"}`` (``released`` False when nothing was live)."""
    now = _clock(now)
    orch_store.require_owner(ctx.principal)
    _check_role(role)
    st = _open(ctx)
    actor = _actor_ref(ctx.principal)
    with st.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM orch_leases WHERE board=? AND role=?", (board, role)
        ).fetchone()
        if row is None or row["token_hash"] is None:
            return {"epoch": row["epoch"] if row else None, "released": False}
        conn.execute(
            "UPDATE orch_leases SET token_hash=NULL, released_at=?, released_by=? "
            "WHERE board=? AND role=?",
            (now, actor, board, role),
        )
        _log(conn, board, role, None, row["epoch"], "force_release", actor, now)
        return {"epoch": row["epoch"], "released": True}


# ------------------------------------------------------------------ journal API


def journal_intent(
    ctx: ToolContext,
    board: str,
    role: str,
    epoch: int,
    token: str,
    step_key: str,
    kind: str,
    *,
    card: int | None = None,
    detail: str | None = None,
    now=None,
) -> dict:
    """Write a new step before its effect (state ``intent``; a ``next`` note is stored
    ``done``). A retry with the same key, kind and card returns the stored step unchanged
    (never a redo); anything else under that key is ``step_exists`` (409, details: step).
    ``runner_start`` steps come from ``orch_card_start``; gates from ``gate_open``."""
    now = _clock(now)
    _check_step_key(step_key)
    if kind == "runner_start":
        raise DurableError("use_orch_card_start", 400, "runner starts go through orch_card_start")
    if kind == "gate":
        raise DurableError("use_gate_open", 400, "gates go through gate_open")
    if kind not in KINDS:
        raise DurableError("invalid_kind", 400, "unknown step kind")
    detail = _text(detail, "detail")
    if card is not None:
        _int(card, "card")
    elif kind == "card":
        raise DurableError("invalid_card", 400, "a card step needs a card")
    st, _version = _holder(ctx, board, role)
    if card is not None:
        _card_row(ctx, board, card)
    actor = _actor_ref(ctx.principal)
    with st.transaction() as conn:
        _fence(conn, board, role, epoch, token, now)
        existing = step_in(conn, board=board, role=role, step_key=step_key)
        if existing is not None:
            if existing["kind"] == kind and existing["card"] == card:
                return existing
            raise DurableError("step_exists", 409, "step key already used", step=existing)
        state = "done" if kind == "next" else "intent"
        conn.execute(
            "INSERT INTO orch_steps(board,role,step_key,kind,card,state,detail,created_epoch,"
            "epoch,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (board, role, step_key, kind, card, state, detail, epoch, epoch, now, now),
        )
        _log(conn, board, role, step_key, epoch, "intent", actor, now, state=state)
        return step_in(conn, board=board, role=role, step_key=step_key)


def _parse_evidence(evidence):
    if evidence is None:
        return None
    if not isinstance(evidence, dict) or evidence.get("type") not in EVIDENCE_TYPES:
        raise DurableError(
            "invalid_evidence", 400, "evidence type must be one of " + ", ".join(EVIDENCE_TYPES)
        )
    kind = evidence["type"]
    allowed = {
        "runner_job": {"type", "job"},
        "card_state": {"type"},
        "holder_attested": {"type", "note"},
    }
    if set(evidence) - allowed[kind]:
        raise DurableError("invalid_evidence", 400, "unknown evidence field")
    if kind == "runner_job" and (
        not isinstance(evidence.get("job"), str)
        or not evidence["job"]
        or len(evidence["job"]) > MAX_JOB_ID
    ):
        raise DurableError(
            "invalid_evidence", 400, f"runner_job evidence needs a job id of 1-{MAX_JOB_ID} chars"
        )
    if kind == "holder_attested":
        _text(evidence.get("note"), "note")
    return dict(evidence)


def _job_evidence(conn, step, board, role, step_key, job_id, state):
    """runner_job binding: the job is this step's job, for this card and board."""
    row = conn.execute("SELECT * FROM runner_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise DurableError("evidence_unbound", 409, "job not found")
    keys = row.keys()
    if (
        "step_key" not in keys
        or row["step_key"] != step_key
        or row["request_key"] != f"orch:{role}:{step_key}"
        or row["board_slug"] != board
        or step["card"] is None
        or row["task_number"] != step["card"]
        or (step["job_id"] is not None and step["job_id"] != job_id)
    ):
        raise DurableError("evidence_unbound", 409, "job does not belong to this step")
    if state == "done" and row["state"] != "started":
        raise DurableError("evidence_mismatch", 409, "job has not started", job_state=row["state"])
    if state == "failed" and row["state"] not in ("failed", "cancelled", "uncertain"):
        raise DurableError("evidence_mismatch", 409, "job has not failed", job_state=row["state"])
    return {"type": "runner_job", "job": job_id, "job_state": row["state"]}


def journal_record(
    ctx: ToolContext,
    board: str,
    role: str,
    epoch: int,
    token: str,
    step_key: str,
    state: str,
    *,
    evidence: dict | None = None,
    code: str | None = None,
    now=None,
) -> dict:
    """Move a step to ``state`` (``TRANSITIONS``), with optional evidence:

    - ``{"type": "runner_job", "job": <id>}``: the job's step key, request key, card and
      board must be this step's (``evidence_unbound``); ``done`` needs a started job.
    - ``{"type": "card_state"}``: the hub reads the step's card; ``done`` needs it in
      ``done`` (``card_not_done``).
    - ``{"type": "holder_attested", "note": <text>}``: stored, never acted on.

    ``done`` or ``failed`` of a ``runner_start`` step needs ``runner_job`` (``failed``: a
    job in ``failed``, ``cancelled`` or ``uncertain``); ``done`` of a ``card`` step
    ``card_state``; leaving ``needs_person`` needs ``runner_job`` or ``card_state``. Gate
    steps use ``gate_record``. Repeating the current state with the same evidence returns
    the stored step."""
    now = _clock(now)
    _check_step_key(step_key)
    if state not in STATES or state == "intent":
        raise DurableError("invalid_state", 400, "unknown target state")
    code = _code(code)
    evidence = _parse_evidence(evidence)
    st, _version = _holder(ctx, board, role)
    card_snapshot = None
    if evidence is not None and evidence["type"] == "card_state":
        with st.connection() as conn:
            pre = step_in(conn, board=board, role=role, step_key=step_key)
        if pre is None:
            raise DurableError("step_not_found", 404, "step not found")
        if pre["card"] is None:
            raise DurableError("evidence_unbound", 409, "step has no card")
        card_snapshot = _card_row(ctx, board, pre["card"])
    actor = _actor_ref(ctx.principal)
    with st.transaction() as conn:
        _fence(conn, board, role, epoch, token, now)
        step = step_in(conn, board=board, role=role, step_key=step_key)
        if step is None:
            raise DurableError("step_not_found", 404, "step not found")
        if step["voided"]:
            raise DurableError("step_voided", 409, "step was voided by the owner", step=step)
        if step["kind"] == "gate":
            raise DurableError("use_gate_record", 400, "gate verdicts go through gate_record")
        stored = None
        if evidence is not None:
            if evidence["type"] == "runner_job":
                stored = _job_evidence(conn, step, board, role, step_key, evidence["job"], state)
            elif evidence["type"] == "card_state":
                if state == "done" and card_snapshot["status"] != "done":
                    raise DurableError(
                        "card_not_done",
                        409,
                        "card is not done",
                        card_status=card_snapshot["status"],
                    )
                stored = {
                    "type": "card_state",
                    "card": card_snapshot["id"],
                    "status": card_snapshot["status"],
                    "version": card_snapshot["version"],
                }
            else:
                stored = {"type": "holder_attested", "note": evidence["note"], "by": actor}
        if step["state"] == state:
            if stored == step["evidence"] and code == step["code"]:
                return step
            raise DurableError("transition", 409, "step is already in that state", step=step)
        if state not in TRANSITIONS[step["state"]]:
            raise DurableError("transition", 409, "step cannot move to that state", step=step)
        verified = stored is not None and stored["type"] in VERIFIED_EVIDENCE
        if step["state"] == "needs_person" and not verified:
            raise DurableError(
                "evidence_required", 409, "needs_person ends only with hub-checked evidence"
            )
        if (
            state in ("done", "failed")
            and step["kind"] == "runner_start"
            and (stored or {}).get("type") != "runner_job"
        ):
            raise DurableError(
                "evidence_required",
                409,
                "a runner start is done or failed only with runner_job evidence",
            )
        if (
            state == "done"
            and step["kind"] == "card"
            and (stored or {}).get("type") != "card_state"
        ):
            raise DurableError(
                "evidence_required", 409, "a card step is done only with card_state evidence"
            )
        values = {"state": state, "code": code, "epoch": epoch, "updated_at": now}
        if stored is not None:
            values["evidence"] = json.dumps(stored, sort_keys=True)
            if stored["type"] == "runner_job" and step["job_id"] is None:
                values["job_id"] = stored["job"]
        conn.execute(
            "UPDATE orch_steps SET "
            + ",".join(k + "=?" for k in values)
            + " WHERE board=? AND role=? AND step_key=?",
            (*values.values(), board, role, step_key),
        )
        _log(conn, board, role, step_key, epoch, "record", actor, now, state=state, code=code)
        return step_in(conn, board=board, role=role, step_key=step_key)


# --------------------------------------------------------------------- gates API


def _verdict(verdict):
    state = verdict if verdict in GATE_STATES else "gate_" + str(verdict)
    if state not in GATE_STATES:
        raise DurableError("invalid_verdict", 400, "verdict must be pass, fail, error or budget")
    return state


def _gate_public(row):
    data = dict(row)
    data.pop("id", None)
    data["voided"] = data["voided_at"] is not None
    return data


def gate_open(
    ctx: ToolContext,
    board: str,
    role: str,
    epoch: int,
    token: str,
    step_key: str,
    *,
    repo: str,
    pr: int,
    head: str,
    card: int | None = None,
    now=None,
) -> dict:
    """Reserve the single gate attempt for ``(board, repo, pr, head)`` and write its step
    (``intent``). Returns ``{"step", "gate"}``. A retry of the same step returns it;
    another unvoided gate on that head (any role, any verdict) is ``gate_exists`` (409,
    details: gate)."""
    now = _clock(now)
    _check_step_key(step_key)
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise DurableError("invalid_repo", 400, "repo must be owner/name")
    _int(pr, "pr")
    if not isinstance(head, str) or not HEAD.fullmatch(head):
        raise DurableError("invalid_head", 400, "head must be a full lowercase commit sha")
    if card is not None:
        _int(card, "card")
    st, _version = _holder(ctx, board, role)
    if card is not None:
        _card_row(ctx, board, card)
    actor = _actor_ref(ctx.principal)
    with st.transaction() as conn:
        _fence(conn, board, role, epoch, token, now)
        existing = step_in(conn, board=board, role=role, step_key=step_key)
        gate = conn.execute(
            "SELECT * FROM orch_gates WHERE board=? AND repo=? AND pr=? AND head=? "
            "AND voided_at IS NULL",
            (board, repo, pr, head),
        ).fetchone()
        if existing is not None:
            if (
                existing["kind"] == "gate"
                and gate is not None
                and (gate["role"], gate["step_key"]) == (role, step_key)
            ):
                return {"step": existing, "gate": _gate_public(gate)}
            raise DurableError("step_exists", 409, "step key already used", step=existing)
        if gate is not None:
            raise DurableError(
                "gate_exists",
                409,
                "this head already had its gate; push a new head or ask the owner to void it",
                gate=_gate_public(gate),
            )
        conn.execute(
            "INSERT INTO orch_steps(board,role,step_key,kind,card,state,detail,created_epoch,"
            "epoch,created_at,updated_at) VALUES (?,?,?,'gate',?,'intent',?,?,?,?,?)",
            (board, role, step_key, card, f"{repo}#{pr}@{head}", epoch, epoch, now, now),
        )
        conn.execute(
            "INSERT INTO orch_gates(board,repo,pr,head,role,step_key,state,created_at) "
            "VALUES (?,?,?,?,?,?,'open',?)",
            (board, repo, pr, head, role, step_key, now),
        )
        _log(conn, board, role, step_key, epoch, "gate_open", actor, now, state="intent")
        gate = conn.execute(
            "SELECT * FROM orch_gates WHERE board=? AND role=? AND step_key=? AND voided_at IS NULL",
            (board, role, step_key),
        ).fetchone()
        return {
            "step": step_in(conn, board=board, role=role, step_key=step_key),
            "gate": _gate_public(gate),
        }


def gate_record(
    ctx: ToolContext,
    board: str,
    role: str,
    epoch: int,
    token: str,
    step_key: str,
    verdict: str,
    *,
    now=None,
) -> dict:
    """Record the attested verdict (``pass|fail|error|budget`` or ``gate_*``) of an open
    gate. ``gate_pass`` -> step ``done``; the others -> step ``failed`` with the gate state
    as code. Every verdict is final for that head (``gate_decided`` on a different one;
    the same one returns the stored result). Returns ``{"step", "gate"}``."""
    now = _clock(now)
    _check_step_key(step_key)
    state = _verdict(verdict)
    st, _version = _holder(ctx, board, role)
    actor = _actor_ref(ctx.principal)
    with st.transaction() as conn:
        _fence(conn, board, role, epoch, token, now)
        step = step_in(conn, board=board, role=role, step_key=step_key)
        if step is None or step["kind"] != "gate":
            raise DurableError("step_not_found", 404, "gate step not found")
        if step["voided"]:
            raise DurableError("step_voided", 409, "step was voided by the owner", step=step)
        gate = conn.execute(
            "SELECT * FROM orch_gates WHERE board=? AND role=? AND step_key=? AND voided_at IS NULL",
            (board, role, step_key),
        ).fetchone()
        if gate["state"] != "open":
            if gate["state"] == state:
                return {"step": step, "gate": _gate_public(gate)}
            raise DurableError(
                "gate_decided", 409, "gate verdict is final", gate=_gate_public(gate)
            )
        if step["state"] not in ("intent", "started", "waiting", "in_doubt"):
            raise DurableError("transition", 409, "gate step cannot take a verdict", step=step)
        conn.execute(
            "UPDATE orch_gates SET state=?, attested_by=?, decided_at=? WHERE id=?",
            (state, actor, now, gate["id"]),
        )
        evidence = {"type": "gate_verdict", "verdict": state, "attested_by": actor, "epoch": epoch}
        conn.execute(
            "UPDATE orch_steps SET state=?, code=?, evidence=?, epoch=?, updated_at=? "
            "WHERE board=? AND role=? AND step_key=?",
            (
                "done" if state == "gate_pass" else "failed",
                state,
                json.dumps(evidence, sort_keys=True),
                epoch,
                now,
                board,
                role,
                step_key,
            ),
        )
        _log(conn, board, role, step_key, epoch, "gate_record", actor, now, code=state)
        gate = conn.execute("SELECT * FROM orch_gates WHERE id=?", (gate["id"],)).fetchone()
        return {
            "step": step_in(conn, board=board, role=role, step_key=step_key),
            "gate": _gate_public(gate),
        }


def void_step(ctx: ToolContext, board: str, role: str, step_key: str, *, now=None) -> dict:
    """Owner/local void retires runner-start keys and cancels only queued/leased jobs.

    Gate void permits a new attempt under a new key. History and launched jobs
    remain; repeats preserve the first actor/time and write nothing.
    """
    now = _clock(now)
    orch_store.require_owner(ctx.principal)
    _check_role(role)
    _check_step_key(step_key)
    st = _open(ctx)
    actor = _actor_ref(ctx.principal)
    job_store = jobs.open_store(ctx.gateway, ctx.principal.workspace_id)
    with st.transaction() as conn:
        step = step_in(conn, board=board, role=role, step_key=step_key)
        if step is None:
            raise DurableError("step_not_found", 404, "step not found")
        if step["voided"]:
            return step
        if step["kind"] == "runner_start" and step["job_id"] is not None:
            job = conn.execute(
                "SELECT state FROM runner_jobs WHERE id=?", (step["job_id"],)
            ).fetchone()
            if job is not None and job["state"] in ("queued", "leased"):
                jobs.cancel(
                    job_store,
                    step["job_id"],
                    ctx.principal,
                    conn=conn,
                    now=now,
                    actor=actor,
                )
        conn.execute(
            "UPDATE orch_steps SET voided_at=?, voided_by=?, updated_at=? "
            "WHERE board=? AND role=? AND step_key=?",
            (now, actor, now, board, role, step_key),
        )
        conn.execute(
            "UPDATE orch_gates SET voided_at=?, voided_by=? "
            "WHERE board=? AND role=? AND step_key=? AND voided_at IS NULL",
            (now, actor, board, role, step_key),
        )
        _log(conn, board, role, step_key, None, "void", actor, now)
        return step_in(conn, board=board, role=role, step_key=step_key)


# ----------------------------------------------------------- resume internals


def _mark_in_doubt(conn, board, role, epoch, actor, now):
    """New epoch: external steps still open from an older epoch become ``in_doubt``."""
    marks = ",".join("?" * len(EXTERNAL_KINDS))
    rows = conn.execute(
        f"SELECT step_key FROM orch_steps WHERE board=? AND role=? AND voided_at IS NULL "
        f"AND state IN ('intent','started','waiting') AND kind IN ({marks}) AND epoch<?",
        (board, role, *sorted(EXTERNAL_KINDS), epoch),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE orch_steps SET state='in_doubt', epoch=?, updated_at=? "
            "WHERE board=? AND role=? AND step_key=?",
            (epoch, now, board, role, row["step_key"]),
        )
        _log(conn, board, role, row["step_key"], epoch, "in_doubt", actor, now, state="in_doubt")


def _sync_jobs(conn, board, role, epoch, actor, now):
    """Runner-start steps follow their job (``JOB_STEP_STATE``) where the move is legal."""
    rows = conn.execute(
        "SELECT s.step_key, s.state, s.card, j.state AS job_state, j.outcome_code, j.id AS job "
        "FROM orch_steps s JOIN runner_jobs j ON j.id=s.job_id WHERE s.board=? AND s.role=? "
        "AND s.kind='runner_start' AND s.voided_at IS NULL "
        "AND s.state NOT IN ('done','failed')",
        (board, role),
    ).fetchall()
    for row in rows:
        target = JOB_STEP_STATE[row["job_state"]]
        if target == row["state"] or target not in TRANSITIONS[row["state"]]:
            continue
        evidence = {"type": "runner_job", "job": row["job"], "job_state": row["job_state"]}
        conn.execute(
            "UPDATE orch_steps SET state=?, code=?, evidence=?, epoch=?, updated_at=? "
            "WHERE board=? AND role=? AND step_key=?",
            (
                target,
                row["outcome_code"] if target == "failed" else None,
                json.dumps(evidence, sort_keys=True),
                epoch,
                now,
                board,
                role,
                row["step_key"],
            ),
        )
        _log(conn, board, role, row["step_key"], epoch, "sync", actor, now, state=target)


def _card_classes(ctx, board_row, principal, now, *, heartbeat):
    """Read-only classes of the board's doing cards; heartbeat the caller's held ones."""
    actor = principal.actor
    with BoardStore(ctx.workspace_dir()).connection() as conn:
        rows = conn.execute(
            "SELECT id, title, assignee, claim_until, claim_expired_at, version FROM tasks "
            "WHERE board_id=? AND status='doing' ORDER BY id LIMIT ?",
            (board_row["id"], MAX_LIST * 3),
        ).fetchall()
    classes = {"held": [], "expired": [], "not_held_by_caller": []}
    for row in rows:
        card = {
            "card": row["id"],
            "title": row["title"],
            "assignee": row["assignee"],
            "claim_until": row["claim_until"],
        }
        if row["assignee"] != actor:
            classes["not_held_by_caller"].append(card)
        elif row["claim_expired_at"] is None and (
            row["claim_until"] is None or row["claim_until"] > now
        ):
            classes["held"].append(card)
        else:
            classes["expired"].append(card)
    if heartbeat and classes["held"]:
        from commonplace.boards.service import BoardService

        service = BoardService(ToolContext(principal=principal, gateway=ctx.gateway))
        for card in classes["held"]:
            try:
                renewed = service.task_heartbeat(card["card"])
                card["heartbeat"] = "renewed"
                card["claim_until"] = renewed.get("claim_until", card["claim_until"])
            except ToolError as exc:
                card["heartbeat"] = str(exc)
    return {key: value[:MAX_LIST] for key, value in classes.items()}


FIRING_EVENT = (
    "((to_status='todo' AND (from_status IS NULL OR from_status!='todo')) "
    "OR (kind='edit' AND choice='approve plan' AND to_status='todo'))"
)


def unstarted_todo(ctx, st, board_row) -> list[dict]:
    """Cards in ``todo`` with no start for their current todo occurrence: no job in
    ``queued|leased|starting|uncertain`` and no ``started`` job whose ``started_at`` is
    strictly after the card's latest firing event (todo entry or plan approval).
    ``cancelled`` and ``failed`` jobs never hide a card.

    Both stores stamp whole seconds, so a start and a firing event in the same second
    cannot be ordered; such a tie stays listed (a possible false positive, never a
    hidden unstarted card)."""
    with BoardStore(ctx.workspace_dir()).connection() as conn:
        cards = conn.execute(
            "SELECT t.id, t.title, (SELECT e.created_at FROM task_events e WHERE e.task_id=t.id "
            f"AND {FIRING_EVENT} ORDER BY e.id DESC LIMIT 1) AS boundary "
            "FROM tasks t WHERE t.board_id=? AND t.status='todo' ORDER BY t.id",
            (board_row["id"],),
        ).fetchall()
    if not cards:
        return []
    by_card: dict = {}
    with st.connection() as conn:
        numbers = [row["id"] for row in cards]
        for start in range(0, len(numbers), 500):
            chunk = numbers[start : start + 500]
            for job in conn.execute(
                "SELECT task_number, state, started_at FROM runner_jobs WHERE task_number IN ("
                + ",".join("?" * len(chunk))
                + ")",
                chunk,
            ):
                by_card.setdefault(job["task_number"], []).append(job)
    listed = []
    for row in cards:
        hidden = False
        for job in by_card.get(row["id"], ()):
            if job["state"] in ACTIVE_JOB_STATES:
                hidden = True
            elif job["state"] == "started" and (
                row["boundary"] is None or (job["started_at"] or "") > row["boundary"]
            ):
                hidden = True
        if not hidden:
            listed.append({"card": row["id"], "title": row["title"], "todo_since": row["boundary"]})
    return listed


def _misses(conn, board):
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orch_misses'"
    ).fetchone():
        return []
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM orch_misses WHERE board=? ORDER BY rowid DESC LIMIT ?",
            (board, MAX_LIST),
        )
    ]


def _derived(step, job_state):
    """Checkpoint view of a runner-start step: its job's mapped state when legal."""
    if step["kind"] != "runner_start" or job_state is None or step["voided"]:
        return step
    target = JOB_STEP_STATE[job_state]
    if target != step["state"] and target in TRANSITIONS[step["state"]]:
        return {**step, "state": target}
    return step


def _checkpoint(ctx, st, board_row, role, now, cards):
    board = board_row["slug"]
    with st.connection() as conn:
        lease = conn.execute(
            "SELECT * FROM orch_leases WHERE board=? AND role=?", (board, role)
        ).fetchone()
        version = orch_store.accepted_in(conn, board)
        restore = orch_store.restore_state_in(conn)
        steps = conn.execute(
            "SELECT s.*, j.state AS job_state FROM orch_steps s "
            "LEFT JOIN runner_jobs j ON j.id=s.job_id WHERE s.board=? AND s.role=? "
            "ORDER BY s.id",
            (board, role),
        ).fetchall()
        job_rows = conn.execute(
            "SELECT j.id, j.task_number, j.state, j.outcome_code, j.thread_id, j.started_at, "
            "j.updated_at, s.step_key FROM runner_jobs j JOIN orch_steps s ON s.job_id=j.id "
            "WHERE s.board=? AND s.role=? ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?",
            (board, role, MAX_LIST),
        ).fetchall()
        gates = conn.execute(
            "SELECT * FROM orch_gates WHERE board=? AND voided_at IS NULL ORDER BY id DESC LIMIT ?",
            (board, MAX_LIST),
        ).fetchall()
        misses = _misses(conn, board)
    view = {"open": [], "in_doubt": [], "needs_person": [], "holds": [], "finished": []}
    next_note = None
    for row in steps:
        job_state = row["job_state"]
        data = dict(row)
        data.pop("job_state")
        step = _derived(step_public(data), job_state)
        if step["voided"]:
            continue
        if step["kind"] == "next":
            next_note = step["detail"]
            continue
        if step["state"] in OPEN_STATES:
            view["holds" if step["kind"] == "hold" else "open"].append(step)
        elif step["state"] in ("in_doubt", "needs_person"):
            view[step["state"]].append(step)
        else:
            view["finished"].append(step)
    for key in view:
        view[key] = view[key][-MAX_LIST:]
    view["finished"].reverse()
    from commonplace.projects import orchestration, split

    inbox_note = {}
    try:
        if orchestration.enabled(ctx.db, ctx.principal.workspace_id):
            note = split.context(ctx.gateway, ctx.principal, board)
            if note:
                inbox_note = {"inbox": note}
    except Exception as exc:
        log_failure(logger, "checkpoint inbox unavailable", exc)
    return {
        **inbox_note,
        "board": board,
        "role": role,
        "lease": None
        if lease is None
        else {
            "epoch": lease["epoch"],
            "holder": lease["holder"],
            "expires_at": lease["expires_at"],
            "live": _lease_live(lease, now),
        },
        "config": None
        if version is None
        else {"version": version.version, "sha256": version.sha256},
        "restore_pending": bool(restore.flagged),
        **view,
        "next": next_note,
        "jobs": [dict(row) for row in job_rows],
        "gates": [_gate_public(row) for row in gates],
        "misses": misses,
        "cards": cards,
        "unstarted": unstarted_todo(ctx, st, board_row)[:MAX_LIST],
    }


def checkpoint(ctx: ToolContext, board: str, role: str, *, now=None) -> dict:
    """``orch_checkpoint``: the read-only checkpoint (no claim, no heartbeat, no writes).
    Needs read access to the board's wing."""
    now = _clock(now)
    _check_role(role)
    if getattr(ctx.principal, "runner", None) is not None:
        raise DurableError("not_allowed", 403, "requester not allowed")
    board_row = _board(ctx, board)
    st = _open(ctx)
    cards = _card_classes(ctx, board_row, ctx.principal, now, heartbeat=False)
    return _checkpoint(ctx, st, board_row, role, now, cards)
