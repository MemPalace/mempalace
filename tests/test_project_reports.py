"""Durable project reports: public transition witnesses and deterministic fault proofs."""

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from commonplace import events, sessions, webhooks
from commonplace.boards.service import BoardService
from commonplace.control_db import ControlDB
from commonplace.extensions import ToolContext, ToolError
from commonplace.gateway import Gateway
from commonplace.projects import connect, orchestration
from commonplace.workers import WorkerSupervisor


@pytest.mark.parametrize("supervisor_kind", ["stub", "forbidden", "worker"])
def test_gateway_install_does_not_resolve_or_create_workspaces(
    tmp_path, monkeypatch, supervisor_kind
):
    monkeypatch.setattr(webhooks.Dispatcher, "start", lambda self: webhooks.ensure_schema(self.db))
    monkeypatch.setenv("COMMONPLACE_CHAT_WORKER", "off")
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    uid = db.create_user("owner@example.com", is_admin=True)
    db.create_workspace("unopened", uid, personal=True)
    supervisor = SimpleNamespace(root=tmp_path / "workspaces")
    if supervisor_kind == "forbidden":

        def forbidden(ws):
            raise AssertionError("workspace resolution during construction")

        supervisor.workspace_dir = forbidden
    elif supervisor_kind == "worker":
        supervisor = WorkerSupervisor(str(tmp_path), launcher=None)
    failure = None
    try:
        try:
            Gateway(db, supervisor)
        except Exception as exc:
            failure = exc
        assert failure is None, f"Gateway construction failed: {type(failure).__name__}"
        assert not (tmp_path / "workspaces").exists()
    finally:
        if supervisor_kind == "worker":
            supervisor.stop_all()
        db.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    # Use production install and explicit passes, with no timing-sensitive daemon.
    def start(dispatcher):
        webhooks.ensure_schema(dispatcher.db)
        return dispatcher

    monkeypatch.setattr(webhooks.Dispatcher, "start", start)
    monkeypatch.setenv("COMMONPLACE_CHAT_WORKER", "off")
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    supervisor = WorkerSupervisor(str(tmp_path), launcher=None)
    gw = Gateway(db, supervisor)
    uid = db.create_user("owner@example.com", is_admin=True)
    ws = db.create_workspace("reports", uid, personal=True)
    owner = sessions.person_principal(db, uid, ws)
    service = BoardService(ToolContext(principal=owner, gateway=gw))
    service.board_add(slug="work", wing="work")
    orchestration.set_enabled(db, owner, True)
    status, setup = connect.connect(gw, owner, "work")
    assert status in (200, 201)
    seat = db.authenticate_token(setup["token"])
    assert seat is not None
    clock = [2_000_000_000.0]
    monkeypatch.setattr(webhooks.time, "time", lambda: clock[0])
    posts = []

    def post(url, body, *args):
        assert not db._lock._is_owned(), "control lock during POST"
        posts.append((url, json.loads(body)))
        return 204

    monkeypatch.setattr(webhooks, "_post", post)
    hook = webhooks.add(db, seat, "http://127.0.0.1/report", kinds=["project.report"])
    w = SimpleNamespace(
        db=db,
        gw=gw,
        ws=ws,
        uid=uid,
        owner=owner,
        service=service,
        seat=seat,
        setup=setup,
        hook=hook,
        posts=posts,
        clock=clock,
    )
    try:
        yield w
    finally:
        supervisor.stop_all()
        db.close()


def pump(w, *, send=True):
    # Base adapter uses its existing pass. New entry points are never required
    # before the concrete report-POST witness has failed on the base.
    report_pump = getattr(w.gw, "project_reports", None)
    if report_pump is not None:
        report_pump.pass_once()
    w.gw.webhooks.reconcile()
    if send:
        for _ in range(1000):
            row = w.gw.webhooks._due()
            if row is None:
                break
            w.gw.webhooks._deliver(row)
        else:
            pytest.fail("delivery pass did not converge")


def reports(w, reason=None):
    return [
        p
        for _, p in w.posts
        if p["kind"] == "project.report" and (reason is None or p["data"]["reason"] == reason)
    ]


def sql(w, statement, args=()):
    with w.service.store.transaction() as conn:
        return [dict(r) for r in conn.execute(statement, args).fetchall()]


def control(w, statement, args=()):
    with w.db._lock:
        return [dict(r) for r in w.db._conn.execute(statement, args).fetchall()]


def card(w, **kw):
    result = w.service.task_add("Private card words", board="work", **kw)
    assert result["number"] > 0
    sql(
        w,
        "UPDATE tasks SET status='doing',assignee=?,heartbeat_at=? WHERE id=?",
        (w.seat.actor, iso(w.clock[0]), result["number"]),
    )
    return result["number"]


def doing(w):
    n = card(w)
    sql(
        w,
        "UPDATE tasks SET status='doing',assignee=?,claim_until=NULL,heartbeat_at=? WHERE id=?",
        (w.seat.actor, iso(w.clock[0]), n),
    )
    return n


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stale(w, n):
    sql(
        w,
        "UPDATE tasks SET claim_until=NULL,heartbeat_at=? WHERE id=?",
        (iso(w.clock[0] - 1801), n),
    )


def witness(w):
    n = doing(w)
    pump(w)
    result = w.service.task_update(n, status="done")
    assert result["status"] == "done"
    pump(w)
    report_posts = len(reports(w))
    assert report_posts == 1
    return n


def new_delivery(w):
    n = card(w)
    w.service.task_update(n, status="done")
    pump(w, send=False)
    rows = control(w, "SELECT * FROM webhook_deliveries WHERE status='pending' ORDER BY rowid DESC")
    assert rows
    return rows[0]


def restart(w):
    from commonplace.projects.reports import ReportPump

    w.gw.project_reports = ReportPump(w.gw)
    w.gw.webhooks = webhooks.Dispatcher(w.db, gateway=w.gw)


def test_dispatch_recovers_existing_source_without_creating_workspaces(world, monkeypatch, caplog):
    w = world
    n = card(w)
    assert w.service.task_update(n, status="done")["status"] == "done"
    sql(w, "DROP TABLE project_report_scan")
    unused_ws = w.db.create_workspace("unopened", w.uid)
    broken_name = "private-unavailable-workspace"
    broken_ws = w.db.create_workspace(broken_name, w.uid)
    supervisor = w.gw.supervisor
    resolve = supervisor.workspace_dir
    unused_path = Path(resolve(unused_ws))
    assert not unused_path.exists()

    def forbidden(ws):
        raise AssertionError("workspace resolution during construction")

    monkeypatch.setattr(supervisor, "workspace_dir", forbidden)
    failure = None
    try:
        gateway = Gateway(w.db, supervisor)
    except Exception as exc:
        failure = exc
    assert failure is None, f"Gateway construction failed: {type(failure).__name__}"
    assert not sql(w, "SELECT name FROM sqlite_master WHERE name='project_report_scan'")

    def resolve_existing(ws):
        if ws == broken_ws:
            raise RuntimeError(
                f"unavailable workspace {broken_ws}: {broken_name}; Private card words"
            )
        return resolve(ws)

    monkeypatch.setattr(supervisor, "workspace_dir", resolve_existing)
    monkeypatch.setattr(gateway, "workspace_dir", forbidden)
    w.gw = gateway
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="commonplace.projects.reports"):
        w.gw.webhooks.scan_reports()
    failures = [r for r in caplog.records if "project report pass failed" in r.getMessage()]
    assert len(failures) == 1
    assert failures[0].name == "commonplace.projects.reports"
    assert "RuntimeError" in failures[0].getMessage()
    for private in (
        "unavailable workspace",
        broken_ws,
        broken_name,
        w.ws,
        "Private card words",
    ):
        assert private not in caplog.text
    pump(w)
    assert len(reports(w, "done")) == 1
    assert sql(w, "SELECT * FROM project_report_scan")
    assert not unused_path.exists()


def test_dispatch_reinitializes_changed_source(world):
    w = world
    pump(w)
    sql(w, "DROP TABLE project_report_scan")
    pump(w)
    assert sql(w, "SELECT name FROM sqlite_master WHERE name='project_report_scan'")


def test_dispatch_initializes_source_once_across_normal_passes(world, monkeypatch):
    from commonplace.projects import reports as project_reports

    card(world)
    initialize = project_reports.ensure_source
    calls = []

    def counted(conn):
        calls.append(None)
        initialize(conn)

    monkeypatch.setattr(project_reports, "ensure_source", counted)
    for _ in range(3):
        pump(world)
    assert len(calls) == 1


def test_dispatch_does_not_lock_source_before_wal_initialization(world, monkeypatch):
    from commonplace.boards.store import BoardStore
    from commonplace.projects import reports as module

    w = world
    ws = w.db.create_workspace("new-source", w.uid)
    owner = sessions.person_principal(w.db, w.uid, ws)
    orchestration.set_enabled(w.db, owner, True)
    directory = Path(w.gw.supervisor.workspace_dir(ws))
    directory.mkdir(parents=True)
    path = directory / "boards.sqlite3"
    # BoardStore's connect creates an empty file before its WAL pragma runs.
    sqlite3.connect(path).close()
    monkeypatch.setattr(w.db, "workspaces", lambda: [{"id": ws}])
    rendezvous = Barrier(2)
    resume = Barrier(2)
    held = []
    write = module.source_write

    @contextmanager
    def held_write(*args):
        with write(*args) as conn:
            held.append((conn.in_transaction, "BEGIN IMMEDIATE"))
            rendezvous.wait(timeout=30)
            resume.wait(timeout=30)
            yield conn

    monkeypatch.setattr(module, "source_write", held_write)

    def scan():
        try:
            w.gw.project_reports.pass_once()
        finally:
            if not held:
                rendezvous.wait(timeout=30)
                resume.wait(timeout=30)

    failure = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        scanning = pool.submit(scan)
        rendezvous.wait(timeout=30)
        try:
            BoardStore(directory)
        except sqlite3.OperationalError as exc:
            failure = str(exc)
        finally:
            resume.wait(timeout=30)
        scanning.result(timeout=30)
    assert failure is None, f"BoardStore open failed: {failure}; pump lock: {held}"
    # Skipping is temporary: after the creator has finished, the same pump
    # must discover and initialize the source on its next pass.
    assert not w.gw.project_reports._initialized
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE project_report_scan")
    monkeypatch.setattr(module, "source_write", write)
    w.gw.project_reports.pass_once()
    assert any(key[0] == path.resolve() for key in w.gw.project_reports._initialized)


@pytest.mark.parametrize("existing_tables", [(), ("boards",), ("tasks",)])
def test_dispatch_defers_source_without_base_schema(world, monkeypatch, existing_tables):
    from commonplace.boards.store import BoardStore
    from commonplace.projects import reports as module

    w = world
    ws = w.db.create_workspace("partial-source", w.uid)
    directory = Path(w.gw.supervisor.workspace_dir(ws))
    directory.mkdir(parents=True)
    path = directory / "boards.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE creator_marker (id INTEGER)")
        for table in existing_tables:
            conn.execute(f"CREATE TABLE {table} (id INTEGER)")
    assert path.stat().st_size > 0
    monkeypatch.setattr(w.db, "workspaces", lambda: [{"id": ws}])
    write = module.source_write
    writes = []

    @contextmanager
    def counted_write(*args):
        writes.append(None)
        with write(*args) as conn:
            yield conn

    monkeypatch.setattr(module, "source_write", counted_write)
    w.gw.project_reports.pass_once()
    assert not writes, "pump opened a write transaction before the base schema existed"
    assert not w.gw.project_reports._initialized
    with sqlite3.connect(path) as conn:
        for table in existing_tables:
            conn.execute(f"DROP TABLE {table}")
    BoardStore(directory)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE project_report_scan")
    w.gw.project_reports.pass_once()
    assert writes
    assert any(key[0] == path.resolve() for key in w.gw.project_reports._initialized)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT cursor FROM project_report_scan").fetchone() == (0,)


def test_dispatch_isolates_publish_programming_error(world, monkeypatch, caplog):
    from commonplace.projects import reports as project_reports

    w = world
    other_ws = w.db.create_workspace("healthy-reports", w.uid)
    other_owner = sessions.person_principal(w.db, w.uid, other_ws)
    other_service = BoardService(ToolContext(principal=other_owner, gateway=w.gw))
    other_service.board_add(slug="healthy", wing="healthy")
    orchestration.set_enabled(w.db, other_owner, True)
    status, setup = connect.connect(w.gw, other_owner, "healthy")
    assert status == 201
    other_seat = w.db.authenticate_token(setup["token"])
    webhooks.add(w.db, other_seat, "http://127.0.0.1/healthy", kinds=["project.report"])
    n = other_service.task_add("Healthy source", board="healthy")["number"]
    with other_service.store.transaction() as conn:
        conn.execute("UPDATE tasks SET status='doing',assignee=? WHERE id=?", (other_seat.actor, n))
    other_service.task_update(n, status="done")
    workspaces = {row["id"]: row for row in w.db.workspaces()}
    monkeypatch.setattr(w.db, "workspaces", lambda: [workspaces[w.ws], workspaces[other_ws]])
    publish = project_reports.publish
    attempted = []

    def publish_one_workspace_error(gateway, ws):
        attempted.append(ws)
        if ws == w.ws:
            raise sqlite3.ProgrammingError("Incorrect number of bindings supplied.")
        return publish(gateway, ws)

    monkeypatch.setattr(project_reports, "publish", publish_one_workspace_error)
    caplog.clear()
    failure = None
    with caplog.at_level(logging.ERROR, logger="commonplace.projects.reports"):
        try:
            w.gw.project_reports.pass_once()
        except Exception as exc:
            failure = type(exc).__name__
    assert failure is None, f"workspace error escaped: {failure}"
    assert attempted == [w.ws, other_ws]
    failures = [r for r in caplog.records if "project report pass failed" in r.getMessage()]
    assert len(failures) == 1
    assert failures[0].name == "commonplace.projects.reports"
    assert "ProgrammingError" in failures[0].getMessage()
    assert "Incorrect number of bindings supplied" not in caplog.text
    # Deliver without running another report pass, so this pass must have published it.
    w.gw.webhooks.reconcile()
    for _ in range(1000):
        row = w.gw.webhooks._due()
        if row is None:
            break
        w.gw.webhooks._deliver(row)
    else:
        pytest.fail("delivery pass did not converge")
    assert len([p for url, p in w.posts if url.endswith("/healthy")]) == 1


@pytest.mark.parametrize("writer", ["task_update", "decide", "seed_card"])
def test_done_writers(world, writer):
    w = world
    n = card(w)
    if writer == "task_update":
        assert w.service.task_update(n, status="done")["status"] == "done"
    elif writer == "decide":
        sql(
            w,
            "UPDATE tasks SET status='review',ask='Answer',ask_to=? WHERE id=?",
            (w.owner.actor, n),
        )
        assert w.service.decide(n, reply="Finished", then="done")["status"] == "done"
    else:
        assert (
            w.service.seed_card(board="work", title="Imported", status="done")["status"] == "done"
        )
    pump(w)
    report_posts = len(reports(w))
    assert report_posts == 1
    # Done edits, repeated imports and direct done INSERTs do not transition.
    w.service.task_update(n, title="Annotation")
    with pytest.raises(ToolError, match="new cards must start"):
        w.service.task_add("Already complete", board="work", status="done")
    if writer == "seed_card":
        w.service.seed_card(board="work", title="Imported", status="done")
    m = card(w)
    with pytest.raises(RuntimeError):
        with w.service.transaction() as conn:
            from commonplace.boards.store import task_row

            w.service._set(conn, task_row(conn, m), {"status": "done"})
            raise RuntimeError("rollback")
    pump(w)
    assert len(reports(w)) == 1
    w.service.task_update(m, status="done")
    pump(w)
    assert len(reports(w)) == 2


def test_seed_done_new_board_history_only(world):
    w = world
    witness(w)
    result = w.service.seed_card(board="new-project", title="Historical", status="done")
    assert result["status"] == "done"
    pump(w)
    raw = [
        e
        for e in events.after(w.db, w.ws, limit=1000)
        if e["kind"] == "card.moved" and e["data"]["board"] == "new-project"
    ]
    assert len(raw) == 1
    assert sql(w, "SELECT * FROM boards WHERE slug='new-project'")
    assert len(reports(w)) == 1
    from commonplace.boards.store import task_row

    n = card(w)
    with pytest.raises(orchestration.SettingsUnavailable):
        with w.service.transaction() as conn:
            conn.execute("UPDATE boards SET coordinator=NULL WHERE slug='work'")
            w.service._set(conn, task_row(conn, n), {"status": "done"})
    assert sql(w, "SELECT status FROM tasks WHERE id=?", (n,))[0]["status"] == "doing"
    with pytest.raises(orchestration.SettingsUnavailable):
        with w.service.transaction() as conn:
            w.service._report_snapshot.boards.pop("work")
            w.service._set(conn, task_row(conn, n), {"status": "done"})
    control(w, "DELETE FROM project_orchestrators WHERE workspace_id=? AND board='work'", (w.ws,))
    assert w.service.task_update(n, status="done")["status"] == "done"
    pump(w)
    assert len(reports(w)) == 1


@pytest.mark.parametrize("writer", ["task_update", "decide", "seed_card"])
def test_settings_unavailable_rejects_done(world, monkeypatch, writer):
    w = world
    n = card(w)
    if writer == "decide":
        sql(
            w,
            "UPDATE tasks SET status='review',ask='Answer',ask_to=? WHERE id=?",
            (w.owner.actor, n),
        )
    before = {
        table: sql(w, f"SELECT * FROM {table}")
        for table in ("tasks", "task_events", "task_links", "boards")
    }
    before_events = events.after(w.db, w.ws, limit=1000)
    drawers = []
    monkeypatch.setattr(
        ToolContext,
        "memory",
        lambda self, name, args: drawers.append((name, args)) or {"drawer_id": "lesson-one"},
    )
    monkeypatch.setattr(BoardService, "_memory_theme", lambda self, ref: ("work", "Lesson"))
    original = orchestration.enabled

    def unavailable(*args):
        raise orchestration.SettingsUnavailable()

    monkeypatch.setattr(orchestration, "enabled", unavailable)
    retryable_error = False
    try:
        if writer == "task_update":
            w.service.task_update(n, status="done", lesson="Learned verbatim")
        elif writer == "decide":
            w.service.decide(n, reply="Finished", then="done")
        else:
            w.service.seed_card(board="work", title="Imported", status="done")
    except orchestration.SettingsUnavailable as error:
        retryable_error = error.status == 503 and error.code == "settings_unavailable"
    assert retryable_error is True
    assert {t: sql(w, f"SELECT * FROM {t}") for t in before} == before
    assert events.after(w.db, w.ws, limit=1000) == before_events
    assert drawers == []
    assert w.service.task_update(n, title="Ordinary edit")["title"] == "Ordinary edit"
    assert w.service.seed_card(board="work", title="Ordinary seed")["status"] == "todo"
    monkeypatch.setattr(orchestration, "enabled", original)
    if writer == "task_update":
        w.service.task_update(n, status="done", lesson="Learned verbatim")
        assert len(drawers) == 1
    elif writer == "decide":
        w.service.decide(n, reply="Finished", then="done")
    else:
        w.service.seed_card(board="work", title="Imported", status="done")
    pump(w)
    assert len(reports(w)) == 1


def test_idle_stuck_and_episode_resets(world):
    w = world
    n = doing(w)
    pump(w)
    w.clock[0] += 1802
    pump(w)
    stuck_posts = len(reports(w, "stuck"))
    assert stuck_posts == 1
    restart(w)
    pump(w)
    assert len(reports(w, "stuck")) == 1
    # Same timestamps, no fresh scan between resets: revision distinguishes them.
    for _ in range(3):
        sql(w, "UPDATE tasks SET heartbeat_at=? WHERE id=?", (iso(w.clock[0]), n))
        stale(w, n)
        pump(w)
    assert len(reports(w, "stuck")) == 4
    sql(w, "UPDATE tasks SET status='todo' WHERE id=?", (n,))
    sql(w, "UPDATE tasks SET status='doing' WHERE id=?", (n,))
    restart(w)
    pump(w)
    assert len(reports(w, "stuck")) == 5
    for values in (
        ("review", w.seat.actor, "Help", None),
        ("doing", w.owner.actor, None, None),
        ("doing", w.seat.actor, None, w.owner.actor),
    ):
        sql(w, "UPDATE tasks SET status=?,assignee=?,ask=?,ask_to=? WHERE id=?", (*values, n))
        pump(w)
    assert len(reports(w, "stuck")) == 5


def test_annotations_do_not_reset_episode(world):
    w = world
    n = witness(w)
    n = doing(w)
    pump(w)
    stale(w, n)
    pump(w)
    old = sql(w, "SELECT report_reset_rev FROM tasks WHERE id=?", (n,))[0]
    assert w.service.task_comment(n, "Non-holder annotation")
    sql(
        w,
        "UPDATE tasks SET flags='[]',updated_at=?,version=version+1,claim_expired_at=? WHERE id=?",
        (iso(w.clock[0]), iso(w.clock[0]), n),
    )
    pump(w)
    assert len(reports(w, "stuck")) == 1
    assert sql(w, "SELECT report_reset_rev FROM tasks WHERE id=?", (n,))[0] == old
    with w.service.store.transaction() as conn:
        conn.execute("PRAGMA recursive_triggers=ON")
        conn.execute("UPDATE tasks SET heartbeat_at=NULL,claim_until=NULL WHERE id=?", (n,))
    pump(w)
    sql(w, "UPDATE tasks SET updated_at=? WHERE id=?", (iso(w.clock[0] - 1900), n))
    pump(w)
    assert len(reports(w, "stuck")) == 2


def test_activation_and_raw_boundaries(world, monkeypatch):
    w = world
    witness(w)
    webhooks.add(w.db, w.seat, "http://127.0.0.1/raw", kinds=["card.moved"])
    orchestration.set_enabled(w.db, w.owner, False)
    from commonplace.boards.store import task_row

    n = card(w)
    with w.service.transaction() as conn:
        orchestration.set_enabled(w.db, w.owner, True)
        w.service._set(conn, task_row(conn, n), {"status": "done"})
        w.service._emit(conn, n, "card.moved", to="done")
    pump(w)
    assert len(reports(w)) == 1
    assert len([p for url, p in w.posts if url.endswith("/raw")]) == 1
    n = card(w)
    w.service.task_update(n, status="done")
    orchestration.set_enabled(w.db, w.owner, False)
    pump(w)
    assert len(reports(w)) == 1
    assert len([p for url, p in w.posts if url.endswith("/raw")]) == 2
    n = doing(w)
    stale(w, n)
    orchestration.set_enabled(w.db, w.owner, True)
    assert connect.connect(w.gw, w.owner, "work")[0] == 200
    pump(w)
    assert len(reports(w, "stuck")) == 0
    sql(w, "UPDATE tasks SET heartbeat_at=? WHERE id=?", (iso(w.clock[0]), n))
    stale(w, n)
    pump(w)
    assert len(reports(w, "stuck")) == 1


def test_connect_setup_and_legacy_card_star(world):
    w = world
    recommended_events = w.setup["setup"]["webhook_add"]["events"]
    # The original base returned a comma string; both bases must reach the
    # missing-report assertion before enforcing the reconciled list contract.
    assert "project.report" in recommended_events
    assert recommended_events == ["space_message", "card.moved", "card.created", "project.report"]
    witness(w)
    webhooks.remove(w.db, w.seat, w.hook["id"])
    raw = webhooks.add(w.db, w.seat, "http://127.0.0.1/raw", kinds=["card.*"])
    n = card(w)
    w.service.task_update(n, status="done")
    pump(w)
    assert len(reports(w)) == 1  # no replay through a later setup
    assert len([p for _, p in w.posts if p["kind"] == "card.moved"]) == 1
    status, _ = connect.connect(w.gw, w.owner, "work")
    assert status == 200
    webhooks.remove(w.db, w.seat, raw["id"])
    webhooks.add(w.db, w.seat, "http://127.0.0.1/both", kinds=["card.moved", "project.report"])
    n = card(w)
    w.service.task_update(n, status="done")
    pump(w)
    assert len([p for url, p in w.posts if url.endswith("/both")]) == 2
    from commonplace.projects.types import ProjectDenied

    with pytest.raises(ProjectDenied):
        connect.connect(w.gw, w.seat, "work")


def test_reserved_routing_and_binding_lifecycle(world):
    w = world
    witness(w)
    token = w.db.create_token(
        workspace_ref=w.ws,
        user_ref=w.uid,
        actor=w.uid,
        name="other-seat",
        client="test",
        policy=w.seat.policy,
    )[1]
    stranger = w.db.authenticate_token(token)
    webhooks.add(w.db, stranger, "http://127.0.0.1/stranger", kinds=["*"])
    webhooks.add(w.db, stranger, "http://127.0.0.1/explicit", kinds=["project.report"])
    n = card(w)
    w.service.task_update(n, status="done")
    pump(w)
    assert len(reports(w)) == 2
    assert not [
        p
        for url, p in w.posts
        if url.endswith(("/stranger", "/explicit")) and p["kind"] == "project.report"
    ]
    row = new_delivery(w)
    connect.connect(w.gw, w.owner, "work")  # fresh binding even same identity
    w.gw.webhooks._deliver(row)
    assert (
        control(w, "SELECT status FROM webhook_deliveries WHERE id=?", (row["id"],))[0]["status"]
        == "cancelled"
    )
    row = new_delivery(w)
    connect.trust(w.gw, w.owner, "work", {"trusted": False})
    w.gw.webhooks._deliver(row)
    assert (
        control(w, "SELECT status FROM webhook_deliveries WHERE id=?", (row["id"],))[0]["status"]
        == "cancelled"
    )


def test_enqueue_deferral_raw_progress_and_backoff(world, monkeypatch):
    w = world
    witness(w)
    from commonplace.projects import report_dispatch

    webhooks.add(w.db, w.seat, "http://127.0.0.1/raw", kinds=["card.moved"])
    original = report_dispatch.control_authority

    def unavailable(*args):
        if w.db._conn.execute("SELECT 1 FROM project_report_dispatch_retry").fetchone():
            raise RuntimeError("private fault")
        return original(*args)

    monkeypatch.setattr(report_dispatch, "control_authority", unavailable)
    n = card(w)
    w.service.task_update(n, status="done")
    # Publish first, then append raw traffic AFTER the report in log order.
    w.gw.project_reports.pass_once()
    events.emit(w.db, w.ws, "card.moved", subject=f"#{n}", data={"board": "work", "number": n})
    other_ws = w.db.create_workspace("other-reports", w.uid)
    other_owner = sessions.person_principal(w.db, w.uid, other_ws)
    other_service = BoardService(ToolContext(principal=other_owner, gateway=w.gw))
    other_service.board_add(slug="other", wing="other")
    webhooks.add(w.db, other_owner, "http://127.0.0.1/other-raw", kinds=["card.moved"])
    events.emit(w.db, other_ws, "card.moved", subject="#1", data={"board": "other", "number": 1})
    pump(w)
    assert len([p for url, p in w.posts if url.endswith("/raw")]) == 2
    assert len([p for url, p in w.posts if url.endswith("/other-raw")]) == 1
    rows = control(w, "SELECT * FROM project_report_dispatch_retry")
    assert len(rows) == 1
    seq = rows[0]["event_seq"]
    cursor = int(control(w, "SELECT value FROM meta WHERE key='webhook_dispatch_seq'")[0]["value"])
    assert cursor >= seq
    for count, delay in enumerate((1, 2, 4, 8, 16, 30, 30), 1):
        if count > 1:
            pump(w)
        r = control(w, "SELECT * FROM project_report_dispatch_retry")[0]
        assert r["consecutive_failures"] == count
        assert r["next_at"] == w.clock[0] + delay
        restart(w)
        pump(w)
        assert control(w, "SELECT * FROM project_report_dispatch_retry")[0] == r
        w.clock[0] += delay
    # Retain envelope independently of the original event's retention.
    control(w, "DELETE FROM events WHERE seq=?", (seq,))
    late = webhooks.add(w.db, w.seat, "http://127.0.0.1/late", kinds=["project.report"])
    # Registration boundary remains the original seq even after retention.
    control(w, "UPDATE webhooks SET start_seq=? WHERE id=?", (seq, late["id"]))
    monkeypatch.setattr(report_dispatch, "control_authority", original)
    pump(w)
    assert not control(w, "SELECT * FROM project_report_dispatch_retry")
    assert len(reports(w)) == 2
    assert not [p for url, p in w.posts if url.endswith("/late")]
    dispatch_crashes(w, monkeypatch)


def dispatch_crashes(w, monkeypatch):
    from commonplace.projects import reports as module

    n = card(w)
    w.service.task_update(n, status="done")
    module.publish(w.gw, w.ws)
    seq = control(w, "SELECT max(seq) AS seq FROM events WHERE kind='project.report'")[0]["seq"]
    # Failure after inserting work but before cursor advancement must roll back
    # both. A cursor written outside that transaction loses the report here.
    cursor = control(w, "SELECT value FROM meta WHERE key='webhook_dispatch_seq'")[0]["value"]
    control(
        w,
        "CREATE TRIGGER crash_work BEFORE INSERT ON project_report_dispatch_retry "
        "BEGIN SELECT RAISE(ABORT,'simulated work crash'); END",
    )
    with pytest.raises(sqlite3.IntegrityError):
        w.gw.webhooks.reconcile()
    assert (
        int(control(w, "SELECT value FROM meta WHERE key='webhook_dispatch_seq'")[0]["value"]) < seq
    )
    control(w, "DROP TRIGGER crash_work")
    # Fail after enqueue and before work deletion: no partial deliveries persist.
    control(
        w,
        "CREATE TRIGGER crash_delete BEFORE DELETE ON project_report_dispatch_retry "
        "BEGIN SELECT RAISE(ABORT,'simulated routing crash'); END",
    )
    w.gw.webhooks.reconcile()
    assert not control(w, "SELECT * FROM webhook_deliveries WHERE event_seq=?", (seq,))
    assert control(w, "SELECT * FROM project_report_dispatch_retry WHERE event_seq=?", (seq,))
    control(w, "DROP TRIGGER crash_delete")
    w.clock[0] += 30
    restart(w)
    pump(w)
    deliveries = control(w, "SELECT * FROM webhook_deliveries WHERE event_seq=?", (seq,))
    assert len(deliveries) == 2  # original and late report hooks, once each
    assert all(d["status"] == "delivered" for d in deliveries)
    assert int(cursor) < seq


def test_send_retry_accounting_and_identity(world, monkeypatch):
    w = world
    witness(w)
    from commonplace.projects import report_dispatch

    original = report_dispatch.board_authority
    row = new_delivery(w)
    for count, error in enumerate(
        (
            sqlite3.OperationalError("database is locked"),
            sqlite3.OperationalError("database is busy"),
            RuntimeError("private fault"),
            sqlite3.DatabaseError("file is not a database"),
            orchestration.SettingsUnavailable(),
        )
        * 3,
        1,
    ):

        def unavailable(*args, error=error):
            raise error

        monkeypatch.setattr(report_dispatch, "board_authority", unavailable)
        w.gw.webhooks._deliver(row)
        current = control(w, "SELECT * FROM webhook_deliveries WHERE id=?", (row["id"],))[0]
        assert current["attempt"] == 0 and current["status"] == "retry"
        assert current["report_auth_retry_count"] == count
        assert current["next_at"] == w.clock[0] + min(2 ** min(count - 1, 5), 30)
        assert not control(w, "SELECT * FROM webhook_attempts WHERE delivery_id=?", (row["id"],))
        assert not [e for e in events.after(w.db, w.ws) if e["kind"] == "webhook.failed"]
        w.clock[0] = current["next_at"]
        row = current
    monkeypatch.setattr(report_dispatch, "board_authority", original)
    w.gw.webhooks._deliver(row)
    assert len(reports(w)) == 2
    assert (
        control(
            w, "SELECT report_auth_retry_count FROM webhook_deliveries WHERE id=?", (row["id"],)
        )[0]["report_auth_retry_count"]
        == 0
    )
    row = new_delivery(w)

    # A control identity change during the unlocked board read yields RETRY;
    # a subsequent stable read positively cancels the old binding.
    def rebind(*args):
        allowed = original(*args)
        with w.db._lock, w.db._tx():
            from commonplace.projects import reports as module

            module.bind_in(w.db, w.ws, "work", w.seat.connection_id)
        return allowed

    monkeypatch.setattr(report_dispatch, "board_authority", rebind)
    w.gw.webhooks._deliver(row)
    current = control(w, "SELECT * FROM webhook_deliveries WHERE id=?", (row["id"],))[0]
    assert current["status"] == "retry" and current["report_auth_retry_count"] == 1
    monkeypatch.setattr(report_dispatch, "board_authority", original)
    w.gw.webhooks._deliver(current)
    current = control(w, "SELECT * FROM webhook_deliveries WHERE id=?", (row["id"],))[0]
    assert (
        current["status"],
        current["report_auth_retry_count"],
        current["report_auth_retry_since"],
        current["error"],
    ) == ("cancelled", 0, None, "")
    row = new_delivery(w)
    w.service.board_set("work", None)
    w.gw.webhooks._deliver(row)
    assert (
        control(w, "SELECT status FROM webhook_deliveries WHERE id=?", (row["id"],))[0]["status"]
        == "cancelled"
    )


def auth_retry(w, monkeypatch):
    from commonplace.projects import report_dispatch

    row = new_delivery(w)
    original = report_dispatch.board_authority

    def unavailable(*args):
        raise RuntimeError("private marker never exposed")

    monkeypatch.setattr(report_dispatch, "board_authority", unavailable)
    for _ in range(9):
        w.gw.webhooks._deliver(row)
        w.clock[0] += 30
        restart(w)
        row = control(w, "SELECT * FROM webhook_deliveries WHERE id=?", (row["id"],))[0]
    return row, original


def test_auth_retry_delivery_inspection(world, monkeypatch, caplog):
    w = world
    witness(w)
    row, original = auth_retry(w, monkeypatch)
    public = next(
        r for r in webhooks.deliveries(w.db, w.seat, w.hook["id"]) if r["id"] == row["id"]
    )
    assert public["count"] == 9 and public["retry_since"]
    assert public["status"] == "retry" and public["error"] == "report_authorization_retry"
    assert public["attempt"] == 0 and public["attempts"] == []
    assert "private marker" not in json.dumps(public) + caplog.text + json.dumps(w.posts)
    from commonplace.projects import report_dispatch

    monkeypatch.setattr(report_dispatch, "board_authority", original)
    w.gw.webhooks._deliver(row)
    public = next(
        r for r in webhooks.deliveries(w.db, w.seat, w.hook["id"]) if r["id"] == row["id"]
    )
    assert (public["count"], public["retry_since"], public["error"]) == (0, None, "")
    row, _ = auth_retry(w, monkeypatch)
    webhooks.remove(w.db, w.seat, w.hook["id"])
    cleared = control(w, "SELECT * FROM webhook_deliveries WHERE id=?", (row["id"],))[0]
    assert (
        cleared["report_auth_retry_count"],
        cleared["report_auth_retry_since"],
        cleared["error"],
    ) == (0, None, "")


def test_auth_retry_inspection_beyond_latest_100(world, monkeypatch):
    w = world
    witness(w)
    row, _ = auth_retry(w, monkeypatch)
    for i in range(101):
        control(
            w,
            "INSERT INTO webhook_deliveries(id,webhook_id,event_seq,status,next_at,payload) VALUES (?,?,?,'delivered',0,?)",
            (f"new-{i}", w.hook["id"], 10000 + i, b"{}"),
        )
    restart(w)
    public = webhooks.deliveries(w.db, w.seat, w.hook["id"])
    found = next((r for r in public if r["id"] == row["id"]), None)
    assert found is not None
    assert (found["error"], found["count"], found["retry_since"]) == (
        "report_authorization_retry",
        9,
        row["report_auth_retry_since"],
    )
    assert len(public) == 101


def test_scan_budget_isolation_cleanup_recovery(world, monkeypatch):
    w = world
    witness(w)
    ids = [doing(w) for _ in range(105)]
    pump(w)
    pump(w)
    for n in ids:
        stale(w, n)
    pump(w)
    assert len(reports(w, "stuck")) <= 100
    pump(w)
    pump(w)
    assert len(reports(w, "stuck")) == 105
    for n in ids:
        w.service.task_update(n, status="done")
    pump(w)
    assert len(reports(w, "done")) == 101
    restart(w)
    pump(w)
    assert len(reports(w, "done")) == 106
    sql(w, "DROP TRIGGER task_events_no_delete")
    sql(w, "DROP TRIGGER tasks_no_delete")
    sql(w, "DELETE FROM task_events")
    sql(w, "DELETE FROM tasks")
    pump(w)
    assert len(sql(w, "SELECT * FROM project_report_episodes")) <= 6
    pump(w)
    assert not sql(w, "SELECT * FROM project_report_episodes")
    from commonplace.projects import reports as module

    original = module.snapshot

    def unavailable(*args):
        raise orchestration.SettingsUnavailable()

    monkeypatch.setattr(module, "snapshot", unavailable)
    before = sql(w, "SELECT * FROM project_report_scan")
    pump(w)
    assert sql(w, "SELECT * FROM project_report_scan") == before
    monkeypatch.setattr(module, "snapshot", original)
    other_ws = w.db.create_workspace("healthy-reports", w.uid)
    other_owner = sessions.person_principal(w.db, w.uid, other_ws)
    other_service = BoardService(ToolContext(principal=other_owner, gateway=w.gw))
    other_service.board_add(slug="healthy", wing="healthy")
    orchestration.set_enabled(w.db, other_owner, True)
    status, setup = connect.connect(w.gw, other_owner, "healthy")
    assert status == 201
    other_seat = w.db.authenticate_token(setup["token"])
    webhooks.add(w.db, other_seat, "http://127.0.0.1/healthy", kinds=["project.report"])
    n = other_service.task_add("Healthy source", board="healthy")["number"]
    with other_service.store.transaction() as conn:
        conn.execute("UPDATE tasks SET status='doing',assignee=? WHERE id=?", (other_seat.actor, n))
    other_service.task_update(n, status="done")

    def one_workspace_unavailable(gateway, ws):
        if ws == w.ws:
            raise orchestration.SettingsUnavailable()
        return original(gateway, ws)

    monkeypatch.setattr(module, "snapshot", one_workspace_unavailable)
    pump(w)
    assert len([p for url, p in w.posts if url.endswith("/healthy")]) == 1
    # Scan deadlines depend on monotonic time, not request or delivery counts.
    calls = []
    monkeypatch.setattr(w.gw.project_reports, "pass_once", lambda: calls.append(w.clock[0]))
    monkeypatch.setattr(webhooks.time, "monotonic", lambda: w.clock[0])
    w.gw.webhooks.scan_reports()
    for _ in range(20):
        w.gw.webhooks.scan_reports()
    assert len(calls) == 1
    w.clock[0] += 29
    w.gw.webhooks.scan_reports()
    assert len(calls) == 1
    w.clock[0] += 1
    w.gw.webhooks.scan_reports()
    assert len(calls) == 2


def test_publication_crashes_and_lock_order(world, monkeypatch):
    w = world
    witness(w)
    from commonplace.projects import reports as module

    n = card(w)
    w.service.task_update(n, status="done")
    original = module.ack

    def crash(*args):
        raise RuntimeError("ack interrupted")

    monkeypatch.setattr(module, "ack", crash)
    pump(w)
    assert len(reports(w)) == 2
    pump(w)
    assert len(reports(w)) == 2
    assert len(control(w, "SELECT * FROM project_report_receipts")) == 2
    monkeypatch.setattr(module, "ack", original)
    restart(w)
    pump(w)
    assert len(reports(w)) == 2
    assert not sql(w, "SELECT * FROM project_report_outbox")
    # A read-only probe surrounds all report-owned source access. Control
    # methods must never be called while it is active, in either direction.
    original_read = orchestration.board_read
    original_write = module.source_write
    source_depth = [0]
    actual_lock = w.db._lock

    class ProbeLock:
        def __getattr__(self, name):
            return getattr(actual_lock, name)

        def __enter__(self):
            assert source_depth[0] == 0, "source to control overlap"
            actual_lock.acquire()
            return self

        def __exit__(self, *args):
            actual_lock.release()

    @contextmanager
    def checked_read(*args):
        assert not w.db._lock._is_owned(), "control to source overlap"
        with original_read(*args) as conn:
            source_depth[0] += 1
            try:
                yield conn
            finally:
                source_depth[0] -= 1

    @contextmanager
    def checked_write(*args):
        assert not w.db._lock._is_owned(), "control to source overlap"
        with original_write(*args) as conn:
            source_depth[0] += 1
            try:
                yield conn
            finally:
                source_depth[0] -= 1

    monkeypatch.setattr(w.db, "_lock", ProbeLock())
    monkeypatch.setattr(orchestration, "board_read", checked_read)
    monkeypatch.setattr(module, "source_write", checked_write)
    witness_count = len(reports(w))
    n = card(w)
    w.service.task_update(n, status="done")
    pump(w)
    assert len(reports(w)) == witness_count + 1
    monkeypatch.setattr(w.db, "_lock", actual_lock)
    monkeypatch.setattr(module, "source_write", original_write)
    # Both publishers hold the same already-read source row before either
    # enters control; the receipt must provide exactly one stored event.
    n = card(w)
    w.service.task_update(n, status="done")
    barrier = Barrier(2)

    @contextmanager
    def simultaneous_read(*args):
        with original_read(*args) as conn:
            yield conn
        barrier.wait(timeout=10)

    monkeypatch.setattr(orchestration, "board_read", simultaneous_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(module.publish, w.gw, w.ws) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)
    monkeypatch.setattr(orchestration, "board_read", original_read)
    pump(w)
    assert len(reports(w)) == witness_count + 2
    assert not sql(w, "SELECT * FROM project_report_outbox")
    # Missing notification after receipt commit is recovered by reconcile.
    n = card(w)
    w.service.task_update(n, status="done")
    original_notify = events.notify

    def no_notify(*args):
        raise RuntimeError("notification lost")

    monkeypatch.setattr(events, "notify", no_notify)
    with pytest.raises(RuntimeError, match="notification lost"):
        module.publish(w.gw, w.ws)
    monkeypatch.setattr(events, "notify", original_notify)
    restart(w)
    pump(w)
    assert len(reports(w)) == witness_count + 3


def test_report_privacy_and_receiver_dedupe(world, monkeypatch):
    w = world
    witness(w)
    data = reports(w)[0]["data"]
    assert set(data) == {"board", "number", "report_id", "reason", "generation", "coordinator"}
    assert "Private card words" not in json.dumps(reports(w))
    row = new_delivery(w)
    outcomes = [503, 204]

    def post(url, body, *args):
        w.posts.append((url, json.loads(body)))
        return outcomes.pop(0)

    monkeypatch.setattr(webhooks, "_post", post)
    w.gw.webhooks._deliver(row)
    row = control(w, "SELECT * FROM webhook_deliveries WHERE id=?", (row["id"],))[0]
    w.clock[0] = row["next_at"]
    w.gw.webhooks._deliver(row)
    dupes = reports(w)[-2:]
    assert dupes[0] == dupes[1]
    assert len({p["data"]["report_id"] for p in dupes}) == 1
    event = {"workspace_id": w.ws, "kind": "space_replied", "data": {"board": "work"}}
    hook = control(w, "SELECT * FROM webhooks WHERE id=?", (w.hook["id"],))[0]
    hook["kinds"] = '["*"]'
    assert not webhooks._matches(hook, event)
