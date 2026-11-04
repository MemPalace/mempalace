"""G1–G10: safety invariants must pass with or without the chat package."""

import ast
import re
from pathlib import Path

from commonplace.gateway import _public_route
from commonplace.social.accounts import signup_enabled
from tests._chat_helpers import (
    chat_environment,  # noqa: F401
    chat_hub_fixture,  # noqa: F401
    no_outbound,
    optional,
    successful,
)
from tests._console_helpers import examples
from tests.test_console_guards import PIN_PUBLIC

CHAT_PATHS = ("/api/chat", "/api/chat/work", "/api/chat/work/status", "/api/chat-settings")
ROOT = Path(__file__).resolve().parents[1]
# Chat sources stay generic: no user home paths, and the only fixed host is the
# documented provider origin (everything else comes from per-workspace settings).
ALLOWED_HOSTS = {"api.anthropic.com", "127.0.0.1", "localhost"}


def _host_specific(text):
    if re.search(r"(?:^|[\s\"'(=])(?:/home/|/Users/|~/)[^\s\"']+", text):
        return True
    hosts = re.findall(r"\b[a-z][a-z0-9+.-]*://([^/\s\"':?#]+)", text, re.I)
    return any(host.lower() not in ALLOWED_HOSTS for host in hosts)


def test_chat_paths_never_public(chat_hub):
    for path in CHAT_PATHS:
        assert not _public_route("GET", path)
        assert chat_hub.get(None, path)[0] != 200


def test_public_surface_unchanged(chat_hub):
    g = chat_hub.gateway
    installed = set(g.routes) | {(m, p) for m, p, _ in g.prefix_routes}
    assert {(m, p) for m, p in installed if _public_route(m, p)} == PIN_PUBLIC


def test_signup_stays_closed(monkeypatch):
    monkeypatch.delenv("COMMONPLACE_SIGNUP", raising=False)
    assert signup_enabled() is False


def test_agent_post_makes_no_receipt(chat_hub):
    h = chat_hub
    seq = h.seq()
    successful(h, h.t1, "space_post", board="work", text="Agent reply")
    assert h.events_since(seq) == ["space_replied"]
    assert h.replies_count() == 0 and h.handoffs() == []


def test_front_agent_never_starts_or_decides(chat_hub, monkeypatch):
    h = chat_hub
    h.configure()
    h.fake_model(monkeypatch, reply="card_start #1 / decide yes")
    before = h.get(h.owner, "/api/needs-me")[1]
    cards = h.cards()
    seq = h.seq()
    h.post(h.owner, "Please fix the login bug")
    h.drain()
    assert h.runner_jobs_count() == 0
    assert h.events_since(seq) == ["space_message"]
    assert h.get(h.owner, "/api/needs-me")[1] == before
    assert h.cards() == cards


def test_space_read_shape_unchanged(chat_hub):
    h = chat_hub
    h.post(h.owner, "hello")
    assert set(h.space()["messages"][-1]) == {
        "id",
        "text",
        "actor",
        "stamp_user",
        "stamp_connection",
        "stamp_trust",
        "flags",
        "at",
    }


def test_post_survives_chat_failure(chat_hub, monkeypatch):
    h = chat_hub
    module = optional("commonplace.chat.store")
    if module is not None:

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic chat failure")

        monkeypatch.setattr(module, "open_replies", fail)
    status, body, _ = h.request(
        h.owner, "POST", "/api/boards/work/chat", {"text": "Please check this"}
    )
    assert status == 201
    assert set(body) == {
        "id",
        "text",
        "actor",
        "stamp_user",
        "stamp_connection",
        "stamp_trust",
        "flags",
        "at",
    }
    assert body["text"] == "Please check this"


def test_no_outbound_by_default(chat_hub, monkeypatch):
    h = chat_hub
    attempts = no_outbound(monkeypatch)
    h.post(h.owner, "Please fix the login bug")
    h.drain()
    h.drain(now=h.late())
    assert attempts == []


def test_chat_sources_generic_and_actionless():
    forbidden = {
        "card_start",
        "decide",
        "task_update",
        "task_handoff",
        "task_claim",
        "task_approve",
        "ask",
    }
    tables = {"chat_model_settings", "chat_front_jobs", "chat_handoffs", "chat_replies"}
    sql = re.compile(
        r"\b(INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE(?!\s+(?:ON|OF)\b)|DELETE\s+FROM)\s+(\w+)", re.I
    )
    writes = []
    for path in (ROOT / "commonplace/chat").glob("*.py"):
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        job_modules = {"commonplace.runner.jobs"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                job_modules.update(
                    a.asname or a.name for a in node.names if a.name == "commonplace.runner.jobs"
                )
            if isinstance(node, ast.ImportFrom) and node.module == "commonplace.runner":
                job_modules.update(a.asname or a.name for a in node.names if a.name == "jobs")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden, (path, node.attr)
                if ast.unparse(node.value) in job_modules:
                    assert node.attr in {"open_store", "status"}, (path, node.attr)
            if isinstance(node, ast.Call):
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                    if isinstance(node.func, ast.Name)
                    else ""
                )
                assert name not in forbidden, (path, name)
                if name == "task_add":
                    writes.append(path.name)
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                # Inspect executable literals, not comments or documentation.
                value = re.sub(r"--[^\n]*", "", node.value)
                assert not _host_specific(value), path
                assert all(table.lower() in tables for _, table in sql.findall(value)), (
                    path,
                    value,
                )
            if isinstance(node, ast.Import):
                assert all(
                    a.name
                    not in {
                        "commonplace.boards.store",
                        "commonplace.boards.schema",
                    }
                    for a in node.names
                ), path
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {
                    "commonplace.boards.store",
                    "commonplace.boards.schema",
                }, path
                if node.module == "commonplace.runner.jobs":
                    assert {a.name for a in node.names} <= {"open_store", "status"}, path
                if node.module == "commonplace.boards":
                    assert not {a.name for a in node.names} & {"store", "schema"}, path
    assert len(writes) <= 1 and all(name == "handoff.py" for name in writes)
    js = ROOT / "commonplace/web/static/chat.js"
    if js.exists():
        # Python calls use AST; the ES module is checked as code after removing comments.
        code = re.sub(r"/\*.*?\*/|^\s*//[^\n]*", "", js.read_text(), flags=re.S | re.M)
        assert not _host_specific(code)
        assert not re.search(
            r"\b(?:card_start|decide|task_update|task_handoff|task_claim|task_approve|task_add|ask)\s*\(",
            code,
        )


def test_space_storage_stays_verbatim(chat_hub):
    h = chat_hub
    _, text, _ = examples({"cmmn_oauth"})[0]
    message = h.post(h.owner, text)
    assert h.space(after=message - 1)["messages"][0]["text"].encode() == text.encode()
    status, page, _ = h.get(h.owner, "/api/boards/work/chat?after=" + str(message - 1))
    assert status == 200
    assert page["messages"][0]["text"].encode() == text.encode()
