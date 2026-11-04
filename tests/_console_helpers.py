"""Real HTTP console fixture and generated, synthetic redaction examples."""

import http.client
import importlib
import json
import re
import secrets
import string
from pathlib import Path

import pytest

from commonplace import sessions
from tests._commonplace_helpers import Hub, InProcessLauncher
from tests._mcp_server_helpers import _patch_mcp_server
from tests.test_commonplace_boards import mcp, service

CORPUS = Path(__file__).parent / "fixtures" / "console_secrets.json"


def optional(module):
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name == module or module.startswith(exc.name + "."):
            return None
        raise


def generate(template):
    """Expand templates without storing credentials in the fixture file."""
    values = []

    def expand(match):
        kind, count = match[1], int(match[2])
        alphabet = {
            "urlsafe": string.ascii_letters + string.digits + "_-",
            "alnum": string.ascii_letters + string.digits,
            "upper": string.ascii_uppercase + string.digits,
            "hex": string.hexdigits[:16],
            "base64": string.ascii_letters + string.digits + "+/",
        }[kind]
        value = "".join(secrets.choice(alphabet) for _ in range(count))
        assert len(value) == count
        values.append(value)
        return value

    text = re.sub(r"\{(urlsafe|alnum|upper|hex|base64):(\d+)\}", expand, template)
    assert values
    return text, values


def examples(kinds=None):
    rows = json.loads(CORPUS.read_text())["templates"]
    return [
        (row, *generate(row["template"])) for row in rows if kinds is None or row["kind"] in kinds
    ]


def successful(hub, token, name, **arguments):
    ok, text = mcp(hub, token, name, **arguments)
    assert ok, text
    if name == "task_add":
        match = re.match(r"#(\d+) added\b", text)
        assert match is not None
        return {"number": int(match[1])}
    return text


class ConsoleHub:
    def __init__(self, hub):
        self.hub = hub
        self.owner = self.cookie("owner@example.com", hub.workspace)
        self.member_user = hub.db.create_user("member@example.com")
        hub.db.add_member(hub.workspace, self.member_user, "member", actor=hub.admin)
        self.member = self.cookie("member@example.com", hub.workspace)
        self.other_user = hub.db.create_user("other@example.com")
        self.other_workspace = hub.db.create_workspace("other", self.other_user, personal=True)
        self.other = self.cookie("other@example.com", self.other_workspace)
        service(hub, self.person(hub.admin)).board_add(slug="work", wing="shared")
        self.a1, self.t1 = self.agent(hub.admin, "a1")
        self.a2, self.t2 = self.agent(hub.admin, "a2")
        self.m1, self.tm = self.agent(self.member_user, "m1")

    def __getattr__(self, name):
        return getattr(self.hub, name)

    def cookie(self, email, workspace):
        link = sessions.create_login_link(self.hub.db, email, workspace)
        session = sessions.redeem_login_link(self.hub.db, link)
        assert session
        return {"Cookie": f"{sessions.COOKIE}={session}"}

    def agent(self, user, name):
        token = self.hub.token(
            name,
            user=user,
            profile="full",
            scopes=["memory:read", "memory:write"],
            read_wings=["shared"],
            write_wings=["shared"],
            trust="standard",
        )
        return self.principal(token).connection_id, token

    def principal(self, token):
        return self.hub.gateway.authenticate("Bearer " + token)

    def person(self, user):
        return sessions.person_principal(self.hub.db, user, self.hub.workspace)

    def request(self, auth, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=30)
        headers = {"Content-Type": "application/json"}
        if isinstance(auth, dict):
            headers.update(auth)
        elif auth:
            headers["Authorization"] = "Bearer " + auth
        try:
            conn.request(
                method, path, json.dumps(body).encode() if body is not None else None, headers
            )
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw), raw
        finally:
            conn.close()

    def get(self, auth, path):
        return self.request(auth, "GET", path)

    def post(self, auth, path, body):
        return self.request(auth, "POST", path, body)

    def toggle(self, cid=None, auth=None, record=True):
        return self.post(
            auth or self.owner, "/api/console/agents/" + (cid or self.a1), {"record": record}
        )

    def stream(self, cid=None, auth=None, suffix=""):
        return self.get(
            auth or self.owner, "/api/console/stream?agent=" + (cid or self.a1) + suffix
        )

    def activity_files(self):
        return list(
            Path(self.hub.supervisor.workspace_dir(self.hub.workspace)).glob("console.sqlite3*")
        )


@pytest.fixture(name="console_hub")
def console_hub_fixture(tmp_path, monkeypatch, config, kg):
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    _patch_mcp_server(monkeypatch, config, kg)
    hub = Hub(tmp_path / "data", InProcessLauncher(monkeypatch))
    try:
        yield ConsoleHub(hub)
    finally:
        hub.close()
