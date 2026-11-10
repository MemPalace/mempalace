"""Imported history recall and non-widening access regressions."""

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

import pytest

from commonplace import mailer, plans, ratelimit, session, sessions, sync_protocol, urls
from commonplace.capture import EMPTY_SHA256
from commonplace.control_db import ControlDB
from commonplace.extensions import ToolContext
from commonplace.gateway import Gateway
from commonplace.handoff.palace_mirror import mirror_pending
from commonplace.policy import Policy
from commonplace.rules import checks
from commonplace.workers import WorkerSupervisor
from tests._commonplace_helpers import InProcessLauncher, functional_worker_transport  # noqa: F401
from tests._mcp_server_helpers import _patch_mcp_server
from tests.test_commonplace_email_auth import Client, Mailbox


def native(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


@pytest.fixture
def world(tmp_path, monkeypatch, config, kg):
    clock = {"now": datetime(2026, 11, 3, 12, tzinfo=timezone.utc)}
    monkeypatch.setattr(plans, "_now", lambda: clock["now"])
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "open")
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace import palace_graph

    monkeypatch.setattr(
        palace_graph, "_get_tunnel_file", lambda *a, **k: str(tmp_path / "tunnels.json")
    )
    monkeypatch.setattr(palace_graph, "_legacy_tunnel_file", lambda: str(tmp_path / "legacy.json"))
    mailbox = Mailbox()
    monkeypatch.setattr(mailer, "from_env", lambda: mailbox)
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    supervisor = WorkerSupervisor(str(tmp_path), launcher=InProcessLauncher(monkeypatch))
    gateway = Gateway(db, supervisor)
    mailbox.auth = gateway.email_auth
    gateway.limiter = ratelimit.TokenBuckets(clock=lambda: 0.0)
    server = gateway.build_server("127.0.0.1", 0)
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(urls, "APP_URL", origin)
    monkeypatch.setattr(urls, "AGENT_URL", origin)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    client = Client(gateway, server.server_address[1])
    client.db = db
    cookie = client.signin(mailbox, "synthetic@example.test")
    me = client.me(cookie)
    yield SimpleNamespace(
        client=client, cookie=cookie, me=me, gateway=gateway, mailbox=mailbox, clock=clock
    )
    server.shutdown()
    server.server_close()
    gateway.webhooks.stop()
    if getattr(gateway, "handoff", None) is not None:
        gateway.handoff.close()
    supervisor.stop_all()
    db.close()


def _post(w, path, data):
    return w.client.request(
        path, "POST", cookie=w.cookie, headers={"Origin": urls.APP_URL}, data=data
    )


def words(label):
    return f"  zqimport{label} café 🦉\r\nKeep  every word.\t"


def drain(w):
    w.gateway.handoff.service.wait_for_mirrors(timeout=30)


def post(w, path, data, token=None):
    w.clock["now"] += timedelta(seconds=901)
    headers = {"Origin": urls.APP_URL}
    if token:
        headers["Authorization"] = "Bearer " + token
    status, _, raw = w.client.request(
        path, "POST", cookie=None if token else w.cookie, headers=headers, data=data
    )
    drain(w)
    return status, json.loads(raw)


def payload(text, source="claude-code"):
    tid = "synthetic-session"
    fmt, transcript = "claude-code", native(text)
    if source == "native-memory":
        fmt, transcript = "file", text
        tid = sync_protocol.memory_thread_id(
            "claude", "memory/notes.md", hashlib.sha256(text.encode()).hexdigest()
        )
    return {
        "source": source,
        "threads": [{"thread_id": tid, "format": fmt, "transcript": transcript}],
    }


def imported(w, text, token=None, source="claude-code"):
    return post(w, "/api/import", payload(text, source), token)


@pytest.mark.parametrize("wing", [None, "private_chats"])
def test_import_reader_cli_preserves_imported_location(world, wing):
    w = world
    text = words("clirowlocation")
    status, connection = post(
        w,
        "/api/connections",
        {
            "name": "synthetic-import-reader",
            "client": "generic",
            "import": True,
            "import_read": True,
        },
    )
    assert status == 201
    token = connection["token"]
    principal = w.gateway.authenticate("Bearer " + token)
    assert principal.policy.has_scope("memory:import:read")
    assert not principal.policy.has_scope("private:read")
    status, _ = imported(w, text, token)
    assert status == 200
    args = {"query": text.split()[0], **({"wing": wing} if wing else {})}
    status, reply = rpc(w, token, "mempalace_search", args)
    assert status == 200
    structured = tool_value(reply)
    row = next(r for r in structured["results"] if r["drawer_id"].startswith("imported:"))
    assert row["wing"] == "private_chats" and row["room"].startswith("thr_")
    assert args["query"] in row["text"]
    status, reply = rpc(w, token, "mempalace_search", {**args, "cli_compatible": True})
    assert status == 200
    result = tool_value(reply)
    output = result["cli_output"]
    for line in row["text"].strip().split("\n"):
        assert f"      {line}" in output, "Imported text changed in CLI output"
    assert f"private_chats / {row['room']}" in output, "Allowed imported-row location is missing"
    assert result["results"] == []


def search(w, text):
    status, _, raw = w.client.request(
        "/api/memory/search?q=" + quote(text.split()[0]), cookie=w.cookie
    )
    return status, json.loads(raw)


def grant(w):
    return post(
        w, "/api/connections", {"name": "synthetic-grant", "client": "generic", "import": True}
    )


def operator(w, scopes=None, user=None, policy=None):
    w.token_serial = getattr(w, "token_serial", 0) + 1
    owner = w.client.db.role(w.me["workspace"], user or w.me["user"]) == "owner"
    return w.client.db.create_token(
        workspace_ref=w.me["workspace"],
        user_ref=user or w.me["user"],
        name=f"synthetic-reader-{w.token_serial}",
        client="generic",
        policy=policy
        or Policy.build(
            profile="standard",
            scopes=scopes or ["memory:read", "threads:read"] + (["private:read"] if owner else []),
            read_wings=["*"],
            trust="standard",
        ),
        actor=w.me["user"],
    )


def rpc(w, token, name, args):
    status, _, raw = w.client.request(
        "/mcp",
        "POST",
        headers={"Authorization": "Bearer " + token, "Origin": urls.APP_URL},
        data={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )
    return status, json.loads(raw)


def quoted(text, reply):
    # Match exact JSON-quoted words inside public tool output.
    return json.dumps(text, ensure_ascii=False)[1:-1] in "\n".join(strings(reply))


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def mirror(w, result, *, private=False):
    ctx = w.gateway.handoff
    store = ctx.store_for(w.me["workspace"])
    if private:
        stored = store.by_source(
            w.me["workspace"],
            f"import-private:{w.me['user']}:claude-code:synthetic-session",
        )
    else:
        stored = store.get(result["results"][0]["stored_thread_id"])
    principal = sessions.person_principal(w.client.db, w.me["user"], w.me["workspace"])
    ids = mirror_pending(ctx.call_tool, principal, stored, store)
    if ids:
        return ids
    result = ctx.call_tool(
        principal,
        "mempalace_list_drawers",
        {
            "wing": "private_threads",
            "room": stored.id,
        },
    )
    return [row["drawer_id"] for row in result.get("drawers", [])]


def test_imported_search_r01_acl_import_http_search(world, monkeypatch):
    w = world
    text = words("acl")
    status, result = imported(w, text)
    assert status == 200
    status, reply = search(w, text)
    assert status == 200
    assert any(text in record["text"] for record in reply["results"])  # RED
    assert any(
        record["source"]["thread"] == result["results"][0]["stored_thread_id"]
        for record in reply["results"]
        if text in record["text"]
    )


def test_imported_search_r02_grant_import_http_search(world, monkeypatch):
    w = world
    text = words("grant")
    status, connection = grant(w)
    assert status == 201
    status, result = imported(w, text, connection["token"])
    assert status == 200
    status, reply = search(w, text)
    assert status == 200
    assert any(text in record["text"] for record in reply["results"])  # RED


def test_imported_search_r03_native_memory_import_http_search(world, monkeypatch):
    w = world
    text = words("nativememory")
    status, connection = grant(w)
    assert status == 201
    status, result = imported(w, text, connection["token"], "native-memory")
    assert status == 200
    status, reply = search(w, text)
    assert status == 200
    assert any(text in record["text"] for record in reply["results"])  # RED


def test_imported_search_r04_capture_http_search(world, monkeypatch):
    w = world
    text = words("capture")
    status, _ = post(w, "/api/settings/capture", {"enabled": True})
    assert status == 200
    status, result = post(
        w,
        "/api/capture",
        {
            "source": "claude-code",
            "thread_id": "synthetic-capture",
            "offset": 0,
            "prefix_sha256": EMPTY_SHA256,
            "transcript": native(text),
        },
    )
    assert status == 200
    status, reply = search(w, text)
    assert status == 200
    assert any(text in record["text"] for record in reply["results"])  # RED


def test_imported_search_r05_acl_import_mcp_memory_search(world, monkeypatch):
    w = world
    text = words("mcpmemory")
    status, result = imported(w, text)
    assert status == 200
    _, token = operator(w)
    status, reply = rpc(w, token, "memory_search", {"query": text.split()[0]})
    assert status == 200
    assert quoted(text, reply)  # RED


def test_imported_search_r06_acl_import_mcp_session_start(world, monkeypatch):
    w = world
    text = words("mcpsession")
    status, result = imported(w, text)
    assert status == 200
    _, token = operator(w)
    status, reply = rpc(w, token, "session_start", {"query": text.split()[0]})
    assert status == 200
    assert quoted(text, reply)  # RED
    assert "Relevant memories" in "\n".join(strings(reply))


def test_imported_search_r07_acl_import_startup_injection(
    world,
    monkeypatch,
    functional_worker_transport,  # noqa: F811
):
    # A fixed clock still leaves an 80 ms wall-clock worker socket timeout.
    # Use the functional I/O allowance while retaining logical deadline checks.
    w = world
    text = words("startup")
    status, result = imported(w, text)
    assert status == 200
    _, token = operator(w)
    monkeypatch.setattr(checks, "monotonic", lambda: 0.0)
    root = Path(w.gateway.supervisor.workspace_dir(w.me["workspace"]))
    marker = root / "palace" / "chroma.sqlite3"
    marker.parent.mkdir(exist_ok=True)
    with sqlite3.connect(marker) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS embeddings(id TEXT)")
        conn.execute("INSERT INTO embeddings VALUES ('memory')")
    principal = w.gateway.authenticate("Bearer " + token)
    reply = session.initialize_context(
        ToolContext(principal, w.gateway), budget=3000, query=text.split()[0]
    )
    assert quoted(text, reply)  # RED


def test_imported_search_r08_mirror_hidden_without_thread_read(world):
    w = world
    text = words("nothread")
    status, result = imported(w, text)
    assert status == 200
    ids = mirror(w, result)
    _, token = operator(w, ["memory:read", "private:read"])
    status, reply = rpc(w, token, "memory_search", {"query": text.split()[0]})
    assert status == 200
    assert not quoted(text, reply)  # RED: match the full imported words, not a header
    for drawer_id in ids:
        status, reply = rpc(w, token, "memory_get", {"id": drawer_id})
        assert status == 200
        assert not quoted(text, reply)
        assert "memory not found" in "\n".join(strings(reply))


def test_imported_search_r09_member_cannot_find_owner_grant_import(world):
    w = world
    text = words("member")
    status, connection = grant(w)
    assert status == 201
    status, result = imported(w, text, connection["token"])
    assert status == 200
    ids = shared_mirror(w, mirror(w, result, private=True), text)
    cookie = w.client.signin(w.mailbox, "second@example.test")
    user = w.client.me(cookie)["user"]
    w.client.db.add_member(w.me["workspace"], user, "member", actor=w.me["user"])
    _, token = operator(w, user=user)
    shared_read_control(w, token)
    status, reply = rpc(w, token, "memory_search", {"query": text.split()[0]})
    assert status == 200
    assert not quoted(text, reply)  # RED: match the full imported words, not a header
    for drawer_id in ids:
        status, reply = rpc(w, token, "memory_get", {"id": drawer_id})
        assert status == 200
        assert not quoted(text, reply)


def test_imported_search_g01_default_pat_never_finds_import(world):
    w = world
    text = words("defaultpat")
    status, result = imported(w, text)
    assert status == 200
    mirror(w, result)
    control(w, text)
    status, connection = post(w, "/api/connections", {"name": "synthetic", "client": "generic"})
    assert status == 201
    status, reply = rpc(w, connection["token"], "memory_search", {"query": text.split()[0]})
    assert status == 200
    assert not quoted(text, reply)


def test_imported_search_g02_oauth_shaped_token_never_finds_import(world):
    w = world
    text = words("oauth")
    status, result = imported(w, text)
    assert status == 200
    mirror(w, result)
    control(w, text)
    _, token = operator(
        w,
        policy=Policy.build(
            profile="slim",
            trust="standard",
            write_wing="inbox",
            read_wings=["shared", "inbox", "project_*"],
            scopes=["memory:read"],
        ),
    )
    status, reply = rpc(w, token, "memory_search", {"query": text.split()[0]})
    assert status == 200
    assert not quoted(text, reply)


def test_imported_search_g03_threads_read_without_private_read(world):
    w = world
    text = words("noprivate")
    status, result = imported(w, text)
    assert status == 200
    mirror(w, result)
    control(w, text)
    status, connection = grant(w)
    assert status == 201
    private_text = words("noprivategrant")
    status, result = imported(w, private_text, connection["token"])
    assert status == 200
    mirror(w, result, private=True)
    status, _ = post(w, "/api/settings/capture", {"enabled": True})
    assert status == 200
    capture_text = words("noprivatecapture")
    status, _ = post(
        w,
        "/api/capture",
        {
            "source": "claude-code",
            "thread_id": "guard-capture",
            "offset": 0,
            "prefix_sha256": EMPTY_SHA256,
            "transcript": native(capture_text),
        },
    )
    assert status == 200
    store = w.gateway.handoff.store_for(w.me["workspace"])
    principal = sessions.person_principal(w.client.db, w.me["user"], w.me["workspace"])
    for stored in store.list_source_range(w.me["workspace"], "capture:", limit=20):
        mirror_pending(w.gateway.handoff.call_tool, principal, stored, store)
    _, token = operator(w, ["memory:read", "threads:read"])
    for value in (text, private_text, capture_text):
        control(w, value)
        status, reply = rpc(w, token, "memory_search", {"query": value.split()[0]})
        assert status == 200
        assert not quoted(value, reply)


def test_imported_search_g04_exact_reimport_is_noop(world):
    w = world
    text = words("reimport")
    status, first = imported(w, text)
    assert status == 200
    status, second = imported(w, text)
    assert status == 200
    assert second["results"][0]["stored_thread_id"] == first["results"][0]["stored_thread_id"]
    assert second["results"][0]["status"] == "duplicate"
    mirror(w, first)
    control(w, text)


def test_imported_search_g05_revoked_reader_token_refused(world):
    w = world
    text = words("revoked")
    status, result = imported(w, text)
    assert status == 200
    mirror(w, result)
    control(w, text)
    cid, token = operator(w)
    control(w, text, token)
    w.client.db.revoke(cid, actor=w.me["user"], workspace_ref=w.me["workspace"])
    status, reply = rpc(w, token, "memory_search", {"query": text.split()[0]})
    assert status in (401, 403)
    assert not quoted(text, reply)


def control(w, text, token=None):
    if token is None:
        _, token = operator(w)
    status, reply = rpc(w, token, "memory_search", {"query": text.split()[0]})
    assert status == 200
    assert quoted(text, reply)


def writer(w, user=None):
    owner = w.client.db.role(w.me["workspace"], user or w.me["user"]) == "owner"
    return operator(
        w,
        user=user,
        policy=Policy.build(
            profile="full",
            trust="standard",
            read_wings=["*"],
            write_wings=["*"],
            scopes=["memory:read", "memory:write", "threads:read"]
            + (["private:read", "private:write"] if owner else []),
        ),
    )[1]


def member(w):
    cookie = w.client.signin(w.mailbox, "member@example.test")
    user = w.client.me(cookie)["user"]
    w.client.db.add_member(w.me["workspace"], user, "member", actor=w.me["user"])
    return user


def tool_value(reply):
    try:
        return json.loads(reply["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        return {}


def mirror_record(w, ids, text, token):
    for drawer_id in ids:
        status, reply = rpc(w, token, "mempalace_get_drawer", {"drawer_id": drawer_id})
        record = tool_value(reply)
        if status == 200 and isinstance(record, dict) and text in record.get("content", ""):
            return record
    return {}


def private_mirror(w, text):
    status, connection = grant(w)
    assert status == 201
    status, result = imported(w, text, connection["token"])
    assert status == 200
    ids = mirror(w, result, private=True)
    control(w, text)
    return ids


def shared_read_control(w, token, wing="shared", owner=None):
    """Prove the real actor can read ordinary content in the tested wing."""
    content = "Public fixture independently readable control"
    status, reply = rpc(
        w,
        owner or writer(w),
        "mempalace_add_drawer",
        {"wing": wing, "room": "read-control", "content": content},
    )
    assert status == 200 and tool_value(reply).get("success"), reply
    drawer_id = tool_value(reply)["drawer_id"]
    status, reply = rpc(w, token, "mempalace_get_drawer", {"drawer_id": drawer_id})
    assert status == 200 and tool_value(reply).get("content") == content, reply
    return drawer_id


def shared_mirror(w, ids, text):
    """Put a legacy mirror copy in a readable wing, retaining its protected room."""
    owner = writer(w)
    record = mirror_record(w, ids, text, owner)
    assert text in record.get("content", ""), record
    status, reply = rpc(
        w,
        owner,
        "mempalace_add_drawer",
        {
            "wing": "shared",
            "room": record["room"],
            "content": record["content"],
            "source_file": f"handoff:{record['room']}#v2:0-0:conversation:0",
        },
    )
    assert status == 200 and tool_value(reply).get("success"), reply
    return [tool_value(reply)["drawer_id"]]


def refused(w, token, name, **args):
    """Unprojected whole-palace tools must not reach a worker for a member."""
    principal = w.gateway.authenticate("Bearer " + token)
    assert principal.role == "member" and not principal.policy.has_scope("private:read")
    with patch.object(
        w.gateway.mediator.workers, "post", wraps=w.gateway.mediator.workers.post
    ) as worker:
        status, reply = rpc(w, token, name, args)
    assert worker.call_count == 0, f"Authority leak: member dispatched {name} to a worker"
    assert status == 200 and reply.get("error") == {
        "code": -32601,
        "message": "Unknown tool: " + name,
    }, reply
    return reply


def other_filer_copy(w):
    """Seed a legacy private mirror filed by someone other than the token owner."""
    from commonplace.handoff.models import ThreadMessage
    from mempalace import mcp_server

    user = member(w)
    text = "zqotherfiler exact hidden copy of synthetic original words"
    stored = w.gateway.handoff.store_for(w.me["workspace"]).store(
        w.me["workspace"],
        f"import-private:{user}:hidden-copy",
        [ThreadMessage(0, "user", "message", text, text)],
        actor=user,
        wing="private_threads",
    )
    drawer_id = "drawer_private_threads_" + stored.id + "_legacy"
    collection = mcp_server._get_collection(create=True)
    collection.add(
        ids=[drawer_id],
        documents=[text],
        metadatas=[{"wing": "private_threads", "room": stored.id}],
    )
    assert collection.get(ids=[drawer_id], include=["documents"])["documents"] == [text]
    status, reply = rpc(w, writer(w), "mempalace_get_drawer", {"drawer_id": drawer_id})
    assert status == 200 and text not in json.dumps(reply), reply
    return text


def test_imported_search_r10_duplicate_words_without_thread_read(world):
    w = world
    text = words("duplicate")
    status, result = imported(w, text)
    assert status == 200
    mirror(w, result)
    control(w, text)
    _, token = operator(w, ["memory:read", "private:read"])
    status, reply = rpc(w, token, "mempalace_check_duplicate", {"content": text, "threshold": 0.0})
    assert status == 200
    assert not quoted(text, reply)  # RED


def test_imported_search_r10b_duplicate_flag_without_thread_read(world):
    w = world
    text = words("duplicateflag")
    status, result = imported(w, text)
    assert status == 200
    ids = mirror(w, result)
    control(w, text)
    owner = writer(w)
    record = mirror_record(w, ids, text, owner)
    document = record.get("content")
    assert isinstance(document, str) and text in document
    public_duplicate_control(w, owner)
    _, token = operator(w, ["memory:read", "private:read"])
    status, reply = rpc(w, token, "mempalace_check_duplicate", {"content": document})
    assert status == 200
    assert "is_duplicate" in tool_value(reply)
    assert tool_value(reply)["is_duplicate"] is False  # RED


def test_imported_search_r11_member_duplicate_words(world):
    w = world
    text = words("memberduplicate")
    private_mirror(w, text)
    _, token = operator(w, user=member(w))
    shared_read_control(w, token)
    refused(w, token, "mempalace_check_duplicate", content=text, threshold=0.0)


def test_imported_search_r11_owner_duplicate_excludes_other_filer(world):
    w = world
    text = other_filer_copy(w)
    owner = writer(w)
    public_duplicate_control(w, owner)
    status, reply = rpc(w, owner, "mempalace_check_duplicate", {"content": text})
    assert status == 200
    assert not quoted(text, reply), "Other filer's duplicate words leaked to the owner"
    assert tool_value(reply).get("is_duplicate") is False, "Hidden copy leaked into duplicate flag"


def public_rooms(w, token):
    for room in ("source", "destination"):
        status, reply = rpc(
            w,
            token,
            "mempalace_add_drawer",
            {
                "wing": "shared",
                "room": room,
                "content": "Public fixture " + room,
            },
        )
        assert status == 200
        assert tool_value(reply)["success"]


def test_imported_search_r12_tunnel_preview_uses_drawer_metadata(world):
    w = world
    text = words("tunnel")
    ids = private_mirror(w, text)
    owner = writer(w)
    record = mirror_record(w, ids, text, owner)
    assert text in record.get("content", "")
    public_rooms(w, owner)
    status, reply = rpc(
        w,
        owner,
        "mempalace_create_tunnel",
        {
            "source_wing": "shared",
            "source_room": "source",
            "target_wing": "shared",
            "target_room": "destination",
            "target_drawer_id": record["drawer_id"],
        },
    )
    assert status == 200
    assert not tool_value(reply).get("error")
    args = {"wing": "shared", "room": "source"}
    status, reply = rpc(w, owner, "mempalace_follow_tunnels", args)
    assert status == 200
    assert quoted(text, reply)
    _, token = operator(w, ["memory:read", "private:read"])
    status, reply = rpc(w, token, "mempalace_follow_tunnels", args)
    assert status == 200
    assert not quoted(text, reply)  # RED
    assert tool_value(reply) == []


def public_duplicate_control(w, token, checkpoint=False):
    content = "Public reference document with unrelated botanical observations."
    status, reply = rpc(
        w,
        token,
        "mempalace_add_drawer",
        {
            "wing": "shared",
            "room": "control",
            "content": content,
        },
    )
    assert status == 200
    assert tool_value(reply).get("success"), reply
    if checkpoint:
        status, reply = rpc(
            w,
            token,
            "mempalace_checkpoint",
            {
                "items": [{"wing": "shared", "room": "control-copy", "content": content}],
            },
        )
        assert status == 200
        assert tool_value(reply).get("duplicates"), reply
    else:
        status, reply = rpc(w, token, "mempalace_check_duplicate", {"content": content})
        assert status == 200
        assert tool_value(reply).get("is_duplicate") is True, reply


def test_imported_search_r13_checkpoint_does_not_skip_hidden_copy(world):
    w = world
    text = words("checkpoint")
    ids = private_mirror(w, text)
    owner = writer(w)
    record = mirror_record(w, ids, text, owner)
    document = record.get("content")
    assert isinstance(document, str) and text in document
    token = writer(w, member(w))
    shared_read_control(w, token)
    status, before = rpc(w, owner, "mempalace_list_drawers", {"wing": "shared", "room": "saved"})
    assert status == 200
    refused(
        w,
        token,
        "mempalace_checkpoint",
        items=[{"wing": "shared", "room": "saved", "content": document}],
    )
    status, after = rpc(w, owner, "mempalace_list_drawers", {"wing": "shared", "room": "saved"})
    assert status == 200 and tool_value(after) == tool_value(before), (
        "Refused checkpoint wrote data"
    )


def test_imported_search_r13_owner_checkpoint_does_not_skip_other_filer_copy(world):
    w = world
    document = other_filer_copy(w)
    owner = writer(w)
    public_duplicate_control(w, owner, checkpoint=True)
    status, reply = rpc(
        w,
        owner,
        "mempalace_checkpoint",
        {"items": [{"wing": "shared", "room": "saved", "content": document}]},
    )
    assert status == 200
    value = tool_value(reply)
    assert "added" in value and "duplicates" in value, reply
    assert value.get("added") and not value.get("duplicates"), (
        "Hidden copy leaked into deduplication"
    )


def test_imported_search_r14_member_cannot_link_owner_thread_room(world):
    w = world
    text = words("linkwrite")
    ids = shared_mirror(w, private_mirror(w, text), text)
    owner = writer(w)
    public_rooms(w, owner)
    status, reply = rpc(w, owner, "mempalace_get_drawer", {"drawer_id": ids[0]})
    assert status == 200
    room = tool_value(reply)["room"]
    args = {
        "source_wing": "shared",
        "source_room": "source",
        "target_wing": "shared",
        "target_room": room,
    }
    # Owner can link the room; the member uses a distinct public source.
    status, reply = rpc(w, owner, "mempalace_create_tunnel", args)
    assert status == 200
    value = tool_value(reply)
    assert not value.get("error")
    # Use the other public source so the refused write has a distinct id.
    args["source_room"] = "destination"
    token = writer(w, member(w))
    shared_read_control(w, token)
    status, reply = rpc(w, token, "mempalace_create_tunnel", args)
    assert status == 200
    refused = tool_value(reply)
    status, reply = rpc(w, owner, "mempalace_list_tunnels", {})
    assert status == 200
    connections = tool_value(reply)
    assert isinstance(connections, list), reply
    assert not any(  # RED
        t.get("source", {}).get("room") == "destination" and t.get("target", {}).get("room") == room
        for t in connections
    )
    assert refused.get("error") == "Room not found"


def test_imported_search_r15_member_cannot_find_owner_derived_fact(world):
    w = world
    text = words("derivedfact")
    ids = shared_mirror(w, private_mirror(w, text), text)
    scopes = [
        "memory:read",
        "memory:write",
        "private:read",
        "private:write",
        "threads:read",
        "kg:read",
        "kg:write",
    ]
    _, owner = operator(
        w,
        policy=Policy.build(
            profile="full",
            scopes=scopes,
            read_wings=["*"],
            write_wings=["*"],
            trust="trusted",
        ),
    )
    record = mirror_record(w, ids, text, owner)
    assert text in record.get("content", "")
    value = "zqimportderivedfactvalue"
    status, reply = rpc(
        w,
        owner,
        "fact_save",
        {
            "subject": "SyntheticProject",
            "predicate": "deploy_port",
            "object": value,
            "theme": "shared",
            "sensitivity": "internal",
            "source": {"thread": record["room"], "seq_start": 1, "seq_end": 1},
        },
    )
    assert status == 200 and not reply.get("error"), reply
    status, reply = rpc(w, owner, "mempalace_search", {"query": "SyntheticProject"})
    assert status == 200
    assert quoted(value, reply), reply
    _, token = operator(
        w,
        user=member(w),
        policy=Policy.build(
            profile="full",
            scopes=["memory:read", "threads:read", "kg:read"],
            read_wings=["*"],
            trust="standard",
        ),
    )
    shared_read_control(w, token)
    status, reply = rpc(w, token, "mempalace_search", {"query": "SyntheticProject"})
    assert status == 200
    assert not quoted(value, reply)  # RED


@pytest.mark.parametrize(
    "wing", ["private_threads", "Private-Threads", "wing_private_threads", "private threads"]
)
@pytest.mark.parametrize("scope", ["see_all", "scoped"])
def test_imported_search_r16_cli_search_excludes_mirror_copies(world, wing, scope):
    w = world
    body = "zqimportprivateclibody"
    text = words("climirror") + " " + body
    ids = private_mirror(w, text)
    owner = writer(w)
    public = "zqimportpubliccli visible shared note"
    status, reply = rpc(
        w,
        owner,
        "mempalace_add_drawer",
        {
            "wing": "shared",
            "room": "notes",
            "content": public,
        },
    )
    assert status == 200 and tool_value(reply).get("success"), reply
    _, token = operator(
        w,
        user=member(w),
        policy=Policy.build(
            profile="full",
            scopes=["memory:read", "threads:read"],
            read_wings=["*"] if scope == "see_all" else ["shared", "private_threads"],
            trust="standard",
        ),
    )
    # The member reads shared content. The existing owner's token can also
    # read the private copy, so CLI exclusion cannot pass only through the
    # member's lack of private:read.
    assert text in mirror_record(w, ids, text, owner).get("content", "")
    for actor in (token, owner):
        status, reply = rpc(
            w, actor, "mempalace_search", {"query": "zqimportpubliccli", "cli_compatible": True}
        )
        assert status == 200
        assert "visible shared note" in "\n".join(strings(reply))
        for args in (
            {"query": text.split()[0], "cli_compatible": True},
            {"query": text.split()[0], "wing": wing, "cli_compatible": True},
        ):
            status, reply = rpc(w, actor, "mempalace_search", args)
            assert status == 200
            assert body not in "\n".join(strings(reply)), (
                "Private mirror words leaked into CLI search"
            )


def test_imported_search_r17_superseded_reference_requires_kept_target(world):
    w = world
    text = words("factreference")
    ids = private_mirror(w, text)
    scopes = [
        "memory:read",
        "memory:write",
        "private:read",
        "private:write",
        "threads:read",
        "kg:read",
        "kg:write",
    ]
    _, owner = operator(
        w,
        policy=Policy.build(
            profile="full", scopes=scopes, read_wings=["*"], write_wings=["*"], trust="trusted"
        ),
    )
    record = mirror_record(w, ids, text, owner)
    assert text in record.get("content", "")
    status, reply = rpc(
        w,
        owner,
        "mempalace_kg_save",
        {
            "subject": "SyntheticReference",
            "predicate": "deploy_port",
            "object": "8000",
            "wing": "shared",
            "single_valued": True,
            "valid_from": "2026-01-01",
        },
    )
    assert status == 200 and tool_value(reply).get("success"), reply
    status, reply = rpc(
        w,
        owner,
        "fact_save",
        {
            "subject": "SyntheticReference",
            "predicate": "deploy_port",
            "object": "zqimportsuccessor",
            "theme": "shared",
            "single_valued": True,
            "valid_from": "2026-02-01",
            "source": {"thread": record["room"], "seq_start": 1, "seq_end": 1},
        },
    )
    assert status == 200 and not reply.get("error"), reply
    status, reply = rpc(w, owner, "mempalace_kg_query", {"entity": "SyntheticReference"})
    assert status == 200
    rows = tool_value(reply).get("facts", [])
    old = next((row for row in rows if row.get("object") == "8000"), {})
    new = next((row for row in rows if row.get("object") == "zqimportsuccessor"), {})
    assert new.get("id") and old.get("superseded_by") == new["id"]
    _, token = operator(
        w,
        user=member(w),
        policy=Policy.build(
            profile="full",
            scopes=["memory:read", "threads:read", "kg:read"],
            read_wings=["*"],
            trust="standard",
        ),
    )
    shared_read_control(w, token)
    # The successor is outside this payload's temporal selection on BOTH revisions.
    status, reply = rpc(
        w, token, "mempalace_kg_query", {"entity": "SyntheticReference", "as_of": "2026-01-15"}
    )
    assert status == 200
    rows = tool_value(reply).get("facts", [])
    assert [row.get("object") for row in rows] == ["8000"]
    assert "superseded_by" not in rows[0]  # RED
