"""Console safety invariants that also pass on the unmodified pin."""

from commonplace import events
from commonplace.gateway import _public_route
from tests._console_helpers import console_hub_fixture, optional, successful  # noqa: F401

# Enumerated with the pin's installed exact/prefix routes and _public_route.
PIN_PUBLIC = {
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/.well-known/oauth-protected-resource/mcp"),
    ("GET", "/app/"),
    ("GET", "/app/auth/"),
    ("GET", "/app/auth/email/verify"),
    ("GET", "/app/cards/"),
    ("GET", "/app/login"),
    ("GET", "/app/settings/memory"),
    ("GET", "/app/static/"),
    ("GET", "/app/threads/"),
    ("GET", "/oauth/authorize"),
    ("GET", "/s/"),
    ("GET", "/skills/decks/"),
    ("POST", "/app/auth/"),
    ("POST", "/app/auth/email"),
    ("POST", "/app/auth/email/verify"),
    ("POST", "/oauth/register"),
    ("POST", "/oauth/revoke"),
    ("POST", "/oauth/token"),
}


def test_console_paths_never_public(console_hub):
    h = console_hub
    for method, path in (
        ("GET", "/api/console"),
        ("GET", "/api/console/stream"),
        ("POST", "/api/console/agents/x"),
    ):
        assert not _public_route(method, path)
    assert h.get(None, "/api/console")[0] != 200


def test_tool_calls_never_write_hub_events(console_hub):
    h = console_hub
    installed = ("POST", "/api/console/agents/") in {(m, p) for m, p, _ in h.gateway.prefix_routes}
    assert h.toggle()[0] == (200 if installed else 404)
    before = events.latest_seq(h.db, h.workspace)
    for _ in range(5):
        successful(h, h.t1, "task_list")
    assert events.latest_seq(h.db, h.workspace) == before


def test_tool_response_unchanged_when_recorder_fails(console_hub, monkeypatch):
    h = console_hub
    request = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "task_list", "arguments": {}},
    }
    first = h.raw(h.t1, request)
    assert first[0] == 200
    recorder = optional("commonplace.console.recorder")
    if recorder is not None:

        def fail(*args):
            raise RuntimeError("synthetic recorder failure")

        monkeypatch.setattr(recorder, "observe", fail)
    second = h.raw(h.t1, request)
    assert first == second


def test_public_surface_unchanged(console_hub):
    gateway = console_hub.gateway
    routes = set(gateway.routes) | {(m, p) for m, p, _ in gateway.prefix_routes}
    assert {(m, p) for m, p in routes if _public_route(m, p)} == PIN_PUBLIC
    assert _public_route("POST", "/oauth/register")
    for method in ("GET", "POST"):
        assert _public_route(method, "/app/auth/provider/callback")


def test_opted_out_connection_writes_nothing(console_hub):
    h = console_hub
    for _ in range(3):
        successful(h, h.t1, "task_list")
    h.get(h.owner, "/api/console")
    h.stream()
    assert not h.activity_files()
