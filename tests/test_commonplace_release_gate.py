"""Fast release-gate helper checks; full browser/MCP evidence is a separate run."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from scripts.commonplace_release import CHECKS, Journey, email_login, main
from scripts.commonplace_release_support import (
    Blocked,
    Mailbox,
    Report,
    checked_json,
    throwaway_instance,
)


def signup(client, origin, email):
    """Exercise production start/callback/account creation with a fake provider."""
    response = client.get(origin + "/app/auth/github", follow_redirects=False)
    assert response.status_code == 302
    authorize = response.headers["Location"]
    state = parse_qs(urlsplit(authorize).query)["state"][0]
    response = client.post(authorize, data={"email": email, "state": state})
    assert response.status_code == 200
    response = client.get(origin + "/api/me")
    assert response.status_code == 200
    return response.json()


def test_empty_instance_two_new_nonadmin_users_have_isolated_workspace_routes():
    with (
        throwaway_instance() as instance,
        httpx.Client(trust_env=False, follow_redirects=True, timeout=5) as first,
        httpx.Client(trust_env=False, follow_redirects=True, timeout=5) as second,
    ):
        assert first.get(instance.origin + "/api/me").status_code == 401
        one = signup(first, instance.origin, "first-release@example.test")
        two = signup(second, instance.origin, "second-release@example.test")
        assert one["user"] != two["user"]
        assert one["workspace"] != two["workspace"]
        assert not instance.gateway.db.user(one["user"])["is_admin"]
        assert not instance.gateway.db.user(two["user"])["is_admin"]
        created = first.post(
            instance.origin + "/api/boards",
            json={"slug": "first-only", "name": "First user's space", "wing": "inbox"},
        )
        assert created.status_code == 201
        assert "first-only" in first.get(instance.origin + "/api/boards").text
        assert "first-only" not in second.get(instance.origin + "/api/boards").text
        assert second.get(instance.origin + "/api/boards/first-only/cards").status_code == 404
        plan = second.get(instance.origin + "/api/plan").json()
        assert plan["enabled"] and plan["plan"] == "free"


def test_storage_limit_uses_new_signup_real_meter_and_public_write():
    with (
        throwaway_instance(storage_limit=0) as instance,
        httpx.Client(trust_env=False, follow_redirects=True, timeout=5) as client,
    ):
        signup(client, instance.origin, "quota-release@example.test")
        plan = client.get(instance.origin + "/api/plan").json()
        assert plan["storage"]["limit"] == 0
        response = client.post(instance.origin + "/api/memory", json={"text": "Quota canary"})
        assert response.status_code == 507
        assert response.json()["limit"] == "storage"
        assert "storage is full" in response.json()["error"]
        assert "Delete something" in response.json()["error"]


def test_mailbox_requires_recipient_same_origin_and_real_login_token():
    box = Mailbox()
    box.send(
        "other@example.test", "link", "http://127.0.0.1:1234/app/auth/email/verify?t=wrong-user"
    )
    box.send(
        "new@example.test", "link", "https://elsewhere.test/app/auth/email/verify?t=wrong-host"
    )
    box.send("new@example.test", "link", "http://127.0.0.1:1234/app/auth/email/verify")
    box.send("new@example.test", "link", "http://127.0.0.1:1234/app/login?t=pre-created-link")
    with pytest.raises(Blocked, match="did not deliver"):
        box.login_link("new@example.test", "http://127.0.0.1:1234", timeout=0)
    expected = "http://127.0.0.1:1234/app/auth/email/verify?t=fresh-link"
    box.send("new@example.test", "link", f"Sign in: {expected}\nExpires soon.")
    assert box.login_link("new@example.test", "http://127.0.0.1:1234") == expected


@pytest.mark.parametrize("status", [404, 405])
def test_missing_routes_block_instead_of_passing_or_leaking_secret(status):
    with pytest.raises(Blocked) as caught:
        checked_json(status, {"token": "secret"}, "GET", "/app/login?t=secret")
    assert "GET /app/login" in str(caught.value)
    assert "secret" not in str(caught.value)


def test_report_never_counts_unrun_or_blocked_checks_as_pass():
    report = Report(["signup", "isolation"])
    report.results["signup"].status = "PASS"
    assert not report.passed
    assert report.payload()["checks"][1]["status"] == "BLOCKED"
    assert "Verdict: FAIL" in report.markdown()
    assert "Real social providers: NOT TESTED" in report.markdown()
    report.results["isolation"].status = "PASS"
    assert report.passed
    report.runner_error = "Timed out during cleanup"
    assert not report.passed
    assert "Timed out during cleanup" in report.markdown()


def test_runner_writes_failure_report_when_browser_dependency_is_missing(tmp_path, monkeypatch):
    async def unavailable(output, report):
        raise ModuleNotFoundError("synthetic missing browser dependency")

    monkeypatch.setattr("scripts.commonplace_release.run", unavailable)
    output = tmp_path / "report"
    assert main(["--output", str(output)]) == 1
    payload = json.loads((output / "report.json").read_text())
    assert not payload["passed"]
    assert len(payload["checks"]) == len(CHECKS)
    assert {row["status"] for row in payload["checks"]} == {"BLOCKED"}
    assert "ModuleNotFoundError" in (output / "report.md").read_text()


def test_runner_cannot_report_success_after_only_one_finished_check(tmp_path, monkeypatch):
    async def incomplete(output, report):
        report.results["email_signup"].status = "PASS"

    monkeypatch.setattr("scripts.commonplace_release.run", incomplete)
    output = tmp_path / "report"
    assert main(["--output", str(output)]) == 1
    assert not json.loads((output / "report.json").read_text())["passed"]


@pytest.mark.parametrize("signup", [False, True])
def test_email_login_waits_through_stale_app_until_signed_in_board(signup):
    email, origin = "signup-release@example.test", "http://127.0.0.1:1234"
    me = {"email": email, "user": "new-person", "actor": "user:new-person", "workspace": "new"}
    states = [{".app"}]
    seen = []

    async def confirm():
        # Confirmation replaces the old app; the board arrives after the shell.
        states[:] = [set(), {".shell"}, {".shell", "board"}]

    async def send_mail():
        instance.mail.send(email, "link", origin + "/app/auth/email/verify?t=synthetic-link")

    class DOMLocator:
        def __init__(self, key):
            self.key = key

        async def wait_for(self):
            for state in states:
                seen.append(state)
                if self.key in state:
                    return
            raise TimeoutError("Signed-in DOM never matched the requested locator")

    field = SimpleNamespace(
        count=AsyncMock(return_value=1),
        fill=AsyncMock(),
        locator=lambda _: SimpleNamespace(
            get_by_role=lambda _: SimpleNamespace(click=AsyncMock(side_effect=send_mail))
        ),
    )

    def by_role(role, *, name, exact):
        if (role, name) == ("textbox", "Email"):
            return field
        if (role, name) == ("button", "Sign in"):
            return SimpleNamespace(click=AsyncMock(side_effect=confirm))
        if (role, name) == ("region", "Board"):
            return DOMLocator("board")
        raise AssertionError(f"Unexpected role: {role}, {name}")

    responses = iter([202, 200, 303])

    @asynccontextmanager
    async def expect_response(predicate):
        future = asyncio.get_running_loop().create_future()
        future.set_result(SimpleNamespace(status=next(responses)))
        yield SimpleNamespace(value=future)

    page = SimpleNamespace(
        url=origin + "/app/",
        goto=AsyncMock(),
        wait_for_load_state=AsyncMock(),
        wait_for_url=AsyncMock(),
        get_by_role=by_role,
        locator=DOMLocator,
        expect_response=expect_response,
        context=SimpleNamespace(
            request=SimpleNamespace(
                get=AsyncMock(return_value=SimpleNamespace(status=401)),
                fetch=AsyncMock(
                    return_value=SimpleNamespace(status=200, json=AsyncMock(return_value=me))
                ),
            )
        ),
    )
    instance = SimpleNamespace(origin=origin, mail=Mailbox())
    report = Report(["email_signup"])
    journey = Journey(None, instance, None, report)
    journey.screenshot = AsyncMock(return_value="email_signup.png")

    async def login():
        identity = await email_login(page, instance, email, signup=signup)
        assert "board" in seen[-1], "Login returned before the signed-in board appeared"
        assert identity == me

    asyncio.run(journey.check("email_signup", login))
    assert report.results["email_signup"].status == "PASS", report.results["email_signup"].detail
    assert seen == [set(), {".shell"}, {".shell", "board"}]


@pytest.mark.parametrize("exception", [TimeoutError, RuntimeError, AssertionError, Blocked])
def test_step_failure_reports_real_message_without_credentials(exception):
    report = Report(["email_signup"])
    journey = Journey(None, None, None, report)
    message = (
        "Board did not appear after confirmation\n"
        "Authorization: Bearer synthetic-bearer-value\n"
        "Cookie: cmmn_session=synthetic-session-value\n"
        "http://127.0.0.1/app/auth/email/verify?t=synthetic-login-value\n"
        "http://127.0.0.1/callback?code=synthetic-code-value&state=synthetic-state-value\n"
        + "cmmn_"
        + "x" * 43
    )

    async def fail():
        raise exception(message)

    asyncio.run(journey.check("email_signup", fail))
    row = report.results["email_signup"]
    assert row.status == ("BLOCKED" if exception is Blocked else "FAIL")
    assert "Board did not appear after confirmation" in row.detail
    if exception in (TimeoutError, RuntimeError):
        assert exception.__name__ in row.detail
    for rendered in (json.dumps(report.payload()), report.markdown()):
        for secret in (
            "synthetic-bearer-value",
            "synthetic-session-value",
            "synthetic-login-value",
            "synthetic-code-value",
            "synthetic-state-value",
            "cmmn_" + "x" * 43,
        ):
            assert secret not in rendered
        assert "[redacted:" in rendered


def test_report_distinguishes_protocol_assertions_and_multiple_visual_captures():
    report = Report(["protocol", "deck"])
    row = report.results["protocol"]
    row.status = "PASS"
    row.evidence_kind = "protocol assertions"
    deck = report.results["deck"]
    deck.status = "PASS"
    deck.screenshots = ["deck-wide.png", "deck-phone.png"]
    payload = report.payload()
    assert payload["checks"][0]["screenshot"] == ""
    assert payload["checks"][0]["evidence_kind"] == "protocol assertions"
    assert payload["checks"][1]["screenshots"] == ["deck-wide.png", "deck-phone.png"]
    assert report.passed
    text = report.markdown()
    assert "No screenshot; see the recorded assertions." in text
    assert "deck-wide.png" in text and "deck-phone.png" in text
    row.status = "BLOCKED"
    assert not report.passed  # Typed evidence never promotes an unrun check.


@pytest.mark.parametrize("preload", [False, True])
def test_mailbox_waits_for_matching_delayed_capture_without_sleep(monkeypatch, preload):
    box = Mailbox()
    entered = threading.Event()
    original = box.changed.wait_for

    def waiting(predicate, timeout):
        entered.set()
        return original(predicate, timeout)

    monkeypatch.setattr(box.changed, "wait_for", waiting)
    origin = "http://127.0.0.1:1234"
    expected = origin + "/app/auth/email/verify?t=delayed-link"
    if preload:
        box.send("later@example.test", "link", origin + "/app/auth/email/verify?t=old-link")
    position = len(box.messages)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(box.login_link, "later@example.test", origin, after=position)
        assert entered.wait(3)
        box.send("other@example.test", "link", expected)
        box.send("later@example.test", "link", expected)
        assert result.result(timeout=3) == expected


@pytest.mark.parametrize("fault", [None, "duplicate", "raw", "retry_status", "bearer_read"])
def test_grant_import_check_uses_public_routes_and_detects_regressions(monkeypatch, fault):
    import asyncio
    from types import SimpleNamespace

    from scripts.commonplace_release import NO_SCREENSHOT_CHECKS, Journey

    name = "grant_import_no_duplicates"
    assert name in CHECKS
    assert CHECKS.index(name) == CHECKS.index("mcp_startup") + 1
    assert name in NO_SCREENSHOT_CHECKS
    with (
        throwaway_instance() as instance,
        httpx.Client(trust_env=False, follow_redirects=True, timeout=5) as owner,
    ):
        signup(owner, instance.origin, "import-release@example.test")
        reads = []

        async def fetch(url, *, method, data, headers, timeout):
            response = owner.request(method, url, content=data, headers=headers)
            value = response.json()
            if "/api/imports" in url:
                reads.append(value)
                if fault == "duplicate" and len(reads) == 2:
                    value["imports"].append(dict(value["imports"][0]))
            if "/messages?" in url and fault == "raw":
                value[0]["raw"] = value[0]["raw"].replace("\r\n", "\n")

            async def result():
                return value

            return SimpleNamespace(status=response.status_code, json=result)

        original = httpx.AsyncClient

        class BearerClient(original):
            async def post(self, *args, **kwargs):
                assert not self.cookies
                response = await super().post(*args, **kwargs)
                if fault == "retry_status" and getattr(self, "uploaded", False):
                    value = response.json()
                    value["results"][0]["status"] = "duplicate"
                    return httpx.Response(200, json=value)
                self.uploaded = True
                return response

            async def get(self, *args, **kwargs):
                assert not self.cookies
                response = await super().get(*args, **kwargs)
                if fault == "bearer_read":
                    return httpx.Response(200, json={"imports": []})
                return response

        monkeypatch.setattr(httpx, "AsyncClient", BearerClient)
        journey = Journey(None, instance, None, Report(CHECKS))
        journey.page = SimpleNamespace(
            url=instance.origin + "/app/",
            context=SimpleNamespace(request=SimpleNamespace(fetch=fetch)),
        )
        journey.values["signup"] = True
        if fault:
            with pytest.raises(AssertionError):
                asyncio.run(journey.grant_import_no_duplicates())
        else:
            detail = asyncio.run(journey.grant_import_no_duplicates())
            assert "accepted=1" in detail and "404" in detail
            assert len(reads) == 2
            assert reads[0] == reads[1]
