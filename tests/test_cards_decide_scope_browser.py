"""The cards:decide consent screen and Agents scope UI in a real browser, synthetic data only.

Run with Playwright installed (for example ``uv run --with playwright==1.64.1``) and a
Chromium cache; set PLAYWRIGHT_BROWSERS_PATH when HOME is isolated. Without Playwright
this file errors at import: a missing browser is never a silent skip. Set
DECIDE_SCOPE_SHOTS to a directory to keep screenshots.
"""

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect

from commonplace import sessions
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_cards_decide_scope import api
from tests.test_commonplace_oauth import Client

SCOPE = "memory:read memory:write cards:decide"
DECIDE = "cards:decide"
# A literal copy, so the file runs (and fails behaviourally) on a base without the scope.
SCOPE_TEXT = (
    "Allow this app to answer your Needs-me questions as you (scope cards:decide). "
    "Every answer is recorded with this connection, and you can stop this at any time "
    "under Agents. It can answer only questions addressed to you, never another "
    "person's, and only on boards you can write to."
)


async def _shot(target, name):
    """Screenshot a page or element; no token is on screen when this runs."""
    folder = os.environ.get("DECIDE_SCOPE_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        await target.screenshot(path=str(Path(folder) / name))


async def consent_and_agents(hub):
    base = f"http://127.0.0.1:{hub.port}"
    # A loopback redirect URI on the gateway's own origin: the browser follows the
    # real consent redirect (the consent page allows forms to post only to itself).
    callback = f"{base}/oauth-test-callback?fixed=1"
    session = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, "owner@example.com", hub.workspace)
    )
    client = Client(hub)
    cid = client.register(client_name="Owner Phone", redirect_uris=[callback])
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context(viewport={"width": 1100, "height": 900})
            await context.add_cookies([{"name": sessions.COOKIE, "value": session, "url": base}])
            page = await context.new_page()
            page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))

            # Consent screen: the scope is named, unticked by default, and grantable.
            await page.goto(base + client.authorize_path(cid, scope=SCOPE, redirect_uri=callback))
            box = page.get_by_label(SCOPE_TEXT)
            await expect(box).to_be_visible()
            await expect(box).not_to_be_checked()
            await _shot(page, "consent.png")
            await box.check()
            await page.get_by_role("button", name="Allow").click()
            await page.wait_for_url(f"{base}/oauth-test-callback?**")
            code = parse_qs(urlsplit(page.url).query)["code"][0]
            status, _, raw = client.exchange(cid, code, redirect_uri=callback)
            assert status == 200
            access = json.loads(raw)["access_token"]
            app = hub.gateway.authenticate("Bearer " + access)
            assert app.policy.has_scope(DECIDE)

            # Agent connections next to an ordinary app: only the app may be granted.
            cookie = {"Cookie": f"{sessions.COOKIE}={session}"}
            for name, extra in (
                ("plain-app", {}),
                ("child-agent", {"parent": "plain-app"}),
                ("seat-like", {"client": "commonplace-orchestrator"}),
            ):
                status, _ = api(hub, cookie, "POST", "/api/connections", {"name": name, **extra})
                assert status == 201, name

            # Agents settings: the OAuth client shows the grant and a way to stop it.
            await page.goto(base + "/app/#/settings")
            section = page.locator(".agents-settings")
            await expect(section).to_be_visible(timeout=15000)
            allow = "Allow answering Needs-me"
            for name, offered in (("plain-app", 1), ("child-agent", 0), ("seat-like", 0)):
                agent_row = section.locator(".setting-row", has_text=name)
                await expect(agent_row.get_by_role("button", name=allow)).to_have_count(offered)
            row = section.locator(".setting-row", has_text=app.connection)
            await expect(row.locator('[data-agent-decide="on"]')).to_have_text(
                "Answers your Needs-me questions (cards:decide)"
            )
            # The create form offers the same consent for a new token.
            form_box = section.locator(".agent-form").get_by_label(SCOPE_TEXT, exact=False)
            await expect(form_box).not_to_be_checked()
            await section.locator('.agent-form input[name="name"]').fill("phone-ui")
            await form_box.check()
            await section.get_by_role("button", name="Create token").click()
            await expect(section.locator("[data-agent-token]")).to_be_visible()
            pat_row = section.locator(".setting-row", has_text="phone-ui")
            await expect(pat_row.locator('[data-agent-decide="on"]')).to_be_visible()
            await section.get_by_role("button", name="Done").click()
            await expect(section.locator("[data-agent-token]")).to_have_count(0)
            await _shot(section.locator("[data-agent-list]"), "agents-granted.png")
            pat = hub.db.connection("phone-ui", hub.workspace)
            assert DECIDE in json.loads(pat["policy"])["scopes"]

            # Stop answering on the OAuth client: the row updates and the token loses it at once.
            await row.get_by_role("button", name="Stop answering").click()
            await expect(row.locator('[data-agent-decide="on"]')).to_have_count(0)
            assert not hub.gateway.authenticate("Bearer " + access).policy.has_scope(DECIDE)
            # And on the PAT: the row then offers to allow it again.
            await pat_row.get_by_role("button", name="Stop answering").click()
            await expect(
                pat_row.get_by_role("button", name="Allow answering Needs-me")
            ).to_be_visible()
            await _shot(section.locator("[data-agent-list]"), "agents-stopped.png")
            pat = hub.db.connection("phone-ui", hub.workspace)
            assert DECIDE not in json.loads(pat["policy"])["scopes"]
        finally:
            await browser.close()


def test_consent_and_agents_ui_show_and_revoke_cards_decide(hub):
    asyncio.run(consent_and_agents(hub))
