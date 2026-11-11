"""The owner's Release, handoff and refusal controls in a real browser.

Set PLAYWRIGHT_BROWSERS_PATH to an installed browser cache when HOME is isolated.
"""

import asyncio

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api
from tests.test_commonplace_origin_holder_release import raw
from tests.test_commonplace_release_loop import released


async def with_card(hub, fresh, number, check):
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
            await page.goto(f"http://127.0.0.1:{hub.port}/app/#/card/{number}")
            await page.wait_for_selector(f'#card-panel [data-card="{number}"]', timeout=15000)
            # Start only after the first refresh has settled; a redraw after that fails the test.
            await page.wait_for_load_state("networkidle")
            await check(page)
        finally:
            await browser.close()


def run(hub, fresh, number, check):
    pytest.importorskip("playwright.async_api", reason="optional browser regression dependency")
    asyncio.run(with_card(hub, fresh, number, check))


HANDOFF = 'form[data-form="handoff"]'


async def open_handoff(page):
    await page.click('[data-action="handoff"]')
    await page.wait_for_selector(HANDOFF)


async def present(page, selector, state="visible"):
    from playwright.async_api import TimeoutError as Timeout

    try:
        await page.wait_for_selector(selector, state=state, timeout=4000)
    except Timeout:
        return False
    return True


async def submit_handoff(page, recipient):
    if not await page.locator(HANDOFF).count():
        await open_handoff(page)
    await page.fill(f'{HANDOFF} [name="to"]', recipient, timeout=5000)
    await page.fill(f'{HANDOFF} [name="note"]', "Passing this on", timeout=5000)
    await page.click(f"{HANDOFF} button.blockbtn", timeout=5000)


def test_owner_release_button_is_reflected_on_the_card(hub, fresh):
    creator = fresh.connect("creator", trust="untrusted", write_wing="inbox")
    fresh.owner.board_set("work", fresh.connect("coordinator", trust="standard").principal.actor)
    number = creator.work.task_add(board="work", title="Waiting")["number"]

    async def check(page):
        form = 'form[data-form="release-origin"]'
        assert await present(page, form), "the owner has no Release control on this card"
        await page.fill(f"{form} [name=reason]", "Checked by the owner")
        await page.click(f"{form} button")
        await page.wait_for_selector(form, state="detached", timeout=15000)

    run(hub, fresh, number, check)
    assert raw(fresh.owner, number)["approved_by"] == f"user:{fresh.user}"


def test_handoff_form_names_the_orchestrator_and_the_pool(hub, fresh):
    _, coordinator, number = released(hub, fresh)

    async def check(page):
        await open_handoff(page)
        values = []
        if await present(page, "#handoff-recipients option", state="attached"):
            values = await page.eval_on_selector_all(
                "#handoff-recipients option", "nodes => nodes.map(n => n.value)"
            )
        assert {"orchestrator", "anyone"} <= set(values)
        await submit_handoff(page, "orchestrator")
        await page.wait_for_selector(HANDOFF, state="detached", timeout=15000)

    run(hub, fresh, number, check)
    assert raw(fresh.owner, number)["assignee"] == coordinator.principal.actor


def test_refused_handoff_is_visible_in_the_card_panel(hub, fresh):
    _, _, number = released(hub, fresh)

    async def check(page):
        await open_handoff(page)
        await submit_handoff(page, "no-such-agent")
        await page.locator(".message.error").first.wait_for(state="visible", timeout=15000)
        assert await present(page, "#card-panel [data-panel-error]"), (
            "the refusal is not shown in the card panel the owner is looking at"
        )
        assert "recipient" in (await page.inner_text("#card-panel [data-panel-error]")).lower()

    run(hub, fresh, number, check)
    status, card = api(hub, fresh.cookie, "GET", f"/api/cards/{number}")
    assert status == 200 and card["assignee"] is not None
