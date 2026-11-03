"""A primary-touch decision drag must stay inside the document viewport."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("playwright")

STATIC = Path(__file__).parents[1] / "commonplace" / "web" / "static"


def rendered_decision():
    node = shutil.which("node")
    assert node, "Node is required to render the actual decision view"
    script = """
        import {readFile} from 'node:fs/promises';
        const read = name => readFile(process.argv[1] + '/' + name, 'utf8');
        const url = source => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
        const api = url(await read('api.js'));
        const source = (await read('decide.js')).replace('"./api.js"', JSON.stringify(api));
        const {createSession, reduce, renderDecide} = await import(url(source));
        const card = {number:1, title:'Synthetic decision', board:'example', version:1,
            status:'review', ask:'Proceed with this synthetic check?',
            review_context:'Verify feedback without answering during drag.',
            ask_options:['Proceed', 'Stop']};
        const {session} = reduce(createSession(1), {type:'load', cards:[card]});
        console.log(renderDecide(session));
    """
    return subprocess.run(
        [node, "--input-type=module", "-e", script, str(STATIC)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout


def assert_document_fits(observed):
    assert observed["scrollWidth"] <= observed["viewport"], (
        f"Decision drag widened the document: {observed}"
    )


async def assert_touch_drag_fits(page, *, width=768, decide_css=None, screenshot=None):
    """Use the production renderer, CSS and pointer handler, then measure mid-drag."""
    await page.set_viewport_size({"width": width, "height": 900})
    await page.set_content(
        '<div class="app" data-screen="decide"><aside class="side"></aside>'
        '<main class="main"><header class="head"><h1>Decide</h1></header>'
        '<div id="content">' + rendered_decision() + "</div></main></div>"
    )
    for name in ("app.css", "ui.css", "decide.css"):
        css = (
            decide_css
            if name == "decide.css" and decide_css is not None
            else (STATIC / name).read_text()
        )
        await page.add_style_tag(content=css)
    await page.evaluate(
        """async source => {
            const {bindSwipe} = await import('data:text/javascript;charset=utf-8,' + encodeURIComponent(source));
            const card = document.querySelector('.decide-card.now');
            window.dragCommits = [];
            window.dragEvents = [];
            for (const type of ['pointerdown', 'pointermove', 'pointercancel']) {
                card.addEventListener(type, event => window.dragEvents.push({
                    type, pointer:event.pointerType, primary:event.isPrimary,
                }));
            }
            bindSwipe(card, document.querySelector('.decide-dock'), {
                armedAt:0, onCommit:choice => window.dragCommits.push(choice),
                labels:{yes:'Yes', no:'No'},
            });
        }""",
        (STATIC / "decide-swipe.js").read_text(),
    )
    original = await page.locator(".decide-card.now").element_handle()
    box = await original.bounding_box()
    assert box and box["width"] > 100, "The real decision card must have a usable size"
    x, y = box["x"] + box["width"] * 0.4, box["y"] + box["height"] * 0.45
    client = await page.context.new_cdp_session(page)
    started = False
    original_error = None
    try:
        await client.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"id": 1, "x": x, "y": y}]},
        )
        started = True
        await page.wait_for_function("e => e.classList.contains('dragging')", arg=original)
        await client.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchMove",
                "touchPoints": [{"id": 1, "x": x + box["width"] * 0.18, "y": y}],
            },
        )
        await page.wait_for_function("e => e.dataset.drag === 'yes'", arg=original)
        observed = await original.evaluate("""e => ({
            connected:e.isConnected, drag:e.dataset.drag,
            opacity:Number(getComputedStyle(e.querySelector(':scope > .stamp.yes')).opacity),
            scrollWidth:document.documentElement.scrollWidth, viewport:innerWidth,
            bodyTouch:getComputedStyle(e.querySelector('.decide-body')).touchAction,
            events:window.dragEvents,
        })""")
        assert observed["connected"] and observed["drag"] == "yes", observed
        assert observed["opacity"] > 0, "The dragged card must retain visible YES feedback"
        assert observed["bodyTouch"] == "pan-y", "Native vertical panning must remain available"
        assert observed["events"][0] == {
            "type": "pointerdown",
            "pointer": "touch",
            "primary": True,
        }, observed
        assert not any(event["type"] == "pointercancel" for event in observed["events"]), observed
        if screenshot:
            await page.screenshot(path=str(screenshot), full_page=True)
        assert_document_fits(observed)
        return observed
    except Exception as exc:
        original_error = exc
        raise
    finally:
        try:
            try:
                if started:
                    await client.send(
                        "Input.dispatchTouchEvent", {"type": "touchCancel", "touchPoints": []}
                    )
                    await page.wait_for_function(
                        "e => !e.classList.contains('dragging') && !e.dataset.drag", arg=original
                    )
                    assert await page.evaluate("window.dragCommits") == [], "Cancel must not decide"
            finally:
                await client.detach()
        except Exception as cleanup_error:
            if original_error is not None:
                raise original_error from cleanup_error
            raise


async def computed_drag():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(has_touch=True)
            for width in (1440, 768, 390):
                await assert_touch_drag_fits(page, width=width)
        finally:
            await browser.close()


def test_primary_touch_decision_drag_does_not_widen_document():
    asyncio.run(computed_drag())
