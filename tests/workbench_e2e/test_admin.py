"""Real-browser Admin shell acceptance over synthetic data and held sources."""

import os
from pathlib import Path

import pytest

from commonplace import sessions
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_admin_api import agent, member


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
        connection_reads = 0

        def connections(route):
            nonlocal connection_reads
            connection_reads += 1
            reads.append(route.request.method)
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
        assert page.get_by_text("Available in a later slice", exact=True).count() == 5
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
