"""G1–G8: invariants that pass on both the pin and the frozen skeleton."""

import ast
from pathlib import Path

from commonplace.gateway import _public_route
from commonplace.social.accounts import signup_enabled
from tests._chat_helpers import no_outbound
from tests._projects_helpers import projects_hub_fixture  # noqa: F401
from tests.test_chat_guards import _host_specific
from tests.test_console_guards import PIN_PUBLIC

ROOT = Path(__file__).resolve().parents[1]


def test_unheld_seat_keeps_person_routing(projects_hub):
    h = projects_hub
    assert h.create_board(h.owner, "alpha")[0] == 201
    status, proposal = h.propose("alpha")
    assert status == 201 and proposal["proposal_status"] == "pending"
    code, numbers = h.backlog(h.owner)
    assert code == 200 and proposal["number"] in numbers
    assert h.coordinator("alpha") is None


def test_public_surface_unchanged(projects_hub):
    h = projects_hub
    installed = set(h.gateway.routes) | {(m, p) for m, p, _ in h.gateway.prefix_routes}
    assert {(m, p) for m, p in installed if _public_route(m, p)} == PIN_PUBLIC
    assert h.get(None, "/api/projects/x")[0] != 200
    for method in ("GET", "POST", "PUT"):
        assert not _public_route(method, "/api/projects/x")


def test_signup_stays_closed(monkeypatch):
    monkeypatch.delenv("COMMONPLACE_SIGNUP", raising=False)
    assert signup_enabled() is False


def test_no_outbound_on_create_and_connect(projects_hub, monkeypatch):
    h = projects_hub
    attempts = no_outbound(monkeypatch)
    assert h.create_board(h.owner, "alpha")[0] == 201
    h.project(h.owner, "alpha")
    h.connect(h.owner, "alpha")
    assert attempts == []


def test_board_set_unchanged(projects_hub):
    h = projects_hub
    assert h.mcp(h.a1, "board_set", board="work", coordinator="conn:a1")[1]["ok"]
    assert h.coordinator("work") == "conn:a1"
    assert h.mcp(h.a1, "board_set", board="work", coordinator=None)[1]["ok"]
    assert h.coordinator("work") is None


def test_chat_listing_shape_unchanged(projects_hub):
    h = projects_hub
    status, body, _ = h.get(h.owner, "/api/chat")
    assert status == 200 and set(body) == {"projects", "default", "front"}
    assert next(p for p in body["projects"] if p["slug"] == "work")["orchestrator"] is None


def test_project_sources_generic_and_secret_safe():
    forbidden_imports = {"http.client", "urllib.request", "commonplace.chat.model"}
    for path in (ROOT / "commonplace/projects").glob("*.py"):
        source = path.read_text()
        assert not _host_specific(source), path
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {a.name for a in node.names} & forbidden_imports, path
            if isinstance(node, ast.ImportFrom):
                imported = {node.module} | {f"{node.module}.{a.name}" for a in node.names}
                assert not imported & forbidden_imports, path
            if isinstance(node, ast.Call):
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                    if isinstance(node.func, ast.Name)
                    else ""
                )
                assert name not in {"card_start", "decide", "task_add"}, path
                logger = (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"logger", "log"}
                )
                if name in {"print", "log_failure"} or logger:
                    arguments = [*node.args, *(kw.value for kw in node.keywords)]
                    assert not any(
                        isinstance(n, ast.Name) and n.id == "token"
                        for arg in arguments
                        for n in ast.walk(arg)
                    ), path


def test_manual_coordinator_release_unchanged(projects_hub):
    h = projects_hub
    assert h.mcp(h.a1, "board_set", board="work", coordinator="conn:a1")[1]["ok"]
    assert h.mcp(h.a1, "space_read", board="work")[1]["ok"]
    status, proposal = h.propose("work")
    assert status == 201 and proposal["proposal_status"] == "pending"
    assert proposal["number"] not in h.backlog(h.owner)[1]
    code, numbers = h.backlog(h.a1)
    assert code == 200 and proposal["number"] not in numbers
    approved = h.request(
        h.a1,
        "POST",
        f"/api/cards/{proposal['number']}/approve",
        {"version": proposal["version"], "approve": True, "reason": "synthetic reason"},
    )
    assert approved[0] == 200 and approved[1]["proposal_status"] == "approved"
