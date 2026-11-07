"""Config-driven runner starts: fenced_start, the todo trigger sweep and the runner side.

Head-only feature and guard tests (class (b)) for the orchestrator's runner unit. The
pin-RED acceptance forms of R1, R1b, R1d, R1e, R8b, R10a and R11e drive public routes
in ``tests/test_orch_acceptance.py``; this file covers the guards behind them. Each hub
is the production gateway in-process with a real enrolled and attested machine; the
config is written through the orchestrator store. Concurrency tests pause a thread at
a named barrier and fail (not hang) when a barrier is not released.
"""

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from commonplace import sessions
from commonplace.backup import RESTORE_MARKER
from commonplace.boards.service import BoardService
from commonplace.boards.store import BoardStore
from commonplace.extensions import ToolContext, ToolError
from commonplace.orch import durable, runner_bridge, store as orch_store, triggers
from commonplace.projects import limits as project_limits, models
from commonplace.runner import jobs, registry, tools
from commonplace.runner.agent import SPAWN_MESSAGE
from mempalace.sqlite_crypto import connect
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.runner_harness import (
    attest as http_attest,
    cli_token,
    enroll as http_enroll,
    http,
    launches,
    owners,  # noqa: F401
    runner,
    runner_config,
    signup_hub,  # noqa: F401
)

BOARD = "acme"
PLAIN = "plain"
ROLE = "main"
PROVIDER = "example-provider"
MODEL = "example-model"
OTHER_MODEL = "example-model-two"
PROMPT = "Build the card; run fast checks. Marker: runner-prompt-7d2e."
STEPS = ("implement", "fast checks", "report")
PIN_JOB_KEYS = {
    "lease_until",
    "task_number",
    "board_slug",
    "project",
    "topic",
    "runtime_mode",
    "job_id",
    "title_marker",
    "lease_token",
}


def config_text(
    machine,
    *,
    board=BOARD,
    runner=True,
    max_agents=3,
    prompt=PROMPT,
    steps=STEPS,
    topic="worker",
    model_role="worker",
    harness="t3",
):
    lines = [
        'schema = "commonplace.orchestrator/1"',
        f'project = "{board}"',
        "[limits]",
        f"max_agents = {max_agents}",
        "[roles.main]",
        'kind = "orchestrator"',
        'harness = "t3"',
        'topic = "orchestrate"',
        'model_role = "main"',
        'workflow = "card-pipeline"',
        "prompt = '''Coordinate this project.'''",
        "[roles.builder]",
        'kind = "builder"',
        'parent = "main"',
        f'harness = "{harness}"',
        f'topic = "{topic}"',
        f'model_role = "{model_role}"',
        'workflow = "build-unit"',
        f"prompt = '''{prompt}'''",
        "[workflows.card-pipeline]",
        'steps = ["builder"]',
        "[workflows.build-unit]",
        "steps = [" + ", ".join(f'"{step}"' for step in steps) + "]",
    ]
    if runner:
        lines += [
            "[runner]",
            'role = "builder"',
            f'machine = "{machine}"',
            'project = "demo"',
            'runtime_mode = "approval-required"',
            "[[triggers]]",
            'on = "card.todo"',
        ]
    return "\n".join(lines) + "\n"


class Paused:
    """A barrier hook: the paused thread waits until ``go`` (bounded)."""

    def __init__(self):
        self.paused = threading.Event()
        self.go = threading.Event()

    def hook(self):
        self.paused.set()
        if not self.go.wait(15):
            raise RuntimeError("barrier never released")


def in_thread(fn):
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # re-raised in the test thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def joined(thread, box):
    thread.join(20)
    assert not thread.is_alive(), "worker thread did not finish"
    if "error" in box:
        raise box["error"]
    return box.get("value")


class World:
    """One workspace: board ``acme`` (configured by the test), board ``plain`` (never),
    an attested machine whose requesters are the trusted PATs ``a`` and ``b``."""

    def __init__(self, hub, topics=("worker",)):
        self.hub, self.gw, self.db, self.ws = hub, hub.gateway, hub.db, hub.workspace
        self.owner = sessions.person_principal(self.db, hub.admin, self.ws)
        self.boards = BoardService(self.ctx(self.owner))
        self.boards.board_add(slug=BOARD, wing="shared")
        self.boards.board_add(slug=PLAIN, wing="shared")
        self.a = self.pat("orch-a")
        self.b = self.pat("orch-b")
        self.unlisted = self.pat("orch-unlisted")
        enrolled = registry.enroll(
            self.db,
            self.owner,
            {
                "label": "runner-one",
                "projects": ["demo"],
                "topics": list(topics),
                "wings": ["shared"],
                "requesters": [self.a.connection_id, self.b.connection_id],
            },
        )
        self.machine_id = enrolled["machine_id"]
        self.machine_token = enrolled["machine_token"]
        registry.attest(
            self.db,
            self.owner,
            self.machine_id,
            {
                "topics": list(topics),
                "provider_instance_id": "worker",
                "worker_connection_id": enrolled["worker_connection_id"],
            },
        )
        self.set_models(MODEL)
        self.js = jobs.open_store(self.gw, self.ws)
        self.st = orch_store.open_store(self.gw, self.ws)

    # principals and contexts
    def ctx(self, principal):
        return ToolContext(principal=principal, gateway=self.gw)

    def pat(self, name, trust="trusted"):
        token = self.hub.token(
            name,
            profile="full",
            scopes=["memory:read", "memory:write"],
            read_wings=["shared"],
            write_wings=["shared"],
            trust=trust,
        )
        return self.gw.authenticate(f"Bearer {token}")

    def set_models(self, model, role=None):
        body = {r: {"provider": PROVIDER, "model": model} for r in ("main", "worker")}
        if role is not None:
            body = {role: None if model is None else {"provider": PROVIDER, "model": model}}
        models.put(self.db, self.owner, BOARD, body)

    def machine(self):
        return registry.get_machine(self.db, self.ws, self.machine_id)

    # config
    def write(self, text, channel="live"):
        return self.st.write(
            self.owner,
            BOARD,
            text,
            connections=[],
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            channel=channel,
        )

    def configure(self, arm=True, **fields):
        result = self.write(config_text(self.machine_id, **fields))
        if arm:
            triggers.arm_cursor(self.gw, self.ws, BOARD)
        return result

    def unconfigure(self):
        return self.write(config_text(self.machine_id, runner=False))

    def flag_restore(self):
        marker = Path(self.gw.workspace_dir(self.ws)) / RESTORE_MARKER
        marker.write_text(
            json.dumps(
                {
                    "version": 1,
                    "backup_created_at": "2030-01-01T00:00:00Z",
                    "id": secrets.token_hex(16),
                }
            )
        )
        return orch_store.open_store(self.gw, self.ws).restore_state()

    def ack_restore(self):
        state = orch_store.open_store(self.gw, self.ws).restore_state()
        return self.st.restore_ack(self.owner, state.ack_text)

    # cards
    def card(self, board=BOARD, by=None, **fields):
        service = self.boards if by is None else BoardService(self.ctx(by))
        return service.task_add(fields.pop("title", "Example card"), board=board, **fields)[
            "number"
        ]

    def update(self, card, **fields):
        return self.boards.task_update(card, version=self._version(card), **fields)

    def _version(self, card):
        with BoardStore(self.gw.workspace_dir(self.ws)).connection() as conn:
            return conn.execute("SELECT version FROM tasks WHERE id=?", (card,)).fetchone()[0]

    def board_row(self, slug=BOARD):
        with BoardStore(self.gw.workspace_dir(self.ws)).connection() as conn:
            return dict(conn.execute("SELECT * FROM boards WHERE slug=?", (slug,)).fetchone())

    def max_event(self):
        with BoardStore(self.gw.workspace_dir(self.ws)).connection() as conn:
            return conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]

    # runner.sqlite3 reads
    def rows(self, sql, args=()):
        with self.js.connection() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def jobs_for(self, card):
        return self.rows("SELECT * FROM runner_jobs WHERE task_number=? ORDER BY rowid", (card,))

    def all_jobs(self):
        return self.rows("SELECT * FROM runner_jobs ORDER BY rowid")

    def misses(self):
        with self.js.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orch_misses'"
            ).fetchone():
                return []
        return self.rows("SELECT * FROM orch_misses ORDER BY id")

    def steps(self):
        return self.rows("SELECT * FROM orch_steps ORDER BY id")

    def cursor(self, board=BOARD):
        with self.st.connection() as conn:
            return orch_store.cursor_in(conn, board)

    def unstarted(self):
        cp = durable.checkpoint(self.ctx(self.owner), BOARD, ROLE)
        return {row["card"] for row in cp["unstarted"]}

    # starts
    def sweep(self):
        return triggers.run(self.js)

    def claim(self, principal, force=False):
        return durable.resume(self.ctx(principal), BOARD, ROLE, force=force)

    def orch_start(self, principal, lease, card, step_key, **kwargs):
        return runner_bridge.orch_card_start(
            self.ctx(principal),
            BOARD,
            ROLE,
            card,
            step_key,
            lease["epoch"],
            lease["token"],
            **kwargs,
        )

    def person_start(self, card, topic="worker", **kwargs):
        return tools.card_start(
            self.ctx(self.owner), card, self.machine_id, "demo", topic, **kwargs
        )

    # runner machine routes (HTTP: lease, starting, result)
    def machine_call(self, action, body=None):
        return http(
            self.hub.port,
            "POST",
            "/api/runner/" + action,
            token=self.machine_token,
            body=body or {},
        )

    def lease(self):
        status, payload = self.machine_call("lease")
        assert status == 200, (status, payload)
        return payload

    def launch(self, payload, thread="demo-thread"):
        token = {"lease_token": payload["lease_token"]}
        status, body = self.machine_call("starting", token)
        assert status == 200, (status, body)
        status, body = self.machine_call(
            "result", {**token, "outcome": "started", "thread_id": thread}
        )
        assert status == 200, (status, body)
        return body


@pytest.fixture
def world(hub):
    return World(hub)


def refused(code, fn, *args, **kwargs):
    with pytest.raises(runner_bridge.StartRefused) as info:
        fn(*args, **kwargs)
    assert info.value.code == code, (info.value.code, str(info.value))
    return info.value


# ------------------------------------------------------------------ triggers


def test_r1c_failed_trigger_start_is_retried_by_the_next_sweep(world, monkeypatch):
    """R1c: the trigger start fails with an infrastructure error: no job, no miss, the
    cursor stays; the next sweep starts exactly one job."""
    world.configure()
    card = world.card()
    before = world.cursor()
    real = jobs.create_in

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic disk I/O error")

    monkeypatch.setattr(jobs, "create_in", broken)
    assert world.sweep()[BOARD] is None
    assert world.jobs_for(card) == [] and world.misses() == []
    assert world.cursor() == before
    monkeypatch.setattr(jobs, "create_in", real)
    world.sweep()
    world.sweep()
    assert len(world.jobs_for(card)) == 1
    assert world.cursor() == (world.max_event(), before[1])


def test_r1f_first_configuration_never_fires_old_todo_cards(world):
    """R1f: a board's first live accept arms past its old todo events; a new one fires."""
    old = [world.card(), world.card()]
    world.configure()
    world.sweep()
    assert all(world.jobs_for(card) == [] for card in old)
    new = world.card()
    world.sweep()
    world.sweep()
    assert len(world.jobs_for(new)) == 1
    assert set(old) <= world.unstarted()
    assert new not in world.unstarted()


def _started_card_back_in_todo(world):
    """A card started on the configured board, then moved out of todo."""
    card = world.card()
    world.sweep()
    world.launch(world.lease())
    [job] = world.jobs_for(card)
    assert job["state"] == "started"
    # Launched two minutes ago: the next todo entry is strictly later (1 s clock).
    with world.js.connection() as conn:
        conn.execute(
            "UPDATE runner_jobs SET started_at=? WHERE id=?",
            (jobs._after(jobs._now(), -120), job["id"]),
        )
    world.update(card, status="backlog")
    return card


def _ago(seconds):
    return jobs._after(jobs._now(), -seconds)


def _set_job(world, card, **values):
    with world.js.connection() as conn:
        conn.execute(
            "UPDATE runner_jobs SET "
            + ",".join(f"{key}=?" for key in values)
            + " WHERE task_number=?",
            (*values.values(), card),
        )


def _todo_entry(world, card):
    with BoardStore(world.gw.workspace_dir(world.ws)).connection() as conn:
        return conn.execute(
            "SELECT MAX(created_at) FROM task_events WHERE task_id=? AND to_status='todo'",
            (card,),
        ).fetchone()[0]


def _next_second(after):
    while jobs._now() <= after:  # board events are append-only; wait out the 1 s clock
        time.sleep(0.05)


def test_r1g_reconfiguration_resets_the_cursor(world):
    """R1g: configured -> unconfigured -> todo cards -> configured again: 0 jobs for the
    unconfigured interval (all listed as unstarted), then a new todo fires once. A card
    launched after its todo entry and then only edited stays off the list."""
    world.configure()
    started = _started_card_back_in_todo(world)
    kept = world.card()
    world.sweep()
    entry = _todo_entry(world, kept)
    _set_job(world, kept, state="started", started_at=jobs._after(entry, 1))
    _next_second(jobs._after(entry, 1))
    world.update(kept, title="Edited while in todo")
    world.unconfigure()
    assert world.cursor() is None
    world.update(started, status="todo")
    olds = [world.card(), world.card()]
    world.configure()
    world.sweep()
    world.sweep()
    assert all(world.jobs_for(card) == [] for card in olds)
    assert len(world.jobs_for(started)) == 1
    assert world.misses() == []
    assert {*olds, started} <= world.unstarted()
    assert kept not in world.unstarted()
    new = world.card()
    world.sweep()
    assert len(world.jobs_for(new)) == 1


def test_r1h_events_before_arming_never_fire(world):
    """R1h: an event committed between the configuring accept and the arm (or a kill
    before the arm) is never fired; the sweep arms the NULL cursor itself. A card whose
    queued job launched after the card re-entered todo stays off the unstarted list."""
    world.configure()
    started = _started_card_back_in_todo(world)
    relaunched = world.card()
    world.sweep()
    _set_job(world, relaunched, created_at=_ago(300))
    world.unconfigure()
    world.update(started, status="todo")
    world.update(relaunched, status="backlog")
    world.update(relaunched, status="todo")
    _next_second(_todo_entry(world, relaunched))  # a same-second launch would be a listed tie
    _set_job(world, relaunched, state="started", started_at=jobs._now())
    world.configure(arm=False)
    assert world.cursor()[0] is None
    window = world.card()
    world.sweep()  # arms, then sweeps from the armed cursor
    assert world.cursor() == (world.max_event(), world.cursor()[1])
    assert world.jobs_for(window) == [] and world.misses() == []
    assert {window, started} <= world.unstarted()
    assert relaunched not in world.unstarted()  # its queued job launched after re-entry
    assert triggers.arm_cursor(world.gw, world.ws, BOARD) is False  # already armed
    after = world.card()
    world.sweep()
    world.sweep()
    assert len(world.jobs_for(after)) == 1


def test_r1h_empty_history_arms_at_zero(world):
    """R1h: a workspace with no task events arms at 0; the first todo fires."""
    assert world.max_event() == 0
    world.configure(arm=False)
    assert triggers.arm_cursor(world.gw, world.ws, BOARD) is True
    assert world.cursor()[0] == 0
    card = world.card()
    world.sweep()
    assert len(world.jobs_for(card)) == 1


@pytest.mark.parametrize("reset", ["reconfigure", "restore_ack"])
def test_r1i_stale_arm_is_discarded(world, reset):
    """R1i: an arm paused between its gen read and its MAX read loses to a reset made
    meanwhile: 0 rows updated, and the interval's event never fires."""
    world.configure(arm=False)
    gen = world.cursor()[1]
    barrier = Paused()
    with triggers.barrier("arm_after_gen", barrier.hook):
        thread, box = in_thread(lambda: triggers.arm_cursor(world.gw, world.ws, BOARD))
        assert barrier.paused.wait(15)
        if reset == "reconfigure":
            world.unconfigure()
            event_card = world.card()
            world.configure(arm=False)
        else:
            event_card = world.card()
            assert world.flag_restore().flagged
            world.ack_restore()
        barrier.go.set()
        assert joined(thread, box) is False
    assert world.cursor() == (None, gen + 1)
    world.sweep()
    world.sweep()
    assert world.jobs_for(event_card) == []
    assert event_card in world.unstarted()


def _paused_sweep(world, name):
    barrier = Paused()
    context = triggers.barrier(name, barrier.hook)
    context.__enter__()
    thread, box = in_thread(world.sweep)
    assert barrier.paused.wait(15)
    return barrier, context, thread, box


def _reconfigure(world, *, event=True):
    world.unconfigure()
    card = world.card() if event else None
    world.configure()
    return card, world.cursor()


def test_r1j_sweep_paused_after_its_cursor_read_drops_the_interval(world):
    """R1j: sweep paused after its (cursor, gen) read; unconfigure -> E1 -> reconfigure;
    resume: 0 jobs for E1, the cursor is the reconfigure arm's value."""
    world.configure()
    barrier, context, thread, box = _paused_sweep(world, "sweep_after_pair")
    e1, armed = _reconfigure(world)
    barrier.go.set()
    joined(thread, box)
    context.__exit__(None, None, None)
    assert world.jobs_for(e1) == [] and world.misses() == []
    assert world.cursor() == armed
    world.sweep()
    assert world.jobs_for(e1) == []


def test_r1j_b_in_lock_recheck_drops_a_read_candidate(world):
    """R1j-b: barrier after the candidate and (cursor, gen) reads, before fenced_start's
    BEGIN IMMEDIATE; unconfigure -> E1 -> reconfigure; resume: the in-lock re-check
    drops the candidate (0 jobs for E0 and E1), cursor unchanged from the arm."""
    world.configure()
    e0 = world.card()
    barrier, context, thread, box = _paused_sweep(world, "sweep_before_start")
    e1, armed = _reconfigure(world)
    barrier.go.set()
    report = joined(thread, box)
    context.__exit__(None, None, None)
    assert report[BOARD] == [{"event": report[BOARD][0]["event"], "dropped": True}]
    assert world.jobs_for(e0) == [] and world.jobs_for(e1) == []
    assert world.misses() == []
    assert world.cursor() == armed
    world.sweep()
    assert world.jobs_for(e0) == [] and world.jobs_for(e1) == []
    assert {e0, e1} <= world.unstarted()


def test_r1j_c_requester_miss_is_written_only_inside_fenced_start(world):
    """R1j-c: a candidate refused requester_not_allowed before the transaction; the
    board is reconfigured meanwhile: 0 miss rows for that event, cursor unchanged."""
    world.configure()
    world.card(by=world.unlisted)
    barrier, context, thread, box = _paused_sweep(world, "sweep_after_requester")
    _card, armed = _reconfigure(world, event=False)
    barrier.go.set()
    joined(thread, box)
    context.__exit__(None, None, None)
    assert world.misses() == []
    assert world.cursor() == armed
    assert world.all_jobs() == []


def test_r1j_d_cursor_pair_is_read_before_the_candidates(world):
    """R1j-d: E0 committed while configured; barrier between the sweep's (cursor, gen)
    read and its candidate read; unconfigure -> E1 -> reconfigure; resume: 0 jobs for
    E0 and E1, cursor equals the reconfigure arm's value."""
    world.configure()
    e0 = world.card()
    barrier, context, thread, box = _paused_sweep(world, "sweep_after_pair")
    e1, armed = _reconfigure(world)
    barrier.go.set()
    joined(thread, box)
    context.__exit__(None, None, None)
    assert world.jobs_for(e0) == [] and world.jobs_for(e1) == []
    assert world.cursor() == armed


def test_barrier_refuses_a_sweep_connection_inside_a_transaction(world):
    """A barrier hook fails loudly when its connection holds a transaction."""
    with world.st.connection() as conn:
        conn.execute("BEGIN")
        with triggers.barrier("sweep_after_pair", lambda: None):
            with pytest.raises(AssertionError, match="holds a transaction"):
                triggers._barrier("sweep_after_pair", conn)
        conn.execute("ROLLBACK")


_WRITES = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "BEGIN"}


def test_sweep_takes_no_runner_write_lock_before_fenced_start(world, monkeypatch):
    """A21 N3(a): on an initialized, armed board with no candidates, the sweep runs no
    write statement on runner.sqlite3 and completes while another connection holds
    BEGIN IMMEDIATE there. Busy wait is 0, so any lock attempt would raise at once."""
    world.configure()
    world.sweep()
    assert world.cursor()[0] is not None  # armed, nothing to start
    statements = []
    real = orch_store.connect

    def traced(path, **kwargs):
        conn = real(path, **{**kwargs, "timeout": 0})
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(orch_store, "connect", traced)
    monkeypatch.setattr(jobs, "connect", traced)
    holder = connect(world.js.path, timeout=0, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        try:
            report = world.sweep()
        except sqlite3.OperationalError as exc:
            pytest.fail(f"the sweep needed the runner.sqlite3 write lock: {exc}")
    finally:
        holder.rollback()
        holder.close()
    assert report == {BOARD: []}
    assert statements, "the trace spy saw no runner.sqlite3 statement"
    writes = [sql for sql in statements if sql.split(None, 1)[0].upper() in _WRITES]
    assert writes == []


def test_sweep_skips_a_pending_restore_marker_without_converting_it(world):
    """The sweep never converts a restore marker: while one is pending it starts
    nothing and leaves the marker; the next explicit store open converts it."""
    world.configure()
    marker = Path(world.gw.workspace_dir(world.ws)) / RESTORE_MARKER
    marker.write_text(json.dumps({"version": 1, "id": secrets.token_hex(16)}))
    card = world.card()
    assert world.sweep() == {}
    assert marker.exists() and world.jobs_for(card) == [] and world.misses() == []
    with world.st.connection() as conn:
        assert not orch_store.restore_state_in(conn).flagged
    assert orch_store.open_store(world.gw, world.ws).restore_state().flagged
    assert not marker.exists()
    assert world.sweep() == {}
    assert world.jobs_for(card) == []


def test_r18_refused_candidates_are_misses_and_the_cursor_moves_past_them(world, monkeypatch):
    """R18: a todo event by a PAT that fails requester_ok -> miss requester_not_allowed;
    a busy queue -> miss busy; the cursor advances past each, nothing is re-fired."""
    world.configure()
    foreign = world.card(by=world.unlisted)
    monkeypatch.setattr(jobs, "QUEUE_LIMIT", 0)
    busy = world.card()
    world.sweep()
    world.sweep()
    assert [(m["card"], m["code"]) for m in world.misses()] == [
        (foreign, "requester_not_allowed"),
        (busy, "busy"),
    ]
    assert world.all_jobs() == []
    assert world.cursor()[0] == world.max_event()
    monkeypatch.setattr(jobs, "QUEUE_LIMIT", 8)
    world.sweep()
    assert len(world.misses()) == 2 and world.all_jobs() == []


def test_r18b_assigned_todo_card_is_a_miss(world):
    """R18b: an assigned todo card -> miss assigned; the cursor moves past it; an edit
    of a todo card that stays in todo does not fire."""
    world.configure()
    card = world.card(by=world.a, assignee="conn:orch-b")
    world.sweep()
    assert [(m["card"], m["code"]) for m in world.misses()] == [(card, "assigned")]
    assert world.jobs_for(card) == []
    assert world.cursor()[0] == world.max_event()
    world.update(card, title="Renamed while in todo")  # todo -> todo: not a firing event
    world.sweep()
    assert len(world.misses()) == 1 and world.jobs_for(card) == []


def test_r18c_card_started_recently_is_a_miss(world):
    """R18c: a card whose start launched < 24 h ago re-enters todo -> miss
    started_recently (the trigger never uses the person exemption)."""
    world.configure()
    card = _started_card_back_in_todo(world)
    world.update(card, status="todo")
    world.sweep()
    assert [(m["card"], m["code"]) for m in world.misses()] == [(card, "started_recently")]
    assert len(world.jobs_for(card)) == 1


def test_r18d_unconfigured_board_is_never_scanned(world):
    """R18d: events on a board without a [runner] config create no row, and the sweep
    never reads that board."""
    world.configure()
    card = world.card(board=PLAIN)
    report = world.sweep()
    assert set(report) == {BOARD}
    assert world.jobs_for(card) == [] and world.misses() == []
    assert world.cursor(PLAIN) is None


def test_unconfigured_workspace_check_creates_no_orchestrator_tables(hub):
    """The configured() check on a workspace that never stored a config is read-only:
    it answers False and leaves runner.sqlite3 without orchestrator tables."""
    js = jobs.open_store(hub.gateway, hub.workspace)
    assert runner_bridge.configured(hub.gateway, hub.workspace, PLAIN) is False
    with js.connection() as conn:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert not {name for name in names if name.startswith("orch_")}


def test_trigger_actor_is_rebuilt_and_requester_unknown_when_revoked(world):
    """A trigger runs as the event's actor: a listed PAT's event starts a job requested
    by that PAT; an event whose PAT is revoked before the sweep is requester_unknown."""
    world.configure()
    card = world.card(by=world.b)
    world.sweep()
    [job] = world.jobs_for(card)
    assert job["requested_by"] == "conn:" + world.b.connection_id
    assert job["request_key"].startswith("trig:") and job["step_key"] is None
    revoked = world.card(by=world.a)
    world.db.revoke(world.a.connection_id, actor=world.hub.admin)
    world.sweep()
    assert [(m["card"], m["code"]) for m in world.misses()] == [(revoked, "requester_unknown")]
    assert world.jobs_for(revoked) == []


# ------------------------------------------------------------- fenced_start


def test_r3_lost_ack_retries_return_the_stored_job(world):
    """R3: cap 1; the ack of a committed start is lost; retries by the same holder and
    by a successor return the original job; the same key on another card is a 409."""
    first, second = world.card(), world.card()
    world.configure(max_agents=1)
    lease = world.claim(world.a)
    reply = world.orch_start(world.a, lease, first, "build-1")
    assert reply["status"] == 201
    job = reply["job"]["job_id"]
    again = world.orch_start(world.a, lease, first, "build-1")
    assert (again["status"], again["job"]["job_id"]) == (200, job)
    durable.release(world.ctx(world.a), BOARD, ROLE, lease["epoch"], lease["token"])
    successor = world.claim(world.b)
    retry = world.orch_start(world.b, successor, first, "build-1")
    assert (retry["status"], retry["job"]["job_id"]) == (200, job)
    conflict = refused("step_conflict", world.orch_start, world.b, successor, second, "build-1")
    assert conflict.status == 409 and conflict.details["step"]["card"] == first
    [row] = world.all_jobs()
    assert (row["step_key"], row["request_key"]) == ("build-1", "orch:main:build-1")
    assert row["requested_by"] == "conn:" + world.a.connection_id


def test_r4_paused_start_holds_the_lock_and_a_later_call_is_fenced(world, monkeypatch):
    """R4: A pauses inside its fenced transaction; B's claim waits for A's commit; A's
    next start (another card) is fenced by B's epoch."""
    first, second = world.card(), world.card()
    world.configure()
    lease = world.claim(world.a)
    barrier = Paused()
    real = durable.insert_step_in

    def paused_insert(conn, **kwargs):
        if threading.current_thread() is not threading.main_thread():
            barrier.hook()  # A, inside its BEGIN IMMEDIATE, after the job insert
        return real(conn, **kwargs)

    monkeypatch.setattr(durable, "insert_step_in", paused_insert)
    thread_a, box_a = in_thread(lambda: world.orch_start(world.a, lease, first, "s-a"))
    assert barrier.paused.wait(15), "A never reached its insert"
    thread_b, box_b = in_thread(lambda: world.claim(world.owner, force=True))
    thread_b.join(0.5)
    assert thread_b.is_alive(), "B must wait for A's transaction"
    barrier.go.set()
    assert joined(thread_a, box_a)["status"] == 201
    successor = joined(thread_b, box_b)
    assert successor["epoch"] == lease["epoch"] + 1
    refused("fenced", world.orch_start, world.a, lease, second, "s-a2")
    assert len(world.all_jobs()) == 1


class Killed(BaseException):
    pass


def test_r6_kill_before_commit_leaves_no_row(world, monkeypatch):
    """R6: the process dies inside the fenced transaction: no job, no step; a retry of
    the same step then starts normally."""
    card = world.card()
    world.configure()
    lease = world.claim(world.a)
    real = durable.insert_step_in

    def die(*args, **kwargs):
        raise Killed()

    monkeypatch.setattr(durable, "insert_step_in", die)
    with pytest.raises(Killed):
        world.orch_start(world.a, lease, card, "s1")
    assert world.all_jobs() == [] and world.steps() == []
    monkeypatch.setattr(durable, "insert_step_in", real)
    assert world.orch_start(world.a, lease, card, "s1")["status"] == 201


def test_r17_storage_full_rolls_back_the_fenced_start(world, monkeypatch):
    """R17: SQLITE_FULL after the job insert, inside the one transaction: 0 rows."""
    card = world.card()
    world.configure()
    lease = world.claim(world.a)

    def full(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(durable, "insert_step_in", full)
    with pytest.raises(sqlite3.OperationalError):
        world.orch_start(world.a, lease, card, "s1")
    assert world.all_jobs() == [] and world.steps() == []


def test_r8c_restore_flag_refuses_pat_and_person_and_the_sweep(world):
    """R8c: under the restore flag a PAT's orch_card_start and a person's card_start
    are both refused, and the sweep starts nothing."""
    card = world.card()
    world.configure()
    lease = world.claim(world.a)
    assert world.flag_restore().flagged
    refused("restore_pending", world.orch_start, world.a, lease, card, "s1")
    refused("restore_pending", world.person_start, card)
    later = world.card()
    assert world.sweep() == {}
    assert world.jobs_for(card) == [] and world.jobs_for(later) == []


def test_r8d_only_a_person_owner_cancels_an_orchestrator_start(world):
    """R8d: the requesting PAT cannot cancel its orch-linked job; the person owner can."""
    card = world.card()
    world.configure()
    lease = world.claim(world.a)
    job = world.orch_start(world.a, lease, card, "s1")["job"]["job_id"]
    with pytest.raises(ToolError, match="only a workspace owner"):
        tools.card_start_cancel(world.ctx(world.a), job)
    assert world.jobs_for(card)[0]["state"] == "queued"
    assert tools.card_start_cancel(world.ctx(world.owner), job)["state"] == "cancelled"


def test_r8d_requester_still_cancels_a_start_without_a_step(world):
    """R8d control: a non-orchestrator start keeps its requester's cancel right."""
    card = world.card(board=PLAIN)
    status, job = tools.card_start(world.ctx(world.a), card, world.machine_id, "demo", "worker")
    assert status == 201
    assert tools.card_start_cancel(world.ctx(world.a), job["job_id"])["state"] == "cancelled"


def test_r10_experiment_pins_its_values_and_live_is_unchanged(hub):
    """R10: an experiment changes prompt, workflow, topic and model_role; the job pins
    its version, sha and model and the lease payload carries the experiment's values;
    live is unchanged; a diff outside the allow-list is refused."""
    world = World(hub, topics=("worker", "review"))
    card = world.card()
    world.configure()
    world.set_models(OTHER_MODEL, role="main")
    live = world.st.accepted(BOARD)
    exp_text = config_text(
        world.machine_id,
        prompt="Experiment prompt. Marker: exp-prompt-91aa.",
        steps=("try the experiment", "report"),
        topic="review",
        model_role="main",
    )
    exp = world.write(exp_text, channel="exp:fast").version
    status, job = world.person_start(card, config_ref="exp:fast", topic="review")
    assert status == 201
    [row] = world.jobs_for(card)
    assert (row["config_version"], row["config_sha256"], row["channel"]) == (
        exp.version,
        exp.sha256,
        "exp:fast",
    )
    assert (row["model_provider"], row["model"], row["topic"]) == (PROVIDER, OTHER_MODEL, "review")
    payload = world.lease()
    assert payload["topic"] == "review"
    assert "exp-prompt-91aa" in payload["message"] and PROMPT not in payload["message"]
    assert "try the experiment" in payload["message"]
    assert payload["modelSelection"] == {"instanceId": PROVIDER, "model": OTHER_MODEL}
    assert world.st.accepted(BOARD) == live
    bad = config_text(world.machine_id, max_agents=5)
    with pytest.raises(orch_store.OrchError) as info:
        world.write(bad, channel="exp:wide")
    assert info.value.code == "experiment_not_allowed"


def test_r10b_same_step_key_with_another_config_ref_is_409(world):
    """R10b: the same step key with another config_ref is a 409; no second job."""
    card = world.card()
    world.configure()
    world.write(config_text(world.machine_id, prompt="Variant."), channel="exp:alt")
    lease = world.claim(world.a)
    assert world.orch_start(world.a, lease, card, "s1")["status"] == 201
    refused("step_conflict", world.orch_start, world.a, lease, card, "s1", config_ref="exp:alt")
    assert len(world.all_jobs()) == 1


def test_r10c_live_accept_between_attempts_is_409_and_keeps_the_job(world):
    """R10c: a live accept between two attempts of a step: 409, old job untouched."""
    card = world.card()
    world.configure()
    lease = world.claim(world.a)
    job = world.orch_start(world.a, lease, card, "s1")["job"]["job_id"]
    world.configure(prompt="Changed live prompt.")
    refused("step_conflict", world.orch_start, world.a, lease, card, "s1")
    [row] = world.all_jobs()
    assert (row["id"], row["config_version"], row["state"]) == (job, 1, "queued")


def test_r10d_model_is_pinned_at_insert(world):
    """R10d: (i) a model change between attempts of a step is a 409; (ii) a queued job
    launches with its pinned model; a new step uses the new model."""
    first, second = world.card(), world.card()
    world.configure()
    lease = world.claim(world.a)
    assert world.orch_start(world.a, lease, first, "s1")["model"]["model"] == MODEL
    world.set_models(OTHER_MODEL)
    refused("step_conflict", world.orch_start, world.a, lease, first, "s1")
    payload = world.lease()
    assert payload["job_id"] == world.jobs_for(first)[0]["id"]
    assert payload["modelSelection"] == {"instanceId": PROVIDER, "model": MODEL}
    reply = world.orch_start(world.a, lease, second, "s2")
    assert reply["model"] == {"provider": PROVIDER, "model": OTHER_MODEL}
    assert world.jobs_for(second)[0]["model"] == OTHER_MODEL


def test_r11_cap_counts_queued_jobs(world):
    """R11: cap 2 with one worker: the third start is refused."""
    cards = [world.card() for _ in range(3)]
    world.configure(max_agents=2)
    lease = world.claim(world.a)
    world.orch_start(world.a, lease, cards[0], "s1")
    world.orch_start(world.a, lease, cards[1], "s2")
    refused("at_cap", world.orch_start, world.a, lease, cards[2], "s3")
    assert len(world.all_jobs()) == 2


def test_r11b_started_job_on_a_todo_card_is_counted(world):
    """R11b: a started job within the hold counts although its card is still todo."""
    first, second = world.card(), world.card()
    world.configure(max_agents=1)
    lease = world.claim(world.a)
    world.orch_start(world.a, lease, first, "s1")
    world.launch(world.lease())
    assert world.jobs_for(first)[0]["state"] == "started"
    refused("at_cap", world.orch_start, world.a, lease, second, "s2")


def test_r11c_doing_card_held_by_an_agent_without_a_job_is_counted(world):
    """R11c: an agent holds a doing card without any job: it counts."""
    cards = [world.card() for _ in range(3)]
    world.configure(max_agents=2)
    BoardService(world.ctx(world.b)).task_take_next(board=BOARD, task=cards[1])
    lease = world.claim(world.a)
    world.orch_start(world.a, lease, cards[0], "s1")
    refused("at_cap", world.orch_start, world.a, lease, cards[2], "s3")


def test_r11d_project_limit_below_the_config_wins(world):
    """R11d: project_limits 1 with config 3 -> cap 1."""
    first, second = world.card(), world.card()
    world.configure(max_agents=3)
    board_id = world.board_row()["id"]
    with BoardStore(world.gw.workspace_dir(world.ws)).transaction() as conn:
        project_limits.put(conn, board_id, {"max_agents": 1})
    lease = world.claim(world.a)
    world.orch_start(world.a, lease, first, "s1")
    refused("at_cap", world.orch_start, world.a, lease, second, "s2")


def test_r11f_person_retry_returns_its_job_before_the_cap(world):
    """R11f: cap 1; a person start's reply is lost; the identical retry returns the
    original job (count stays 1); the same key with a changed argument is a 409."""
    card = world.card()
    world.configure(max_agents=1)
    world.write(config_text(world.machine_id, max_agents=1, prompt="Variant."), channel="exp:alt")
    status, job = world.person_start(card, request_key="person-1")
    assert status == 201
    again = world.person_start(card, request_key="person-1")
    assert again == (200, job)
    assert len(world.all_jobs()) == 1
    refused(
        "request_conflict", world.person_start, card, request_key="person-1", config_ref="exp:alt"
    )
    assert len(world.all_jobs()) == 1


def test_r11f_unkeyed_person_call_goes_straight_to_the_cap(world):
    """R11f control: an unkeyed person call has no existing-job lookup: the cap refuses it."""
    card = world.card()
    world.configure(max_agents=1)
    assert world.person_start(card)[0] == 201
    refused("at_cap", world.person_start, card)


def test_r12_pin_runner_store_gains_columns_and_keeps_digest_and_payload(world):
    """R12: a runner.sqlite3 created by the earlier schema gains the run-record columns;
    a start without config keeps the earlier digest and lease payload byte for byte."""
    path = Path(world.gw.workspace_dir(world.ws)) / "runner.sqlite3"
    path.unlink()
    conn = connect(str(path), timeout=10, isolation_level=None)
    try:
        conn.executescript(jobs.SCHEMA)
        before = {row[1] for row in conn.execute("PRAGMA table_info(runner_jobs)")}
    finally:
        conn.close()
    assert not before & set(jobs.CONFIG_COLUMNS)
    store = jobs.open_store(world.gw, world.ws)
    with store.connection() as conn:
        after = {row[1] for row in conn.execute("PRAGMA table_info(runner_jobs)")}
    assert after == before | set(jobs.CONFIG_COLUMNS)
    card = world.card(board=PLAIN)
    status, job = world.person_start(card)
    assert status == 201 and set(job) == PIN_JOB_KEYS
    [row] = world.jobs_for(card)
    arguments = dict(
        machine_id=world.machine_id,
        project="demo",
        topic="worker",
        runtime_mode="approval-required",
    )
    digest = hashlib.sha256(
        json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert row["request_hash"] == digest
    assert all(row[name] is None for name in jobs.CONFIG_COLUMNS)
    assert set(world.lease()) == PIN_JOB_KEYS


def test_r20g_plan_label_added_after_queueing_is_refused_at_starting(world):
    """R20g: a job queued on a configured board, then the card gets plan: starting is
    refused plan_pending (job cancelled, no launch) and the card is listed unstarted."""
    card = world.card()
    world.configure()
    world.person_start(card)
    world.update(card, labels=["plan"])
    payload = world.lease()
    status, body = world.machine_call("starting", {"lease_token": payload["lease_token"]})
    assert (status, body) == (409, {"error": "plan_pending"})
    [row] = world.jobs_for(card)
    assert (row["state"], row["outcome_code"], row["starting_at"]) == (
        "cancelled",
        "plan_pending",
        None,
    )
    assert card in world.unstarted()


def test_plan_label_refuses_every_path_even_an_exact_retry(world):
    """plan_pending wins over the step lookup: a same-step retry is refused too."""
    card = world.card()
    world.configure()
    lease = world.claim(world.a)
    world.orch_start(world.a, lease, card, "s1")
    world.update(card, labels=["plan"])
    refused("plan_pending", world.orch_start, world.a, lease, card, "s1")
    refused("plan_pending", world.person_start, card)


def test_r24_runner_harness_and_unresolved_model_are_refused(world):
    """R24: a runner role harness other than t3 is refused at validation; an
    unresolved model_role is refused before insert (orch, person and trigger)."""
    with pytest.raises(orch_store.OrchError) as info:
        world.configure(harness="codex")
    assert info.value.code == "runner_harness_not_t3"
    card = world.card()
    world.configure()
    world.set_models(None, role="worker")
    lease = world.claim(world.a)
    refused("model_unresolved", world.orch_start, world.a, lease, card, "s1")
    refused("model_unresolved", world.person_start, card)
    later = world.card()
    world.sweep()
    assert [(m["card"], m["code"]) for m in world.misses()] == [(later, "model_unresolved")]
    assert world.all_jobs() == []


def test_r24b_model_is_on_the_job_the_reply_and_the_lease_payload(world):
    """R24b: the pinned model is on the job row, the orch_card_start reply and the lease
    payload, whose message is SPAWN_MESSAGE + the role prompt + the workflow steps."""
    card = world.card()
    result = world.configure()
    lease = world.claim(world.a)
    reply = world.orch_start(world.a, lease, card, "s1")
    assert reply["model"] == {"provider": PROVIDER, "model": MODEL}
    assert reply["config"] == {"version": 1, "sha256": result.version.sha256, "channel": "live"}
    [row] = world.all_jobs()
    assert (row["model_provider"], row["model"], row["config_version"]) == (PROVIDER, MODEL, 1)
    payload = world.lease()
    assert payload["message"].startswith(SPAWN_MESSAGE.format(n=card, job_id=row["id"]))
    assert PROMPT in payload["message"] and all(step in payload["message"] for step in STEPS)
    assert payload["modelSelection"] == {"instanceId": PROVIDER, "model": MODEL}


def test_r25_twenty_four_hour_rule_on_the_orch_path(world):
    """R25: an orch re-run < 24 h under a new step key is started_recently; the same
    step retried returns the stored job; a person re-run is admitted."""
    card = world.card()
    world.configure()
    lease = world.claim(world.a)
    job = world.orch_start(world.a, lease, card, "s1")["job"]["job_id"]
    world.launch(world.lease())
    refused("started_recently", world.orch_start, world.a, lease, card, "s2")
    again = world.orch_start(world.a, lease, card, "s1")
    assert (again["status"], again["job"]["job_id"], again["job"]["state"]) == (200, job, "started")
    step = durable.journal_record(
        world.ctx(world.a),
        BOARD,
        ROLE,
        lease["epoch"],
        lease["token"],
        "s1",
        "done",
        evidence={"type": "runner_job", "job": job},
    )
    assert step["state"] == "done"
    status, _job = world.person_start(card)
    assert status == 201
    assert len(world.all_jobs()) == 2


def test_r26_request_key_longer_than_128_is_refused_before_insert(world):
    """R26: step_key longer than 80 (key > 128) and a person key of 129 are refused."""
    card = world.card()
    world.configure()
    lease = world.claim(world.a)
    refused("invalid_step_key", world.orch_start, world.a, lease, card, "s" * 81)
    with pytest.raises(ToolError, match="invalid request_key"):
        world.person_start(card, request_key="k" * 129)
    version = world.st.accepted(BOARD)
    with BoardStore(world.gw.workspace_dir(world.ws)).connection() as conn:
        card_row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (card,)).fetchone())
    plan = runner_bridge.prepare(
        world.gw,
        world.ws,
        board_row=world.board_row(),
        card_row=card_row,
        version=version,
        principal=world.owner,
        requested_by="user:" + world.owner.user_id,
        person=True,
    )
    refused(
        "invalid_request_key",
        runner_bridge.fenced_start,
        world.js,
        plan,
        path="person",
        request_key="k" * 129,
    )
    assert world.all_jobs() == []


def test_person_card_start_arguments_must_match_the_config(world):
    """A person start on a configured board runs the config's grant, not other args."""
    card = world.card()
    world.configure()
    refused("config_mismatch", world.person_start, card, runtime_mode="full-access")
    with pytest.raises(ToolError, match="config_ref needs"):
        tools.card_start(
            world.ctx(world.owner),
            world.card(board=PLAIN),
            world.machine_id,
            "demo",
            "worker",
            config_ref="live",
        )


def test_agent_refuses_a_malformed_config_launch():
    """The runner never launches a config job whose message or model is malformed."""
    from commonplace.runner import agent

    job = {"task_number": 7, "job_id": "job_0123456789abcdef"}
    base = SPAWN_MESSAGE.format(n=7, job_id=job["job_id"])
    good = {"instanceId": PROVIDER, "model": MODEL}
    assert agent._launch_text(dict(job)) == (base, None)
    assert agent._launch_text({**job, "message": base + "\n\nx", "modelSelection": good})[1] == good
    for bad in (
        {"message": "Something else", "modelSelection": good},
        {"message": base, "modelSelection": None},
        {"message": base + "x" * agent.MAX_MESSAGE_CHARS, "modelSelection": good},
        {"message": base, "modelSelection": {**good, "extra": 1}},
    ):
        with pytest.raises(ValueError):
            agent._launch_text({**job, **bad})


# ---------------------------------------------------------- real-time launch


def test_r2_stalled_launch_is_uncertain_in_doubt_and_never_redone(
    signup_hub,  # noqa: F811
    owners,  # noqa: F811
    tmp_path,
):
    """R2 (real time): the fake thread launcher stalls past the runner's timeout: the
    job is uncertain, its step in_doubt; the same step returns that job, no 2nd job.
    The launch carried the pinned message and model."""
    hub, owner = signup_hub, owners["A"]
    status, body = http(
        hub.port,
        "POST",
        "/api/memory",
        cookie=owner["cookie"],
        body={"text": "Synthetic workspace example.", "theme": "shared", "topic": "demo"},
    )
    assert status == 201, (status, body)
    cid, token = cli_token(owner, "orch-holder", profile="standard")
    machine = http_enroll(hub, owner, requesters=[cid])
    http_attest(hub, owner, machine)
    status, body = http(
        hub.port,
        "POST",
        "/api/boards",
        cookie=owner["cookie"],
        body={"slug": BOARD, "wing": "shared"},
    )
    assert status == 201, (status, body)
    status, body = http(
        hub.port,
        "PUT",
        f"/api/projects/{BOARD}/models",
        cookie=owner["cookie"],
        body={r: {"provider": PROVIDER, "model": MODEL} for r in ("main", "worker")},
    )
    assert status == 200, (status, body)
    status, card = http(
        hub.port,
        "POST",
        f"/api/boards/{BOARD}/cards",
        cookie=owner["cookie"],
        body={"title": "Demo"},
    )
    assert status == 201, (status, card)
    gateway, ws = hub.gateway, owner["workspace"]
    person = sessions.person_principal(gateway.db, owner["user_id"], ws)
    text = config_text(machine["machine_id"])
    orch_store.open_store(gateway, ws).write(
        person, BOARD, text, connections=[], sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    triggers.arm_cursor(gateway, ws, BOARD)
    holder = gateway.authenticate("Bearer " + token)
    ctx = ToolContext(principal=holder, gateway=gateway)
    lease = durable.resume(ctx, BOARD, ROLE)
    start = runner_bridge.orch_card_start(
        ctx, BOARD, ROLE, card["number"], "s1", lease["epoch"], lease["token"]
    )
    assert start["status"] == 201
    path = runner_config(tmp_path, hub, machine["machine_token"], mode="sleep")
    env = {
        "COMMONPLACE_RUNNER_TOKEN": machine["machine_token"],
        "RUNNER_T3_GRANT": "synthetic-grant",
    }
    result = runner(["run", "--config", str(path), "--once"], env)
    assert result.returncode == 0, result.stderr
    [launch] = launches(tmp_path)
    assert launch["launch"]["modelSelection"] == {"instanceId": PROVIDER, "model": MODEL}
    assert PROMPT in launch["launch"]["message"]
    checkpoint = durable.checkpoint(ctx, BOARD, ROLE)
    assert [step["step_key"] for step in checkpoint["in_doubt"]] == ["s1"]
    again = runner_bridge.orch_card_start(
        ctx, BOARD, ROLE, card["number"], "s1", lease["epoch"], lease["token"]
    )
    assert (again["status"], again["job"]["job_id"], again["job"]["state"]) == (
        200,
        start["job"]["job_id"],
        "uncertain",
    )
    status, rows = http(
        hub.port, "GET", f"/api/card-starts?task={card['number']}", cookie=owner["cookie"]
    )
    assert status == 200 and len(rows) == 1, (status, rows)


# ------------------------------------------------------- runner-start void contract


@pytest.fixture(params=[False, True], ids=["switch-off", "switch-on"])
def void_world(world, request):
    from commonplace.projects import orchestration

    orchestration.set_enabled(world.db, world.owner, request.param)
    world.orchestration_on = request.param
    return world


def start_result(fn, *args, **kwargs):
    """Capture refusals so a wrong code/admission fails as a test-body assertion."""
    try:
        return None, fn(*args, **kwargs)
    except runner_bridge.StartRefused as exc:
        return exc.code, exc


def void_start(world, principal=None, **kwargs):
    return durable.void_step(world.ctx(principal or world.owner), BOARD, ROLE, "s1", **kwargs)


def prepare_void_job(world, state="queued", *, max_agents=3):
    card, other = world.card(), world.card()
    world.configure(max_agents=max_agents)
    lease = world.claim(world.a)
    reply = world.orch_start(world.a, lease, card, "s1")
    payload = None
    if state != "queued":
        payload = world.lease()
        assert payload["job_id"] == reply["job"]["job_id"]
    if state in ("starting", "uncertain", "started"):
        status, body = world.machine_call("starting", {"lease_token": payload["lease_token"]})
        assert status == 200, body
    if state in ("uncertain", "started", "failed"):
        body = {"lease_token": payload["lease_token"], "outcome": state}
        if state == "started":
            body["thread_id"] = "synthetic-thread"
        elif state == "failed":
            body["code"] = "t3_missing"
        status, result = world.machine_call("result", body)
        assert status == 200, result
    if state == "cancelled":
        jobs.cancel(world.js, reply["job"]["job_id"], world.owner)
    assert world.jobs_for(card)[0]["state"] == state
    return card, other, lease, payload


def test_voided_runner_start_rejects_old_key_then_allows_new_key(void_world):
    w = void_world
    card, _, lease, _ = prepare_void_job(w)
    old = w.jobs_for(card)[0]
    step = void_start(w)
    code, result = start_result(w.orch_start, w.a, lease, card, "s1")
    assert code == "step_voided"
    assert result.status == 409 and result.details["step"] == step
    assert step["job_id"] == old["id"]
    fresh = w.orch_start(w.a, lease, card, "s2")
    assert fresh["status"] == 201 and fresh["job"]["job_id"] != old["id"]
    again = w.orch_start(w.a, lease, card, "s2")
    assert (again["status"], again["job"]["job_id"]) == (200, fresh["job"]["job_id"])
    assert [s["step_key"] for s in w.steps()] == ["s1", "s2"]
    assert len(w.jobs_for(card)) == 2


def test_voided_runner_start_refusal_precedes_changed_request(void_world):
    w = void_world
    card, other, lease, _ = prepare_void_job(w)
    step = void_start(w)
    before = (w.steps(), w.all_jobs())
    code, result = start_result(w.orch_start, w.a, lease, other, "s1")
    assert code == "step_voided"
    assert result.status == 409 and result.details["step"] == step
    assert (w.steps(), w.all_jobs()) == before
    assert step["card"] == card


@pytest.mark.parametrize("state", ["queued", "leased"])
@pytest.mark.parametrize("actor", ["person", "local"])
def test_void_cancels_unstarted_runner_job(void_world, state, actor):
    import dataclasses

    w = void_world
    card, _, _, payload = prepare_void_job(w, state)
    principal = (
        w.owner
        if actor == "person"
        else dataclasses.replace(
            w.owner, person=False, auth="local", connection_id="", connection="cli"
        )
    )
    before = w.jobs_for(card)[0]
    step = void_start(w, principal)
    job = w.jobs_for(card)[0]
    assert job["state"] == "cancelled"
    assert job["cancelled_by"] == step["voided_by"] == durable._actor_ref(principal)
    assert job["cancel_requested_at"] == step["voided_at"]
    assert job["request_hash"] == before["request_hash"]
    assert step["job_id"] == job["id"] and job["step_key"] == "s1"
    assert void_start(w, principal) == step
    assert w.jobs_for(card)[0] == job
    # (a) lease -> void -> starting: the saved token is fenced before spawn.
    if payload:
        status, body = w.machine_call("starting", {"lease_token": payload["lease_token"]})
        assert status == 409 and "start is not leased" in str(body)
    # (c) void -> lease: there is no queued job left to launch.
    assert jobs.lease(w.js, w.machine()) is None


@pytest.mark.parametrize("state", ["starting", "uncertain", "started", "failed", "cancelled"])
def test_runner_start_void_preserves_job_and_is_idempotent(void_world, monkeypatch, state):
    w = void_world
    card, _, lease, _ = prepare_void_job(w, state)
    durable.journal_record(
        w.ctx(w.a),
        BOARD,
        ROLE,
        lease["epoch"],
        lease["token"],
        "s1",
        "in_doubt",
        evidence={"type": "holder_attested", "note": "Synthetic launch evidence."},
    )
    before_job, before_step = w.jobs_for(card)[0], w.steps()[0]

    def forbidden(*args, **kwargs):
        pytest.fail("void must not cancel or resolve a job past the launch boundary")

    monkeypatch.setattr(jobs, "cancel", forbidden)
    monkeypatch.setattr(jobs, "resolve", forbidden)
    # (b) starting -> void: the raw job remains untouched.
    first = void_start(w)
    second = void_start(w, now=jobs._after(first["voided_at"], 1))
    assert first == second and w.jobs_for(card)[0] == before_job
    after = w.steps()[0]
    for key in ("state", "code", "evidence", "job_id", "request_hash", "created_at", "epoch"):
        assert after[key] == before_step[key]
    assert len(w.rows("SELECT * FROM orch_journal WHERE event='void'")) == 1
    checkpoint = durable.checkpoint(w.ctx(w.owner), BOARD, ROLE)
    assert [(j["id"], j["state"]) for j in checkpoint["jobs"]] == [(before_job["id"], state)]
    assert all(not checkpoint[k] for k in ("open", "in_doubt", "needs_person", "finished"))
    code, result = start_result(w.orch_start, w.a, lease, card, "s2")
    expected = {
        "starting": "active_start",
        "uncertain": "active_start",
        "started": "started_recently",
    }.get(state)
    assert code == expected
    if expected:
        assert result.status == 409 and len(w.steps()) == 1
    else:
        assert result["status"] == 201
    if state == "started":
        owner_lease = w.claim(w.owner, force=True)
        code, result = start_result(w.orch_start, w.owner, owner_lease, card, "s3")
        assert code == "started_recently" and result.status == 409
        assert w.jobs_for(card)[0] == before_job


@pytest.mark.parametrize("state", ["started", "queued", "leased"])
def test_voided_job_keeps_its_cap_slot(void_world, state):
    w = void_world
    card, other, lease, _ = prepare_void_job(w, state, max_agents=1)
    before = w.jobs_for(card)[0]
    assert w.board_row()["slug"] == BOARD
    with BoardStore(w.gw.workspace_dir(w.ws)).connection() as conn:
        row = conn.execute("SELECT status,assignee FROM tasks WHERE id=?", (card,)).fetchone()
        assert tuple(row) == ("todo", None)
    void_start(w)
    code, result = start_result(w.orch_start, w.a, lease, other, "s2")
    if state == "started":
        assert code == ("project_limit" if w.orchestration_on else "at_cap")
        assert result.status == 409
        assert w.jobs_for(card)[0] == before and w.jobs_for(other) == []
        assert [s["step_key"] for s in w.steps()] == ["s1"]
    else:
        assert code is None
        assert result["status"] == 201 and w.jobs_for(card)[0]["state"] == "cancelled"


@pytest.mark.parametrize("actor", ["trusted", "owner-pat", "child"])
def test_runner_start_void_is_owner_only(void_world, monkeypatch, actor):
    import dataclasses

    w = void_world
    prepare_void_job(w)
    principal = {
        "trusted": w.a,
        "owner-pat": dataclasses.replace(w.a, role="owner"),
        "child": dataclasses.replace(w.owner, runner=w.machine_id),
    }[actor]
    before = (w.steps(), w.all_jobs(), w.rows("SELECT * FROM orch_journal"))

    def forbidden(*args, **kwargs):
        pytest.fail("unauthorized void reached cancel")

    monkeypatch.setattr(jobs, "cancel", forbidden)
    with pytest.raises(orch_store.OrchError) as info:
        void_start(w, principal)
    assert info.value.code == "owner_only" and info.value.status == 403
    assert (w.steps(), w.all_jobs(), w.rows("SELECT * FROM orch_journal")) == before


@pytest.mark.parametrize("condition", ["cap", "fenced", "restore", "plan", "requester", "machine"])
def test_new_key_after_void_keeps_admission_checks(void_world, condition):
    w = void_world
    card, other, lease, _ = prepare_void_job(w, "cancelled", max_agents=1)
    void_start(w)
    principal = w.a
    expected = condition
    if condition == "cap":
        w.orch_start(w.a, lease, other, "occupant")
        expected = "project_limit" if w.orchestration_on else "at_cap"
    elif condition == "fenced":
        w.claim(w.owner, force=True)
    elif condition == "restore":
        w.flag_restore()
        expected = "restore_pending"
    elif condition == "plan":
        w.update(card, labels=["plan"])
        expected = "plan_pending"
    elif condition == "requester":
        principal = w.unlisted
        expected = "not_allowed"
    elif condition == "machine":
        registry.revoke(w.db, w.owner, w.machine_id)
        expected = "machine_unavailable"
    before = w.steps()
    code, result = start_result(w.orch_start, principal, lease, card, "s2")
    assert code == expected
    assert result.status in (403, 409) and w.steps() == before
    assert not any(j["step_key"] == "s2" for j in w.all_jobs())


def test_voided_gate_key_keeps_step_conflict(void_world, monkeypatch):
    w = void_world
    card = w.card()
    w.configure()
    lease = w.claim(w.a)
    durable.gate_open(
        w.ctx(w.a),
        BOARD,
        ROLE,
        lease["epoch"],
        lease["token"],
        "s1",
        repo="example/repo",
        pr=1,
        head="a" * 40,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("gate void must not cancel jobs")

    monkeypatch.setattr(jobs, "cancel", forbidden)
    step = void_start(w)
    code, result = start_result(w.orch_start, w.a, lease, card, "s1")
    assert code == "step_conflict" and result.status == 409
    assert result.details["step"] == step and w.all_jobs() == []


@pytest.mark.parametrize("state", ["queued", "leased"])
def test_void_cancel_and_marker_roll_back_together(void_world, monkeypatch, state):
    w = void_world
    prepare_void_job(w, state)
    before = (w.steps(), w.all_jobs(), w.rows("SELECT * FROM orch_journal"))
    real_log = durable._log

    def fail_log(*args, **kwargs):
        if args[5] == "void":
            raise RuntimeError("synthetic post-cancel failure")
        return real_log(*args, **kwargs)

    monkeypatch.setattr(durable, "_log", fail_log)
    with pytest.raises(RuntimeError, match="synthetic post-cancel failure"):
        void_start(w)
    assert (w.steps(), w.all_jobs(), w.rows("SELECT * FROM orch_journal")) == before
