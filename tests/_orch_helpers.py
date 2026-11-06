"""Seed an accepted orchestrator config with raw SQL, for pin-RED acceptance tests.

Nothing here imports ``commonplace.orch``: the same seed must run against a tree
that has no orchestrator package, so a test that drives only existing public
APIs can show what that tree gets wrong. Helpers never use ``assert``; a helper
failure raises ``RuntimeError`` so it cannot pass for a test-body assertion.

``SEED_DDL`` is the subset of ``commonplace/orch/schema.sql`` the seed writes.
A head-only test checks that both scripts create identical objects.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from mempalace.sqlite_crypto import connect

SEED_TABLES = ("orch_configs", "orch_cursor", "orch_cursor_gen")

SEED_DDL = """
CREATE TABLE IF NOT EXISTS orch_configs (
 board TEXT NOT NULL,
 version INTEGER NOT NULL CHECK (version >= 1),
 parent_version INTEGER,
 channel TEXT NOT NULL,
 status TEXT NOT NULL CHECK (status IN ('proposed','accepted','rejected','superseded')),
 source_ref TEXT,
 sha256 TEXT NOT NULL,
 body TEXT NOT NULL,
 proposed_by TEXT NOT NULL,
 proposer_trust TEXT NOT NULL,
 decided_by TEXT,
 decided_at TEXT,
 created_at TEXT NOT NULL,
 PRIMARY KEY (board, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS orch_configs_one_accepted
 ON orch_configs(board, channel) WHERE status='accepted';
CREATE TABLE IF NOT EXISTS orch_cursor (
 board TEXT PRIMARY KEY,
 cursor INTEGER,
 gen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orch_cursor_gen (
 board TEXT PRIMARY KEY,
 last_gen INTEGER NOT NULL
);
"""

PROMPT = "Implement the card's plan unit; run fast checks. Marker: orch-seed-prompt-5c1d."
STEPS = ("read plan", "write RED test", "implement", "fast checks", "report")
PROVIDER = "example-provider"
MODEL = "example-model"


def _quote(value):
    if not isinstance(value, str) or any(ch in value for ch in '"\\\n'):
        raise RuntimeError("seed values must be simple strings")
    return '"' + value + '"'


def config_toml(
    *,
    board,
    machine,
    project="demo",
    topic="worker",
    max_agents=3,
    prompt=PROMPT,
    steps=STEPS,
    runtime_mode="approval-required",
    model_role="worker",
    planner_connections=None,
):
    """Return a live config with a ``[runner]`` table, shaped like the format example."""
    if "'''" in prompt:
        raise RuntimeError("prompt must not contain a TOML literal-string delimiter")
    lines = [
        'schema = "commonplace.orchestrator/1"',
        f"project = {_quote(board)}",
        "[limits]",
        f"max_agents = {int(max_agents)}",
        "[roles.main]",
        'kind = "orchestrator"',
        'harness = "t3"',
        'topic = "orchestrate"',
        'model_role = "main"',
        'workflow = "card-pipeline"',
        "prompt = '''Coordinate this project.'''",
    ]
    pipeline = ["builder"]
    if planner_connections is not None:
        names = ", ".join(_quote(name) for name in planner_connections)
        lines += [
            "[roles.planner]",
            'kind = "planner"',
            'parent = "main"',
            'topic = "plan"',
            f"connections = [{names}]",
            "prompt = '''{{include:design_before_code}}'''",
        ]
        pipeline.insert(0, "planner")
    lines += [
        "[roles.builder]",
        'kind = "builder"',
        'parent = "main"',
        'harness = "t3"',
        f"topic = {_quote(topic)}",
        f"model_role = {_quote(model_role)}",
        'workflow = "build-unit"',
        f"prompt = '''{prompt}'''",
        "[workflows.card-pipeline]",
        "steps = [" + ", ".join(_quote(name) for name in pipeline) + "]",
        "[workflows.build-unit]",
        "steps = [" + ", ".join(_quote(step) for step in steps) + "]",
        "[runner]",
        'role = "builder"',
        f"machine = {_quote(machine)}",
        f"project = {_quote(project)}",
        f"runtime_mode = {_quote(runtime_mode)}",
        "[[triggers]]",
        'on = "card.todo"',
    ]
    return "\n".join(lines) + "\n"


def seed_config(
    workspace_dir,
    board,
    body,
    *,
    version=1,
    channel="live",
    proposed_by="user:seed",
    proposer_trust="trusted",
    arm=True,
):
    """Write ``body`` as the accepted ``channel`` config of ``board`` into runner.sqlite3.

    The previous accepted row of that channel becomes ``superseded``. With ``arm``,
    the board's trigger cursor is set to the current ``MAX(task_events.id)`` with a
    new generation, as a configuring accept followed by arming leaves it, so only
    later board events fire. Returns the stored row values.
    """
    directory = Path(workspace_dir)
    if not directory.is_dir():
        raise RuntimeError("workspace directory does not exist")
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    cursor = None
    if arm:
        cursor = 0
        boards = directory / "boards.sqlite3"
        if boards.exists():
            conn = connect(str(boards), timeout=10)
            try:
                cursor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
            finally:
                conn.close()
    conn = connect(str(directory / "runner.sqlite3"), timeout=10, isolation_level=None)
    try:
        conn.executescript(SEED_DDL)
        conn.execute("BEGIN IMMEDIATE")
        try:
            parent = conn.execute(
                "SELECT version FROM orch_configs WHERE board=? AND channel=? AND status='accepted'",
                (board, channel),
            ).fetchone()
            conn.execute(
                "UPDATE orch_configs SET status='superseded' "
                "WHERE board=? AND channel=? AND status='accepted'",
                (board, channel),
            )
            conn.execute(
                "INSERT INTO orch_configs(board,version,parent_version,channel,status,source_ref,"
                "sha256,body,proposed_by,proposer_trust,decided_by,decided_at,created_at) "
                "VALUES (?,?,?,?,'accepted',NULL,?,?,?,?,?,?,?)",
                (
                    board,
                    version,
                    parent[0] if parent else None,
                    channel,
                    sha,
                    body,
                    proposed_by,
                    proposer_trust,
                    proposed_by,
                    at,
                    at,
                ),
            )
            gen = None
            if arm:
                last = conn.execute(
                    "SELECT last_gen FROM orch_cursor_gen WHERE board=?", (board,)
                ).fetchone()
                gen = (last[0] if last else 0) + 1
                conn.execute(
                    "INSERT OR REPLACE INTO orch_cursor_gen(board,last_gen) VALUES (?,?)",
                    (board, gen),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO orch_cursor(board,cursor,gen) VALUES (?,?,?)",
                    (board, cursor, gen),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {
        "board": board,
        "version": version,
        "channel": channel,
        "sha256": sha,
        "cursor": cursor,
        "gen": gen,
    }
