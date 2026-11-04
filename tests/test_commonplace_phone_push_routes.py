"""Signup-backed phone notification route acceptance, with a local receiver."""

import importlib
import json
import os
import re
import threading
from urllib.parse import urlsplit

import pytest

from commonplace import urls
from tests._commonplace_helpers import REVIEW_CONTEXT, REVIEW_OPTIONS, REVIEW_QUESTION
from tests.phone_push_harness import (
    agent,
    api,
    cli,
    enable,
    fresh as fresh,
    hub as hub,
    off,
    on,
    positive_get,
    receiver as receiver,
    remote as remote,
    rows,
    signup as signup,
)


@pytest.fixture(autouse=True)
def unconfigured(monkeypatch):
    monkeypatch.delenv("COMMONPLACE_NTFY_URL", raising=False)
    monkeypatch.delenv("COMMONPLACE_NTFY_TOKEN", raising=False)


def ask(hub, owner, worker):
    code, card = api(
        hub,
        None,
        "POST",
        "/api/boards/commonplace/cards",
        {"title": "A decision to make"},
        token=worker.token,
    )
    assert code == 201, card
    number = card["number"]
    code, body = api(
        hub,
        None,
        "POST",
        f"/api/cards/{number}/handoff",
        {
            "to": "owner",
            "note": REVIEW_QUESTION,
            "context": REVIEW_CONTEXT,
            "options": REVIEW_OPTIONS,
        },
        token=worker.token,
    )
    assert code == 200, body
    code, needs = api(hub, owner, "GET", "/api/needs-me")
    assert code == 200, needs
    assert number in {card["number"] for card in needs["asks"]}, needs
    return number


def join(hub, person, workspace, by, role):
    """Membership and sign-in use the ordinary CLI and login route."""
    data = os.path.dirname(hub.db.path)
    cli("--data", data, "member", "add", workspace, person, "--role", role, "--by", by)
    link = cli("--data", data, "login-link", person, "--workspace", workspace)
    parsed = urlsplit(link)
    status, headers, body = hub.request(parsed.path + "?" + parsed.query)
    assert status == 302, (status, body)
    assert "Set-Cookie" in headers, headers
    return headers["Set-Cookie"].split(";", 1)[0]


def test_t1_unconfigured_and_not_opted_in_are_inert(hub, signup, monkeypatch):
    _, _, register = signup
    owner, _ = register()
    data = positive_get(hub, owner)
    assert data == {"available": False, "eligible": True, "subscription": None, "test": None}
    code, body = api(hub, owner, "POST", "/api/phone-push", {})
    assert code == 409, body
    assert body == {"error": "no: phone notifications are not set up on this server"}
    assert not any(thread.name == "commonplace-phone-push" for thread in threading.enumerate())
    worker = agent(hub, owner, "worker")
    ask(hub, owner, worker)
    assert hub.ntfy.requests == []

    dispatcher = enable(monkeypatch, hub)
    data = positive_get(hub, owner)
    assert data == {"available": True, "eligible": True, "subscription": None, "test": None}
    ask(hub, owner, worker)
    assert dispatcher.reconcile()
    assert dispatcher.drain() == 0
    assert rows(hub, "phone_push_subscriptions") == []
    assert rows(hub, "phone_push_deliveries") == []
    assert hub.ntfy.requests == []


def test_t12_auth_options_csrf_and_own_subscription(hub, signup, monkeypatch):
    _, _, register = signup
    owner, _ = register()
    positive_get(hub, owner)
    enable(monkeypatch, hub, token="tk_testtoken")
    subscription = on(hub, owner)
    assert re.fullmatch(r"cp[A-Za-z0-9_-]{40}", subscription["topic"])
    assert subscription["server"] == hub.ntfy.url
    assert on(hub, owner) == subscription
    assert len(rows(hub, "phone_push_subscriptions")) == 1

    code, body = api(hub, None, "GET", "/api/phone-push")
    assert code == 401, body
    assert body == {"error": "unauthorized"}
    worker = agent(hub, owner, "worker")
    # The public agent route issues untrusted credentials with the owner's role.
    principal = hub.gateway.authenticate("Bearer " + worker.token)
    assert principal.role == "owner" and not principal.person
    for method, path in (
        ("GET", "/api/phone-push"),
        ("POST", "/api/phone-push"),
        ("DELETE", "/api/phone-push"),
        ("POST", "/api/phone-push/test"),
    ):
        code, body = api(hub, None, method, path, {}, token=worker.token)
        assert code == 403, body
        assert body == {"error": "no: phone notifications need a signed-in person"}
        code, body = api(hub, owner, method, path + "?x=1", {})
        assert code == 400, body
        assert body == {"error": "no: no query parameters"}

    assert api(hub, owner, "GET", "/api/phone-push/x")[0] == 404
    before = rows(hub, "phone_push_subscriptions")
    for headers in ({}, {"Content-Type": "application/json", "Origin": "https://evil.example"}):
        code, _, raw = hub.request(
            "/api/phone-push", "POST", cookie=owner, raw="{}", headers=headers
        )
        assert code == 403, raw
        assert rows(hub, "phone_push_subscriptions") == before
    for method in ("GET", "POST", "DELETE"):
        code, body = api(hub, owner, method, "/api/phone-push", {"categories": ["done"]})
        assert code == 400, body
        assert body == {"error": "no: no options"}
    code, _, body = hub.request(
        "/api/phone-push",
        "POST",
        cookie=owner,
        raw="{",
        headers={"Content-Type": "application/json"},
    )
    assert code == 400, body

    other, _ = register(email="other@example.test")
    other_data = positive_get(hub, other)
    assert other_data["subscription"] is None
    assert subscription["topic"] not in json.dumps(other_data)
    assert "tk_testtoken" not in json.dumps(positive_get(hub, owner))

    assert api(hub, owner, "POST", "/api/phone-push/test", {}) == (202, {"status": "pending"})
    off(hub, owner)
    assert api(hub, owner, "DELETE", "/api/phone-push") == (
        404,
        {"error": "no: phone notifications are off"},
    )
    ended = rows(hub, "phone_push_subscriptions")
    assert len(ended) == 1
    assert not ended[0]["active"] and ended[0]["ended_at"]
    assert rows(hub, "phone_push_state") == []
    tests = rows(hub, "phone_push_deliveries")
    assert len(tests) == 1
    assert tests[0]["status"] == "cancelled"
    assert positive_get(hub, owner)["subscription"] is None
    replacement = on(hub, owner)
    assert replacement["topic"] != subscription["topic"]
    # Turn off remains available after server configuration is removed.
    monkeypatch.delenv("COMMONPLACE_NTFY_URL")
    importlib.import_module("commonplace.phone_push").install(hub.gateway)
    assert not positive_get(hub, owner)["available"]
    off(hub, owner)


@pytest.mark.parametrize(
    "variable,value",
    [
        ("COMMONPLACE_NTFY_URL", "http://203.0.113.5"),
        ("COMMONPLACE_NTFY_URL", "https://u:p@ntfy.example"),
        ("COMMONPLACE_NTFY_URL", "https://ntfy.example/x"),
        ("COMMONPLACE_NTFY_URL", "https://ntfy.example?a=1"),
        ("COMMONPLACE_NTFY_URL", "ftp://ntfy.example"),
        ("COMMONPLACE_NTFY_TOKEN", "Bearer x"),
        ("COMMONPLACE_NTFY_TOKEN", "token\n"),
        ("AGENT_URL", "https://127.0.0.1"),
        ("APP_URL", "https://localhost"),
        ("APP_URL", "https://[::]"),
    ],
)
def test_t13_invalid_configuration_is_off(hub, signup, monkeypatch, caplog, variable, value):
    _, _, register = signup
    owner, _ = register()
    positive_get(hub, owner)
    module = importlib.import_module("commonplace.phone_push")
    dispatcher = importlib.import_module("commonplace.phone_push.dispatcher")
    monkeypatch.setattr(dispatcher.PushDispatcher, "start", lambda self: None)
    monkeypatch.setenv("COMMONPLACE_NTFY_URL", "https://ntfy.example")
    module.install(hub.gateway)
    assert positive_get(hub, owner)["available"]
    if variable in ("AGENT_URL", "APP_URL"):
        monkeypatch.setattr(urls, variable, value)
    else:
        monkeypatch.setenv(variable, value)
    caplog.clear()
    module.install(hub.gateway)
    assert not positive_get(hub, owner)["available"]
    messages = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert messages == [f"invalid {variable}"]
    assert value not in caplog.text
    assert hub.ntfy.requests == []


def test_t18_member_is_ineligible_via_cli_signin(hub, signup, monkeypatch):
    _, _, register = signup
    owner, _ = register()
    positive_get(hub, owner)
    enable(monkeypatch, hub)
    on(hub, owner)
    register(email="member@example.test")
    me = hub.me(owner)
    member = join(hub, "member@example.test", me["workspace"], me["user"], "member")
    assert hub.me(member)["workspace"] == me["workspace"]
    data = positive_get(hub, member)
    assert data["available"] and not data["eligible"] and data["subscription"] is None
    for path in ("/api/phone-push", "/api/phone-push/test"):
        assert api(hub, member, "POST", path, {}) == (
            409,
            {"error": "no: phone notifications are only for the owner, in their home workspace"},
        )


def test_t19_non_home_owner_does_not_change_home_subscription(hub, signup, monkeypatch):
    _, _, register = signup
    owner, _ = register()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    subscription = on(hub, owner)
    second, _ = register(email="second@example.test")
    me, other = hub.me(owner), hub.me(second)
    non_home = join(hub, me["email"], other["workspace"], other["user"], "owner")
    assert hub.me(non_home)["workspace"] == other["workspace"]
    data = positive_get(hub, non_home)
    assert data["available"] and not data["eligible"] and data["subscription"] is None
    for path in ("/api/phone-push", "/api/phone-push/test"):
        assert api(hub, non_home, "POST", path, {}) == (
            409,
            {"error": "no: phone notifications are only for the owner, in their home workspace"},
        )
    assert hub.me(owner)["workspace"] == me["workspace"]
    worker = agent(hub, owner, "worker")
    number = ask(hub, owner, worker)
    assert dispatcher.reconcile()
    assert dispatcher.drain() == 1
    assert len(hub.ntfy.requests) == 1
    request = hub.ntfy.requests[0]
    assert request.path == "/" + subscription["topic"]
    assert request.body == f"#{number} needs you".encode()


def test_configuration_repr_hides_token(hub, signup, monkeypatch):
    owner, _ = signup[2]()
    positive_get(hub, owner)
    sentinel = "synthetic_secret_for_repr"
    enable(monkeypatch, hub, token=sentinel)
    config = importlib.import_module("commonplace.phone_push.config").load()
    assert config is not None and config.token == sentinel
    assert sentinel not in repr(config)
