"""Pin-safe relay helpers: return responses and rows without asserting."""

import http.client
import json
import os
import time
from datetime import datetime, timezone

import pytest

from commonplace import control_db
from tests._chat_helpers import no_outbound
from tests._commonplace_helpers import Hub, InProcessLauncher
from tests._console_helpers import optional
from tests._mcp_server_helpers import _patch_mcp_server
from tests._projects_helpers import ProjectsHub


class RelayHub(ProjectsHub):
    no_outbound = staticmethod(no_outbound)

    def request(self, auth, method, path, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        return self._request_bytes(auth, method, path, payload, "application/json")

    def _request_bytes(self, auth, method, path, payload, content_type):
        headers = {"Content-Type": content_type}
        if isinstance(auth, dict):
            headers.update(auth)
        elif auth:
            headers["Authorization"] = "Bearer " + auth
        conn = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=30)
        try:
            conn.request(method, path, payload, headers)
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}, raw
        finally:
            conn.close()

    def machine(self, auth=None, label="relay-1", wing="work") -> dict:
        auth = auth or self.owner
        status, _, _ = self.request(
            auth,
            "POST",
            "/api/memory",
            {"text": "Synthetic workspace example.", "theme": wing, "topic": "relay"},
        )
        if status != 201:
            return {}
        status, body, _ = self.request(
            auth,
            "POST",
            "/api/runners",
            {"label": label, "projects": [wing], "topics": ["chat"], "wings": [wing]},
        )
        return body if status == 201 else {}

    def poll(self, m, clis=("claude",), context=None, relay_always=None):
        body = {"clis": list(clis)}
        if context is not None:
            body["context"] = context
        if relay_always is not None:
            body["relay_always"] = relay_always
        return self.request(m.get("machine_token", ""), "POST", "/api/runner/chat/lease", body)[:2]

    def answer(self, m, job, token, text, cli="claude", model="claude-haiku-5-5"):
        return self.request(
            m.get("machine_token", ""),
            "POST",
            "/api/runner/chat/answer",
            {"job": job, "lease_token": token, "text": text, "cli": cli, "model": model},
        )[:2]

    def fail(self, m, job, token, code):
        return self.request(
            m.get("machine_token", ""),
            "POST",
            "/api/runner/chat/fail",
            {"job": job, "lease_token": token, "code": code},
        )[:2]

    def raw(self, m, body_bytes, content_type):
        return self._request_bytes(
            m.get("machine_token", ""), "POST", "/api/runner/chat/lease", body_bytes, content_type
        )[:2]

    def set_main_model(self, provider, model):
        return self.models(self.owner, "work", {"main": {"provider": provider, "model": model}})

    def _table(self, name):
        return (
            self.db._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    def jobs(self):
        with self.db._lock:
            if not self._table("chat_runner_jobs"):
                return []
            return [
                dict(r)
                for r in self.db._conn.execute("SELECT * FROM chat_runner_jobs ORDER BY message_id")
            ]

    def age_seen(self, m, seconds):
        with self.db._lock:
            if self._table("chat_runners_seen"):
                self.db._conn.execute(
                    "UPDATE chat_runners_seen SET seen_at=? WHERE machine_id=?",
                    (
                        control_db._iso(
                            datetime.fromtimestamp(time.time() - seconds, timezone.utc)
                        ),
                        m.get("machine_id", ""),
                    ),
                )

    def age_connection(self, cid, seconds):
        with self.db._lock:
            self.db._conn.execute(
                "UPDATE connections SET last_used_at=? WHERE id=?",
                (control_db._iso(datetime.fromtimestamp(time.time() - seconds, timezone.utc)), cid),
            )

    def seed_jobs(self, n, state="done"):
        now = control_db._iso(datetime.now(timezone.utc))
        with self.db._lock:
            if self._table("chat_runner_jobs"):
                self.db._conn.executemany(
                    "INSERT INTO chat_runner_jobs "
                    "(id,workspace_id,board,message_id,user_id,state,code,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            f"chat_{i:016x}",
                            self.workspace,
                            "work",
                            1_000_000 + i,
                            self.admin,
                            state,
                            "ok",
                            now,
                            now,
                        )
                        for i in range(n)
                    ],
                )

    def propose_titled(self, title):
        return self.request(
            self.untrusted("work"), "POST", "/api/boards/work/cards", {"title": title}
        )[:2]

    def relay(self):
        return optional("commonplace.runner.chat_relay")


@pytest.fixture(name="relay_hub")
def relay_hub_fixture(tmp_path, monkeypatch, config, kg):
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    monkeypatch.setenv("COMMONPLACE_CHAT_WORKER", "off")
    for name in tuple(os.environ):
        if name.startswith("COMMONPLACE_CHAT_KEY_"):
            monkeypatch.delenv(name)
    _patch_mcp_server(monkeypatch, config, kg)
    hub = Hub(tmp_path / "data", InProcessLauncher(monkeypatch))
    try:
        yield RelayHub(hub)
    finally:
        hub.close()
