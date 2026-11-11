"""G0–G17: synthetic public journeys, identical on the pinned base and head.

G0/G1/G9 are controls. G14 is a pure read-policy test, and G15 is a pure
early-boundary test; neither claims public-route membership coverage. All other
actors sign up by captured email. Store/DB access below is read-only inspection.
Do not run this file until the parent's exact-hash artifact review has passed.
"""

import base64
import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import unescape
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from commonplace import plans, urls
from commonplace.control_db import Principal
from commonplace.handoff import recall
from commonplace.handoff.models import HandoffRequest, StoredThread, ThreadMeta
from commonplace.handoff.service import HandoffService
from commonplace.sessions import person_policy
from tests.test_commonplace_email_auth import Client, fresh as fresh
from tests.test_commonplace_import import multipart_items

READ_WRITE = "memory:read memory:write"
IMPORT_SCOPES = "memory:read memory:write memory:import"
REDIRECT = "https://client.example.test/callback"
VERIFIER = "synthetic-0123456789abcdefghijklmnopqrstuvwxyz-._~"
WORDS = "  café 🦉\r\nKeep  every word.\t"
BATCH_KEYS = {"source", "status", "processed", "total", "accepted", "errors", "facts", "results"}
WHOLE_KEYS = {"index", "thread_id", "status", "messages", "source_bytes"}
PART_KEYS = {
    "index",
    "thread_id",
    "status",
    "unit",
    "format",
    "part",
    "source_bytes",
    "messages",
    "offset",
    "prefix_sha256",
    "next_index",
    "session_state",
}


def native(text=WORDS):
    return (
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": text}},
            ensure_ascii=False,
        )
        + "\r\n"
    )


def body(identity="S", text=WORDS):
    return {
        "source": "claude-code",
        "threads": [{"thread_id": identity, "transcript": native(text), "format": "auto"}],
    }


class Journey:
    def __init__(self, client: Client, mailbox):
        self.client, self.mailbox = client, mailbox
        self.serial = 0
        self.now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)

    def signup(self, email):
        # Client.signin uses the public email POST, confirmation GET and
        # same-origin confirmation POST. No control-DB auth shortcuts.
        cookie = self.client.signin(self.mailbox, email)
        me = self.client.me(cookie)
        return SimpleNamespace(cookie=cookie, user=me["user"], workspace=me["workspace"])

    def request(self, path, method="GET", *, owner=None, token=None, data=None):
        headers = {"Origin": urls.APP_URL}
        if method in {"POST", "PATCH", "DELETE"}:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        return self.client.request(
            path, method, cookie=owner.cookie if owner else None, headers=headers, data=data
        )

    def pat(self, *, owner=None, grant=True):
        self.serial += 1
        data = {"name": f"synthetic-{self.serial}", "client": "generic"}
        if grant is not None:
            data["import"] = grant
        status, _, raw = self.request(
            "/api/connections", "POST", owner=owner or self.owner, data=data
        )
        # On base this is the counted AssertionError, before token subscripting.
        assert status == 201
        return json.loads(raw)

    def register(self):
        status, _, raw = self.request(
            "/oauth/register",
            "POST",
            data={"client_name": "Synthetic importer", "redirect_uris": [REDIRECT]},
        )
        assert status == 201
        return json.loads(raw)

    def authorize(self, client_id, scope=IMPORT_SCOPES):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
        parameters = {
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": challenge.rstrip(b"=").decode(),
            "code_challenge_method": "S256",
            "resource": urls.mcp_url(),
            "state": "synthetic",
        }
        if scope is not None:
            parameters["scope"] = scope
        path = "/oauth/authorize?" + urlencode(parameters)
        return path, self.request(path, owner=self.owner)

    def allow(self, path, page, *, tick):
        status, _, raw = page
        # On base explicit import requests redirect invalid_scope here.
        assert status == 200
        match = re.search(rb'name="request" value="([^"]+)"', raw)
        assert match is not None
        data = {"request": unescape(match[1].decode()), "decision": "allow"}
        if tick:
            data["grant_import"] = "1"
        return self.form(path, data, owner=self.owner)

    def form(self, path, data, *, owner=None):
        return self.client.request(
            path,
            "POST",
            raw=urlencode(data),
            cookie=owner.cookie if owner else None,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": urls.APP_URL},
        )

    def oauth(self, *, tick=True, scope=IMPORT_SCOPES):
        registered = self.register()
        client_id = registered["client_id"]
        path, page = self.authorize(client_id, scope)
        status, headers, _ = self.allow(path, page, tick=tick)
        assert status == 303
        parameters = parse_qs(urlsplit(headers["Location"]).query)
        assert "code" in parameters
        status, _, raw = self.form(
            "/oauth/token",
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": parameters["code"][0],
                "code_verifier": VERIFIER,
                "redirect_uri": REDIRECT,
                "resource": urls.mcp_url(),
            },
        )
        assert status == 200
        return client_id, json.loads(raw)

    def refresh(self, client_id, refresh_token, scope=None):
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "resource": urls.mcp_url(),
        }
        if scope is not None:
            data["scope"] = scope
        return self.form("/oauth/token", data)

    def upload(self, data=None, *, token=None, owner=None):
        status, _, raw = self.request(
            "/api/import",
            "POST",
            data=body() if data is None else data,
            token=token,
            owner=owner,
        )
        assert status == 200
        return json.loads(raw)

    def rpc_response(self, token, method, params=None):
        return self.request(
            "/mcp",
            "POST",
            token=token,
            data={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        )

    def rpc(self, token, method, params=None):
        status, _, raw = self.rpc_response(token, method, params)
        assert status == 200
        return json.loads(raw)

    def tool(self, token, name, arguments=None):
        response = self.rpc(token, "tools/call", {"name": name, "arguments": arguments or {}})
        assert "error" not in response
        assert not response["result"].get("isError", False)
        return response["result"]["content"][0]["text"]

    def checked_args(self, token, **arguments):
        text = self.tool(token, "rules_get", {"effective": True, "format": "md"})
        marker = re.search(r"(?m)^rules_seq (\d+)\s*$", text)
        if marker:
            arguments["rules_seq"] = int(marker[1])
        else:
            assert "rules_seq off" in text
        return arguments

    def names(self, token):
        return {tool["name"] for tool in self.rpc(token, "tools/list")["result"]["tools"]}

    def store(self, owner=None):
        # Inspection only; never seeds an HTTP journey.
        return self.client.gateway.handoff.store_for((owner or self.owner).workspace)

    def stored(self, identity="S", *, owner=None, source="claude-code"):
        owner = owner or self.owner
        return self.store(owner).by_source(
            owner.workspace, f"import-private:{owner.user}:{source}:{identity}"
        )

    def row_counts(self, owner=None):
        # Read-only inspection also catches accidental extra fork/thread rows.
        store = self.store(owner)
        with store._lock:
            return tuple(
                store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("threads", "messages", "chunks")
            )

    def advance(self):
        self.now += timedelta(seconds=901)


@pytest.fixture
def journey(fresh, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    monkeypatch.setenv("COMMONPLACE_APP_URL", "http://127.0.0.1")
    client, mailbox = fresh()
    origin = f"http://127.0.0.1:{client.server.server_port}"
    monkeypatch.setattr(urls, "APP_URL", origin)
    monkeypatch.setattr(urls, "AGENT_URL", origin)
    result = Journey(client, mailbox)
    monkeypatch.setattr(plans, "_now", lambda: result.now)
    result.owner = result.signup("a@example.test")
    return result


def assert_accepted(report, *, count=1, keys=WHOLE_KEYS):
    assert set(report) == BATCH_KEYS
    assert report["status"] == "completed"
    assert (report["processed"], report["total"], report["accepted"], report["errors"]) == (
        count,
        count,
        count,
        0,
    )
    assert all(set(row) == keys and row["status"] == "accepted" for row in report["results"])


def assert_revoked(journey, token, data):
    status, _, _ = journey.request("/api/import", "POST", token=token, data=data)
    assert status == 401
    status, _, _ = journey.rpc_response(
        token, "tools/call", {"name": "facts_import", "arguments": {"jsonl": "synthetic"}}
    )
    assert status == 401


def test_public_signup_fixture_on_base(journey):
    """G0 control: normal public signup creates a non-admin personal owner."""
    owner, db = journey.owner, journey.client.db
    assert not db.user(owner.user)["is_admin"]
    assert db.workspace(owner.workspace)["personal_of"] == owner.user
    assert db.role(owner.workspace, owner.user) == "owner"
    assert urls.APP_URL == urls.AGENT_URL
    assert journey.mailbox.configured and len(journey.mailbox.messages) == 1


def test_metadata_defaults_exclude_import(journey):
    """G1 control: PRM, DCR and absent OAuth scope retain the two defaults."""
    status, _, raw = journey.request("/.well-known/oauth-protected-resource")
    assert status == 200
    assert set(json.loads(raw)["scopes_supported"]) == set(READ_WRITE.split())
    assert set(journey.register()["scope"].split()) == set(READ_WRITE.split())
    _, pair = journey.oauth(scope=None)
    assert set(pair["scope"].split()) == set(READ_WRITE.split())


def test_as_metadata_lists_import(journey):
    """G2 counted: only AS metadata advertises the explicit capability."""
    status, _, raw = journey.request("/.well-known/oauth-authorization-server")
    assert status == 200
    assert b"memory:import" in raw
    assert set(json.loads(raw)["scopes_supported"]) == set(
        (IMPORT_SCOPES + " memory:import:read cards:decide").split()
    )


def test_consent_import_box_unticked_and_optional(journey):
    """G3 counted at the first positive authorize GET; later cases are guards."""
    client_id = journey.register()["client_id"]
    path, page = journey.authorize(client_id)
    assert page[0] == 200
    boxes = re.findall(rb"<input\b[^>]*>", page[2])
    boxes = [tag for tag in boxes if re.search(rb"name=[\"\']grant_import[\"\']", tag)]
    assert len(boxes) == 1
    assert re.search(rb"type=[\"\']checkbox[\"\']", boxes[0])
    assert re.search(rb"value=[\"\']1[\"\']", boxes[0])
    assert not re.search(rb"\bchecked(?:\s|=|/?>)", boxes[0])
    assert b"word for word" in page[2]
    # Consume this real form, and independently mint unticked and ticked grants.
    assert journey.allow(path, page, tick=False)[0] == 303
    _, no_import = journey.oauth(tick=False)
    assert set(no_import["scope"].split()) == set(READ_WRITE.split())
    _, granted = journey.oauth(tick=True)
    assert set(granted["scope"].split()) == set(IMPORT_SCOPES.split())
    for scope, tick in (
        ("memory:import", False),
        ("memory:import", True),
        ("memory:read memory:import", False),
        ("memory:read memory:import", True),
        ("memory:write memory:import", False),
        ("memory:write memory:import", True),
    ):
        path, page = journey.authorize(client_id, scope)
        response = journey.allow(path, page, tick=tick) if page[0] == 200 else page
        status, headers, raw = response
        assert status in (303, 400)
        error = (
            parse_qs(urlsplit(headers["Location"]).query).get("error")
            if status == 303
            else [json.loads(raw)["error"]]
        )
        assert error == ["invalid_scope"]


def test_refresh_drops_import_and_cannot_regain(journey):
    """G4 counted at explicit OAuth grant; narrowing is a head guard."""
    client_id, pair = journey.oauth()
    status, _, raw = journey.refresh(client_id, pair["refresh_token"], READ_WRITE)
    assert status == 200
    narrowed = json.loads(raw)
    assert set(narrowed["scope"].split()) == set(READ_WRITE.split())
    assert "facts_import" not in journey.names(narrowed["access_token"])
    status, _, _ = journey.request(
        "/api/import", "POST", token=narrowed["access_token"], data=body()
    )
    assert status == 403
    status, _, raw = journey.refresh(client_id, narrowed["refresh_token"], IMPORT_SCOPES)
    assert status == 400
    assert json.loads(raw)["error"] == "invalid_scope"


def test_pat_import_tick_and_type_check(journey):
    """G5 counted at import=true; non-bool and default remain head guards."""
    granted = journey.pat()
    assert bool(granted.get("token"))
    status, _, _ = journey.request(
        "/api/connections",
        "POST",
        owner=journey.owner,
        data={"name": "bad-import-type", "import": "yes"},
    )
    assert status == 400
    plain = journey.pat(grant=None)
    false = journey.pat(grant=False)
    plain_policy = journey.client.gateway.authenticate("Bearer " + plain["token"]).policy
    false_policy = journey.client.gateway.authenticate("Bearer " + false["token"]).policy
    assert plain_policy == false_policy
    assert "memory:import" not in plain_policy.scopes
    assert plain_policy.trust == "untrusted" and plain_policy.write_wing == "inbox"


def test_grant_import_private_write_only_exact_repeat(journey):
    """G6 counted at PAT mint; exact replay and ACL denial are head guards."""
    token = journey.pat()["token"]
    data = body()
    first = journey.upload(data, token=token)
    assert_accepted(first)
    assert first["results"][0]["source_bytes"] == len(native().encode())
    assert first["results"][0]["messages"] == 1
    stored = journey.stored()
    assert stored is not None and stored.wing == "private_chats"
    before = journey.store().messages(stored.id)
    counts = journey.row_counts()
    assert before[0].raw == native() and before[1].text == WORDS
    assert journey.upload(data, token=token) == first
    assert journey.stored() == stored and journey.store().messages(stored.id) == before
    assert journey.row_counts() == counts and counts[0] == 1
    status, _, raw = journey.request(f"/api/threads/{stored.id}/messages", owner=journey.owner)
    assert status == 200
    assert json.loads(raw)[0]["raw"] == native()
    status, _, _ = journey.request(f"/api/threads/{stored.id}", token=token)
    assert status == 404  # Missing threads:read: this does not prove the filer seam.


def test_grant_responses_uniform_no_oracle(journey, monkeypatch):
    """G7: separate real-accounting under-limit and at-limit subcases."""
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    small = replace(plans.FREE, storage_bytes=4 * 1024 * 1024)
    monkeypatch.setattr(plans, "FREE", small)
    monkeypatch.setitem(plans.PLANS, "free", small)
    token = journey.pat()["token"]
    data = body("F", "synthetic quota " + "x" * 8192)
    assert small.storage_bytes >= 2 * len(json.dumps(data).encode())
    first = journey.upload(data, token=token)
    journey.advance()
    changed = journey.upload(body("F", "synthetic quota " + "y" * 8192), token=token)
    journey.advance()
    new = journey.upload(body("G", "synthetic quota " + "x" * 8192), token=token)
    for report in (first, changed, new):
        assert_accepted(report)
    assert set(first["results"][0]) == set(changed["results"][0]) == set(new["results"][0])

    # Separate personal workspace: first F is admitted at its t0. All padding
    # is ordinary, unpaced owner/ACL import, using the same transcript size so
    # the first quota refusal also bounds an identical whole-F precheck.
    owner = journey.signup("b@example.test")
    full_token = journey.pat(owner=owner)["token"]
    assert_accepted(journey.upload(data, token=full_token))
    for index in range(256):
        padding = body(f"padding-{index}", "synthetic quota " + "x" * 8192)
        report = journey.upload(padding, owner=owner)
        if report["errors"]:
            assert report["results"][0]["code"] == "storage_limit"
            break
        assert report["imported"] == 1
    else:
        pytest.fail("Synthetic owner imports did not reach the small real quota")
    stored = journey.stored("F", owner=owner)
    before = journey.store(owner).messages(stored.id)
    counts = journey.row_counts(owner)
    covered = journey.upload(data, token=full_token)  # Exact own request, at t0.
    journey.advance()  # New G must pass cadence before exercising the quota.
    absent = journey.upload(body("G", "synthetic quota " + "x" * 8192), token=full_token)
    for report in (covered, absent):
        assert set(report) == BATCH_KEYS
        assert report["accepted"] == 0 and report["errors"] == 1
        row = report["results"][0]
        assert set(row) == {"index", "thread_id", "status", "code", "error"}
        assert row["status"] == "error" and row["code"] == "storage_limit"
    assert {k: v for k, v in covered["results"][0].items() if k != "thread_id"} == {
        k: v for k, v in absent["results"][0].items() if k != "thread_id"
    }
    assert journey.stored("G", owner=owner) is None
    assert journey.store(owner).messages(stored.id) == before
    assert journey.row_counts(owner) == counts


def test_two_signups_isolated(journey):
    """G8 counted at B's PAT mint; cross-user denials are head guards."""
    a = journey.owner
    report = journey.upload(body("F"), owner=a)
    assert report["results"][0]["status"] == "imported"
    a_id = report["results"][0]["stored_thread_id"]
    a_before = journey.store(a).messages(a_id)
    a_counts = journey.row_counts(a)
    b = journey.signup("b@example.test")
    token = journey.pat(owner=b)["token"]
    assert_accepted(journey.upload(body("F"), token=token))
    stored_b = journey.stored("F", owner=b)
    assert stored_b.workspace_id == b.workspace and stored_b.wing == "private_chats"
    assert journey.store(a).messages(a_id) == a_before
    assert journey.row_counts(a) == a_counts
    for owner, grant, identity in ((b, None, a_id), (None, token, a_id), (a, None, stored_b.id)):
        status, _, _ = journey.request(f"/api/threads/{identity}", owner=owner, token=grant)
        assert status == 404


def test_ungranted_tokens_still_refused(journey):
    """G9 control: ordinary ungranted PAT keeps the base refusal."""
    token = journey.pat(grant=None)["token"]
    status, _, raw = journey.request("/api/import", "POST", token=token, data=body())
    assert status == 403
    assert json.loads(raw)["error"] == (
        "Import requires memory:read, memory:write and threads:read for threads"
    )
    assert "facts_import" not in journey.names(token)


def test_facts_import_mcp_and_http_same_ids(journey):
    """G10 counted at OAuth grant; raw-line HTTP/MCP continuity is a head guard."""
    _, pair = journey.oauth()
    token = pair["access_token"]
    lines = [
        json.dumps(
            {
                "source": "grok-bot:synthetic",
                "memory_type": "shared_profile",
                "learned_date": "2026-11-03",
                "text": f"synthetic fact {index} café",
            },
            ensure_ascii=False,
        )
        + ending
        for index, ending in enumerate(("\r\n", "\n", ""))
    ]
    jsonl = lines[0] + "\r\n" + lines[1] + lines[2]
    args = journey.checked_args(token, jsonl=jsonl)
    report = json.loads(journey.tool(token, "facts_import", args))
    assert_accepted(report, count=3, keys=WHOLE_KEYS | {"facts", "source_counts"})
    assert report["facts"] == 3
    identities = [hashlib.sha256(line.encode()).hexdigest() for line in lines]
    records = [journey.stored(identity, source="grok-bot") for identity in identities]
    assert all(record is not None for record in records)
    snapshots = [journey.store().messages(record.id) for record in records]
    counts = journey.row_counts()
    assert counts[0] == 3
    assert [messages[0].raw for messages in snapshots] == lines
    assert json.loads(journey.tool(token, "facts_import", args)) == report
    assert journey.row_counts() == counts
    journey.advance()
    http_report = journey.upload(
        {
            "source": "grok-bot",
            "threads": [
                {"thread_id": identity, "transcript": line, "format": "auto"}
                for identity, line in zip(identities, lines)
            ],
        },
        token=token,
    )
    assert_accepted(http_report, count=3, keys=WHOLE_KEYS | {"facts", "source_counts"})
    assert [journey.stored(identity, source="grok-bot") for identity in identities] == records
    assert [journey.store().messages(record.id) for record in records] == snapshots
    assert journey.row_counts() == counts


def test_session_start_import_guidance(journey):
    """G11 counted at PAT mint; ordinary connections retain existing guidance."""
    grant = journey.pat()["token"]
    ordinary = journey.pat(grant=None)["token"]
    text = journey.tool(grant, "session_start")
    assert "facts_import" in text
    assert "one request per 15 minutes while plans are enabled" in text
    assert "resend exactly the same request" in text
    ordinary_text = journey.tool(ordinary, "session_start")
    assert "facts_import" not in ordinary_text
    assert "one request per 15 minutes while plans are enabled" not in ordinary_text


def test_grant_multipart_private_own_cursor(journey):
    """G12 counted at PAT mint; two native parts and exact retries are head guards."""
    token = journey.pat()["token"]
    chunks = [native("synthetic first café"), native("synthetic second 🦉")]
    parts = multipart_items(chunks)
    reports = []
    for index, part in enumerate(parts):
        if index:
            journey.advance()
        data = {"source": "claude-code", "threads": [part]}
        report = journey.upload(data, token=token)
        assert_accepted(report, keys=PART_KEYS)
        row = report["results"][0]
        assert row["part"] == part["part"] and row["next_index"] == index + 1
        assert row["offset"] == len("".join(chunks[: index + 1]).encode())
        assert (
            row["prefix_sha256"]
            == hashlib.sha256("".join(chunks[: index + 1]).encode()).hexdigest()
        )
        assert row["session_state"] == ("complete" if index == 1 else "incomplete")
        assert journey.upload(data, token=token) == report
        reports.append(report)
    stored = journey.stored("multipart")
    assert stored.wing == "private_chats"
    status, _, raw = journey.request(f"/api/threads/{stored.id}/messages", owner=journey.owner)
    assert status == 200
    messages = json.loads(raw)
    assert [m["raw"] for m in messages if m["name"] == "import.raw"] == chunks
    assert [m["text"] for m in messages if m["role"] == "user"] == [
        "synthetic first café",
        "synthetic second 🦉",
    ]
    assert len(reports) == 2


def test_grant_cadence_free_900_per_account(journey, monkeypatch):
    """G13 counted at PAT mint; per-account/own-connection retry pacing follows."""
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    first_token = journey.pat()["token"]
    data = body("F")
    assert_accepted(journey.upload(data, token=first_token))
    second_token = journey.pat()["token"]
    _, oauth_pair = journey.oauth()
    oauth_token = oauth_pair["access_token"]
    for token, request in (
        (first_token, body("G")),
        (second_token, body("G")),
        (second_token, data),
    ):
        status, _, raw = journey.request("/api/import", "POST", token=token, data=request)
        assert status == 429
        assert re.fullmatch(r"Import not due; retry in (\d+) seconds", json.loads(raw)["error"])
        seconds = int(re.search(rb"retry in (\d+)", raw)[1])
        assert 1 <= seconds <= 900
    assert_accepted(journey.upload(data, token=first_token))
    arguments = journey.checked_args(
        oauth_token,
        jsonl=json.dumps(
            {
                "source": "grok-bot:synthetic",
                "memory_type": "shared_profile",
                "learned_date": "2026-11-03",
                "text": "synthetic paced fact",
            }
        )
        + "\n",
    )
    denied = journey.rpc(
        oauth_token, "tools/call", {"name": "facts_import", "arguments": arguments}
    )
    assert denied["result"]["isError"]
    assert "no: import not due; retry in" in denied["result"]["content"][0]["text"]
    journey.advance()
    assert_accepted(journey.upload(body("G"), token=second_token))


def test_filer_only_read_same_workspace_pure():
    """G14 PURE: future-member policy, not public-route membership E2E."""
    owner = Principal(
        user_id="usr_aaaaaaaaaaaaaaaa",
        user_email="a@example.test",
        workspace_id="ws_same",
        connection_id="",
        connection="web:a@example.test",
        client="web",
        auth="session",
        role="owner",
        policy=person_policy("owner"),
        person=True,
    )
    member = replace(
        owner, user_id="usr_bbbbbbbbbbbbbbbb", user_email="b@example.test", role="member"
    )
    stored = StoredThread(
        id="thr_aaaaaaaaaaaaaaaa",
        workspace_id="ws_same",
        source_thread_id="import-private:usr_aaaaaaaaaaaaaaaa:claude-code:S",
        message_count=2,
        chunk_count=1,
        sha256="a" * 64,
        wing="private_chats",
    )
    assert recall._readable(member, stored) is False
    assert recall._readable(owner, stored) is True
    for malformed in (
        "import-private:",
        "import-private::S",
        "import-private:usr_aaaaaaaaaaaaaaaa",
    ):
        assert recall._readable(owner, replace(stored, source_thread_id=malformed)) is False
    ordinary = replace(stored, source_thread_id="import:claude-code:S")
    assert recall._readable(member, ordinary) is True
    assert recall._readable(owner, replace(ordinary, workspace_id="ws_other")) is False


def test_handoff_cannot_squat_private_import_namespace():
    """G15 PURE early boundary: no public authentication or real store/launcher."""
    calls = []

    class BoundaryReached(Exception):
        pass

    def deliver_pending(*, workspace_id):
        calls.append(("deliver_pending", workspace_id))
        raise BoundaryReached("synthetic boundary reached")

    def begin(*args, **kwargs):
        calls.append(("begin",))
        raise BoundaryReached("synthetic begin reached")

    def store_for(workspace_id):
        calls.append(("store_for", workspace_id))
        raise BoundaryReached("synthetic store reached")

    principal = Principal(
        user_id="usr_aaaaaaaaaaaaaaaa",
        user_email="a@example.test",
        workspace_id="ws_same",
        connection_id="",
        connection="web:a@example.test",
        client="web",
        auth="session",
        role="owner",
        policy=person_policy("owner"),
        person=True,
    )
    service = HandoffService(
        control_db=SimpleNamespace(),
        handoff_db=SimpleNamespace(set_document_store=lambda _: None, begin=begin),
        settings=SimpleNamespace(),
        store_for=store_for,
        events=SimpleNamespace(deliver_pending=deliver_pending),
    )
    request = HandoffRequest(
        meta=ThreadMeta(
            thread_id="import-private:usr_aaaaaaaaaaaaaaaa:claude-code:S", client="test"
        ),
        transcript='[{"role":"user","content":"synthetic boundary"}]',
        transcript_format="messages",
    )
    caught = None
    try:
        service.run(principal, request)
    except (ValueError, BoundaryReached) as error:
        caught = error
    assert calls == []
    assert type(caught) is ValueError
    assert str(caught) == "thread_id uses a reserved namespace"


def test_scope_trust_and_unrequested_tick(journey):
    """G16 counted at PAT mint; scope/trust and forged-tick checks are head guards."""
    pat = journey.pat()["token"]
    _, pair = journey.oauth()
    for token, trust in ((pat, "untrusted"), (pair["access_token"], "standard")):
        principal = journey.client.gateway.authenticate("Bearer " + token)
        assert principal is not None
        assert set(principal.policy.scopes) == set(IMPORT_SCOPES.split())
        assert principal.policy.trust == trust
        assert not principal.policy.has_scope("threads:read")
        assert not principal.policy.can_read_wing("private_chats")
        assert principal.policy.write_wing == "inbox"
    _, forged = journey.oauth(scope=READ_WRITE, tick=True)
    assert set(forged["scope"].split()) == set(READ_WRITE.split())
    assert "facts_import" not in journey.names(forged["access_token"])


def test_revoked_grant_connection_refused(journey):
    """G17 counted at OAuth grant; real OAuth and owner-PAT revocation guards."""
    client_id, pair = journey.oauth()
    token = pair["access_token"]
    assert_accepted(journey.upload(body("oauth"), token=token))
    stored = journey.stored("oauth")
    before = journey.store().messages(stored.id)
    counts = journey.row_counts()
    assert counts[0] == 1
    status, _, _ = journey.form(
        "/oauth/revoke", {"client_id": client_id, "token": pair["refresh_token"]}
    )  # Ordinary form, no Authorization header.
    assert status == 200
    assert_revoked(journey, token, body("oauth-after-revoke"))
    status, _, raw = journey.refresh(client_id, pair["refresh_token"])
    assert status == 400
    assert json.loads(raw)["error"] == "invalid_grant"
    assert journey.stored("oauth-after-revoke") is None
    assert journey.store().messages(stored.id) == before
    assert journey.row_counts() == counts

    pat = journey.pat()
    journey.advance()
    assert_accepted(journey.upload(body("pat"), token=pat["token"]))
    pat_stored = journey.stored("pat")
    pat_before = journey.store().messages(pat_stored.id)
    counts = journey.row_counts()
    assert counts[0] == 2
    status, _, raw = journey.request(
        "/api/connections/" + pat["connection"]["id"], "DELETE", owner=journey.owner
    )
    assert status == 200
    assert json.loads(raw)["ok"] is True
    assert_revoked(journey, pat["token"], body("pat-after-revoke"))
    assert journey.stored("pat-after-revoke") is None
    assert journey.store().messages(pat_stored.id) == pat_before
    assert journey.row_counts() == counts


def test_grant_surrogate_matches_invalid_transcript(journey):
    token = journey.pat()["token"]
    invalid = body()
    invalid["threads"][0]["transcript"] = ""
    expected = journey.upload(invalid, token=token)
    assert expected["results"][0]["code"] == "invalid_thread"
    status, _, _ = journey.request("/api/import", "POST", token=token, data=body("next"))
    assert status == 429
    journey.advance()
    surrogate = body()
    surrogate["threads"][0]["transcript"] = "\ud800"
    actual = journey.upload(surrogate, token=token)
    assert actual == expected
    status, _, _ = journey.request("/api/import", "POST", token=token, data=body("next"))
    assert status == 429
    assert journey.row_counts()[0] == 0


def test_grant_multipart_noncontinuation_conflict(journey):
    token = journey.pat()["token"]
    parts = multipart_items([native("first"), native("second")])
    report = journey.upload({"source": "claude-code", "threads": [parts[1]]}, token=token)
    assert report["results"] == [
        {
            "index": 0,
            "thread_id": parts[1]["thread_id"],
            "status": "error",
            "code": "conflict",
            "error": "Part does not continue this import; resend from the start or the last accepted part",
        }
    ]
