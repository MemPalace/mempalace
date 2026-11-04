"""Workspace-local start jobs, fenced leases and conservative launch accounting."""

import hashlib
import json
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commonplace.extensions import ToolError
from mempalace.sqlite_crypto import connect, row_factory

from . import registry

QUEUE_LIMIT = 8
STARTED_HOLD_SECONDS = 3600
MODES = ("approval-required", "auto-accept-edits", "auto", "full-access")
PRESPAWN_CODES = frozenset(
    (
        "project_not_configured",
        "topic_not_allowed",
        "mode_not_allowed",
        "config_invalid",
        "binding_not_acked",
        "job_invalid",
        "t3_missing",
        "busy",
    )
)
THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}\Z")
SCHEMA = """
CREATE TABLE IF NOT EXISTS runner_jobs (
 id TEXT PRIMARY KEY,
 task_number INTEGER NOT NULL, board_slug TEXT NOT NULL,
 machine_id TEXT NOT NULL, project TEXT NOT NULL, topic TEXT NOT NULL,
 grant_version INTEGER NOT NULL, runtime_mode TEXT NOT NULL,
 requested_by TEXT NOT NULL, request_key TEXT, request_hash TEXT,
 state TEXT NOT NULL CHECK (state IN
 ('queued','leased','starting','started','failed','uncertain','cancelled')),
 attempt INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until TEXT, starting_at TEXT, started_at TEXT,
 thread_id TEXT, outcome_code TEXT,
 cancel_requested_at TEXT, cancelled_by TEXT, resolved_by TEXT, resolved_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS runner_jobs_one_active ON runner_jobs(task_number)
 WHERE state IN ('queued','leased','starting','uncertain');
CREATE UNIQUE INDEX IF NOT EXISTS runner_jobs_request
 ON runner_jobs(task_number,requested_by,request_key) WHERE request_key IS NOT NULL;
"""


class Conflict(Exception):
    """A requested job transition conflicts with its current state."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _after(now, seconds):
    return (datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class JobStore:
    def __init__(self, gateway, ws):
        self.gateway, self.workspace_id = gateway, ws
        directory = Path(gateway.workspace_dir(ws))
        directory.mkdir(parents=True, exist_ok=True)
        self.path = str(directory / "runner.sqlite3")
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        conn = connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = row_factory
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise


def open_store(gateway, ws):
    return JobStore(gateway, ws)


def job_object(row, *, tokens=False):
    """The launch contract contains identifiers and grant arguments only."""
    data = {
        key: row[key]
        for key in ("lease_until", "task_number", "board_slug", "project", "topic", "runtime_mode")
    }
    data.update(job_id=row["id"], title_marker="commonplace-job:" + row["id"])
    data["lease_token"] = row["lease_token"] if tokens else None
    return data


def requester_ref(principal):
    return "user:" + principal.user_id if principal.person else "conn:" + principal.connection_id


def _row(conn, job):
    row = conn.execute("SELECT * FROM runner_jobs WHERE id=?", (job,)).fetchone()
    if row is None:
        raise ToolError("no: start not found")
    return row


def _fenced(conn, machine_id, lease_token):
    row = conn.execute(
        "SELECT * FROM runner_jobs WHERE machine_id=? AND lease_token=?",
        (machine_id, lease_token),
    ).fetchone()
    if row is None:
        if not conn.execute(
            "SELECT 1 FROM runner_jobs WHERE machine_id=? LIMIT 1", (machine_id,)
        ).fetchone():
            raise ToolError("no: start not found")
        raise Conflict("lease is no longer current")
    return row


def _update(conn, row, values, *, token=False):
    clause = "id=? AND machine_id=? AND state=?"
    args = [row["id"], row["machine_id"], row["state"]]
    if token:
        clause += " AND lease_token=?"
        args.append(row["lease_token"])
    changed = conn.execute(
        "UPDATE runner_jobs SET " + ",".join(key + "=?" for key in values) + " WHERE " + clause,
        [*values.values(), *args],
    ).rowcount
    if changed != 1:
        raise Conflict("start changed")
    return dict(_row(conn, row["id"]))


def sweep(store, db, ws):
    """Take the control snapshot before the workspace transaction (lock order)."""
    machines = {m["id"]: m for m in registry.machines(db, ws)}
    now = _now()
    with store.transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM runner_jobs WHERE state IN ('queued','leased','starting')"
        ).fetchall()
        for row in rows:
            machine = machines.get(row["machine_id"])
            disabled = machine is None or machine["revoked_at"] or not machine["enabled"]
            unbound = not disabled and machine["binding_state"] != "owner_attested"
            values = None
            if row["state"] in ("queued", "leased") and (disabled or unbound):
                values = {
                    "state": "cancelled",
                    "outcome_code": ("machine_revoked" if disabled else "binding_not_attested"),
                }
            elif row["state"] == "starting" and (
                disabled or _after(row["starting_at"], 120) <= now
            ):
                values = {"state": "uncertain"}
            elif row["state"] == "leased" and row["lease_until"] <= now:
                values = {"state": "queued", "lease_token": None, "lease_until": None}
            if values:
                _update(conn, row, {**values, "updated_at": now}, token=bool(row["lease_token"]))


def _sweep(store):
    sweep(store, store.gateway.db, store.workspace_id)


def _hourly(conn, machine, now):
    return (
        conn.execute(
            "SELECT COUNT(*) FROM runner_jobs WHERE machine_id=? AND starting_at>?",
            (machine["id"], _after(now, -3600)),
        ).fetchone()[0]
        >= machine["max_starts_per_hour"]
    )


def create(
    store,
    *,
    task_number,
    board_slug,
    machine,
    project,
    topic,
    requested_by,
    runtime_mode="approval-required",
    request_key=None,
    person=False,
):
    """Return (HTTP status, stored row); machine is the resolved registry row.

    requested_by is user:<id> or conn:<connection_id>, never a connection name.
    """
    _sweep(store)
    now = _now()
    arguments = dict(
        machine_id=machine["id"], project=project, topic=topic, runtime_mode=runtime_mode
    )
    digest = hashlib.sha256(
        json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with store.transaction() as conn:
        if request_key is not None:
            previous = conn.execute(
                "SELECT * FROM runner_jobs WHERE task_number=? AND requested_by=? AND request_key=?",
                (task_number, requested_by, request_key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise Conflict("request_key reused with different arguments")
                return 200, dict(previous)
        if conn.execute(
            "SELECT 1 FROM runner_jobs WHERE task_number=? AND state IN "
            "('queued','leased','starting','uncertain')",
            (task_number,),
        ).fetchone():
            raise Conflict("card already has an active start")
        if (
            not person
            and conn.execute(
                "SELECT 1 FROM runner_jobs WHERE task_number=? AND started_at>?",
                (task_number, _after(now, -86400)),
            ).fetchone()
        ):
            raise Conflict("card was started recently")
        queued = conn.execute(
            "SELECT COUNT(*) FROM runner_jobs WHERE machine_id=? AND state='queued'",
            (machine["id"],),
        ).fetchone()[0]
        if queued >= QUEUE_LIMIT or _hourly(conn, machine, now):
            raise Conflict("busy")
        job = "job_" + secrets.token_hex(8)
        conn.execute(
            "INSERT INTO runner_jobs(id,task_number,board_slug,machine_id,project,topic,"
            "grant_version,runtime_mode,requested_by,request_key,request_hash,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)",
            (
                job,
                task_number,
                board_slug,
                machine["id"],
                project,
                topic,
                machine["grant_version"],
                runtime_mode,
                requested_by,
                request_key,
                digest,
                now,
                now,
            ),
        )
        return 201, dict(_row(conn, job))


def lease(store, machine):
    _sweep(store)
    if machine["binding_state"] != "owner_attested":
        return None
    now = _now()
    with store.transaction() as conn:
        in_flight = conn.execute(
            "SELECT project FROM runner_jobs WHERE machine_id=? AND "
            "(state IN ('leased','starting','uncertain') OR (state='started' AND started_at>?))",
            (machine["id"], _after(now, -STARTED_HOLD_SECONDS)),
        ).fetchall()
        if len(in_flight) >= machine["max_in_flight"]:
            return None
        projects = {r["project"] for r in in_flight}
        row = next(
            (
                r
                for r in conn.execute(
                    "SELECT * FROM runner_jobs WHERE machine_id=? AND state='queued' ORDER BY created_at,rowid",
                    (machine["id"],),
                )
                if r["project"] not in projects
            ),
            None,
        )
        if row is None:
            return None
        return _update(
            conn,
            row,
            dict(
                state="leased",
                lease_token=secrets.token_urlsafe(32),
                lease_until=_after(now, 60),
                attempt=row["attempt"] + 1,
                updated_at=now,
            ),
        )


def renew(store, machine, lease_token):
    _sweep(store)
    now = _now()
    with store.transaction() as conn:
        row = _fenced(conn, machine["id"], lease_token)
        if row["state"] != "leased":
            raise Conflict("start is not leased")
        return _update(conn, row, dict(lease_until=_after(now, 60), updated_at=now), token=True)


def starting(store, machine, lease_token, *, validate):
    """validate(row) returns a refusal code or None; called before the guarded write."""
    _sweep(store)
    now = _now()
    refusal = None
    with store.transaction() as conn:
        row = _fenced(conn, machine["id"], lease_token)
        if row["state"] == "starting" and row["thread_id"] is None and row["outcome_code"] is None:
            return dict(row)
        if row["state"] != "leased":
            raise Conflict("start is not leased")
        refusal = validate(row)
        if refusal is None and _hourly(conn, machine, now):
            refusal = "busy"
        values = (
            dict(state="cancelled", outcome_code=refusal)
            if refusal
            else dict(state="starting", starting_at=now)
        )
        updated = _update(conn, row, {**values, "updated_at": now}, token=True)
    # A refusal must commit its cancellation before the 409 reaches the caller.
    if refusal:
        raise Conflict(refusal)
    return updated


def result(store, machine, lease_token, outcome, thread_id=None, code=None):
    _sweep(store)
    if outcome not in ("started", "uncertain", "failed"):
        raise ValueError("invalid outcome")
    if outcome == "started":
        if (
            not isinstance(thread_id, str)
            or THREAD_ID.fullmatch(thread_id) is None
            or code is not None
        ):
            raise ValueError("invalid started result")
    elif thread_id is not None:
        raise ValueError("thread_id is only valid for started")
    if outcome == "failed" and code not in PRESPAWN_CODES:
        raise ValueError("invalid pre-spawn code")
    if code is not None and (
        not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
    ):
        raise ValueError("invalid outcome code")
    now = _now()
    with store.transaction() as conn:
        row = _fenced(conn, machine["id"], lease_token)
        if row["state"] == "started" and outcome == "started" and row["thread_id"] == thread_id:
            return dict(row)
        if row["state"] == "uncertain" and outcome == "uncertain":
            return dict(row)
        allowed = (
            (row["state"] == "leased" and outcome == "failed")
            or (row["state"] == "starting" and outcome in ("started", "uncertain"))
            or (row["state"] == "uncertain" and outcome == "started")
        )
        if not allowed:
            raise Conflict("result conflicts with start state")
        values = dict(state=outcome, thread_id=thread_id, outcome_code=code, updated_at=now)
        if outcome == "started":
            values["started_at"] = now
        return _update(conn, row, values, token=True)


def cancel(store, job, principal):
    _sweep(store)
    now = _now()
    with store.transaction() as conn:
        row = _row(conn, job)
        if not (principal.person and principal.role == "owner") and row[
            "requested_by"
        ] != requester_ref(principal):
            raise ToolError("no: only the requester or a workspace owner may cancel")
        if row["state"] == "uncertain":
            raise Conflict("start outcome is uncertain; a workspace owner must resolve it")
        if row["state"] in ("started", "failed", "cancelled"):
            raise Conflict("start is already finished")
        values = dict(
            cancel_requested_at=row["cancel_requested_at"] or now,
            cancelled_by=requester_ref(principal),
            updated_at=now,
        )
        if row["state"] in ("queued", "leased"):
            values["state"] = "cancelled"
        return _update(conn, row, values)


def resolve(store, job, principal, outcome, thread_id=None):
    """Caller holds the control transaction and writes its audit before this update."""
    if not (principal.person and principal.role == "owner"):
        raise ToolError("no: a signed-in workspace owner is required")
    if outcome not in ("started", "failed"):
        raise ValueError("invalid outcome")
    if outcome == "started" and (
        not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id)
    ):
        raise ValueError("invalid thread_id")
    if outcome == "failed" and thread_id is not None:
        raise ValueError("thread_id is only valid for started")
    _sweep(store)
    now = _now()
    with store.transaction() as conn:
        row = _row(conn, job)
        if row["state"] != "uncertain":
            raise Conflict("start is not uncertain")
        values = dict(
            state=outcome,
            thread_id=thread_id,
            resolved_by=requester_ref(principal),
            resolved_at=now,
            updated_at=now,
        )
        if outcome == "started":
            values["started_at"] = now
        return _update(conn, row, values)


def status(store, task, principal):
    _sweep(store)
    machines = {m["id"]: m for m in registry.machines(store.gateway.db, store.workspace_id)}
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM runner_jobs WHERE task_number=? ORDER BY created_at,rowid", (task,)
        ).fetchall()
    return [
        dict(
            job=row["id"],
            machine_label=machines[row["machine_id"]]["label"]
            if row["machine_id"] in machines
            else None,
            **{key: row[key] for key in ("state", "thread_id", "outcome_code", "updated_at")},
        )
        for row in rows
        if (principal.person and principal.role == "owner")
        or row["requested_by"] == requester_ref(principal)
    ]
