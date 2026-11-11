"""Run ledger: one durable row per card start, with what the runner reported about it.

A run is one ``runner_jobs`` row. Its ledger row lives in the same ``runner.sqlite3`` and
is written in the job's own transaction: created with the job, then following the job's
state until the machine reports how the run itself ended. Tokens, cost and the log pointer
are only ever what the machine reported; a value nobody reported stays NULL.

This module is also imported by the runner client, so it uses the standard library only.
"""

import re

# What a machine may report as the end of a started run.
END_STATUSES = ("completed", "errored", "stopped")
MAX_COUNT = 10**15
LOG_REF_LIMIT = 512
REPORT_KEYS = frozenset(
    ("job_id", "status", "log_ref", "input_tokens", "output_tokens", "cost_micros", "currency")
)
USAGE_KEYS = ("log_ref", "input_tokens", "output_tokens", "cost_micros", "currency")
VIEW_KEYS = (
    "status",
    "started_at",
    "ended_at",
    "log_ref",
    "input_tokens",
    "output_tokens",
    "cost_micros",
    "currency",
    "reported_at",
    "model_provider",
    "model",
)
JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
CURRENCY = re.compile(r"[A-Z]{3}\Z")
TABLE = """
CREATE TABLE IF NOT EXISTS run_ledger (
 job_id TEXT PRIMARY KEY,
 task_number INTEGER NOT NULL, board_slug TEXT NOT NULL,
 project TEXT NOT NULL, topic TEXT NOT NULL,
 machine_id TEXT NOT NULL, agent TEXT, requested_by TEXT NOT NULL,
 model_provider TEXT, model TEXT,
 status TEXT NOT NULL,
 started_at TEXT, ended_at TEXT,
 log_ref TEXT,
 input_tokens INTEGER, output_tokens INTEGER,
 cost_micros INTEGER, currency TEXT,
 reported_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)
"""
INDEXES = (
    "CREATE INDEX IF NOT EXISTS run_ledger_task ON run_ledger(task_number)",
    "CREATE INDEX IF NOT EXISTS run_ledger_created ON run_ledger(created_at)",
)
_MISSING = (
    "SELECT j.* FROM runner_jobs j "
    "WHERE NOT EXISTS (SELECT 1 FROM run_ledger l WHERE l.job_id=j.id)"
)


class ReportError(Exception):
    """A refused usage report, with its HTTP status."""

    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def ensure(conn):
    """Create the table and give every job that predates it a row. Autocommit connection."""
    conn.execute(TABLE)
    for statement in INDEXES:
        conn.execute(statement)
    if conn.execute(_MISSING + " LIMIT 1").fetchone() is None:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in conn.execute(_MISSING).fetchall():
            sync(conn, row)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _optional(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def sync(conn, job, *, agent=None):
    """Write the ledger row of ``job`` (a ``runner_jobs`` row) on the caller's transaction.

    Inserts the row for a new job and otherwise follows the job's state. Once the machine
    reported the run's end, the reported status stays. ``agent`` is the worker connection
    id, known when the job is created; a row made later leaves it NULL.
    """
    # The job may be written through a connection that never opened a JobStore.
    conn.execute(TABLE)
    conn.execute(
        "INSERT INTO run_ledger(job_id,task_number,board_slug,project,topic,machine_id,agent,"
        "requested_by,model_provider,model,status,started_at,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "status=CASE WHEN run_ledger.ended_at IS NULL THEN excluded.status "
        "ELSE run_ledger.status END, "
        "started_at=excluded.started_at, model_provider=excluded.model_provider, "
        "model=excluded.model, updated_at=MAX(run_ledger.updated_at, excluded.updated_at)",
        (
            job["id"],
            job["task_number"],
            job["board_slug"],
            job["project"],
            job["topic"],
            job["machine_id"],
            agent,
            job["requested_by"],
            _optional(job, "model_provider"),
            _optional(job, "model"),
            job["state"],
            job["started_at"],
            job["created_at"],
            job["updated_at"],
        ),
    )


def _count(body, key):
    value = body[key]
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise ValueError("invalid " + key)
    return value


def clean(body):
    """Validate a usage report; return its fields. Raises ValueError with a safe message."""
    if not isinstance(body, dict) or body.keys() - REPORT_KEYS:
        raise ValueError("invalid request body")
    job_id = body.get("job_id")
    if not isinstance(job_id, str) or JOB_ID.match(job_id) is None:
        raise ValueError("invalid job_id")
    fields = {"job_id": job_id}
    if "status" in body:
        if body["status"] not in END_STATUSES:
            raise ValueError("invalid status")
        fields["status"] = body["status"]
    if "log_ref" in body:
        value = body["log_ref"]
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= LOG_REF_LIMIT
            or not value.isprintable()
        ):
            raise ValueError("invalid log_ref")
        fields["log_ref"] = value
    for key in ("input_tokens", "output_tokens"):
        if key in body:
            fields[key] = _count(body, key)
    if ("cost_micros" in body) != ("currency" in body):
        raise ValueError("cost_micros and currency go together")
    if "cost_micros" in body:
        fields["cost_micros"] = _count(body, "cost_micros")
        currency = body["currency"]
        if not isinstance(currency, str) or CURRENCY.match(currency) is None:
            raise ValueError("invalid currency")
        fields["currency"] = currency
    if len(fields) == 1:
        raise ValueError("nothing to report")
    return fields


def report(conn, machine_id, fields, now):
    """Record what a machine reports about one of its started runs; return the row.

    ``fields`` comes from ``clean``. Token and cost values are the run's totals so far and
    replace the stored ones; a field left out keeps its value. The end status is set once.
    """
    job = conn.execute(
        "SELECT * FROM runner_jobs WHERE id=? AND machine_id=?", (fields["job_id"], machine_id)
    ).fetchone()
    if job is None:
        raise ReportError(404, "start not found")
    if job["state"] != "started":
        raise ReportError(409, "run has not started")
    sync(conn, job)
    row = conn.execute("SELECT * FROM run_ledger WHERE job_id=?", (job["id"],)).fetchone()
    values = {key: fields[key] for key in USAGE_KEYS if key in fields}
    status = fields.get("status")
    if status is not None:
        if row["ended_at"] is None:
            values.update(status=status, ended_at=now)
        elif status != row["status"]:
            raise ReportError(409, "run already ended")
    values.update(reported_at=now, updated_at=now)
    conn.execute(
        "UPDATE run_ledger SET " + ",".join(key + "=?" for key in values) + " WHERE job_id=?",
        (*values.values(), job["id"]),
    )
    return conn.execute("SELECT * FROM run_ledger WHERE job_id=?", (job["id"],)).fetchone()


def view(row):
    """What the app shows of a ledger row."""
    return {key: row[key] for key in VIEW_KEYS}


def views(conn, job_ids):
    """Ledger views of the given jobs, by job id."""
    result = {}
    job_ids = list(job_ids)
    for start in range(0, len(job_ids), 500):
        chunk = job_ids[start : start + 500]
        for row in conn.execute(
            "SELECT * FROM run_ledger WHERE job_id IN (" + ",".join("?" for _ in chunk) + ")",
            chunk,
        ):
            result[row["job_id"]] = view(row)
    return result
