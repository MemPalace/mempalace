"""Project contract fixture; helpers return responses or rows without assertions."""

import json

import pytest

from commonplace import sessions
from commonplace.policy import Policy
from tests._chat_helpers import ChatHub
from tests._commonplace_helpers import Hub, InProcessLauncher
from tests._console_helpers import optional
from tests._mcp_server_helpers import _patch_mcp_server


class ProjectsHub(ChatHub):
    def __init__(self, hub):
        self.hub = hub
        self.owner = self.cookie("owner@example.com", hub.workspace)
        self.member_user = hub.db.create_user("member@example.com")
        hub.db.add_member(hub.workspace, self.member_user, "member", actor=hub.admin)
        self.member = self.cookie("member@example.com", hub.workspace)
        self.other_user = hub.db.create_user("other@example.com")
        self.other_workspace = hub.db.create_workspace("other", self.other_user, personal=True)
        self.other = self.cookie("other@example.com", self.other_workspace)
        self.create_board(self.owner, "work")
        wings = ["shared", "work", "alpha", "beta", "zz"]
        self.a1 = hub.token(
            "a1",
            profile="full",
            scopes=["memory:read", "memory:write"],
            read_wings=wings,
            write_wings=wings,
            trust="trusted",
        )
        self._untrusted_count = 0

    def untrusted(self, wing):
        self._untrusted_count += 1
        name = f"untrusted-{self._untrusted_count}"
        # The pin makes proposals only for children. A read-only parent cannot
        # approve, so release falls back to the owner or the board coordinator.
        parent, _ = self.db.create_token(
            workspace_ref=self.workspace,
            user_ref=self.admin,
            actor=self.admin,
            name=name + "-parent",
            client="test",
            policy=Policy.build(
                profile="slim", scopes=["memory:read"], read_wings=[wing], trust="standard"
            ),
        )
        _, token = self.db.create_token(
            workspace_ref=self.workspace,
            user_ref=self.admin,
            actor=self.admin,
            name=name,
            client="test",
            parent=parent,
            policy=Policy.build(
                profile="full",
                scopes=["memory:read", "memory:write"],
                read_wings=[wing],
                write_wing=wing,
                trust="untrusted",
            ),
        )
        return token

    def cookie(self, email, workspace):
        link = sessions.create_login_link(self.hub.db, email, workspace)
        session = sessions.redeem_login_link(self.hub.db, link)
        return {"Cookie": f"{sessions.COOKIE}={session}"}

    def create_board(self, auth, slug, wing=None):
        return self.request(auth, "POST", "/api/boards", {"slug": slug, "wing": wing or slug})[:2]

    def project(self, auth, slug):
        return self.get(auth, f"/api/projects/{slug}")[:2]

    def connect(self, auth, slug, body=None):
        return self.request(auth, "POST", f"/api/projects/{slug}/orchestrator/connect", body or {})[
            :2
        ]

    def models(self, auth, slug, body=None):
        if body is None:
            return self.project(auth, slug)
        return self.request(auth, "PUT", f"/api/projects/{slug}/models", body)[:2]

    def seats(self, ws=None):
        with self.db._lock:
            if not self.db._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='project_orchestrators'"
            ).fetchone():
                return []
            return [
                dict(r)
                for r in self.db._conn.execute(
                    "SELECT * FROM project_orchestrators WHERE workspace_id=? ORDER BY board, role",
                    (ws or self.workspace,),
                )
            ]

    def delete_seat(self, board):
        with self.db._lock:
            if self.db._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='project_orchestrators'"
            ).fetchone():
                self.db._conn.execute(
                    "DELETE FROM project_orchestrators WHERE workspace_id=? AND board=?",
                    (self.workspace, board),
                )

    def deliver(self, event):
        module = optional("commonplace.projects.seats")
        if module is not None:
            module.on_event(self.gateway, event)

    def board(self, slug):
        rows = self.get(self.owner, "/api/boards")[1].get("boards", [])
        return next((row for row in rows if row["slug"] == slug), {})

    def coordinator(self, slug):
        return self.board(slug).get("coordinator")

    def mcp(self, auth, tool, **arguments):
        status, body, _ = self.request(
            auth,
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
        )
        result = body.get("result", {})
        return status, {
            "ok": status == 200 and "error" not in body and not result.get("isError", False),
            "text": "\n".join(c.get("text", "") for c in result.get("content", []))
            if result
            else json.dumps(body),
        }

    def propose(self, board):
        wing = self.board(board)["wing"]
        token = self.untrusted(wing)
        return self.request(token, "POST", f"/api/boards/{board}/cards", {"title": "Proposal"})[:2]

    def backlog(self, auth):
        status, body, _ = self.get(auth, "/api/needs-me")
        return status, {c["number"] for c in body.get("backlog_from_agents", [])}


@pytest.fixture(name="projects_hub")
def projects_hub_fixture(tmp_path, monkeypatch, config, kg):
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    monkeypatch.setenv("COMMONPLACE_CHAT_WORKER", "off")
    _patch_mcp_server(monkeypatch, config, kg)
    hub = Hub(tmp_path / "data", InProcessLauncher(monkeypatch))
    try:
        yield ProjectsHub(hub)
    finally:
        hub.close()
