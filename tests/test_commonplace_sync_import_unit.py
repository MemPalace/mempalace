"""Import-owned units; continuity and read implementations use frozen API doubles.

The integrated acceptance tests verify storage sharing across units. These tests
exercise the import boundary without loading another builder's implementation.
"""

import hashlib
import io
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

import commonplace
from commonplace import import_grant, imports, ingest_cadence, plans, sync_protocol
from commonplace.control_db import ControlDB, Principal
from commonplace.handoff.thread_store import ThreadStore, canonical_sha256
from commonplace.handoff.transcripts import parse
from commonplace.import_sources import parse_import
from commonplace.policy import Policy
from commonplace.sessions import person_policy
from examples import import_memory as client

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def native(text="exact café 🦉", ending="\r\n"):
    return (
        json.dumps(
            {
                "type": "user",
                "sessionId": "synthetic",
                "message": {"role": "user", "content": text},
            },
            ensure_ascii=False,
        )
        + ending
    )


def item(text=None, tid="synthetic", fmt="auto", part=None):
    value = {"thread_id": tid, "transcript": native() if text is None else text, "format": fmt}
    if part is not None:
        value["part"] = part
    return value


def descriptor(text, *, previous=b"", index=0, whole=None):
    raw = text.encode()
    whole = previous + raw if whole is None else whole
    return client.descriptor(
        len(whole),
        sha(whole),
        index,
        len(previous),
        sha(previous),
        sha(raw),
        len(previous) + len(raw) == len(whole),
    )


@pytest.fixture
def unit(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    monkeypatch.setattr(plans, "_now", lambda: NOW)
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    user = db.create_user("synthetic-owner@example.test")
    ws = db.create_workspace("synthetic", user, personal=True)
    store = ThreadStore(str(tmp_path / "threads.sqlite3"))
    principal = Principal(
        user_id=user,
        user_email="synthetic-owner@example.test",
        workspace_id=ws,
        connection_id="",
        connection="",
        client="test",
        auth="session",
        role="owner",
        person=True,
        policy=person_policy(),
    )
    routes = {}
    ctx = SimpleNamespace(control_db=db, data_dir=tmp_path, principal=principal)

    def store_for(workspace):
        assert workspace == ws
        assert not db._conn.in_transaction
        assert not db._lock._is_owned()
        return store

    ctx.store_for = store_for
    ctx.has_threads = lambda workspace: bool(
        store.list_source_range(workspace, "import-private:", limit=1)
    )
    ctx.gateway = SimpleNamespace(
        principal_for=lambda handler: ctx.principal,
        add_route=lambda method, path, handler: routes.__setitem__((method, path), handler),
        storage_meter=SimpleNamespace(audit_limit=Mock()),
    )
    continuity = ModuleType("commonplace.continuity")
    continuity.backfill = Mock()
    continuity.import_item = Mock(return_value=None)
    continuity.stream_push = Mock()
    reader = ModuleType("commonplace.continuity_read")
    reader.listed_messages = Mock(return_value=None)
    base_admit = ingest_cadence.admit
    # The cadence module owns this keyword; retain base admission in these units.
    monkeypatch.setattr(
        ingest_cadence, "admit", lambda db, *, ride=False, **kw: base_admit(db, **kw)
    )
    monkeypatch.setitem(sys.modules, continuity.__name__, continuity)
    monkeypatch.setattr(commonplace, "continuity", continuity, raising=False)
    monkeypatch.setitem(sys.modules, reader.__name__, reader)
    monkeypatch.setattr(commonplace, "continuity_read", reader, raising=False)
    monkeypatch.setattr(imports, "_read_json", lambda handler, limit: handler.body)
    imports.register(ctx)

    def request(body=None, path="/api/import", method="POST", principal=None):
        ctx.principal = principal or ctx.principal
        handler = SimpleNamespace(body=body, path=path)
        reply = []
        handler._send_json = lambda *args: reply.append(args)
        routes[method, path.split("?")[0]](handler)
        return reply[0][0], reply[0][1]

    yield SimpleNamespace(
        db=db,
        ctx=ctx,
        principal=principal,
        store=store,
        request=request,
        continuity=continuity,
        reader=reader,
    )
    store.close()
    db.close()


def grant(unit):
    return replace(
        unit.principal,
        person=False,
        connection_id="conn_synthetic",
        auth="pat",
        policy=Policy.build(
            scopes=["memory:read", "memory:write", "memory:import"],
            read_wings=["*"],
            write_wing="inbox",
            trust="untrusted",
        ),
    )


def batch(unit, threads, source="claude-code", path="grant"):
    return imports.run_batch(unit.ctx, unit.principal, source, threads, path=path)


def listing(unit):
    return unit.store.list_source_range(unit.principal.workspace_id, "import-private:", limit=100)


def test_private_person_member_token_and_acl(unit):
    body = {"source": "claude-code", "threads": [item()], "private": True}
    status, report = unit.request(body)
    assert status == 200 and report["accepted"] == 1
    assert listing(unit)[0].wing == "private_chats"
    member = unit.db.create_user("synthetic-member@example.test")
    unit.db.add_member(unit.principal.workspace_id, member, "member", actor=unit.principal.user_id)
    status, report = unit.request(
        body, principal=replace(unit.principal, user_id=member, role="member")
    )
    assert (status, report) == (403, {"error": import_grant.OWNER_ONLY_TEXT})
    assert unit.request(body, principal=grant(unit))[0] == 400
    limited = replace(grant(unit), policy=Policy.build(scopes=["memory:read"]))
    assert unit.request(body, principal=limited)[0] == 403
    assert unit.request({"source": "claude-code", "threads": [item()]}, principal=limited)[0] == 403
    assert unit.request(dict(body, private=False), principal=unit.principal)[0] == 400
    status, report = unit.request(
        {"source": "claude-code", "threads": [item()]},
        principal=unit.principal,
    )
    assert status == 200 and report["imported"] == 1
    stored = unit.store.by_source(unit.principal.workspace_id, "import:claude-code:synthetic")
    assert stored.wing == "threads"


@pytest.mark.parametrize(
    "source,ride",
    [
        ("claude-code", True),
        ("codex", True),
        ("t3", True),
        ("native-memory", True),
        ("cursor", False),
        ("chatgpt", False),
        ("claude-ai", False),
        ("grok-bot", False),
    ],
)
def test_only_native_and_memory_imports_ride(unit, monkeypatch, source, ride):
    admit = Mock(return_value=0)
    monkeypatch.setattr(ingest_cadence, "admit", admit)
    monkeypatch.setattr(imports, "run_batch", Mock(return_value={"accepted": 1}))
    assert unit.request({"source": source, "threads": [item()]}, principal=grant(unit))[0] == 200
    args = admit.call_args.kwargs
    assert args["ride"] is ride and args["account_id"] == unit.principal.user_id
    assert "window" not in args and "conn" not in args
    imports.grant_admit(unit.ctx, grant(unit), "other")
    assert admit.call_args.kwargs["ride"] is False


@pytest.mark.parametrize("source", ["claude-code", "t3"])
def test_snapshot_contract_and_transaction_order(unit, source):
    text = native()

    def backfill(db, store, ws, user):
        assert not db._lock._is_owned() and not db._conn.in_transaction

    def grouped(conn, store, **args):
        assert unit.db._lock._is_owned() and conn.in_transaction
        assert args["transcript"] == text and args["thread_id"] == "synthetic"
        assert args["messages"] == parse_import(source, text)
        assert args["actor"] == unit.principal.actor
        assert args["maximum"] is None
        return {"status": "accepted"}

    unit.continuity.backfill.side_effect = backfill
    unit.continuity.import_item.side_effect = grouped
    report = batch(unit, [item(text)], source)
    assert report["results"] == [
        {
            "index": 0,
            "thread_id": "synthetic",
            "status": "accepted",
            "messages": len(parse(text)),
            "source_bytes": len(text.encode()),
        }
    ]
    assert listing(unit) == []  # A handled item must never fall through to a second writer.


def test_none_fallback_identity_repeat_and_live_tail_conflict(unit):
    prefix = native()
    tail = '{"type":"user","sessionId":"synthetic","message":{"content":"open'
    live = prefix + tail
    first = batch(unit, [item(live)])
    assert first["accepted"] == 1
    unit.continuity.import_item.assert_not_called()
    original = listing(unit)[0]
    assert unit.store.messages(original.id)[0].raw == live
    complete = live + '"}}\n'
    assert batch(unit, [item(complete)])["accepted"] == 1
    rows = listing(unit)
    digest = canonical_sha256(parse_import("claude-code", complete))
    assert {row.source_thread_id for row in rows} == {
        import_grant.grant_id(unit.principal.user_id, "claude-code", "synthetic"),
        import_grant.grant_id(unit.principal.user_id, "claude-code", "synthetic")
        + f"#sha256:{digest}",
    }
    before = [(row, unit.store.messages(row.id)) for row in rows]
    assert batch(unit, [item(complete)])["accepted"] == 1
    assert [(row, unit.store.messages(row.id)) for row in listing(unit)] == before
    assert imports._import_row(original)["stored_messages"] == len(parse(live))


def test_part_contract_response_uses_own_stream_state(unit):
    text = native(ending="")
    part = descriptor(text)
    result = dict(
        unit="part",
        format="claude-code",
        part=part,
        source_bytes=len(text.encode()),
        messages=len(parse(text)),
        offset=len(text.encode()),
        prefix_sha256=sha(text.encode()),
        next_index=1,
        session_state="complete",
        stored_thread_id="thr_marker",
        status="appended",
    )

    def push(conn, store, **args):
        assert conn.in_transaction and unit.db._lock._is_owned()
        assert args["kind"] == "import" and args["source"] == "t3"
        assert args["native_source"] == "claude-code" and args["data"] == text.encode()
        assert args["stream_id"] == import_grant.grant_id(unit.principal.user_id, "t3", "synthetic")
        assert args["part"] == part and args["gate"]() is None
        assert args["maximum"]() is None
        return result

    unit.continuity.stream_push.side_effect = push
    report = batch(unit, [item(text, fmt="claude-code", part=part)], "t3")
    assert report["accepted"] == 1
    row = report["results"][0]
    assert row == {
        "index": 0,
        "thread_id": "synthetic",
        "status": "accepted",
        **{k: v for k, v in result.items() if k not in ("status", "stored_thread_id")},
    }


def test_incomplete_final_object_refused_before_continuity_or_store(unit):
    text = native() + '{"type":"user","message":{"content":"open'
    report = batch(unit, [item(text, fmt="claude-code", part=descriptor(text))])
    assert report["results"][0]["code"] == "invalid_transcript"
    assert listing(unit) == []
    unit.continuity.backfill.assert_not_called()
    unit.continuity.stream_push.assert_not_called()


def test_acl_part_count_and_snapshot_conflict_unchanged(unit):
    first, second = native(), native("second", "")
    whole = (first + second).encode()
    for i, (text, previous) in enumerate(((first, b""), (second, first.encode()))):
        part = descriptor(text, previous=previous, index=i, whole=whole)
        assert batch(unit, [item(text, fmt="claude-code", part=part)], path="acl")["errors"] == 0
    stored = unit.store.by_source(unit.principal.workspace_id, "import:claude-code:synthetic")
    assert stored.message_count - 1 == len(parse(first + second)) + 3
    batch(unit, [item()], path="acl")  # Snapshot against the part thread conflicts.
    assert unit.store.by_source(unit.principal.workspace_id, stored.source_thread_id) == stored
    unit.continuity.stream_push.assert_not_called()


@pytest.mark.parametrize("count", [0, 1, 5, 12])
def test_listing_uses_control_count_without_reading_holder(unit, monkeypatch, count):
    batch(unit, [item(native() + '{"type":"user","message":{"content":"open')])
    row = listing(unit)[0]
    unit.reader.listed_messages.return_value = count
    monkeypatch.setattr(unit.store, "messages", Mock(side_effect=AssertionError("holder read")))
    status, report = unit.request(path="/api/imports", method="GET", principal=unit.principal)
    assert status == 200
    assert report == {"imports": [imports._import_row(row, count)], "next": None}
    assert set(report["imports"][0]) == {
        "stored_thread_id",
        "source",
        "thread_id",
        "conflict_of",
        "created_at",
        "stored_messages",
    }
    unit.reader.listed_messages.assert_called_once_with(unit.db, row)


def test_memory_is_verbatim_private_versioned_and_validated(unit):
    text = "  synthetic note café 🦉\r\nKeep every word.\t"
    tid = sync_protocol.memory_thread_id("notes", "folder/note.md", sha(text.encode()))
    body = {"source": "native-memory", "threads": [item(text, tid, "file")]}
    assert unit.request(body, principal=unit.principal)[0] == 400
    status, report = unit.request(body, principal=grant(unit))
    assert status == 200 and report["accepted"] == 1
    before = listing(unit)
    assert unit.request(body, principal=grant(unit))[1]["accepted"] == 1
    assert listing(unit) == before
    rows = unit.store.messages(before[0].id)
    assert [(m.role, m.kind, m.text, m.raw) for m in rows] == [
        ("system", "other", "", text),
        ("system", "message", text, text),
    ]
    for bad in ("notes:../note.md:" + sha(text.encode()), "notes:file:bad", tid + "x"):
        assert (
            batch(unit, [item(text, bad, "file")], "native-memory")["results"][0]["code"]
            == "invalid_thread"
        )
    changed = text + "\nnext"
    tid2 = sync_protocol.memory_thread_id("notes", "folder/note.md", sha(changed.encode()))
    assert batch(unit, [item(changed, tid2, "file")], "native-memory")["accepted"] == 1
    assert len(listing(unit)) == 2


def test_refused_import_keeps_free_slot(unit, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    monkeypatch.setattr(plans, "FREE", replace(plans.FREE, storage_bytes=1))
    original_admit = ingest_cadence.admit
    # The cadence window extension is owned by another unit; use base admission
    # here to prove the import commits admission before the refused item.
    monkeypatch.setattr(
        ingest_cadence, "admit", lambda db, *, ride=False, **kw: original_admit(db, **kw)
    )
    status, report = unit.request(
        {"source": "claude-code", "threads": [item()]}, principal=grant(unit)
    )
    assert status == 200 and report["results"][0]["code"] == "storage_limit"
    wait = original_admit(
        unit.db,
        account_id=unit.principal.user_id,
        connection_id="conn_capture",
        request_sha256="different",
        interval=900,
        now=NOW + timedelta(seconds=1),
    )
    assert 0 < wait <= 900
    assert listing(unit) == []


def test_client_derives_identity_warns_and_keeps_tail_bytes(capsys):
    text = native() + '{"type":"user","message":{"content":"open'
    sent = []

    def send(body):
        sent.append(json.loads(body))
        return {"errors": 0}

    assert (
        client.upload_native(
            io.BytesIO(text.encode()), "claude-code", None, "auto", send, private=True
        )
        is False
    )
    assert sent[0]["private"] is True
    assert sent[0]["threads"][0]["thread_id"] == "synthetic"
    assert sent[0]["threads"][0]["transcript"] == text
    assert "kept separate" in capsys.readouterr().err
    client.upload_native(io.BytesIO(native().encode()), "claude-code", "other", "auto", send)
    assert "differs" in capsys.readouterr().err
    assert sent[-1]["threads"][0]["thread_id"] == "other"


def test_client_private_multipart_budget_and_accepted_ack():
    text = (native("x" * 80) * 12).encode()
    plan = client.native_plan(
        io.BytesIO(text), "claude-code", "synthetic", budget=1000, private=True
    )
    assert not plan["legacy"]
    for span in plan["parts"]:
        body = client.planned_body(io.BytesIO(text), plan, span)
        assert len(body) <= 1000 and json.loads(body)["private"] is True
        end = span["offset"] + span["size"]
        report = {
            "source": "claude-code",
            "errors": 0,
            "results": [
                {
                    "status": "accepted",
                    "unit": "part",
                    "format": plan["format"],
                    "thread_id": plan["thread_id"],
                    "part": span["part"],
                    "offset": end,
                    "next_index": span["part"]["index"] + 1,
                    "prefix_sha256": sha(text[:end]),
                    "session_state": "complete" if end == len(text) else "incomplete",
                }
            ],
        }
        assert client.acknowledge(report, plan, span)["status"] == "accepted"


@pytest.mark.parametrize("mutate", [False, True])
def test_client_identity_scan_preserves_bounded_reads_and_mutation_detection(mutate):
    raw = (native("AAAA" * 20) * 20).encode()

    class CheckedStream(io.BytesIO):
        seeks = 0

        def read(self, count=-1):
            assert 0 <= count <= 1001
            return super().read(count)

        def seek(self, *args):
            self.seeks += 1
            if mutate and self.seeks == 2:
                super().seek(0)
                self.write(raw.replace(b"AAAA", b"BBBB"))
            return super().seek(*args)

    stream = CheckedStream(raw)
    if mutate:
        sent = []
        with pytest.raises(ValueError, match="Source changed during preflight"):
            client.upload_native(stream, "claude-code", None, "auto", sent.append, budget=1000)
        assert sent == []
    else:
        plan = client.native_plan(stream, "claude-code", None, budget=1000)
        assert plan["thread_id"] == "synthetic" and not plan["legacy"]
        assert (
            b"".join(
                json.loads(client.planned_body(stream, plan, span))["threads"][0][
                    "transcript"
                ].encode()
                for span in plan["parts"]
            )
            == raw
        )


def test_client_429_resends_identical_body(tmp_path, monkeypatch):
    file = tmp_path / "native.jsonl"
    file.write_bytes(native().encode())
    monkeypatch.setenv("COMMONPLACE_TOKEN", "synthetic-token")
    calls, waits = [], []

    def open_request(request, timeout):
        calls.append(request.data)
        if len(calls) == 1:
            raise HTTPError(
                request.full_url,
                429,
                "not due",
                {},
                io.BytesIO(b'{"error":"Import not due; retry in 900 seconds"}'),
            )
        return io.BytesIO(b'{"errors":0}')

    monkeypatch.setattr(client, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    monkeypatch.setattr(client.time, "sleep", waits.append)
    assert client.main([str(file), "--source", "claude-code", "--private"]) == 0
    assert waits == [900] and len(calls) == 2 and calls[0] == calls[1]
