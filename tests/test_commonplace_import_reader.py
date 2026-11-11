"""Explicit reader consent and bounded imported-history access, using synthetic data."""

import hashlib
import json
import re
from dataclasses import replace
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from commonplace import plans, sessions, urls
from commonplace.continuity_read import PrivateReader
from commonplace.handoff.recall import Recall, ThreadNotFound
from commonplace.import_sources import parse_import
from commonplace.policy import Policy
from tests._commonplace_helpers import functional_worker_transport as functional_worker_transport
from tests._mirror_drain import drain_service
from tests.test_commonplace_email_auth import fresh as fresh
from tests.test_commonplace_import_grant import (
    IMPORT_SCOPES,
    READ_WRITE,
    REDIRECT,
    VERIFIER,
    WORDS,
    Journey,
    body,
    journey as journey,
    native,
)
from tests.test_imported_history_search import world as world

READER = "memory:import:read"
READ_SCOPE = "memory:read " + READER
COPY = (
    "Allow this agent to read and search your past and future conversations and memory files "
    "imported through the connection import route in this workspace, including imports from "
    "your other connections. Imports through the signed-in/full-ACL route, live session captures "
    "and other people's history are excluded. To stop access, revoke this connection."
)


def mint(j, **extra):
    j.serial += 1
    status, _, raw = j.request(
        "/api/connections",
        "POST",
        owner=j.owner,
        data={"name": f"reader-{j.serial}", "import_read": True, **extra},
    )
    assert status == 201
    return json.loads(raw)


def principal(j, token):
    return j.client.gateway.authenticate("Bearer " + token)


def oauth(j, *, scope=READ_SCOPE, tick=True, import_tick=False, decision="allow"):
    cid = j.register()["client_id"]
    path, page = j.authorize(cid, scope)
    assert page[0] == 200
    raw = page[2].decode()
    request = unescape(re.search(r'name="request" value="([^"]+)"', raw)[1])
    data = {"request": request, "decision": decision}
    if tick:
        data["grant_import_read"] = "1"
    if import_tick:
        data["grant_import"] = "1"
    status, headers, _ = j.form(path, data, owner=j.owner)
    assert status == 303
    query = parse_qs(urlsplit(headers["Location"]).query)
    if decision == "deny":
        assert query["error"] == ["access_denied"] and "code" not in query
        return cid, None
    assert "code" in query
    status, _, raw = j.form(
        "/oauth/token",
        {
            "grant_type": "authorization_code",
            "client_id": cid,
            "code": query["code"][0],
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
            "resource": urls.mcp_url(),
        },
    )
    assert status == 200
    return cid, json.loads(raw)


def listed(j, token):
    status, _, raw = j.request("/api/imports", token=token)
    assert status == 200
    return json.loads(raw)


def raw_view(j, token, stored_id):
    status, _, raw = j.request(f"/api/threads/{stored_id}/messages", token=token)
    assert status == 200
    return "".join(
        m["raw"]
        for m in json.loads(raw)
        if m["name"] in ("import.raw", "capture.raw") or m["seq"] == 0 and m["role"] == "system"
    )


@pytest.fixture
def live(world, monkeypatch):
    j = Journey(world.client, world.mailbox)
    j.owner = SimpleNamespace(
        cookie=world.cookie, user=world.me["user"], workspace=world.me["workspace"]
    )
    monkeypatch.setattr(plans, "_now", lambda: j.now)
    return j


def drain(j):
    drain_service(j.client.gateway.handoff.service)


def memory(j, token, name, args):
    return j.rpc(token, "tools/call", {"name": name, "arguments": args})


def rendered(value):
    return json.dumps(value, ensure_ascii=False)


def has_words(value, text):
    return (
        text in rendered(value)
        or json.dumps(text, ensure_ascii=False)[1:-1] in rendered(value)
        or json.dumps(json.dumps(text, ensure_ascii=False), ensure_ascii=False)[1:-1]
        in rendered(value)
    )


def test_r01_pat_consent(journey):
    j = journey
    reader = mint(j)
    p = principal(j, reader["token"])
    assert set(p.policy.scopes) == {"memory:read", "memory:write", READER}
    assert p.policy.trust == "untrusted" and not p.policy.can_read_wing("private_chats")
    assert reader["connection"]["import_read"] is True
    for value in (None, False, "true", 1, [], {}):
        j.serial += 1
        data = {"name": f"plain-{j.serial}"}
        if value is not None:
            data["import_read"] = value
        status, _, raw = j.request("/api/connections", "POST", owner=j.owner, data=data)
        if value is None or value is False:
            assert status == 201
            assert READER not in principal(j, json.loads(raw)["token"]).policy.scopes
        else:
            assert status == 400
    writer = j.pat()
    assert READER not in principal(j, writer["token"]).policy.scopes
    source = (Path(__file__).parents[1] / "commonplace/web/static/agents.js").read_text()
    assert COPY in source
    assert re.search(r'type="checkbox" name="import_read"[^>]*>', source)
    assert not re.search(r'name="import_read"[^>]*checked', source)
    assert 'data.import_read = form.elements.namedItem("import_read").checked' in source
    assert "form.reset()" in source
    assert "Imported history: allowed" in source


def test_r02_oauth_consent(journey):
    j = journey
    cid = j.register()["client_id"]
    path, page = j.authorize(cid, READ_SCOPE + " memory:import memory:write")
    assert page[0] == 200
    html = unescape(page[2].decode())
    assert COPY in html
    for name in ("grant_import", "grant_import_read"):
        tag = re.search(r'<input[^>]*name="' + name + r'"[^>]*>', html)[0]
        assert "checked" not in tag
    request = re.search(r'name="request" value="([^"]+)"', html)[1]
    other = j.signup("other@example.test")
    assert (
        j.form(
            path, {"request": request, "decision": "allow", "grant_import_read": "1"}, owner=other
        )[0]
        == 403
    )
    _, pair = oauth(j)
    assert set(pair["scope"].split()) == set(READ_SCOPE.split())
    assert principal(j, pair["access_token"]).policy.trust == "standard"
    oauth(j, decision="deny")
    metadata = json.loads(j.request("/.well-known/oauth-authorization-server")[2])
    assert READER in metadata["scopes_supported"]
    assert (
        READER
        not in json.loads(j.request("/.well-known/oauth-protected-resource")[2])["scopes_supported"]
    )


def test_r03_recall_boundary(journey, tmp_path):
    j = journey
    token = j.pat()["token"]
    assert j.upload(body(), token=token)["accepted"] == 1
    p = principal(j, token)
    # replace does not validate newly supported scopes on the pinned revision.
    p = replace(p, policy=replace(p.policy, scopes=("memory:read", READER)))
    ctx = j.client.gateway.handoff
    r = Recall(lambda _: j.store(), private=PrivateReader(j.client.db, ctx.store_for))
    r._import_eligible = lambda _: True
    own = j.stored()
    assert r.can_read(p, own.id) is True
    assert r.thread(p, own.id).id == own.id
    assert r.get(p, own.id)[0].raw == native()
    for who in (replace(p, user_id="usr_other", role="owner"), replace(p, workspace_id="ws_other")):
        assert not r.can_read(who, own.id)
        with pytest.raises(ThreadNotFound):
            r.thread(who, own.id)
    store = j.store()
    for identity in (
        "import-private:",
        f"import-private:{p.user_id}:S",
        f"import-private:{p.user_id}::S",
        f"import-private:{p.user_id}:claude-code:",
        "import:claude-code:S",
        f"capture:{p.user_id}:S",
        f"capture-fork:{p.user_id}:S",
        "ordinary",
    ):
        row = store.store(
            p.workspace_id,
            identity,
            parse_import("claude-code", native(), "auto"),
            actor=p.user_id,
            wing="private_chats",
        )
        assert not r.can_read(p, row.id)
        with pytest.raises(ThreadNotFound):
            r.thread(p, row.id)
    for trust in ("untrusted", "standard"):
        assert not r.can_read(replace(p, policy=replace(p.policy, trust=trust)), row.id)
    r._import_eligible = None
    assert not r.can_read(p, own.id)
    with pytest.raises(ThreadNotFound):
        r.get(p, own.id)
    person = sessions.person_principal(j.client.db, p.user_id, p.workspace_id)
    malformed = replace(own, source_thread_id=f"import-private:{p.user_id}::S")
    from commonplace.handoff.recall import readable

    assert not readable(person, malformed)


def test_r04_continuity(live, monkeypatch, request):
    j = live
    reader = mint(j)["token"]
    writer = j.pat()["token"]
    assert j.upload(body(), token=writer)["accepted"] == 1
    drain(j)
    request.getfixturevalue("functional_worker_transport")
    own = j.stored()
    ctx = j.client.gateway.handoff
    ctx.call_tool(
        sessions.person_principal(j.client.db, j.owner.user, j.owner.workspace),
        "mempalace_add_drawer",
        {"wing": "shared", "room": "control", "content": "zqordinary shared memory"},
    )
    assert (
        j.request(
            "/api/connections/" + principal(j, reader).connection_id,
            "PATCH",
            owner=j.owner,
            data={"read_wings": ["shared"]},
        )[0]
        == 200
    )
    # A readable wing never substitutes for ordinary transcript scope.
    from commonplace.handoff.api import _API

    ordinary = j.store().store(
        j.owner.workspace,
        "ordinary-shared-session",
        parse_import("claude-code", native(), "auto"),
        actor=j.owner.user,
        wing="shared",
    )
    reader_principal = principal(j, reader)
    assert reader_principal.policy.can_read_wing("shared")
    assert not reader_principal.policy.has_scope("threads:read")
    assert not _API(ctx)._thread_row_readable(reader_principal, ordinary)
    assert not ctx.recall.can_read(reader_principal, ordinary.id)
    with pytest.raises(ThreadNotFound):
        ctx.recall.thread(reader_principal, ordinary.id)
    for suffix in ("", "/messages", "/chain"):
        assert j.request(f"/api/threads/{ordinary.id}{suffix}", token=reader)[0] == 404
    assert (
        j.request(
            f"/api/threads/{ordinary.id}/recall", "POST", token=reader, data={"query": "café"}
        )[0]
        == 404
    )
    rows = listed(j, reader)["imports"]
    assert rows[0]["stored_thread_id"] == own.id and rows[0]["stored_messages"] == 1
    assert raw_view(j, reader, own.id) == native()
    assert j.request(f"/api/threads/{own.id}", token=reader)[0] == 200
    assert json.loads(j.request(f"/api/threads/{own.id}/chain", token=reader)[2])[0]["id"] == own.id
    response = j.request(
        f"/api/threads/{own.id}/recall", "POST", token=reader, data={"query": "café"}
    )
    assert response[0] == 200 and has_words(json.loads(response[2]), WORDS)
    for name in ("thread_get", "thread_chain", "thread_recall"):
        assert name not in j.names(reader)
    for token in (reader, oauth(j)[1]["access_token"]):
        for args in ({"query": "café"}, {"query": "café", "theme": "private_chats"}):
            reply = memory(j, token, "memory_search", args)
            assert has_words(reply, WORDS), reply
        reply = memory(j, token, "memory_search", {"query": "café", "theme": "shared"})
        assert not has_words(reply, WORDS)
        for args in (
            {"query": "café", "wing": "private_chats"},
            {"query": "café", "wings": ["private_chats"]},
        ):
            assert has_words(memory(j, token, "mempalace_search", args), WORDS)
        found = j.client.gateway.handoff.recall.import_candidates(
            principal(j, token), "café", limit=5
        )
        assert found
        memory_id = found[0]["drawer_id"]
        assert has_words(memory(j, token, "memory_get", {"id": memory_id}), WORDS)
        assert "mempalace_get_drawers" not in j.names(token)
        # Exercise the existing standard-profile batch route with unchanged
        # scopes; consent itself does not widen a slim tool profile.
        p = principal(j, token)
        p = replace(p, policy=replace(p.policy, tools="standard"))
        response = j.client.gateway.mediator.handle(
            p,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "mempalace_get_drawers",
                    "arguments": {"drawer_ids": [memory_id]},
                },
            },
        )
        assert has_words(response, WORDS)
        for name in ("mempalace_list_drawers", "mempalace_list_rooms"):
            response = j.client.gateway.mediator.handle(
                p,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": {"wing": "private_chats"}},
                },
            )
            assert own.id in rendered(response)
        assert has_words(memory(j, token, "session_start", {"query": "café"}), WORDS)
        assert not has_words(
            memory(j, token, "mempalace_search", {"query": "café", "wing": "private_threads"}),
            WORDS,
        )

    assert has_words(
        memory(j, reader, "memory_search", {"query": "zqordinary"}), "zqordinary shared memory"
    )
    # The in-process palace fixture uses another physical directory. Supply
    # the same read-only startup marker used by the existing search journeys.
    import sqlite3

    from commonplace import session, sync_protocol
    from commonplace.extensions import ToolContext

    marker = (
        Path(j.client.gateway.supervisor.workspace_dir(j.owner.workspace)) / "palace/chroma.sqlite3"
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(marker) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS embeddings(id TEXT)")
        conn.execute("INSERT INTO embeddings VALUES ('synthetic')")
    principal_reader = principal(j, reader)
    # Functional retrieval uses the same frozen logical clock as the existing
    # startup acceptance tests. Physical latency is measured separately on the
    # performance runner; G04 and the session suite exercise real deadlines.
    from commonplace.rules import checks

    with monkeypatch.context() as clock:
        clock.setattr(checks, "monotonic", lambda: 0.0)
        for _ in range(2):
            context = session.initialize_context(
                ToolContext(principal_reader, j.client.gateway), budget=5000, query="café"
            )
            assert has_words(context, WORDS)
    import io

    from commonplace import client_hooks

    class HookClient:
        def __init__(self, **kwargs):
            pass

        def fetch_context(self, **kwargs):
            return context

    monkeypatch.setattr(client_hooks, "Client", HookClient)
    output = io.StringIO()
    assert (
        client_hooks.main(
            [
                "--event",
                "session-start",
                "--client",
                "claude",
                "--url",
                "https://hub.example/mcp",
                "--token-env",
                "SCOPED_TOKEN",
            ],
            stdin=io.StringIO("{}"),
            stdout=output,
        )
        == 0
    )
    wire = json.loads(output.getvalue())
    assert (
        wire["hookSpecificOutput"]["additionalContext"] == client_hooks.GUIDANCE + "\n\n" + context
    )
    exact = "  zqnativefile café\r\nExact file words.\t"
    identity = sync_protocol.memory_thread_id(
        "claude", "memory/notes.md", hashlib.sha256(exact.encode()).hexdigest()
    )
    j.advance()
    assert (
        j.upload(
            {
                "source": "native-memory",
                "threads": [{"thread_id": identity, "format": "file", "transcript": exact}],
            },
            token=writer,
        )["accepted"]
        == 1
    )
    j.advance()
    fact = (
        json.dumps(
            {
                "source": "grok-bot:synthetic",
                "memory_type": "shared_profile",
                "learned_date": "2026-11-03",
                "text": "zqgrokfact café",
            },
            ensure_ascii=False,
        )
        + "\r\n"
    )
    fact_id = hashlib.sha256(fact.encode()).hexdigest()
    assert (
        j.upload(
            {"source": "grok-bot", "threads": [{"thread_id": fact_id, "transcript": fact}]},
            token=writer,
        )["accepted"]
        == 1
    )
    drain(j)
    assert raw_view(j, reader, j.stored(identity, source="native-memory").id) == exact
    assert raw_view(j, reader, j.stored(fact_id, source="grok-bot").id) == fact
    for term in ("zqnativefile", "zqgrokfact"):
        assert has_words(memory(j, reader, "memory_search", {"query": term}), term)


def session_line(word, sid="synthetic-shared"):
    return (
        json.dumps(
            {"type": "user", "sessionId": sid, "message": {"role": "user", "content": word}},
            ensure_ascii=False,
        )
        + "\r\n"
    )


def upload_native(j, token, raw, identity="shared"):
    result = j.upload(
        {
            "source": "claude-code",
            "threads": [{"thread_id": identity, "transcript": raw, "format": "claude-code"}],
        },
        token=token,
    )
    assert result["accepted"] == 1
    drain(j)
    from commonplace.handoff.thread_store import canonical_sha256

    digest = canonical_sha256(parse_import("claude-code", raw, "claude-code"))
    base = f"import-private:{j.owner.user}:claude-code:{identity}"
    return j.store().by_source(j.owner.workspace, base + "#sha256:" + digest) or j.stored(identity)


def capture(j, raw, *, identity="live", before=""):
    j.advance()
    status, _, response = j.request(
        "/api/capture",
        "POST",
        owner=j.owner,
        data={
            "source": "claude-code",
            "thread_id": identity,
            "offset": len(before.encode()),
            "prefix_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "transcript": raw,
        },
    )
    assert status == 200, response
    drain(j)
    return json.loads(response)


def test_r05_shared_holder(live):
    j = live
    reader = mint(j)["token"]
    writer = j.pat()["token"]
    assert (
        j.request("/api/settings/capture", "POST", owner=j.owner, data={"enabled": True})[0] == 200
    )
    prefix, tail, fork = (
        session_line("zqprefix " + WORDS),
        session_line("zqsecrettail"),
        session_line("zqsecretfork"),
    )
    own = upload_native(j, writer, prefix)
    capture(j, prefix + tail)
    assert raw_view(j, reader, own.id) == prefix
    capture(j, prefix + fork, identity="divergent")
    assert raw_view(j, reader, own.id) == prefix
    for term in ("zqsecrettail", "zqsecretfork"):
        assert not has_words(memory(j, reader, "memory_search", {"query": term}), term)
        status, _, raw = j.request(
            f"/api/threads/{own.id}/recall", "POST", token=reader, data={"query": term}
        )
        assert status == 200 and json.loads(raw) == []
    j.advance()
    extended = upload_native(j, writer, prefix + tail)
    assert raw_view(j, reader, extended.id) == prefix + tail
    assert raw_view(j, reader, own.id) == prefix
    rows = listed(j, reader)["imports"]
    assert len(rows) == 2
    assert has_words(memory(j, reader, "memory_search", {"query": "zqsecrettail"}), "zqsecrettail")
    for row in j.store().list_source_range(
        j.owner.workspace, "capture:", limit=10
    ) + j.store().list_source_range(j.owner.workspace, "capture-fork:", limit=10):
        assert j.request(f"/api/threads/{row.id}", token=reader)[0] == 404

    from tests.test_commonplace_import import multipart_items

    for part in multipart_items([prefix, fork]):
        j.advance()
        result = j.upload({"source": "claude-code", "threads": [part]}, token=writer)
        assert result["accepted"] == 1
    drain(j)
    own_fork = j.stored("multipart")
    assert raw_view(j, reader, own_fork.id) == prefix + fork
    assert has_words(memory(j, reader, "memory_search", {"query": "zqsecretfork"}), "zqsecretfork")
    # View metadata does not describe the physical holder's later capture tail.
    record = json.loads(j.request(f"/api/threads/{own.id}", token=reader)[2])
    assert record["message_count"] == 2
    assert len(json.loads(j.request(f"/api/threads/{own.id}/chain", token=reader)[2])) == 1


def test_r06_no_silent_oauth(journey):
    j = journey
    oauth(j)  # Positive issuance is the first old-code assertion.
    for scope, tick, itick in (
        (READ_SCOPE, False, False),
        (READ_WRITE, True, False),
        (IMPORT_SCOPES + " " + READER, False, True),
        (IMPORT_SCOPES, True, True),
    ):
        _, pair = oauth(j, scope=scope, tick=tick, import_tick=itick)
        assert READER not in pair["scope"].split()
        assert READER not in principal(j, pair["access_token"]).policy.scopes


def proof_fixture(j):
    token = j.pat()["token"]
    prefix, tail = session_line("zqproofprefix"), session_line("zqprooftail")
    own = upload_native(j, token, prefix)
    assert (
        j.request("/api/settings/capture", "POST", owner=j.owner, data={"enabled": True})[0] == 200
    )
    capture(j, prefix + tail)
    p = principal(j, token)
    p = replace(p, policy=replace(p.policy, scopes=("memory:read", READER)))
    return p, own


def test_r07_range_gate(live, monkeypatch):
    from commonplace.handoff.mirror_access import gate

    j = live
    p, own = proof_fixture(j)
    ctx = j.client.gateway.handoff
    monkeypatch.setattr(ctx.recall, "can_read", lambda *_: True)
    monkeypatch.setattr(ctx.recall, "_import_eligible", lambda _: True, raising=False)
    # The worker's real mirror rows have authoritative ranges and verified spans.
    raw = ctx.call_tool(
        sessions.person_principal(j.client.db, p.user_id, p.workspace_id),
        "mempalace_search",
        {"query": "zqprooftail", "wing": "private_threads", "room": own.id},
    )
    assert raw["results"]
    cleaned = gate(j.client.gateway, p, "search", raw)
    assert not has_words(cleaned.get("results", []), "zqprooftail")
    raw = ctx.call_tool(
        sessions.person_principal(j.client.db, p.user_id, p.workspace_id),
        "mempalace_search",
        {"query": "zqproofprefix", "wing": "private_threads", "room": own.id},
    )
    assert has_words(gate(j.client.gateway, p, "search", raw).get("results", []), "zqproofprefix")


def test_g01_existing_credentials(journey, monkeypatch):
    j = journey
    old = j.pat()
    cid, pair = j.oauth()
    for token in (old["token"], pair["access_token"]):
        assert READER not in principal(j, token).policy.scopes
    status, _, raw = j.request(
        "/api/connections/" + old["connection"]["id"] + "/rotate", "POST", owner=j.owner, data={}
    )
    assert status == 200 and READER not in principal(j, json.loads(raw)["token"]).policy.scopes
    j.serial += 1
    status, _, raw = j.request(
        "/api/connections",
        "POST",
        owner=j.owner,
        data={"name": "child", "parent": old["connection"]["id"]},
    )
    assert status == 201 and READER not in principal(j, json.loads(raw)["token"]).policy.scopes
    assert READER not in sessions.person_policy().scopes

    status, _, raw = j.refresh(cid, pair["refresh_token"])
    assert status == 200
    refreshed = json.loads(raw)
    assert READER not in refreshed["scope"].split()
    assert j.refresh(cid, refreshed["refresh_token"], READ_SCOPE)[0] == 400
    from commonplace.control_db import ControlDB

    reopened = ControlDB(str(j.client.db.path))
    try:
        assert READER not in reopened.authenticate_connection(old["connection"]["id"]).policy.scopes
        assert (
            READER
            not in reopened.authenticate_connection(
                principal(j, refreshed["access_token"]).connection_id
            ).policy.scopes
        )
    finally:
        reopened.close()
    monkeypatch.setattr(j.client.gateway, "call_memory_tool", lambda *_: {"wings": {"shared": {}}})
    assert (
        j.request(
            "/api/connections/" + old["connection"]["id"],
            "PATCH",
            owner=j.owner,
            data={"read_wings": ["shared"]},
        )[0]
        == 200
    )
    assert READER not in j.client.db.authenticate_connection(old["connection"]["id"]).policy.scopes
    assert (
        j.request(
            "/api/connections/" + old["connection"]["id"],
            "PATCH",
            owner=j.owner,
            data={"import_read": True},
        )[0]
        == 400
    )


def test_g02_refresh_revoke(journey):
    j = journey
    cid, pair = oauth(j)
    token = pair["access_token"]
    writer = j.pat()["token"]
    assert j.upload(body(), token=writer)["accepted"] == 1
    own = j.stored()
    assert raw_view(j, token, own.id) == native()
    status, _, raw = j.refresh(cid, pair["refresh_token"], "memory:read")
    assert status == 200
    narrowed = json.loads(raw)
    assert j.request(f"/api/threads/{own.id}", token=narrowed["access_token"])[0] == 404
    assert raw_view(j, token, own.id) == native()
    connection = principal(j, token).connection_id
    assert READER in j.client.db.authenticate_connection(connection).policy.scopes
    status, _, raw = j.refresh(cid, narrowed["refresh_token"], READ_SCOPE)
    assert status == 400 and json.loads(raw)["error"] == "invalid_scope"
    assert j.form("/oauth/revoke", {"client_id": cid, "token": narrowed["refresh_token"]})[0] == 200
    assert principal(j, token) is None
    pat = mint(j)
    assert raw_view(j, pat["token"], own.id) == native()
    with j.client.db._lock:
        j.client.db._conn.execute(
            "UPDATE workspaces SET personal_of=NULL WHERE id=?", (j.owner.workspace,)
        )
    assert j.request(f"/api/threads/{own.id}", token=pat["token"])[0] == 404
    assert j.request("/api/imports", token=pat["token"])[0] == 404


def test_g03_no_broadening(journey, monkeypatch):
    j = journey
    reader = mint(j)
    p = principal(j, reader["token"])
    assert not set(p.policy.scopes) & {
        "memory:import",
        "threads:read",
        "private:read",
        "kg:read",
        "handoff:write",
    }
    assert j.request("/api/import", "POST", token=reader["token"], data=body())[0] == 403
    for profile in ("slim", "standard", "full"):
        assert READER not in Policy.build(profile=profile, trust="trusted").scopes
    assert READER not in sessions.person_policy().scopes
    from commonplace import cli

    calls = []
    args = SimpleNamespace(
        client="generic",
        scopes=READ_SCOPE.replace(" ", ","),
        profile="slim",
        tools=None,
        read_wings=None,
        write_wings=None,
        write_wing=None,
        trust=None,
    )
    monkeypatch.setattr(
        cli, "_db", lambda _: SimpleNamespace(create_token=lambda **kw: calls.append(kw))
    )
    with pytest.raises(ValueError, match="consent"):
        cli.cmd_token_add(args)
    assert calls == []
    status, _, _ = j.request(
        "/api/connections/" + reader["connection"]["id"],
        "PATCH",
        owner=j.owner,
        data={"import_read": True},
    )
    assert status == 400

    status, _, raw = j.request(
        "/api/connections",
        "POST",
        owner=j.owner,
        data={"name": "reader-child", "parent": reader["connection"]["id"]},
    )
    assert status == 201 and READER not in principal(j, json.loads(raw)["token"]).policy.scopes


def test_g04_fail_closed(live, monkeypatch):
    from commonplace.handoff.mirror_access import gate
    from commonplace.rules import checks

    j = live
    reader = mint(j)["token"]
    p, own = proof_fixture(j)
    p = principal(j, reader)
    ctx = j.client.gateway.handoff
    person = sessions.person_principal(j.client.db, p.user_id, p.workspace_id)
    raw = ctx.call_tool(
        person,
        "mempalace_search",
        {"query": "zqproofprefix", "wing": "private_threads", "room": own.id},
    )
    assert raw["results"]
    assert has_words(gate(j.client.gateway, p, "search", raw).get("results", []), "zqproofprefix")
    for hit in raw["results"]:
        forged = {**hit, "text": "zqforged", "claimed_by": "handoff:0-0:8", "source_path": "fake"}
        assert not has_words(gate(j.client.gateway, p, "search", {"results": [forged]}), "zqforged")
    deadline = checks._deadline.set(0)
    try:
        assert not has_words(
            gate(j.client.gateway, p, "search", raw).get("results", []), "zqproofprefix"
        )
    finally:
        checks._deadline.reset(deadline)
    callback = ctx.recall._import_eligible
    ctx.recall._import_eligible = None
    assert not has_words(
        gate(j.client.gateway, p, "search", raw).get("results", []), "zqproofprefix"
    )
    ctx.recall._import_eligible = callback
    assert has_words(gate(j.client.gateway, p, "search", raw).get("results", []), "zqproofprefix")

    with checks.deadline(1):
        assert has_words(
            gate(j.client.gateway, p, "search", raw).get("results", []), "zqproofprefix"
        )
    stores = ctx._stores.copy()
    ctx._stores.clear()
    try:
        with checks.deadline(1):
            assert not gate(j.client.gateway, p, "search", raw).get("results")
    finally:
        ctx._stores.update(stores)
    safe = raw["results"][0]
    # A public tunnel endpoint cannot authorize a private drawer's preview.
    preview = {
        "wing": "shared",
        "room": "public",
        "connected_wing": "shared",
        "connected_room": "public-destination",
        "drawer_id": "unproved-tail",
        "drawer_wing": "private_threads",
        "drawer_room": own.id,
        "drawer_preview": "zqprooftail",
    }
    assert gate(j.client.gateway, p, "read_wing", [preview]) == []
    for change in (
        {"source_path": "handoff:thr_0000000000000000#v2:1-1:conversation:0"},
        {"source_path": f"handoff:{own.id}#v2:1-3:conversation:0"},
        {"text": "zqforged tail"},
    ):
        assert not gate(j.client.gateway, p, "search", {"results": [{**safe, **change}]}).get(
            "results"
        )
    decorated = {**safe, "drawer_preview": "zqforgedpreview", "claimed_by": "zqforgedlabel"}
    answer = gate(j.client.gateway, p, "search", {"results": [decorated]})
    assert not has_words(answer, "zqforged")
    fact = {
        "predicate": "likes",
        "object": "zqfactwords",
        "sources": [
            {"source_thread": own.id, "source_seq_start": 1, "source_seq_end": 1},
        ],
    }
    assert gate(j.client.gateway, p, "kg_read", {"facts": [fact]})["facts"]
    mixed = {
        **fact,
        "sources": fact["sources"]
        + [{"source_thread": own.id, "source_seq_start": 3, "source_seq_end": 3}],
    }
    assert gate(j.client.gateway, p, "kg_read", {"facts": [mixed]})["facts"] == []
    unknown = {**fact, "sources": [{"source_thread": own.id}]}
    assert gate(j.client.gateway, p, "kg_read", {"facts": [unknown]})["facts"] == []
    db = j.client.db
    with db._lock:
        offset = db._conn.execute(
            "SELECT offset FROM sync_streams WHERE own_stored_id=?", (own.id,)
        ).fetchone()[0]
        db._conn.execute("UPDATE sync_streams SET offset=99999999 WHERE own_stored_id=?", (own.id,))
    assert not gate(j.client.gateway, p, "search", raw).get("results")
    with db._lock:
        db._conn.execute("UPDATE sync_streams SET offset=? WHERE own_stored_id=?", (offset, own.id))
    assert gate(j.client.gateway, p, "search", raw).get("results")
    # Candidate-to-import lookup must not scan the account's unrelated history.
    with db._lock:
        db._conn.executemany(
            "INSERT INTO sync_streams (workspace_id,stream_id,user_id,connection_id,kind,group_id,own_stored_id) "
            "VALUES (?,?,?,'','import','synthetic-unrelated-group',?)",
            [
                (p.workspace_id, f"synthetic-unrelated-{i}", p.user_id, f"synthetic-stored-{i}")
                for i in range(5000)
            ],
        )
        steps = []
        db._conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
        try:
            assert ctx.recall._private.imports_for_source(p, "thr_0000000000000000") == []
        finally:
            db._conn.set_progress_handler(None, 0)
    assert len(steps) < 20, "Import candidate lookup scanned unrelated history"
    assert j.request("/api/connections/" + p.connection_id, "DELETE", owner=j.owner)[0] == 200
    assert not gate(j.client.gateway, p, "search", raw).get("results")


def test_r08_listing_refusal(journey):
    j = journey
    for token in (j.pat()["token"], j.pat(grant=False)["token"], j.oauth()[1]["access_token"]):
        status, _, raw = j.request("/api/imports", token=token)
        assert status == 404
        assert json.loads(raw) == {"error": "not found"}
    assert j.request("/api/imports")[0] == 401


def test_virtual_tunnel_drawer_requires_stored_association(live, functional_worker_transport):
    """A proved imported view is virtual; explicit tunnels require stored metadata."""
    from mempalace import mcp_server as mcp, palace_graph as graph

    j = live
    reader = mint(j)["token"]
    writer = j.pat()["token"]
    own = upload_native(j, writer, session_line("zqvirtualtunnel"))
    p = principal(j, reader)
    recall = j.client.gateway.handoff.recall
    assert recall.import_reader(p)
    virtuals = recall.import_candidates(p, "zqvirtualtunnel", limit=5)
    assert virtuals and virtuals[0]["drawer_id"].startswith("imported:")
    virtual = virtuals[0]
    assert recall.import_drawer(p, virtual["drawer_id"])["content"] == virtual["content"]
    owner = sessions.person_principal(j.client.db, j.owner.user, j.owner.workspace)
    assert owner.person and owner.role == "owner" and recall.can_read(owner, own.id)
    row = {
        "id": "virtual-tunnel",
        "source": {"wing": "shared", "room": "ordinary"},
        "target": {
            "wing": virtual["wing"],
            "room": virtual["room"],
            "drawer_id": virtual["drawer_id"],
        },
    }
    graph._save_tunnels([row], mcp._config)
    assert graph._load_tunnels(mcp._config) == [row]
    assert j.client.gateway.call_memory_tool(owner, "mempalace_list_tunnels", {}) == []
    assert (
        j.client.gateway.call_memory_tool(
            owner, "mempalace_follow_tunnels", {"wing": "shared", "room": "ordinary"}
        )
        == []
    )
