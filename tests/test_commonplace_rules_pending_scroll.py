"""The pending Rules notice stays visible while its list scrolls.

Set PLAYWRIGHT_BROWSERS_PATH when the test environment isolates HOME.
"""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parents[1] / "commonplace" / "web" / "static"


def rendered_rules():
    node = shutil.which("node")
    assert node, "Node is required to render the actual Rules view"
    script = """
        import { readFile } from 'node:fs/promises';
        const read = name => readFile(process.argv[1] + '/' + name, 'utf8');
        const url = source => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
        const api = url(await read('api.js'));
        const {rulesView} = await import(url((await read('rules.js')).replace('"./api.js"', JSON.stringify(api))));
        const rules = Array.from({length: 20}, (_, id) => ({
            id, agent: '*', key: 'scroll-' + id, text: 'Preserve these exact words.',
            kind: 'instruction', scope: 'global', status: 'accepted', version: 1,
        }));
        console.log(rulesView({rules}));
    """
    return subprocess.run(
        [node, "--input-type=module", "-e", script, str(STATIC)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout


async def assert_pending_notice_stays_visible(page, *, rules_css=None):
    """Exercise the production view and styles, including after a real list scroll."""
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.set_content(
        '<div class="app"><aside class="side"></aside><main class="main">'
        '<header class="head"><h1>Rules</h1></header><div id="content">'
        + rendered_rules()
        + "</div></main></div>"
    )
    for name in ("app.css", "ui.css", "rules.css"):
        css = (
            rules_css
            if name == "rules.css" and rules_css is not None
            else (STATIC / name).read_text()
        )
        await page.add_style_tag(content=css)
    marker = page.locator("[data-rule-pending]")
    assert await marker.evaluate("e => e.hidden"), "A fresh list must hide the notice"
    await marker.evaluate("e => e.hidden = false")
    await page.locator(".rules-view").evaluate("e => e.scrollTop = e.scrollHeight")
    await page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )
    observed = await marker.evaluate("""marker => {
        const list = marker.closest('.rules-view'), box = marker.getBoundingClientRect();
        const bounds = list.getBoundingClientRect();
        const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
        return {scrollTop:list.scrollTop, scrollHeight:list.scrollHeight, height:list.clientHeight,
            top:box.top, bottom:box.bottom, listTop:bounds.top, listBottom:bounds.bottom,
            visible:getComputedStyle(marker).display !== 'none', foreground:hit === marker || marker.contains(hit),
            background:getComputedStyle(marker).backgroundColor};
    }""")
    assert observed["scrollTop"] > 100, "Fixture must scroll the real Rules list"
    assert observed["scrollHeight"] > observed["height"] + 100, "Fixture needs a long list"
    assert observed["visible"], "The pending notice must be displayed"
    assert observed["listTop"] <= observed["top"] < observed["bottom"] <= observed["listBottom"], (
        f"Pending Rules notice scrolled out of the visible list: {observed}"
    )
    assert observed["foreground"], "The scrolled rules must not cover the pending notice"
    assert observed["background"] not in {"rgba(0, 0, 0, 0)", "transparent"}, (
        "The notice needs an opaque background above scrolled content"
    )
    return observed


async def computed_scroll():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            await assert_pending_notice_stays_visible(await browser.new_page())
        finally:
            await browser.close()


def test_pending_notice_stays_visible_after_scrolling_rules():
    pytest.importorskip("playwright.async_api", reason="optional browser regression dependency")
    asyncio.run(computed_scroll())
