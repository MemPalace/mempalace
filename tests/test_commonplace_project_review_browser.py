"""Project settings: owner config Accept and Release, in a real browser.

Set PLAYWRIGHT_BROWSERS_PATH to an installed browser cache when HOME is isolated.
"""

import asyncio
import hashlib

import pytest

from commonplace import sessions
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api
from tests.test_commonplace_origin_holder_release import raw
from tests.test_orch_surfaces import config_text


def propose(hub, fresh, text):
    status, version = api(
        hub, fresh.cookie, "POST", "/api/orch/boards/work/configs", {"text": text}
    )
    assert status == 201, version
    return version


def accepted_and_proposed(hub, fresh):
    first = config_text(board="work", runner=False)
    version = propose(hub, fresh, first)
    digest = hashlib.sha256(first.encode()).hexdigest()
    status, reply = api(
        hub,
        fresh.cookie,
        "POST",
        f"/api/orch/boards/work/configs/{version['version']}/accept",
        {"sha256": digest},
    )
    assert status == 200, reply
    return propose(hub, fresh, config_text(board="work", prompt="A changed prompt.", runner=False))


async def with_settings(hub, fresh, check):
    from playwright.async_api import async_playwright

    name, value = fresh.cookie["Cookie"].split("=", 1)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            await context.add_cookies(
                [{"name": name, "value": value, "url": f"http://127.0.0.1:{hub.port}"}]
            )
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{hub.port}/app/#/board/work")
            await page.wait_for_selector(
                '[data-action="coordinator"]', state="attached", timeout=15000
            )
            await page.evaluate(
                """() => document.querySelector('[data-action="coordinator"]').click()"""
            )
            await page.wait_for_selector(
                "[data-project-settings] .workbench-project", timeout=15000
            )
            await check(page)
        finally:
            await browser.close()


def run(hub, fresh, check):
    pytest.importorskip("playwright.async_api", reason="optional browser regression dependency")
    asyncio.run(with_settings(hub, fresh, check))


async def present(page, selector):
    from playwright.async_api import TimeoutError as Timeout

    try:
        await page.wait_for_selector(selector, state="attached", timeout=5000)
    except Timeout:
        return False
    return True


def test_owner_accepts_a_config_version_after_reading_its_changes(hub, fresh):
    proposed = accepted_and_proposed(hub, fresh)
    version = proposed["version"]

    async def check(page):
        button = f'[data-config-accept="{version}"]'
        assert await present(page, button), (
            "Project settings offer no Accept for a proposed version"
        )
        diff = await page.text_content(f'[data-config-version="{version}"] [data-config-diff]')
        assert "A changed prompt." in diff
        await page.click(button)
        await page.wait_for_selector(button, state="detached", timeout=15000)

    run(hub, fresh, check)
    status, reply = api(hub, fresh.cookie, "GET", "/api/orch/boards/work/configs")
    assert status == 200 and reply["accepted"]["version"] == version


@pytest.mark.parametrize("caller", ["agent-token", "member-session"])
def test_config_accept_is_refused_to_anyone_but_the_owner(hub, fresh, caller):
    proposed = accepted_and_proposed(hub, fresh)
    if caller == "agent-token":
        auth = fresh.connect("agent", trust="trusted").token
    else:
        hub.db.add_member(
            fresh.workspace, hub.db.create_user("member@example.test"), "member", actor=fresh.user
        )
        link = sessions.create_login_link(hub.db, "member@example.test", fresh.workspace)
        auth = {"Cookie": f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, link)}"}
        status, project = api(hub, auth, "GET", "/api/projects/work")
        assert status != 200 or not project.get("can_configure"), project
    status, reply = api(
        hub,
        auth,
        "POST",
        f"/api/orch/boards/work/configs/{proposed['version']}/accept",
        {"sha256": proposed["sha256"]},
    )
    assert status == 403, reply
    status, configs = api(hub, fresh.cookie, "GET", "/api/orch/boards/work/configs")
    assert configs["accepted"]["version"] != proposed["version"]


def test_owner_releases_a_stuck_card_from_project_settings(hub, fresh):
    creator = fresh.connect("creator", trust="untrusted", write_wing="inbox")
    fresh.owner.board_set("work", fresh.connect("coordinator", trust="standard").principal.actor)
    number = creator.work.task_add(board="work", title="Stuck card")["number"]

    async def check(page):
        form = f'[data-project-release="{number}"]'
        assert await present(page, form), "Project settings offer no Release for a waiting card"
        await page.fill(f"{form} input", "Checked by the owner")
        await page.click(f"{form} button")
        await page.wait_for_selector(form, state="detached", timeout=15000)

    run(hub, fresh, check)
    assert raw(fresh.owner, number)["approved_by"] == f"user:{fresh.user}"
