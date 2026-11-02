"""Private opt-in capture using synthetic users, connections and native files."""

import hashlib
import http.client
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from test_commonplace_import import api, surface as surface

from commonplace import capture, capture_client, client_hooks, plans, sessions
from commonplace.handoff.thread_store import ThreadStorageLimit
from commonplace.policy import Policy


def connection(s, source="claude-code", **policy):
    s.connection_count = getattr(s, "connection_count", 0) + 1
    _, secret = s.db.create_token(
        workspace_ref=s.workspace,
        user_ref=s.user,
        name=f"native-{s.connection_count}",
        client=source,
        policy=Policy.build(profile="slim", trust="untrusted", write_wing="inbox", **policy),
        actor=s.user,
    )
    return secret


def native(source="claude-code", text="  café 🦉\r\nKeep every word.\t", identity="session-1"):
    if source == "claude-code":
        record = {
            "type": "user",
            "sessionId": identity,
            "timestamp": "2026-10-20T12:34:56Z",
            "message": {"role": "user", "content": text},
        }
    else:
        record = {
            "type": "response_item",
            "timestamp": "2026-10-20T12:34:56Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
    return json.dumps(record, ensure_ascii=False) + "\r\n"


def enable(s, value=True, **kwargs):
    return api(s, "POST", "/api/settings/capture", {"enabled": value}, **kwargs)


def send(
    s,
    bearer,
    raw=None,
    *,
    source="claude-code",
    offset=0,
    prefix=capture.EMPTY_SHA256,
    identity="session-1",
):
    return api(
        s,
        "POST",
        "/api/capture",
        {
            "source": source,
            "thread_id": identity,
            "offset": offset,
            "prefix_sha256": prefix,
            "transcript": native(source) if raw is None else raw,
        },
        cookie=False,
        token=bearer,
    )


def saved(s, stored_id):
    status, result = api(s, "GET", f"/api/threads/{stored_id}/messages")
    assert status == 200
    return result


def test_default_off_owner_only_opt_in_and_opt_out_at_storage_limit(surface, monkeypatch):
    s = surface
    bearer = connection(s)
    assert not s.db.user(s.user)["is_admin"]
    assert api(s, "GET", "/api/settings/capture")[1] == {
        "enabled": False,
        "interval_seconds": 900,
        "private": True,
    }
    assert send(s, bearer)[0] == 409
    assert not s.gateway.handoff.has_threads(s.workspace)
    assert enable(s, cookie=False, token=bearer)[0] == 403
    assert enable(s, headers={"Origin": "https://unrelated.example"})[0] == 403
    member = s.db.create_user("member@example.com")
    s.db.add_member(s.workspace, member, role="member", actor=s.user)
    member_cookie = sessions.start_session(s.db, member, s.workspace)
    assert enable(s, headers={"Cookie": f"{sessions.COOKIE}={member_cookie}"})[0] == 403
    assert enable(s)[0] == 200
    assert send(s, bearer)[0] == 200
    monkeypatch.setitem(plans.PLANS, "free", replace(plans.FREE, storage_bytes=1))
    assert enable(s, False)[0] == 200  # Always allow stopping, even when full.
    assert send(s, bearer)[0] == 409
    assert api(s, "GET", "/api/settings/capture", cookie=False)[0] == 401


@pytest.mark.parametrize("source", capture.SOURCES)
def test_native_growth_retry_exact_raw_provenance_and_private_scope(surface, monkeypatch, source):
    s = surface
    now = plans._now()
    monkeypatch.setattr(plans, "_now", lambda: now)
    bearer = connection(s, source)
    enable(s)
    first = native(source)
    status, result = send(s, bearer, first, source=source)
    assert status == 200 and result["status"] == "appended"
    stored_id = result["stored_thread_id"]
    before = saved(s, stored_id)
    assert before[0]["raw"] == first
    assert before[1]["text"] == "  café 🦉\r\nKeep every word.\t"
    assert before[1]["at"] == "2026-10-20T12:34:56Z"
    assert before[1]["provenance"]["approval_status"] == "proposal"
    record = api(s, "GET", f"/api/threads/{stored_id}")[1]
    assert record["wing"] == "private_chats"
    assert record["created_by"].startswith("conn:native-")
    assert api(s, "GET", f"/api/threads/{stored_id}", cookie=False, token=bearer)[0] == 404
    assert send(s, bearer, first, source=source)[1]["status"] == "duplicate"
    suffix = native(source, text="second exact message") + "\n" + '{"unknown":"retained"}\n'
    assert (
        send(
            s,
            bearer,
            suffix,
            source=source,
            offset=result["offset"],
            prefix=result["prefix_sha256"],
        )[0]
        == 429
    )
    now += timedelta(seconds=901)
    status, grown = send(
        s, bearer, suffix, source=source, offset=result["offset"], prefix=result["prefix_sha256"]
    )
    assert status == 200 and grown["stored_thread_id"] == stored_id
    assert grown["offset"] == len((first + suffix).encode())
    assert grown["prefix_sha256"] == hashlib.sha256((first + suffix).encode()).hexdigest()
    after = saved(s, stored_id)
    assert after[: len(before)] == before
    assert "".join(m["raw"] for m in after if m["name"] == capture.RAW_NAME) == first + suffix
    assert send(s, bearer, first, source=source)[1]["status"] == "duplicate"  # Stale retry.
    assert send(s, bearer, native(source, "edited"), source=source)[0] == 409
    assert saved(s, stored_id) == after


def test_connection_and_workspace_isolation_and_revocation(surface):
    s = surface
    enable(s)
    one, two = connection(s), connection(s)
    _, first = send(s, one)
    _, second = send(s, two)
    assert first["stored_thread_id"] != second["stored_thread_id"]
    # Even a standard thread-reading token cannot read the private capture wing.
    _, reader = s.db.create_token(
        workspace_ref=s.workspace,
        user_ref=s.user,
        name="reader",
        client="generic",
        policy=Policy.build(
            profile="standard", trust="standard", read_wings=["threads", "private_chats"]
        ),
        actor=s.user,
    )
    assert (
        api(s, "GET", f"/api/threads/{first['stored_thread_id']}", cookie=False, token=reader)[0]
        == 404
    )
    assert send(s, connection(s, "codex"))[0] == 403
    assert send(s, connection(s, scopes=["memory:read"]))[0] == 403
    other = s.db.create_user("other-capture@example.com")
    workspace = s.db.create_workspace("isolated-capture", other, personal=True)
    cookie = sessions.start_session(s.db, other, workspace)
    assert (
        api(
            s,
            "GET",
            f"/api/threads/{first['stored_thread_id']}",
            headers={"Cookie": f"{sessions.COOKIE}={cookie}"},
        )[0]
        == 404
    )
    assert not api(
        s, "GET", "/api/settings/capture", headers={"Cookie": f"{sessions.COOKIE}={cookie}"}
    )[1]["enabled"]
    row = s.db.authenticate_token(one)
    s.db.revoke(row.connection_id, actor=s.user)
    assert send(s, one)[0] == 401


def test_quota_atomic_rollback_retry_and_duplicate_when_full(surface, monkeypatch):
    s = surface
    bearer = connection(s)
    enable(s)
    monkeypatch.setitem(plans.PLANS, "free", replace(plans.FREE, storage_bytes=1024))
    assert send(s, bearer)[0] == 507
    assert not s.gateway.handoff.has_threads(s.workspace)
    monkeypatch.setitem(plans.PLANS, "free", replace(plans.FREE, sync_interval_seconds=0))
    _, initial = send(s, bearer)
    store = s.gateway.handoff.store_for(s.workspace)
    before = store.messages(initial["stored_thread_id"])
    check = store._check_storage_budget
    checks = []

    def fail_after_write(maximum, messages=()):
        check(maximum, messages)
        checks.append(bool(messages))
        if not messages:
            raise ThreadStorageLimit()

    with patch.object(store, "_check_storage_budget", fail_after_write):
        assert (
            send(
                s,
                bearer,
                native(text="suffix"),
                offset=initial["offset"],
                prefix=initial["prefix_sha256"],
            )[0]
            == 507
        )
    assert checks == [True, False]
    assert store.messages(initial["stored_thread_id"]) == before
    assert (
        send(
            s,
            bearer,
            native(text="suffix"),
            offset=initial["offset"],
            prefix=initial["prefix_sha256"],
        )[0]
        == 200
    )
    monkeypatch.setitem(plans.PLANS, "free", replace(plans.FREE, storage_bytes=1))
    assert send(s, bearer)[1]["status"] == "duplicate"


def test_concurrent_same_chunk_and_quota_recheck(surface, monkeypatch):
    s = surface
    bearer = connection(s)
    enable(s)
    monkeypatch.setitem(plans.PLANS, "free", replace(plans.FREE, sync_interval_seconds=0))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(s, bearer), range(2)))
    assert sorted(result[1]["status"] for result in results) == ["appended", "duplicate"]
    stored_id = results[0][1]["stored_thread_id"]
    store = s.gateway.handoff.store_for(s.workspace)
    assert len(store.messages(stored_id)) == 2
    # Real quota refusal: no speculative rows, chunks, digest or cursor advance.
    monkeypatch.setitem(
        plans.PLANS, "free", replace(plans.FREE, storage_bytes=200_000, sync_interval_seconds=0)
    )
    current = capture.cursor(store, store.get(stored_id))
    assert (
        send(
            s,
            bearer,
            native(text="x" * 100_000),
            offset=current["offset"],
            prefix=current["prefix_sha256"],
        )[0]
        == 507
    )
    assert capture.cursor(store, store.get(stored_id)) == current


def test_slow_upload_does_not_lock_out_workspace_opt_out(surface):
    s = surface
    bearer = connection(s)
    enable(s)
    reading = threading.Event()
    read_json = capture._read_json

    def observed(handler, *args, **kwargs):
        reading.set()
        return read_json(handler, *args, **kwargs)

    pending = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
    try:
        with patch.object(capture, "_read_json", observed):
            pending.putrequest("POST", "/api/capture")
            pending.putheader("Content-Type", "application/json")
            pending.putheader("Authorization", f"Bearer {bearer}")
            pending.putheader("Content-Length", "100")
            pending.endheaders(b"{")
            assert reading.wait(2)
            assert enable(s, False)[0] == 200
            pending.send(b" " * 98 + b"}")
            assert pending.getresponse().status == 409
    finally:
        pending.close()


@pytest.mark.parametrize("bad", ['{"type":"user"', '{"type":"user"\n', "not-json\n"])
def test_partial_or_invalid_lines_never_become_saved_prefix(surface, bad):
    bearer = connection(surface)
    enable(surface)
    assert send(surface, bearer, bad)[0] == 400
    store = surface.gateway.handoff.store_for(surface.workspace)
    assert not store.by_source(surface.workspace, "session-1")


@pytest.mark.parametrize("source", capture.SOURCES)
def test_explicit_root_runner_growth_rotation_prefix_edit_and_opt_out(
    surface, monkeypatch, tmp_path, capsys, source
):
    s = surface
    enable(s)
    plans.set_plan(s.db, s.user, "paid", actor=s.user)
    monkeypatch.setattr(plans, "load_ratio", lambda: 0)
    bearer = connection(s, source)
    monkeypatch.setenv("COMMONPLACE_URL", f"http://127.0.0.1:{s.port}")
    monkeypatch.setenv("COMMONPLACE_TOKEN", bearer)
    root = tmp_path / "selected"
    root.mkdir()
    path = root / "original.jsonl"
    first = native(source)
    if source == "codex":
        first = '{"type":"session_meta","payload":{"id":"session-1"}}\n' + first
    path.write_bytes(first.encode())
    argv = ["--source", source, "--root", str(root)]
    assert capture_client.main(argv) == 0
    initial = json.loads(capsys.readouterr().out)
    before = saved(s, initial["stored_thread_id"])
    # Rotation by rename, followed by growth, retains the native identity.
    renamed = path.with_name("rotated.jsonl")
    path.rename(renamed)
    suffix = native(source, "next")
    renamed.write_bytes((first + suffix + '{"type":').encode())
    assert capture_client.main(argv) == 0
    grown = json.loads(capsys.readouterr().out)
    assert grown["stored_thread_id"] == initial["stored_thread_id"]
    assert grown["offset"] == len((first + suffix).encode())
    after = saved(s, grown["stored_thread_id"])
    assert after[: len(before)] == before
    renamed.write_bytes((first.replace("Keep", "Edit") + suffix).encode())
    assert capture_client.main(argv) == 1
    assert "prefix changed" in capsys.readouterr().err
    renamed.write_bytes(first.encode())
    assert capture_client.main(argv) == 1
    assert "shrank" in capsys.readouterr().err
    enable(s, False)
    with patch.object(
        capture_client, "sync_file", side_effect=AssertionError("must not read local files")
    ):
        assert capture_client.main(argv) == 0
    assert saved(s, grown["stored_thread_id"]) == after


def test_hook_explicit_opt_in_detaches_without_source_reads_or_secret_arguments(monkeypatch):
    stdin = io.StringIO(json.dumps({"transcript_path": "/synthetic/session.jsonl"}))
    args = ["--event", "capture", "--client", "claude"]
    with patch.object(client_hooks.subprocess, "Popen") as spawn:
        assert client_hooks.main(args, stdin=stdin, stdout=io.StringIO()) == 0
        spawn.assert_not_called()
        monkeypatch.setenv("COMMONPLACE_CAPTURE", "on")
        monkeypatch.setenv("COMMONPLACE_TOKEN", "synthetic-secret")
        stdin.seek(0)
        with patch.object(Path, "open", side_effect=AssertionError("foreground read")):
            assert client_hooks.main(args, stdin=stdin, stdout=io.StringIO()) == 0
        command = spawn.call_args.args[0]
        assert "synthetic-secret" not in " ".join(command)
        assert "commonplace.capture_client" in command


@pytest.mark.parametrize(
    "url",
    ["http://remote.example", "https://name:secret@example.com", "https://example.com?q=secret"],
)
def test_runner_refuses_unsafe_endpoints(url):
    with pytest.raises(capture_client.CaptureError):
        capture_client.endpoint(url)
