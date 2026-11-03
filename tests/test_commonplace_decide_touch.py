"""Computed browser policy at the decision card's inner scroll boundary.

Set PLAYWRIGHT_BROWSERS_PATH to an installed browser cache when HOME is isolated.
"""

import asyncio
from pathlib import Path

import pytest


def assert_vertical_panning(policy):
    assert policy["card"] == "pan-y", "fixture must use the active swipe card"
    assert policy["overflowY"] == "auto", "fixture must retain the inner scroll container"
    assert policy["body"] == "pan-y", "inner scroll container must reserve horizontal gestures"


async def computed_policy():
    from playwright.async_api import async_playwright

    static = Path(__file__).parents[1] / "commonplace" / "web" / "static"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(
                '<article class="decide-card now" data-swipe="on">'
                '<div class="decide-body"><p>Original decision context</p></div></article>'
            )
            for name in ("app.css", "decide.css"):
                await page.add_style_tag(content=(static / name).read_text())
            policy = await page.evaluate("""() => {
                const card = document.querySelector('.decide-card');
                const body = document.querySelector('.decide-body');
                return {card:getComputedStyle(card).touchAction,
                    body:getComputedStyle(body).touchAction,
                    overflowY:getComputedStyle(body).overflowY};
            }""")
            assert_vertical_panning(policy)
        finally:
            await browser.close()


def test_inner_scroll_container_reserves_horizontal_decision_gestures():
    pytest.importorskip("playwright.async_api", reason="optional browser regression dependency")
    asyncio.run(computed_policy())
