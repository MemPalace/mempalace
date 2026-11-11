"""Save models in project settings, with the page's optional reads held open."""

import re

from tests.workbench_e2e.scenarios import settle

SLOW = ("**/api/connections", "**/api/chat-settings*")


def open_models(page):
    page.locator('[data-action="coordinator"]:visible').first.click()
    page.wait_for_selector(".workbench-project", timeout=5000)
    page.locator("summary", has_text="Agent models").click()
    page.wait_for_selector("[data-project-models]", state="visible", timeout=5000)


def test_save_models_while_reads_are_slow_and_after_reload(ui, app):
    page = ui.open("#/board/atlas")
    held = []
    for pattern in SLOW:
        page.route(pattern, lambda route: held.append(route))
    open_models(page)
    assert held, "the settings reads must still be pending when the form is used"
    form = page.locator("[data-project-models]")
    form.locator('[name="main_provider"]').fill("anthropic")
    form.locator('[name="main_model"]').fill("model-a")
    saves = []
    page.on(
        "response",
        lambda r: (
            saves.append(r.status)
            if r.request.method == "PUT" and r.url.endswith("/api/projects/atlas/models")
            else None
        ),
    )
    form.get_by_role("button", name="Save models").click()
    # Wait for the save, or for the navigation that replaces it when no handler is bound.
    for _ in range(100):
        if saves or "main_provider=" in page.url:
            break
        page.wait_for_timeout(100)
    # Without the handler the browser submits a GET and puts the values in the URL.
    assert "main_provider=" not in page.url, "the form must not navigate with its values in the URL"
    page.locator("[data-project-model-status]").get_by_text("Project models saved.").wait_for()
    # A second save would arrive shortly after the first; give it the time to show.
    settle(page, 300)
    assert saves == [200], f"exactly one successful save expected, got {saves}"
    for pattern in SLOW:
        page.unroute(pattern)
    settle(page, 300)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(".shell", timeout=8000)
    open_models(page)
    form = page.locator("[data-project-models]")
    assert form.locator('[name="main_provider"]').input_value() == "anthropic"
    assert form.locator('[name="main_model"]').input_value() == "model-a"
    assert re.search(r"/app/#/board/atlas", page.url)
