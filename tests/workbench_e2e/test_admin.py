"""Real-browser Admin shell acceptance over synthetic data and held sources."""

import os
from pathlib import Path

import pytest

from commonplace import sessions
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_admin_api import agent, member
from tests.test_admin_runtime_api import runtime as runtime


@pytest.mark.parametrize("width", [1440, 768, 390])
def test_admin_owner_cold_reload_retry_and_responsive_navigation(hub, chromium, width):
    from playwright.sync_api import expect

    agent(hub, "workspace-agent")
    link = sessions.create_login_link(hub.db, "owner@example.com", hub.workspace)
    session = sessions.redeem_login_link(hub.db, link)
    origin = f"http://127.0.0.1:{hub.port}"
    context = chromium.new_context(viewport={"width": width, "height": 1000})
    context.add_cookies([{"name": sessions.COOKIE, "value": session, "url": origin}])
    try:
        page = context.new_page()
        errors, reads = [], []
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        # Neither a board nor a shared refresh may be needed for the owner shell.
        for path in ("boards", "needs-me", "rules/count", "events?*"):
            page.route(f"**/api/{path}", lambda route: None)
        page.on(
            "request",
            lambda request: reads.append(request.method) if "/api/" in request.url else None,
        )
        connection_reads = 0

        def connections(route):
            nonlocal connection_reads
            connection_reads += 1
            if connection_reads == 1:
                route.fulfill(status=503, json={"error": "Connections unavailable; try again"})
            else:
                route.continue_()

        page.route("**/api/admin/**/connections", connections)
        page.goto(origin + "/app/#/admin", wait_until="domcontentloaded")
        expect(
            page.get_by_role("heading", name="Workspace administration", exact=True)
        ).to_be_visible()
        expect(
            page.locator('[data-admin-section="users"]').get_by_text(
                "owner@example.com", exact=True
            )
        ).to_be_visible()
        expect(page.get_by_role("button", name="Retry Connections", exact=True)).to_be_visible()
        page.get_by_role("button", name="Retry Connections", exact=True).click()
        expect(page.get_by_text("workspace-agent", exact=True)).to_be_visible()
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_text("workspace-agent", exact=True)).to_be_visible()
        assert page.locator(".admin section").count() == 7
        assert page.get_by_text("Available in a later slice", exact=True).count() == 2
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if width < 700:
            page.get_by_role("button", name="Project: Projects. Switch project", exact=True).click()
            expect(page.get_by_role("option", name="Admin", exact=True)).to_be_visible()
            page.keyboard.press("Escape")
        else:
            page.locator(".user-menu summary").click()
            expect(page.get_by_role("link", name="Admin", exact=True)).to_be_visible()
            page.locator(".user-menu summary").click()
        if os.environ.get("CP_ADMIN_SHOTS"):
            target = Path(os.environ["CP_ADMIN_SHOTS"])
            target.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target / f"admin-{width}.png"), full_page=True)
        assert reads and set(reads) == {"GET"}
        assert not errors
    finally:
        context.close()


def test_admin_member_direct_route_sends_no_inventory_requests(hub, chromium):
    from playwright.sync_api import expect

    member(hub)
    link = sessions.create_login_link(hub.db, "member@example.com", hub.workspace)
    session = sessions.redeem_login_link(hub.db, link)
    origin = f"http://127.0.0.1:{hub.port}"
    context = chromium.new_context(viewport={"width": 390, "height": 844})
    context.add_cookies([{"name": sessions.COOKIE, "value": session, "url": origin}])
    try:
        page = context.new_page()
        reads = []
        page.on(
            "request",
            lambda request: reads.append(request.url) if "/api/admin/" in request.url else None,
        )
        for path in ("boards", "needs-me", "rules/count", "events?*"):
            page.route(f"**/api/{path}", lambda route: None)
        page.goto(origin + "/app/#/admin", wait_until="domcontentloaded")
        expect(
            page.get_by_text("a signed-in workspace owner is required", exact=True)
        ).to_be_visible()
        assert page.locator('a[href="#/admin"]').count() == 0
        page.get_by_role("button", name="Project: Projects. Switch project", exact=True).click()
        assert page.get_by_role("option", name="Admin", exact=True).count() == 0
        assert reads == []
    finally:
        context.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_admin_runtime_panels_retry_independently(hub, runtime, chromium, width):
    from playwright.sync_api import expect

    from tests.test_admin_api import clock
    from tests.test_admin_runtime_api import OLD, machine, task, webhook

    clock(hub)
    cid, _ = agent(hub, "lead", expires_at="2026-11-07T13:00:00Z")
    webhook(hub, cid)
    machine(hub, binding_state="owner_attested")
    task(runtime, 1, claim=OLD)
    link = sessions.create_login_link(hub.db, "owner@example.com", hub.workspace)
    session = sessions.redeem_login_link(hub.db, link)
    origin = f"http://127.0.0.1:{hub.port}"
    context = chromium.new_context(viewport={"width": width, "height": 1000})
    context.add_cookies([{"name": sessions.COOKIE, "value": session, "url": origin}])
    try:
        page = context.new_page()
        reads, errors = [], []
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.on("request", lambda req: reads.append((req.method, req.url)))
        for path in ("boards", "rules/count", "events?*"):
            page.route(f"**/api/{path}", lambda route: None)
        runtime_reads = queue_reads = 0

        def runtime_source(route):
            nonlocal runtime_reads
            runtime_reads += 1
            response = route.fetch()
            data = response.json()
            if runtime_reads == 1:
                data["machines"] = {"state": "error", "error": "Machines unavailable; try again"}
            route.fulfill(response=response, json=data)

        def queue_source(route):
            nonlocal queue_reads
            queue_reads += 1
            if queue_reads == 1:
                route.fulfill(status=503, json={"error": "synthetic source error"})
            else:
                card = {"number": 1, "title": "Decision <now>", "body": "hidden queue body"}
                route.fulfill(json={"count": 1, "asks": [card], "done_review": [card]})

        page.route("**/api/admin/**/runtime", runtime_source)
        page.route("**/api/needs-me", queue_source)
        page.goto(origin + "/app/#/admin", wait_until="domcontentloaded")
        expect(page.get_by_role("button", name="Retry Machines", exact=True)).to_be_visible()
        expect(
            page.locator('[data-admin-panel="stuck"]').get_by_text("Task 1", exact=False)
        ).to_be_visible()
        expect(page.get_by_text("Needs-me unavailable. Retry.", exact=True)).to_be_visible()
        expect(
            page.get_by_text("Workspace concurrency cap: not configured.", exact=False)
        ).to_be_visible()
        page.get_by_role("button", name="Retry Machines", exact=True).click()
        expect(page.get_by_text("Machine 1", exact=True)).to_be_visible()
        page.get_by_role("button", name="Retry Needs-me", exact=True).click()
        expect(page.get_by_text("1 card waiting", exact=True)).to_be_visible()
        assert page.locator('[data-admin-panel="needs"] a[href="#/card/1"]').count() == 2
        assert "hidden queue body" not in page.locator(".admin").inner_text()
        assert page.get_by_text("Available in a later slice", exact=True).count() == 2
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert all(method == "GET" for method, url in reads if "/api/" in url)
        assert not any("/api/admin/instance/" in url for _, url in reads)
        assert not errors
        if os.environ.get("CP_ADMIN_SHOTS"):
            target = Path(os.environ["CP_ADMIN_SHOTS"])
            target.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target / f"admin-runtime-{width}.png"), full_page=True)
            page.locator('section[data-admin-section="runtime"]').screenshot(
                path=str(target / f"runtime-section-{width}.png")
            )
    finally:
        context.close()
