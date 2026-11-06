"""Plan gate on a board with an accepted live orchestrator config (R20-R20f).

R20, R20b, R20c, R20d and R20e are pin-RED: the config is seeded with raw SQL
(``tests/_orch_helpers.py``) and the tests drive only card and runner routes that exist
without the orchestrator package (``task_add``, ``task_update``, ``task_approve``,
``card_start``, the runner lease). Setup raises ``RuntimeError``; only an
``AssertionError`` in a test body is evidence. R20f is head-only (the ``orch`` CLI).
"""

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from tests._orch_helpers import config_toml
from tests.runner_harness import (
    _cli,
    card,
    cli_token,
    http,
    owners as owners,
    signup_hub as signup_hub,
)
from tests.test_orch_acceptance import BOARD, _configure, _jobs, _lease, _machine, _person_start

OTHER = "other"
PLANNER = "planner-demo"

# The design-before-code fixture: passes G0-G7.
PLAN = """## 0. Design before code
**a. Numbers**
- M: lease 600 s (`db.py:51`); T: 0 duplicate jobs after a lost ack.
**b. Smallest design.** One table and one fenced insert; nothing else yet.
**c. Data.** One SQLite file, owned by the workspace owner, backed up as today.
**d. Risk.** No secrets, no money, no new authority; the owner approves.
**e. Failure plan**
| Dependency | Timeout | Bounded retry | Idempotency key | User sees | RED |
|---|---|---|---|---|---|
| Hub | 15 s | ≤ 3 | step_key | retry message | R1 |
**f. RED tests**
| ID | Fault injected | Assertion |
|---|---|---|
| R1 | ack lost after commit | 1 job |
## 1. Format
## 2. Versions
## 3. Authority
## 4. Durable
## 5. Docs
## 6. Units
R-test files: tests/test_orch_acceptance.py: R1
## 7. Risks
"""

# The same plan without its (e) failure plan.
NO_FAILURE_PLAN = "\n".join(
    line
    for line in PLAN.splitlines()
    if not line.startswith(("**e.", "| Dependency", "|---|---|---|---|---|---|", "| Hub"))
)

NOTES = "Rough notes: look at the store first, then decide."


def _must(condition, message):
    if not condition:
        raise RuntimeError(message)


def _approve(hub, owner, task, **fields):
    return http(
        hub.port,
        "POST",
        f"/api/cards/{task['number']}/approve",
        cookie=owner["cookie"],
        body=fields,
    )


def _add(hub, token, **fields):
    return http(
        hub.port,
        "POST",
        f"/api/boards/{BOARD}/cards",
        token=token,
        body={"title": "Planned work", **fields},
    )


def _update(hub, task, *, token=None, cookie=None, **fields):
    status, body = http(hub.port, "GET", f"/api/cards/{task['number']}", token=token, cookie=cookie)
    _must(status == 200, f"card read failed: {status} {body}")
    return http(
        hub.port,
        "PATCH",
        f"/api/cards/{task['number']}",
        token=token,
        cookie=cookie,
        body={"version": body["version"], **fields},
    )


def test_r20_approving_a_plan_card_runs_plan_check(signup_hub, owners):
    """R20: approve omitted / ``true`` is refused for a plan without (e); reject works;
    a board without a config is unchanged."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    _configure(hub, owner, machine)
    task = card(hub, owner, board=BOARD, labels=["plan"], body=NO_FAILURE_PLAN)
    code, body = _approve(hub, owner, task)
    assert code == 403, (code, body)
    assert "plan-check" in str(body.get("error")), body
    code, body = _approve(hub, owner, task, approve=True)
    assert code == 403, (code, body)
    code, body = _approve(hub, owner, task, approve=False)
    assert code == 200 and body["status"] == "backlog", (code, body)
    passing = card(hub, owner, board=BOARD, labels=["plan"], body=PLAN)
    code, body = _approve(hub, owner, passing)
    assert code == 200 and "plan" not in body["labels"], (code, body)
    unconfigured = card(hub, owner, board=OTHER, labels=["plan"], body=NO_FAILURE_PLAN)
    code, body = _approve(hub, owner, unconfigured)
    assert code == 200 and "plan" not in body["labels"], (code, body)


def test_r20b_task_update_cannot_remove_the_plan_label(signup_hub, owners):
    """R20b: the holder (and the owner) cannot drop ``plan`` with task_update; approval can."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    _configure(hub, owner, machine)
    _cid, token = cli_token(owner, "holder-demo")
    code, task = _add(hub, token, body=PLAN, labels=["plan"], claim=True)
    _must(code == 201 and task["status"] == "doing", f"claimed add failed: {code} {task}")
    code, body = _update(hub, task, token=token, labels=["ready"])
    assert code == 403, (code, body)
    assert "task_approve" in str(body.get("error")), body
    code, body = _update(hub, task, cookie=owner["cookie"], labels=[])
    assert code == 403, (code, body)
    code, body = _update(hub, task, token=token, labels=["plan", "ready"])
    assert code == 200 and set(body["labels"]) == {"plan", "ready"}, (code, body)
    code, body = _approve(hub, owner, task)
    assert code == 200 and body["labels"] == ["ready"], (code, body)
    unconfigured = card(hub, owner, board=OTHER, labels=["plan"])
    code, body = _update(hub, unconfigured, cookie=owner["cookie"], labels=[])
    assert code == 200 and body["labels"] == [], (code, body)


def test_r20c_unlabelled_planner_card_needs_plan_check_before_done(signup_hub, owners):
    """R20c: a card created by the configured planner connection reaches ``done`` only
    with a body that passes plan-check; other connections are unchanged."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    _cid, token = cli_token(owner, PLANNER)
    _cid, other = cli_token(owner, "builder-demo")
    _configure(hub, owner, machine, planner_connections=[PLANNER])
    code, task = _add(hub, token, body=NOTES, claim=True)
    _must(code == 201 and task["status"] == "doing", f"claimed add failed: {code} {task}")
    code, body = _update(hub, task, token=token, status="done")
    assert code == 403, (code, body)
    assert "plan-check" in str(body.get("error")), body
    code, body = _update(hub, task, token=token, body=PLAN)
    assert code == 200, (code, body)
    code, body = _update(hub, task, token=token, status="done")
    assert code == 200 and body["status"] == "done", (code, body)
    code, task = _add(hub, other, body=NOTES, claim=True)
    _must(code == 201, f"claimed add failed: {code} {task}")
    code, body = _update(hub, task, token=other, status="done")
    assert code == 200 and body["status"] == "done", (code, body)


def test_r20d_plan_labelled_todo_card_starts_only_after_approval(signup_hub, owners):
    """R20d: a non-planner ``plan`` card in todo gets no job (a person start and its exact
    retry are refused ``plan_pending``); after ``task_approve`` exactly one job."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    _configure(hub, owner, machine)
    task = card(hub, owner, board=BOARD, labels=["plan"], body=PLAN)
    _lease(hub, machine)
    _lease(hub, machine)
    assert _jobs(hub, owner, task) == []
    for _attempt in range(2):
        code, body = _person_start(hub, owner, machine, task)
        assert code in (403, 409), (code, body)
        assert "plan" in str(body), body
    assert _jobs(hub, owner, task) == []
    code, body = _approve(hub, owner, task)
    _must(code == 200, f"approve failed: {code} {body}")
    _lease(hub, machine)
    _lease(hub, machine)
    assert len(_jobs(hub, owner, task)) == 1


def test_r20e_planner_card_into_todo_needs_a_passing_plan(signup_hub, owners):
    """R20e: the planner's todo card with a failing body is refused; a passing ``plan``
    card waits for approval, then starts exactly once."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    _cid, token = cli_token(owner, PLANNER)
    _configure(hub, owner, machine, planner_connections=[PLANNER])
    code, body = _add(hub, token, body=NO_FAILURE_PLAN, labels=["plan"])
    assert code == 403, (code, body)
    assert "plan-check" in str(body.get("error")), body
    code, task = _add(hub, token, body=PLAN, labels=["plan"])
    _must(code == 201 and task["status"] == "todo", f"planner add failed: {code} {task}")
    _lease(hub, machine)
    _lease(hub, machine)
    assert _jobs(hub, owner, task) == []
    code, body = _approve(hub, owner, task)
    assert code == 200 and "plan" not in body["labels"], (code, body)
    _lease(hub, machine)
    _lease(hub, machine)
    assert len(_jobs(hub, owner, task)) == 1


# ---------------------------------------------------------------- R20f (head only)


def _orch(owner, *args, by=True):
    extra = ["--by", owner["user_id"]] if by else []
    return _cli(owner, ["orch", *args, "--workspace", owner["workspace"], *extra])


def _write(tmp_path, name, connections):
    path = tmp_path / name
    path.write_text(
        config_toml(board=BOARD, machine="mach_0123456789abcdef", planner_connections=connections),
        encoding="utf-8",
    )
    return path


def _propose_raw(owner, body):
    """A proposed row written past the store's submit checks (what accept must re-check)."""
    from commonplace.workspace_store import workspace_path

    directory = Path(workspace_path(owner["data_dir"], owner["workspace"]))
    from mempalace.sqlite_crypto import connect

    conn = connect(str(directory / "runner.sqlite3"))
    try:
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM orch_configs WHERE board=?", (BOARD,)
        ).fetchone()[0]
        at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = sha256(body.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO orch_configs(board, version, channel, status, sha256, body, "
            "proposed_by, proposer_trust, created_at) "
            "VALUES (?, ?, 'live', 'proposed', ?, ?, 'user:seed', 'trusted', ?)",
            (BOARD, version, digest, body, at),
        )
        conn.commit()
    finally:
        conn.close()
    return version, digest


def test_r20f_planner_connections_must_be_live_connection_names(signup_hub, owners, tmp_path):
    """R20f: id-shaped or unknown planner connections are refused at submit and at accept."""
    owner = owners["A"]
    cli_token(owner, PLANNER)
    shaped = _orch(owner, "submit", str(_write(tmp_path, "shaped.toml", ["conn_0123456789abcdef"])))
    assert shaped.returncode == 2 and "connection_id_shaped" in shaped.stderr, shaped
    unknown = _orch(owner, "submit", str(_write(tmp_path, "unknown.toml", ["planner-ghost"])))
    assert unknown.returncode == 2 and "connection_unknown" in unknown.stderr, unknown

    good = _orch(owner, "submit", str(_write(tmp_path, "good.toml", [PLANNER])))
    assert good.returncode == 0, good
    assert '"status": "proposed"' in good.stdout and '"ref": "v1"' in good.stdout, good.stdout
    digest = sha256((tmp_path / "good.toml").read_bytes()).hexdigest()
    revoked = _cli(
        owner,
        ["token", "revoke", PLANNER, "--workspace", owner["workspace"], "--by", owner["user_id"]],
    )
    _must(revoked.returncode == 0, f"token revoke failed: {revoked.stderr}")
    late = _orch(owner, "accept", "--board", BOARD, "v1", "--sha256", digest)
    assert late.returncode == 2 and "connection_unknown" in late.stderr, late

    body = config_toml(
        board=BOARD, machine="mach_0123456789abcdef", planner_connections=["conn_0123456789abcdef"]
    )
    version, digest = _propose_raw(owner, body)
    raw = _orch(owner, "accept", "--board", BOARD, f"v{version}", "--sha256", digest)
    assert raw.returncode == 2 and "connection_id_shaped" in raw.stderr, raw
    listed = _orch(owner, "list", "--board", BOARD, "--status", "accepted", by=False)
    assert listed.returncode == 0 and listed.stdout.strip() == "[]", listed
