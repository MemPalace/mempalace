"""Adversarial transitions with real SQLite and independent loopback requests."""

import errno
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from commonplace import events as hub_events
from commonplace.control_db import ControlDB
from commonplace.gateway import Gateway
from commonplace.handoff import db as db_module, launchers, palace_mirror, transcripts
from commonplace.handoff.db import HandoffDB, HandoffDBError
from commonplace.handoff.models import (
    EVENT_COMPLETED,
    EVENT_FAILED,
    EVENT_LAUNCH_FAILED,
    HandoffRequest,
    LauncherConfig,
    SettingRow,
    SuccessorRef,
    ThreadMeta,
)
from commonplace.handoff.thread_store import ThreadConflict, ThreadStore, canonical_sha256
from commonplace.handoff.wiring import install
from commonplace.policy import Policy
from tests._mirror_drain import drain_service


class _NoWorker:
    def get(self, workspace):
        raise AssertionError("These tests must not start a Memory worker")


class Harness:
    def __init__(self, root):
        self.root = root
        self.control = ControlDB(str(root / "control.sqlite3"))
        self.owner = self.control.create_user("owner@example.test")
        self.workspace = self.control.create_workspace("home", self.owner)
        _, self.token = self.control.create_token(
            workspace_ref=self.workspace,
            user_ref=self.owner,
            name="reviewer",
            client="test",
            policy=Policy.build(
                profile="full",
                scopes=["memory:read", "threads:read", "handoff:write"],
                read_wings=["*"],
                trust="trusted",
            ),
            actor=self.owner,
        )
        self._open()
        self.configure(LauncherConfig())

    def _open(self):
        self.gateway = Gateway(self.control, _NoWorker())
        assert not hasattr(self.gateway, "handoff")
        self.ctx = install(self.gateway, self.root)
        assert install(self.gateway, self.root) is self.ctx is self.gateway.handoff
        self.ctx.service.call_tool = None
        self.server = self.gateway.build_server("127.0.0.1", 0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.principal = self.control.authenticate_token(self.token)

    def configure(self, launcher):
        self.ctx.handoff_db.put_setting(
            SettingRow(self.workspace, "workspace", "", enabled=True, launcher=launcher),
            actor=self.owner,
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.ctx.close()
        self.gateway.webhooks.stop()
        self.control.close()

    def restart(self):
        self.close()
        self.control = ControlDB(str(self.root / "control.sqlite3"))
        self._open()

    def request(self, path="/api/handoff", body=None, method="POST"):
        assert self.gateway.authenticate(f"Bearer {self.token}") is not None
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        try:
            connection.request(
                method,
                path,
                body=json.dumps(body).encode() if body is not None else None,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            )
            reply = connection.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            connection.close()

    def assert_event_count(self, event, count):
        audits = [row for row in self.control.audit_entries() if row["action"] == event]
        logged = [
            row for row in hub_events.after(self.control, self.workspace) if row["kind"] == event
        ]
        assert (len(audits), len(logged)) == (count, count), {"audit": audits, "hub": logged}

    def rows(self, table):
        connection = self.ctx.store_for(self.workspace)._conn
        return [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    # Admit the deliberate sixteen-request burst before testing idempotency.
    monkeypatch.setattr(ThreadingHTTPServer, "request_queue_size", 64)
    instance = Harness(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def request(source="source", *, parent="", texts=("Verbatim goal", "Verbatim status")):
    return HandoffRequest(
        ThreadMeta(source, parent_handoff_id=parent),
        json.dumps(
            [
                {"role": "user" if index == 0 else "assistant", "content": text}
                for index, text in enumerate(texts)
            ]
        ),
        transcript_format="messages",
    )


def prepared(harness, state="stored"):
    body = request()
    row, _ = harness.ctx.handoff_db.begin(
        harness.workspace, "thread:source", body.meta, connection_id=harness.principal.connection_id
    )
    saved = harness.ctx.store_for(harness.workspace).store(
        harness.workspace,
        "source",
        transcripts.parse(body.transcript, "messages"),
        actor=harness.principal.actor,
    )
    row = harness.ctx.handoff_db.mark_stored(row.id, saved.id, "Saved document")
    if state in ("claimed", "launched"):
        assert harness.ctx.handoff_db.claim_launch(row.id)
    if state == "launched":
        row = harness.ctx.handoff_db.mark_launched(row.id)
    return row


def test_sixteen_real_http_requests_store_and_launch_once(harness):
    barrier = threading.Barrier(16)
    body = asdict(request())

    def send(_):
        barrier.wait(timeout=10)
        return harness.request(body=body)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(send, range(16)))
    assert all(status == 200 for status, _ in results), results
    assert sum(result["launch"] for _, result in results) == 1
    assert len(harness.rows("threads")) == 1
    assert len(harness.ctx.handoff_db.list(harness.workspace)) == 1
    handoff_id = next(result["handoff"]["id"] for _, result in results if result["launch"])
    registration = {"agent_ref": "agent:successor", "thread_id": "successor"}
    with ThreadPoolExecutor(max_workers=16) as pool:
        registrations = list(
            pool.map(
                lambda _: harness.request(f"/api/handoffs/{handoff_id}/successor", registration),
                range(16),
            )
        )
    assert all(status == 200 for status, _ in registrations)
    assert harness.ctx.handoff_db.get(handoff_id).successor == SuccessorRef(
        "agent:successor", thread_id="successor"
    )
    harness.assert_event_count(EVENT_COMPLETED, 1)


def test_single_real_http_request(harness):
    status, result = harness.request(body=asdict(request()))
    assert status == 200, result
    assert result["launch"], result


@pytest.mark.parametrize("state", ["stored", "claimed", "launched"])
def test_restart_preserves_claim_and_explicit_recovery(harness, state):
    row = prepared(harness, state)
    harness.restart()
    result = harness.ctx.service.run(harness.principal, request())
    assert result.launch is (state == "stored")
    assert len(harness.rows("threads")) == 1
    if state != "stored":
        # A claim without registration is ambiguous. Never automatically spawn again.
        assert result.handoff.successor is None
        harness.ctx.service.report_launch_failure(harness.principal, row.id, "verified no launch")
        assert harness.ctx.service.relaunch(harness.principal, row.id).launch
        with pytest.raises(HandoffDBError):
            harness.ctx.service.relaunch(harness.principal, row.id)


def test_crash_after_thread_commit_before_mark_stored_can_resume(harness, monkeypatch):
    moment = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(db_module, "_now", lambda: moment)
    body = request()
    row, _ = harness.ctx.handoff_db.begin(
        harness.workspace, "thread:source", body.meta, connection_id=harness.principal.connection_id
    )
    saved = harness.ctx.store_for(harness.workspace).store(
        harness.workspace,
        "source",
        transcripts.parse(body.transcript, "messages"),
        actor=harness.principal.actor,
    )
    harness.restart()
    moment += timedelta(seconds=601)
    result = harness.ctx.service.run(harness.principal, body)
    assert result.handoff.id == row.id
    assert len(harness.rows("threads")) == 1
    assert harness.ctx.store_for(harness.workspace).get(saved.id) is not None
    assert result.launch
    harness.ctx.service.register_successor(harness.principal, row.id, SuccessorRef("recovered"))
    assert not harness.ctx.service.run(harness.principal, body).launch
    harness.assert_event_count(EVENT_COMPLETED, 1)


@pytest.mark.parametrize("age", [0, 599, 600])
def test_pending_inside_lease_is_untouched_after_restart(harness, monkeypatch, age):
    moment = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(db_module, "_now", lambda: moment)
    body = request()
    row, _ = harness.ctx.handoff_db.begin(
        harness.workspace, "thread:source", body.meta, connection_id=harness.principal.connection_id
    )
    harness.restart()
    moment += timedelta(seconds=age)
    result = harness.ctx.service.run(harness.principal, body)
    assert (result.action, result.reason, result.launch) == ("compact", "in progress", False)
    assert harness.ctx.handoff_db.get(row.id) == row
    assert not harness.rows("threads")


def test_only_one_connection_takes_over_expired_pending_lease(harness, monkeypatch):
    moment = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(db_module, "_now", lambda: moment)
    body = request()
    row, _ = harness.ctx.handoff_db.begin(
        harness.workspace, "thread:source", body.meta, connection_id=harness.principal.connection_id
    )
    connections = [ControlDB(str(harness.root / "control.sqlite3")) for _ in range(4)]
    try:
        databases = [HandoffDB(control) for control in connections]
        moment += timedelta(seconds=601)
        barrier = threading.Barrier(4)

        def begin(database):
            barrier.wait(timeout=5)
            return database.begin(
                harness.workspace,
                "thread:source",
                body.meta,
                connection_id=harness.principal.connection_id,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(begin, databases))
        assert sum(created for _, created in results) == 1
        assert all(result.id == row.id for result, _ in results)
    finally:
        for control in connections:
            control.close()


@pytest.mark.parametrize("old_failure", [False, True])
def test_paused_old_writer_cannot_mutate_replacement_pending_row(harness, monkeypatch, old_failure):
    moment = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(db_module, "_now", lambda: moment)
    other_control = ControlDB(str(harness.root / "control.sqlite3"))
    other = install(Gateway(other_control, _NoWorker()), harness.root)
    other.service.call_tool = None
    old_entered, old_release = threading.Event(), threading.Event()
    new_entered, new_release = threading.Event(), threading.Event()

    def pause(store, entered, release, fail):
        original = store.store

        def store_and_pause(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            assert release.wait(timeout=10)
            if fail:
                raise OSError(errno.ENOSPC, "old writer resumed into storage failure")
            return result

        monkeypatch.setattr(store, "store", store_and_pause)

    pause(harness.ctx.store_for(harness.workspace), old_entered, old_release, old_failure)
    pause(other.store_for(harness.workspace), new_entered, new_release, False)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(harness.ctx.service.run, harness.principal, request())
            try:
                assert old_entered.wait(timeout=10)
                moment += timedelta(seconds=601)
                new = pool.submit(other.service.run, harness.principal, request())
                assert new_entered.wait(timeout=10)
                row = other.handoff_db.by_key(harness.workspace, "thread:source")
                assert row.state == "pending"
                old_release.set()
                old_result = old.result(timeout=10)
                assert (old_result.action, old_result.launch) == ("compact", False)
                assert other.handoff_db.get(row.id) == row
                harness.assert_event_count(EVENT_FAILED, 0)
                new_release.set()
                new_result = new.result(timeout=10)
                assert new_result.launch
                other.service.register_successor(harness.principal, row.id, SuccessorRef("new"))
                assert len(harness.rows("threads")) == 1
                harness.assert_event_count(EVENT_COMPLETED, 1)
            finally:
                old_release.set()
                new_release.set()
    finally:
        other.close()
        other.gateway.webhooks.stop()
        other_control.close()


@pytest.mark.parametrize("operation", ["mark_stored", "mark_failed", "claim_launch"])
def test_replaced_lease_rejects_old_or_omitted_token(harness, monkeypatch, operation):
    moment = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(db_module, "_now", lambda: moment)
    database = harness.ctx.handoff_db
    row, _ = database.begin(
        harness.workspace,
        "fenced",
        ThreadMeta("fenced"),
        connection_id=harness.principal.connection_id,
        lease_token="old-owner",
    )
    moment += timedelta(seconds=601)
    replacement, created = database.begin(
        harness.workspace,
        "fenced",
        ThreadMeta("fenced"),
        connection_id=harness.principal.connection_id,
        lease_token="new-owner",
    )
    assert created
    arguments = {
        "mark_stored": ("thr_saved", "private quote"),
        "mark_failed": ("failed",),
        "claim_launch": (),
    }
    for token in ("old-owner", ""):
        with pytest.raises(HandoffDBError, match="replaced"):
            getattr(database, operation)(row.id, *arguments[operation], lease_token=token)
        assert database.get(row.id) == replacement


def test_independent_database_connections_claim_and_register_once(harness):
    row = prepared(harness)
    connections = [ControlDB(str(harness.root / "control.sqlite3")) for _ in range(4)]
    try:
        databases = [HandoffDB(control) for control in connections]
        barrier = threading.Barrier(4)

        def claim(database):
            barrier.wait(timeout=5)
            return database.claim_launch(row.id)

        with ThreadPoolExecutor(max_workers=4) as pool:
            assert sum(pool.map(claim, databases)) == 1
        barrier.reset()

        def register(item):
            index, database = item
            barrier.wait(timeout=5)
            try:
                return database.set_successor(row.id, SuccessorRef(f"agent:{index}"))
            except HandoffDBError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(register, enumerate(databases)))
        assert sum(result is not None for result in results) == 1
        assert harness.ctx.handoff_db.get(row.id).state == "done"
    finally:
        for control in connections:
            control.close()


def test_longer_retry_appends_losslessly_and_keeps_one_successor(harness):
    first = harness.ctx.service.run(harness.principal, request())
    original = harness.rows("messages")
    harness.ctx.service.register_successor(harness.principal, first.handoff.id, SuccessorRef("new"))
    harness.restart()
    status, retried = harness.request(
        body=asdict(request(texts=("Verbatim goal", "Verbatim status", "Agent kept going")))
    )
    assert status == 200
    assert retried["launch"] is False
    assert retried["handoff"]["stored_thread_id"] == first.handoff.stored_thread_id
    assert retried["handoff"]["successor"]["agent_ref"] == "new"
    assert harness.rows("messages")[: len(original)] == original
    assert (
        harness.ctx.store_for(harness.workspace).messages(first.handoff.stored_thread_id)[-1].text
        == "Agent kept going"
    )
    assert len(harness.rows("threads")) == 1


@pytest.mark.parametrize("field", ["text", "raw", "role", "kind", "name", "call_id", "seq"])
def test_append_rejects_changed_canonical_prefix_without_mutation(harness, field):
    first = harness.ctx.service.run(harness.principal, request())
    store = harness.ctx.store_for(harness.workspace)
    original = store.messages(first.handoff.stored_thread_id)
    changed = list(original)
    changed[0] = replace(changed[0], **{field: 17 if field == "seq" else "changed"})
    before = {table: harness.rows(table) for table in ("threads", "messages", "chunks")}
    with pytest.raises((ThreadConflict, ValueError)):
        store.append(first.handoff.stored_thread_id, changed)
    assert {table: harness.rows(table) for table in before} == before


def test_append_concurrent_connections_preserve_old_chunks_and_fts(harness):
    first = harness.ctx.service.run(harness.principal, request())
    store = harness.ctx.store_for(harness.workspace)
    original = store.messages(first.handoff.stored_thread_id)
    old_chunks = store.chunks(first.handoff.stored_thread_id)
    extended = original + [
        replace(original[-1], seq=2, text="  uniqueaddedfact 雪\r\n", raw="exact raw tail\r\n")
    ]
    path = harness.root / "workspaces" / harness.workspace / "threads.sqlite3"
    stores = [ThreadStore(str(path)) for _ in range(4)]
    barrier = threading.Barrier(4)
    try:

        def append(current):
            barrier.wait(timeout=5)
            return current.append(first.handoff.stored_thread_id, extended)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(append, stores))
        assert all(result == results[0] for result in results)
        assert results[0].message_count == 3
        assert results[0].sha256 == canonical_sha256(extended)
        assert store.messages(first.handoff.stored_thread_id) == extended
        assert store.chunks(first.handoff.stored_thread_id)[: len(old_chunks)] == old_chunks
        assert len(store.search([first.handoff.stored_thread_id], "uniqueaddedfact")) == 1
        assert store.append(first.handoff.stored_thread_id, original) == results[0]
    finally:
        for current in stores:
            current.close()


def test_append_failure_rolls_back_then_retry_mirrors_new_tail(harness, monkeypatch):
    first = harness.ctx.service.run(harness.principal, request())
    store = harness.ctx.store_for(harness.workspace)
    baseline = {table: harness.rows(table) for table in ("threads", "messages", "chunks")}
    body = request(texts=("Verbatim goal", "Verbatim status", "uniqueaddedfact"))
    store._conn.execute(
        "CREATE TEMP TRIGGER fail_append BEFORE INSERT ON chunks WHEN NEW.idx > 0 "
        "BEGIN SELECT RAISE(ABORT, 'append disk full'); END"
    )
    try:
        failed = harness.ctx.service.run(harness.principal, body)
    finally:
        store._conn.execute("DROP TRIGGER fail_append")
    assert (failed.action, failed.launch) == ("compact", False)
    assert {table: harness.rows(table) for table in baseline} == baseline
    harness.assert_event_count(EVENT_FAILED, 1)
    mirrored = []

    def metadata_only(_principal, name, _arguments):
        assert name == "mempalace_list_drawers"
        return {"drawers": [], "total": 0}

    def capture_mirror(call, principal, stored, chunks):
        mirrored.extend(chunks)
        return [f"unit-{unit.seq_start}-{unit.seq_end}-{unit.index}" for unit in chunks]

    harness.ctx.service.call_tool = metadata_only
    monkeypatch.setattr(palace_mirror, "mirror", capture_mirror)
    result = harness.ctx.service.run(harness.principal, body)
    drain_service(harness.ctx.service)
    assert result.handoff == first.handoff
    assert not result.launch
    assert any("uniqueaddedfact" in chunk.text for chunk in mirrored)
    before = harness.rows("messages")
    mirrored_before_retry = list(mirrored)
    assert not harness.ctx.service.run(harness.principal, body).launch
    drain_service(harness.ctx.service)
    assert harness.rows("messages") == before
    assert mirrored == mirrored_before_retry

    def mirror_down(*args):
        raise OSError("configured Memory mirror unavailable")

    monkeypatch.setattr(palace_mirror, "mirror", mirror_down)
    extended = request(
        texts=(
            "Verbatim goal",
            "Verbatim status",
            "uniqueaddedfact",
            "tail while mirror unavailable",
        )
    )
    assert not harness.ctx.service.run(harness.principal, extended).launch
    assert (
        store.messages(first.handoff.stored_thread_id)[-1].text == "tail while mirror unavailable"
    )
    harness.assert_event_count(EVENT_FAILED, 1)  # Only the actual append failure.


def test_workspace_doc_hydrates_after_restart_and_cli_reads_it(harness, monkeypatch, capsys):
    from commonplace import cli

    result = harness.ctx.service.run(harness.principal, request())
    document = result.handoff.doc_markdown
    assert "Verbatim goal" in document
    assert harness.ctx.store_for(harness.workspace).doc(result.handoff.id) == document
    assert harness.control._conn.execute("SELECT doc_markdown FROM handoffs").fetchone()[0] == ""
    harness.restart()
    assert harness.ctx.handoff_db.get(result.handoff.id).doc_markdown == document
    assert harness.ctx.handoff_db.list(harness.workspace)[0].doc_markdown == document
    status, body = harness.request(f"/api/handoffs/{result.handoff.id}", method="GET")
    assert status == 200 and body["doc_markdown"] == document
    monkeypatch.setattr(cli, "_db", lambda args: harness.control)
    assert cli.cmd_handoff_show(SimpleNamespace(handoff_id=result.handoff.id)) == 0
    assert document in capsys.readouterr().out


def test_relaunch_restores_original_note_from_workspace_doc(harness):
    first = harness.ctx.service.run(
        harness.principal, replace(request(), note="  Original agent note 雪\r\n")
    )
    harness.ctx.service.report_launch_failure(
        harness.principal, first.handoff.id, "verified failed"
    )
    harness.restart()
    result = harness.ctx.service.relaunch(harness.principal, first.handoff.id)
    assert result.launch
    assert first.handoff.doc_markdown in result.prompt
    assert "  Original agent note 雪\r\n" in result.prompt


def test_document_write_failure_never_launches_and_can_retry(harness, monkeypatch):
    store = harness.ctx.store_for(harness.workspace)

    def full_disk(*args):
        raise OSError(errno.ENOSPC, "workspace document disk full")

    with monkeypatch.context() as patch:
        patch.setattr(store, "put_doc", full_disk)
        result = harness.ctx.service.run(harness.principal, request())
    assert (result.action, result.launch, result.handoff.state) == ("compact", False, "failed")
    harness.assert_event_count(EVENT_FAILED, 1)
    assert "Verbatim goal" not in "\n".join(harness.control._conn.iterdump())
    assert harness.ctx.service.run(harness.principal, request()).launch
    assert len(harness.rows("threads")) == 1


def test_pre_release_document_migration_clears_control_files(harness):
    row = prepared(harness)
    private = "old_private_document_0a65fadb 再见"
    harness.control._conn.execute(
        "UPDATE handoffs SET doc_markdown = ? WHERE id = ?", (private, row.id)
    )
    harness.ctx.handoff_db.set_document_store(harness.ctx.store_for)
    assert harness.ctx.handoff_db.get(row.id).doc_markdown == private
    assert harness.ctx.store_for(harness.workspace).doc(row.id) == private
    assert private not in "\n".join(harness.control._conn.iterdump())
    assert not any(
        private.encode() in path.read_bytes() for path in harness.root.glob("control.sqlite3*")
    )


def test_document_scrub_resumes_after_rows_already_moved(harness):
    # A previous process moved the document but exited before its WAL scrub.
    harness.control._conn.execute(
        "INSERT INTO meta(key, value) VALUES ('handoff_docs_scrub_pending', '1')"
    )
    harness.restart()
    assert (
        harness.control._conn.execute(
            "SELECT 1 FROM meta WHERE key = 'handoff_docs_scrub_pending'"
        ).fetchone()
        is None
    )


@pytest.mark.parametrize("off", [False, True])
def test_other_connection_retry_is_empty_without_touching_lease(harness, monkeypatch, off):
    moment = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(db_module, "_now", lambda: moment)
    user = harness.control.create_user("other-member@example.test")
    harness.control.add_member(harness.workspace, user, "member", actor=harness.owner)
    _, token = harness.control.create_token(
        workspace_ref=harness.workspace,
        user_ref=user,
        name="other-member",
        client="test",
        policy=Policy.build(profile="full", scopes=["handoff:write"], trust="trusted"),
        actor=harness.owner,
    )
    principal = harness.control.authenticate_token(token)
    row, _ = harness.ctx.handoff_db.begin(
        harness.workspace,
        "thread:source",
        request().meta,
        connection_id=harness.principal.connection_id,
        lease_token="original",
    )
    moment += timedelta(seconds=601)
    if off:
        harness.ctx.handoff_db.put_setting(
            SettingRow(harness.workspace, "workspace", "", enabled=False), actor=harness.owner
        )
    result = harness.ctx.service.run(principal, request())
    assert (result.action, result.reason, result.handoff, result.launch) == (
        "compact",
        "in progress",
        None,
        False,
    )
    assert harness.ctx.handoff_db.get(row.id) == row
    assert not harness.rows("threads")


@pytest.mark.parametrize("boundary", ["parse", "messages", "chunks", "fts", "disk", "control"])
def test_storage_failure_rolls_back_preserves_existing_and_retries(harness, monkeypatch, boundary):
    existing = harness.ctx.service.run(harness.principal, request("existing"))
    baseline = {table: harness.rows(table) for table in ("threads", "messages", "chunks")}
    store = harness.ctx.store_for(harness.workspace)
    marker = harness.root / "existing-memory.txt"
    marker.write_bytes(b"existing Memory stays untouched\r\n")
    messages = request("failing", texts=("first", "second"))
    with monkeypatch.context() as patch:
        if boundary == "parse":

            def fail_parse(*args):
                raise ValueError("malformed transcript")

            patch.setattr(transcripts, "parse", fail_parse)
        elif boundary == "disk":

            def fail_store(*args, **kwargs):
                raise OSError(errno.ENOSPC, "disk full")

            patch.setattr(store, "store", fail_store)
        elif boundary == "control":

            def fail_begin(*args, **kwargs):
                raise sqlite3.OperationalError("database or disk is full")

            patch.setattr(harness.ctx.handoff_db, "begin", fail_begin)
        else:
            table = {"messages": "messages", "chunks": "chunks", "fts": "chunks_fts"}[boundary]
            # An actual abort after the thread header (and sometimes messages) was inserted.
            if boundary == "fts":

                class BrokenConnection:
                    def __getattr__(self, name):
                        return getattr(self.connection, name)

                    def __enter__(self):
                        self.connection.__enter__()
                        return self

                    def __exit__(self, *args):
                        return self.connection.__exit__(*args)

                    def executemany(self, sql, rows):
                        if sql.startswith("INSERT INTO chunks_fts"):
                            raise sqlite3.OperationalError("database or disk is full")
                        return self.connection.executemany(sql, rows)

                broken = BrokenConnection()
                broken.connection = store._conn
                patch.setattr(store, "_conn", broken)
            else:
                condition = "WHEN NEW.seq = 1" if boundary == "messages" else ""
                store._conn.execute(
                    f"CREATE TEMP TRIGGER fail_store BEFORE INSERT ON {table} {condition} "
                    "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
                )
        try:
            result = harness.ctx.service.run(harness.principal, messages)
        finally:
            if boundary in ("messages", "chunks"):
                store._conn.execute("DROP TRIGGER fail_store")
    assert result.action == "compact"
    assert result.launch is False
    harness.assert_event_count(EVENT_FAILED, 1)
    assert {table: harness.rows(table) for table in baseline} == baseline
    assert harness.ctx.handoff_db.get(existing.handoff.id) == existing.handoff
    assert marker.read_bytes() == b"existing Memory stays untouched\r\n"
    retry = harness.ctx.service.run(harness.principal, messages)
    assert retry.launch
    assert len(harness.rows("threads")) == 2


def test_mirror_failure_is_best_effort_after_durable_storage(harness, monkeypatch):
    harness.ctx.service.call_tool = lambda *args: None

    calls = []

    def fail_mirror(*args):
        assert len(harness.rows("threads")) == 1
        calls.append(True)
        raise OSError(errno.ENOSPC, "mirror disk full")

    monkeypatch.setattr(palace_mirror, "mirror_pending", fail_mirror)
    result = harness.ctx.service.run(harness.principal, request())
    drain_service(harness.ctx.service)
    assert result.launch and calls == [True]
    assert len(harness.rows("threads")) == 1
    harness.assert_event_count(EVENT_FAILED, 0)


@pytest.mark.parametrize("field", ["agent_ref", "thread_id", "url"])
def test_successor_conflict_compares_every_populated_field(harness, field):
    row = prepared(harness)
    ref = SuccessorRef("agent:first", "https://example.test/first", "first")
    harness.ctx.handoff_db.set_successor(row.id, ref)
    changed = asdict(ref)
    changed[field] += "-different"
    with pytest.raises(HandoffDBError):
        harness.ctx.handoff_db.set_successor(row.id, SuccessorRef(**changed))
    assert harness.ctx.handoff_db.get(row.id).successor == ref


def test_successor_empty_metadata_can_be_filled_once_without_completion_duplicate(harness):
    row = prepared(harness)
    harness.ctx.service.register_successor(harness.principal, row.id, SuccessorRef("agent:first"))
    ref = SuccessorRef("agent:first", "https://example.test/first", "first")
    result = harness.ctx.service.register_successor(harness.principal, row.id, ref)
    assert result.successor == ref
    assert harness.ctx.service.register_successor(harness.principal, row.id, ref) == result
    harness.assert_event_count(EVENT_COMPLETED, 1)


def test_registration_failure_after_external_launch_keeps_claim(harness, monkeypatch):
    harness.configure(LauncherConfig("webhook", url="http://127.0.0.1/launch", secret="secret"))
    launches = []

    def launched(*args, **kwargs):
        launches.append(SuccessorRef("external:already-running"))
        return launches[-1]

    def failed_registration(*args):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(launchers, "launch", launched)
    with monkeypatch.context() as patch:
        patch.setattr(harness.ctx.handoff_db, "set_successor", failed_registration)
        result = harness.ctx.service.run(harness.principal, request())
    assert len(launches) == 1
    assert result.handoff.state == "launched"
    harness.assert_event_count(EVENT_LAUNCH_FAILED, 0)
    harness.restart()
    retry = harness.ctx.service.run(harness.principal, request())
    assert retry.launch is False
    with pytest.raises(HandoffDBError):
        harness.ctx.service.relaunch(harness.principal, result.handoff.id)
    completed = harness.ctx.service.register_successor(
        harness.principal, result.handoff.id, launches[0]
    )
    assert completed.state == "done"
    assert len(launches) == 1


@pytest.mark.parametrize("kind", ["command", "webhook"])
def test_real_launcher_failure_then_concurrent_relaunch_once(harness, kind, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_HANDOFF_LAUNCHERS", "command,webhook")
    # Exercise real retries against an isolated receiver after the policy gate.
    monkeypatch.setattr(launchers, "validate_webhook_url", lambda _: None)
    monkeypatch.setattr(launchers, "require_public_address", lambda _: None)
    mode = harness.root / "launch-mode"
    mode.write_text("fail")
    launches = harness.root / "launches"
    webhook_calls = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            webhook_calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            failed = mode.read_text() == "fail"
            if not failed:
                with launches.open("a") as output:
                    output.write("launched\n")
            data = json.dumps({"agent_ref": "webhook:successor"}).encode()
            self.send_response(503 if failed else 200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    receiver_thread = threading.Thread(
        target=receiver.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    receiver_thread.start()
    try:
        if kind == "command":
            script = harness.root / "launcher.py"
            script.write_text(
                "import pathlib, sys\n"
                "if pathlib.Path(sys.argv[1]).read_text() == 'fail': sys.exit(7)\n"
                "with pathlib.Path(sys.argv[2]).open('a') as out: out.write('launched\\n')\n"
            )
            config = LauncherConfig(
                "command", argv=(sys.executable, str(script), str(mode), str(launches))
            )
        else:
            config = LauncherConfig(
                "webhook",
                url=f"http://127.0.0.1:{receiver.server_address[1]}/launch",
                secret="local",
            )
        harness.configure(config)
        result = harness.ctx.service.run(harness.principal, request())
        assert result.handoff.state == "launch_failed"
        assert result.handoff.successor is None
        assert not launches.exists()
        harness.assert_event_count(EVENT_LAUNCH_FAILED, 1)
        assert not harness.ctx.service.run(harness.principal, request()).launch
        mode.write_text("succeed")
        # A restarted gateway reconstructs the stored prompt and uses a durable claim.
        harness.restart()
        barrier = threading.Barrier(2)

        def relaunch(_):
            barrier.wait(timeout=5)
            return harness.request(f"/api/handoffs/{result.handoff.id}/relaunch", {})

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(relaunch, range(2)))
        assert sorted(status for status, _ in results) == [200, 409], results
        row = harness.ctx.handoff_db.get(result.handoff.id)
        assert row.state == "done"
        assert row.successor is not None
        assert launches.read_text() == "launched\n"
        harness.assert_event_count(EVENT_COMPLETED, 1)
        assert len(harness.rows("threads")) == 1
        if kind == "webhook":
            assert len(webhook_calls) == 2
            assert webhook_calls[0]["handoff_id"] == webhook_calls[1]["handoff_id"]
    finally:
        receiver.shutdown()
        receiver.server_close()
        receiver_thread.join(timeout=5)


def test_three_generations_recall_original_and_reject_invalid_parents(harness):
    previous = ""
    stored_ids = []
    for generation in range(3):
        result = harness.ctx.service.run(
            harness.principal, request(f"generation-{generation}", parent=previous)
        )
        assert result.launch
        harness.ctx.service.register_successor(
            harness.principal, result.handoff.id, SuccessorRef(f"agent:{generation + 1}")
        )
        previous = result.handoff.id
        stored_ids.append(result.handoff.stored_thread_id)
    assert [
        row.id for row in harness.ctx.recall.chain(harness.principal, stored_ids[-1])
    ] == stored_ids[::-1]
    other = harness.control.create_workspace("other", harness.owner)
    foreign, _ = harness.ctx.handoff_db.begin(
        other, "foreign", ThreadMeta("foreign"), connection_id=harness.principal.connection_id
    )
    # A forged parent is dropped, never linked: the handoff itself goes through.
    for index, parent in enumerate(("hof_nonexistent", foreign.id)):
        status, body = harness.request(body=asdict(request(f"forged-{index}", parent=parent)))
        assert status == 200, body
        stored = body["handoff"]["stored_thread_id"]
        store = harness.ctx.store_for(harness.workspace)
        assert store.parent_of(stored) in (None, "")
        assert body["handoff"]["parent_handoff_id"] == ""
    baseline = harness.rows("threads")
    # A retry that forges its own parent cannot mutate the existing immutable chain.
    status, retried = harness.request(body=asdict(request("generation-2", parent=previous)))
    assert status == 200
    assert retried["launch"] is False
    assert harness.rows("threads") == baseline
    assert [
        row.id for row in harness.ctx.recall.chain(harness.principal, stored_ids[-1])
    ] == stored_ids[::-1]


def test_forged_storage_cycle_cannot_loop_recall_or_handoff_doc(harness):
    first = harness.ctx.service.run(harness.principal, request("first"))
    second = harness.ctx.service.run(harness.principal, request("second", parent=first.handoff.id))
    store = harness.ctx.store_for(harness.workspace)
    # Simulate corrupt legacy data; public retries cannot rewrite stored parents.
    store._conn.execute(
        "UPDATE threads SET parent_stored_id = ? WHERE id = ?",
        (second.handoff.stored_thread_id, first.handoff.stored_thread_id),
    )
    chain = harness.ctx.recall.chain(harness.principal, second.handoff.stored_thread_id)
    assert [row.id for row in chain] == [
        second.handoff.stored_thread_id,
        first.handoff.stored_thread_id,
    ]
    third = harness.ctx.service.run(harness.principal, request("third", parent=second.handoff.id))
    assert third.launch
    assert len(harness.ctx.recall.chain(harness.principal, third.handoff.stored_thread_id)) == 3


def test_completion_survives_a_crash_and_is_delivered_once(harness):
    row = prepared(harness)
    ref = SuccessorRef("already:running")
    # Process died after set_successor committed and before delivery began:
    # the completion event is queued in the same transaction.
    harness.ctx.handoff_db.set_successor(row.id, ref)
    harness.assert_event_count(EVENT_COMPLETED, 0)
    harness.restart()  # gateway start replays the queue
    harness.assert_event_count(EVENT_COMPLETED, 1)
    assert harness.ctx.service.register_successor(harness.principal, row.id, ref).successor == ref
    harness.restart()
    harness.assert_event_count(EVENT_COMPLETED, 1)
    assert not harness.ctx.handoff_db.pending_events()
    assert not harness.ctx.service.run(harness.principal, request()).launch


class DuplicateCompletion(AssertionError):
    """The known race wrote two completion records to each persistence seam."""


def test_separate_gateways_emit_only_one_completion(harness):
    row = prepared(harness)

    def assert_one_completion():
        try:
            harness.assert_event_count(EVENT_COMPLETED, 1)
        except AssertionError as error:
            # Only the demonstrated duplicate is expected. Setup, import and
            # unrelated persistence failures must still fail this test.
            harness.assert_event_count(EVENT_COMPLETED, 2)
            raise DuplicateCompletion(str(error)) from error

    # Real hub listeners take each gateway's control lock. Bound the whole
    # concurrent call, including executor shutdown, if those locks deadlock.
    script = """
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from commonplace.control_db import ControlDB
from commonplace.gateway import Gateway
from commonplace.handoff.models import SuccessorRef
from commonplace.handoff.wiring import install

root = Path(sys.argv[1])
controls = [ControlDB(str(root / "control.sqlite3")) for _ in range(2)]
contexts = [install(Gateway(control, SimpleNamespace()), root) for control in controls]
principal = controls[0].authenticate_token(sys.argv[3])
barrier = threading.Barrier(2)
try:
    for current in contexts:
        authorize = current.service._authorized

        def simultaneous(principal, identifier, original=authorize):
            result = original(principal, identifier)
            barrier.wait(timeout=5)
            return result

        current.service._authorized = simultaneous
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                current.service.register_successor,
                principal,
                sys.argv[2],
                SuccessorRef("same"),
            )
            for current in contexts
        ]
        print(json.dumps([future.result(timeout=10).state for future in futures]))
finally:
    for current in contexts:
        current.close()
        current.gateway.webhooks.stop()
    for control in controls:
        control.close()
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(harness.root), row.id, harness.token],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        # Both writes can commit before their listeners deadlock. Their event
        # counts must still satisfy the original duplicate-completion contract.
        assert_one_completion()
        pytest.fail("Separate gateways deadlocked while registering the same successor")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["done", "done"]
    assert harness.ctx.handoff_db.get(row.id).successor == SuccessorRef("same")
    assert_one_completion()


@pytest.mark.skipif(os.name != "posix", reason="The hook's fcntl lock is POSIX only")
def test_two_real_hook_processes_share_lock_and_server_claim(harness, tmp_path):
    home = tmp_path / "hook-home"
    settings_dir = home / ".commonplace"
    settings_dir.mkdir(parents=True)
    launches = tmp_path / "hook-launches"
    script = tmp_path / "successor.py"
    script.write_text(
        "import pathlib, sys\n"
        "with pathlib.Path(sys.argv[1]).open('a') as out: out.write('launched\\n')\n"
    )
    (settings_dir / "handoff-client.json").write_text(
        json.dumps({"successor_argv": [sys.executable, str(script), str(launches)]})
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "Exact original"}})
        + "\n"
    )
    event = json.dumps(
        {
            "session_id": "hook-session",
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "hook_event_name": "PreCompact",
        }
    )
    environment = dict(os.environ)
    environment.update(
        HOME=str(home),
        COMMONPLACE_HANDOFF="on",
        COMMONPLACE_TOKEN=harness.token,
        COMMONPLACE_URL=f"http://127.0.0.1:{harness.server.server_address[1]}",
    )
    for key in ("COMMONPLACE_AGENT", "COMMONPLACE_TASK", "COMMONPLACE_HANDOFF_ID"):
        environment.pop(key, None)
    entered, release = threading.Event(), threading.Event()
    route = harness.gateway.routes[("POST", "/api/handoff")]

    def blocked(handler):
        entered.set()
        assert release.wait(timeout=10)
        route(handler)

    harness.gateway.routes[("POST", "/api/handoff")] = blocked
    processes = []
    try:
        first = subprocess.Popen(
            [sys.executable, "-m", "commonplace.handoff.hook", "--client", "claude-code"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        processes.append(first)
        first.stdin.write(event)
        first.stdin.close()
        first.stdin = None
        assert entered.wait(timeout=10)
        second = subprocess.Popen(
            [sys.executable, "-m", "commonplace.handoff.hook", "--client", "claude-code"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        processes.append(second)
        output, error = second.communicate(event, timeout=10)
        assert (second.returncode, output, error) == (0, "", "")
        release.set()
        output, error = first.communicate(timeout=10)
        assert first.returncode == 0, error
        assert json.loads(output)["continue"] is False
        assert launches.read_text() == "launched\n"
        assert len(harness.rows("threads")) == 1
        harness.assert_event_count(EVENT_COMPLETED, 1)
        status, retry = harness.request(body=asdict(request("hook-session")))
        assert status == 200
        assert retry["launch"] is False
    finally:
        release.set()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


def test_failed_completion_delivery_rolls_back_and_retries_once(harness, monkeypatch):
    from commonplace import events as hub_events

    row = prepared(harness)
    ref = SuccessorRef("retry:once")
    original = hub_events.record

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(hub_events, "record", broken)
    done = harness.ctx.service.register_successor(harness.principal, row.id, ref)
    assert done.state == "done"
    # The audit entry rolled back with the failed hub event: nothing half-written.
    harness.assert_event_count(EVENT_COMPLETED, 0)
    [pending] = harness.ctx.handoff_db.pending_events(handoff_id=row.id)
    assert pending["attempts"] == 1
    monkeypatch.setattr(hub_events, "record", original)
    # The idempotent retry delivers it; later retries and restarts don't repeat it.
    harness.ctx.service.register_successor(harness.principal, row.id, ref)
    harness.assert_event_count(EVENT_COMPLETED, 1)
    harness.ctx.service.register_successor(harness.principal, row.id, ref)
    harness.restart()
    harness.assert_event_count(EVENT_COMPLETED, 1)
    assert not harness.ctx.handoff_db.pending_events()
