"""Chat contract fixture: real local HTTP, deterministic worker and model controls."""

import secrets
import socket
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

from commonplace import events
from tests._commonplace_helpers import Hub, InProcessLauncher
from tests._console_helpers import (
    ConsoleHub,
    optional,
    successful,
)
from tests._mcp_server_helpers import _patch_mcp_server

MODEL_FRONT = {
    "provider": "openai_compatible",
    "model": "fast-model",
    "base_url": "http://127.0.0.1:9",
}


class ChatHub(ConsoleHub):
    def post(self, auth, text):
        status, body, _ = self.request(auth, "POST", "/api/boards/work/chat", {"text": text})
        assert status == 201, (status, body)
        return body["id"]

    def chat(self, auth, **q):
        return self.get(auth, "/api/chat/work" + ("?" + urlencode(q) if q else ""))

    def status(self, auth):
        return self.get(auth, "/api/chat/work/status")

    def settings(self, auth, body=None):
        return self.request(auth, "PUT" if body is not None else "GET", "/api/chat-settings", body)

    def configure(self, **overrides):
        return self.settings(self.owner, {"front": {**MODEL_FRONT, **overrides}})

    def drain(self, now=None):
        worker = getattr(self.gateway, "chat_worker", None)
        return worker.drain(now=now) if worker is not None else 0

    def late(self):
        types = optional("commonplace.chat.types")
        return time.time() + (types.ACK_WAIT_S if types else 300) + 1

    def fake_model(self, monkeypatch, reply="A short answer.", raises=None):
        calls = []
        module = optional("commonplace.chat.model")
        if module is not None:

            def complete(settings, key, system, turns, **kwargs):
                calls.append(
                    {"model": settings.model, "key": key, "system": system, "turns": turns}
                )
                if raises is not None:
                    raise raises
                return reply(system, turns) if callable(reply) else reply

            monkeypatch.setattr(module, "complete", complete)
        return calls

    def cards(self, message_id=None):
        status, body, _ = self.get(self.owner, "/api/boards/work/cards")
        assert status == 200
        rows = body["linked"] if message_id is not None else body["cards"]
        return [r for r in rows if message_id is None or r["message_id"] == message_id]

    def rows(self, table):
        assert table in {"chat_front_jobs", "chat_handoffs"}
        with self.db._lock:
            if not self.db._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (table,)
            ).fetchone():
                return []
            return [
                dict(r) for r in self.db._conn.execute(f"SELECT * FROM {table} ORDER BY message_id")
            ]

    def handoffs(self):
        return self.rows("chat_handoffs")

    def replies_count(self):
        path = Path(self.supervisor.workspace_dir(self.workspace)) / "chat.sqlite3"
        if not path.exists():
            return 0
        from mempalace.sqlite_crypto import connect

        conn = connect(path)
        try:
            return conn.execute("SELECT count(*) FROM chat_replies").fetchone()[0]
        finally:
            conn.close()

    def runner_jobs_count(self, ws=None):
        path = Path(self.supervisor.workspace_dir(ws or self.workspace)) / "runner.sqlite3"
        if not path.exists():
            return 0
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='runner_jobs'").fetchone():
                return 0
            return conn.execute("SELECT count(*) FROM runner_jobs").fetchone()[0]

    def seq(self):
        return events.latest_seq(self.db, self.workspace)

    def events_since(self, seq):
        return [e["kind"] for e in events.after(self.db, self.workspace, seq, self.seq())]

    def set_coordinator(self, name):
        result = self.request(self.owner, "PATCH", "/api/boards/work", {"coordinator": name})
        assert result[0] == 200, result[:2]

    def subscribe(self, token):
        return successful(
            self,
            token,
            "webhook_add",
            url="http://127.0.0.1:9/hook",
            events=["space_message"],
            board="work",
        )

    def space(self, user=None, **query):
        from tests.test_commonplace_boards import service

        return service(self, self.person(user or self.admin)).space_read("work", **query)


@pytest.fixture(name="chat_hub")
def chat_hub_fixture(tmp_path, monkeypatch, config, kg, chat_environment):
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    _patch_mcp_server(monkeypatch, config, kg)
    hub = Hub(tmp_path / "data", InProcessLauncher(monkeypatch))
    try:
        yield ChatHub(hub)
    finally:
        hub.close()


@pytest.fixture(autouse=True)
def chat_environment(monkeypatch):
    monkeypatch.setenv("COMMONPLACE_CHAT_WORKER", "off")
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_TEST", secrets.token_urlsafe(32))
    monkeypatch.delenv("COMMONPLACE_CHAT_KEY_WORKSPACES", raising=False)
    monkeypatch.delenv("COMMONPLACE_CHAT_KEY_HOSTS", raising=False)


def no_outbound(monkeypatch):
    """Count and reject external connections, while preserving local HTTP."""
    original = socket.create_connection
    attempts = []

    def connect(address, *args, **kwargs):
        if address[0] not in {"127.0.0.1", "localhost", "::1"}:
            attempts.append(address)
            raise OSError("external network forbidden in chat contracts")
        return original(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", connect)
    return attempts
