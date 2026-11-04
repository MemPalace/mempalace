"""Synthetic public-route journeys and read-only stored-row inspection."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import sqlite3
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from commonplace import cli as cli_module, mailer, plans, urls
from commonplace.control_db import ControlDB
from commonplace.gateway import Gateway
from commonplace.workers import WorkerSupervisor
from tests.test_commonplace_email_auth import Client, Mailbox

T0 = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
REACHED = []
IMPORT_KEYS = {
    "stored_thread_id",
    "source",
    "thread_id",
    "conflict_of",
    "created_at",
    "stored_messages",
}
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


@pytest.fixture(autouse=True)
def reset_reached():
    REACHED.clear()


def reached(row):
    REACHED.append(row)


@dataclass(frozen=True)
class Reply:
    status: int
    json: object
    raw: bytes


@dataclass(frozen=True)
class Rec:
    key: str
    line: str


def records(sid, n=3, tag="f", source="claude-code", *, agent=None):
    result = []
    for i in range(n):
        key = "sentinel_" + hashlib.sha256(f"{sid}/{tag}/{i}".encode()).hexdigest()
        if source == "claude-code":
            value = {
                "type": "user",
                "sessionId": sid,
                "message": {"role": "user", "content": f"  {key} café 🦉\t"},
            }
            if agent is not None:
                value["agentId"] = agent
        elif i == 0 and tag == "f":
            value = {"type": "session_meta", "payload": {"id": sid, "sentinel": key}}
        else:
            value = {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": key}],
                },
            }
        result.append(Rec(key, json.dumps(value, ensure_ascii=False) + "\r\n"))
    return result


def raw(recs):
    return "".join(r.line for r in recs).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def counts(o, recs):
    return [o.archive[r.key] for r in recs]


def item(tid, data, fmt="auto"):
    return {"thread_id": tid, "transcript": data.decode(), "format": fmt}


def parts(tid, data, cut, fmt="claude-code"):
    result = []
    for index, (start, end) in enumerate(((0, cut), (cut, len(data)))):
        chunk = data[start:end]
        result.append(
            {
                **item(tid, chunk, fmt),
                "part": {
                    "version": 1,
                    "session_bytes": len(data),
                    "session_sha256": sha(data),
                    "index": index,
                    "offset": start,
                    "prefix_sha256": sha(data[:start]),
                    "sha256": sha(chunk),
                    "final": end == len(data),
                },
            }
        )
    return result


def accepted(reply):
    assert reply.status == 200
    assert reply.json["accepted"] == 1
    row = reply.json["results"][0]
    assert row["status"] == "accepted"
    return row


def appended(reply):
    assert reply.status == 200
    assert reply.json["status"] == "appended"
    return reply.json


def archive_row(m):
    return (
        m["role"] == "system"
        and m["kind"] == "other"
        and (m["name"] in ("import.raw", "capture.raw") or m["seq"] == 0 and m["name"] == "")
    )


def home_tree(home):
    return {
        str(p.relative_to(home)): (p.read_bytes(), p.stat().st_mode & 0o777)
        for p in home.rglob("*")
        if p.is_file()
    }


class Hub:
    def __init__(self, root, home, fake_log, monkeypatch, *, plans_mode):
        self.root, self.home, self.fake_log = root, home, fake_log
        self.monkeypatch = monkeypatch
        self.plans_mode = plans_mode
        self.now = T0
        self.serial = 0
        self.instance_id = uuid.uuid4().hex
        self.server = None
        self.monkeypatch.setattr(plans, "_now", lambda: self.now)
        self.start()

    def at(self, seconds):
        self.now = T0 + timedelta(seconds=seconds)

    def start(self):
        self.monkeypatch.setenv("COMMONPLACE_PLANS", self.plans_mode)
        self.mailbox = Mailbox()
        self.monkeypatch.setattr(mailer, "from_env", lambda: self.mailbox)
        self.db = ControlDB(str(self.root / "control.sqlite3"))
        self.supervisor = WorkerSupervisor(str(self.root), launcher=object())
        self.gateway = Gateway(self.db, self.supervisor)
        self.mailbox.auth = self.gateway.email_auth
        self.server = self.gateway.build_server("127.0.0.1", getattr(self, "port", 0))
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.monkeypatch.setattr(urls, "APP_URL", self.origin)
        self.monkeypatch.setattr(urls, "AGENT_URL", self.origin)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.client = Client(self.gateway, self.server.server_address[1])

    def stop(self):
        if self.server is None:
            return
        for job in self.client.backgrounds:
            job.thread.join(timeout=8)
            assert not job.thread.is_alive()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()
        self.gateway.webhooks.stop()
        self.gateway.handoff.close()
        self.supervisor.stop_all()
        self.db.close()
        worker = self.gateway.email_auth._worker
        if self.gateway.email_auth._started:
            worker.join(timeout=3)
            assert not worker.is_alive()
        self.server = None

    def restart(self, plans=None):
        self.stop()
        if plans is not None:
            self.plans_mode = plans
        self.start()

    def signup(self, email="synthetic@example.test"):
        cookie = self.client.signin(self.mailbox, email)
        reply = self.get("/api/me", cookie)
        assert reply.status == 200
        return SimpleNamespace(
            cookie=cookie, user=reply.json["user"], workspace=reply.json["workspace"], email=email
        )

    def request(self, path, method="GET", cred=None, data=None):
        headers = {"Origin": self.origin}
        if method in {"POST", "PATCH", "DELETE"}:
            headers["Content-Type"] = "application/json"
        cookie = None
        if cred is not None:
            if hasattr(cred, "cookie"):
                cookie = cred.cookie
            elif cred.startswith("commonplace_session="):
                cookie = cred
            elif "=" in cred and not cred.startswith("cp_"):
                cookie = cred
            else:
                headers["Authorization"] = "Bearer " + cred
        status, _, content = self.client.request(
            path, method, cookie=cookie, headers=headers, data=data
        )
        try:
            value = json.loads(content)
        except ValueError:
            value = None
        return Reply(status, value, content)

    def get(self, path, cred=None):
        return self.request(path, cred=cred)

    def settings(self, cookie):
        return self.get("/api/settings/capture", cookie)

    def post_settings(self, cookie, body):
        return self.request("/api/settings/capture", "POST", cookie, body)

    def set_capture(self, person, enabled):
        return self.post_settings(person, {"enabled": enabled})

    def enable_capture(self, person):
        reply = self.set_capture(person, True)
        assert reply.status == 200
        return reply

    def sync_now(self, person):
        return self.post_settings(person, {"sync_now": True})

    def pat(self, person, client="generic", import_=None):
        self.serial += 1
        body = {"name": f"synthetic-{self.instance_id}-{self.serial}", "client": client}
        if import_ is not None:
            body["import"] = import_
        reply = self.request("/api/connections", "POST", person, body)
        assert reply.status == 201
        return reply.json["token"], reply.json["connection"]["id"]

    def revoke(self, person, connection_id):
        return self.request(f"/api/connections/{connection_id}", "DELETE", person)

    def oauth(self, person, tick=True):
        redirect = "https://client.example.test/callback"
        verifier = "synthetic-0123456789abcdefghijklmnopqrstuvwxyz-._~"
        registered = self.request(
            "/oauth/register",
            "POST",
            data={
                "client_name": "Synthetic importer",
                "redirect_uris": [redirect],
            },
        )
        assert registered.status == 201
        client_id = registered.json["client_id"]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        path = "/oauth/authorize?" + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": urls.mcp_url(),
                "state": "synthetic",
                "scope": "memory:read memory:write memory:import"
                if tick
                else "memory:read memory:write",
            }
        )
        page = self.get(path, person)
        assert page.status == 200
        match = re.search(rb'name="request" value="([^"]+)"', page.raw)
        assert match is not None
        body = {"request": unescape(match[1].decode()), "decision": "allow"}
        if tick:
            body["grant_import"] = "1"
        status, headers, _ = self.client.request(
            path,
            "POST",
            cookie=person.cookie,
            raw=urlencode(body),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": self.origin},
        )
        assert status == 303
        query = parse_qs(urlsplit(headers["Location"]).query)
        assert "code" in query
        status, _, content = self.client.request(
            "/oauth/token",
            "POST",
            raw=urlencode(
                {
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": query["code"][0],
                    "code_verifier": verifier,
                    "redirect_uri": redirect,
                    "resource": urls.mcp_url(),
                }
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": self.origin},
        )
        assert status == 200
        return json.loads(content)["access_token"]

    def push(self, token, source, tid, data, offset=0, prefix=EMPTY_SHA, op=None):
        body = {
            "source": source,
            "thread_id": tid,
            "offset": offset,
            "prefix_sha256": prefix,
            "transcript": data.decode(),
        }
        if op is not None:
            body["client_op_id"] = op
        return self.request("/api/capture", "POST", token, body)

    def imp(self, source, items, *, token=None, cookie=None, **extra):
        return self.request(
            "/api/import", "POST", token or cookie, {"source": source, "threads": items, **extra}
        )

    def feed(self, cred, after=0, wait=None, sync=True):
        query = {"after": after}
        if sync:
            query["feed"] = "sync"
        if wait is not None:
            query["wait"] = wait
        return self.get("/api/events?" + urlencode(query), cred)

    def sessions(self, cookie):
        return self.get("/api/sync/sessions", cookie)

    def store(self, person):
        return self.gateway.handoff.store_for(person.workspace)

    def stored_threads(self, person):
        store = self.store(person)
        result = []
        for prefix in ("import-private:", "capture:", "capture-fork:"):
            after = None
            while True:
                page = store.list_source_range(person.workspace, prefix, after=after, limit=100)
                result.extend(page)
                if len(page) < 100:
                    break
                after = (page[-1].source_thread_id, page[-1].id)
        return result

    def import_thread(self, person, tid, source="claude-code"):
        row = self.store(person).by_source(
            person.workspace, f"import-private:{person.user}:{source}:{tid}"
        )
        assert row is not None
        return row.id

    def oracle(self, person, recs):
        archive, parsed = Counter(), Counter()
        size = 0
        store = self.store(person)
        for thread in self.stored_threads(person):
            for start in range(0, thread.message_count, 100):
                for m in store.messages(thread.id, start, start + 99):
                    if archive_row(m.to_dict()):
                        size += len(m.raw.encode())
                        for r in recs:
                            archive[r.key] += m.raw.count(r.key)
                    elif m.kind == "message":
                        for r in recs:
                            parsed[r.key] += m.text.count(r.key)
        return SimpleNamespace(archive=archive, parsed=parsed, archive_bytes=size)

    def message_rows(self, person, tid):
        rows = []
        start = 0
        while True:
            reply = self.get(f"/api/threads/{tid}/messages?start={start}&end={start + 99}", person)
            assert reply.status == 200
            assert isinstance(reply.json, list)
            rows.extend(reply.json)
            if len(reply.json) < 100:
                return rows
            start += 100

    def read(self, person, tid):
        return b"".join(m["raw"].encode() for m in self.message_rows(person, tid) if archive_row(m))

    def imports(self, person):
        rows, path = [], "/api/imports?limit=2"
        while path:
            reply = self.get(path, person)
            assert reply.status == 200
            rows.extend(reply.json["imports"])
            cursor = reply.json["next"]
            path = "/api/imports?" + urlencode({"limit": 2, "cursor": cursor}) if cursor else None
        return rows

    def env_file(self, capture_token=None, import_token=None):
        path = self.home.parent / "client.env"
        lines = ["COMMONPLACE_URL=" + self.origin]
        if capture_token:
            lines.append("COMMONPLACE_CAPTURE_TOKEN_CLAUDE_CODE=" + capture_token)
        if import_token:
            lines.append("COMMONPLACE_IMPORT_TOKEN=" + import_token)
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)
        return str(path)

    def cli(self, argv):
        if "--enable" in argv and not ({"--no-service", "--no-activate"} & set(argv)):
            raise RuntimeError("Service activation is forbidden in acceptance tests")
        try:
            return cli_module.main(argv)
        except SystemExit as error:
            return error.code

    @contextmanager
    def drop_proxy(self):
        target = urlsplit(self.origin)
        dropped = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def relay(self):
                size = int(self.headers.get("Content-Length", "0"))
                content = self.rfile.read(size) if size else None
                conn = http.client.HTTPConnection(target.hostname, target.port, timeout=8)
                try:
                    conn.request(
                        self.command,
                        self.path,
                        body=content,
                        headers={
                            k: v
                            for k, v in self.headers.items()
                            if k.lower() not in {"host", "connection"}
                        },
                    )
                    response = conn.getresponse()
                    body = response.read()
                    if (
                        self.command == "POST"
                        and self.path == "/api/capture"
                        and response.status == 200
                        and not dropped.is_set()
                    ):
                        dropped.set()
                        self.close_connection = True
                        return
                    self.send_response(response.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    conn.close()

            do_GET = relay
            do_POST = relay

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            assert not thread.is_alive()


def state_rows(home, table):
    allowed = {
        "outbox",
        "cursors",
        "events",
        "feed_cursor",
        "cadence",
        "memory_seen",
        "source_status",
    }
    assert table in allowed
    path = home / ".commonplace/sync/state.db"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


def pending(home):
    return [r for r in state_rows(home, "outbox") if r["state"] == "pending"]


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "open")
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    monkeypatch.setenv("COMMONPLACE_APP_URL", "http://127.0.0.1")
    for key in ("COMMONPLACE_ALLOWED_HOSTS", "COMMONPLACE_COOKIE_DOMAIN"):
        monkeypatch.delenv(key, raising=False)
    for provider in ("GOOGLE", "GITHUB", "APPLE", "X", "DISCORD"):
        for key in ("CLIENT_ID", "CLIENT_SECRET", "TEAM_ID", "KEY_ID", "PRIVATE_KEY"):
            monkeypatch.delenv(f"COMMONPLACE_{provider}_{key}", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_log = tmp_path / "service-calls.log"
    for name in ("systemctl", "launchctl"):
        path = fakebin / name
        path.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$SYNC_FAKE_SERVICE_LOG"\nexit 0\n')
        path.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(fakebin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SYNC_FAKE_SERVICE_LOG", str(fake_log))
    instances = []

    def make(*, plans="on", root=None):
        instance = Hub(
            Path(root) if root else tmp_path / f"hub-{len(instances)}",
            home,
            fake_log,
            monkeypatch,
            plans_mode=plans,
        )
        instances.append(instance)
        return instance

    yield make
    for instance in reversed(instances):
        instance.stop()


def actors(h):
    p = h.signup()
    h.enable_capture(p)
    capture, _ = h.pat(p, client="claude-code")
    grant, _ = h.pat(p, import_=True)
    return p, capture, grant


def machines(h):
    p, A, _ = actors(h)
    B, _ = h.pat(p, client="claude-code")
    C, _ = h.pat(p, client="claude-code")
    F, G = records("S", 3, "f"), records("S", 1, "g")
    e = records("S", 1, "edit")[0]
    assert e.line != F[-1].line
    assert all(e.key != r.key for r in F + G)
    a = appended(h.push(A, "claude-code", "S", raw(F)))
    h.at(10)
    b = appended(h.push(B, "claude-code", "S", raw(F + G)))
    h.at(20)
    c = appended(h.push(C, "claude-code", "S", raw(F[:-1] + [e])))
    h.at(901)
    appended(h.push(A, "claude-code", "S", raw(G), len(raw(F)), sha(raw(F))))
    return p, F, G, e, a, b, c
