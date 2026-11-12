"""Real-browser acceptance: model per role and the escalation rule in project settings."""

import pytest

from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests._projects_helpers import ProjectsHub

SECTION = "Agent models and escalation"


COORDINATOR = '[data-action="coordinator"]:visible'


def open_page(chromium, hub, cookie, slug="alpha", width=1280):
    origin = f"http://127.0.0.1:{hub.port}"
    name, value = next(iter(cookie.values())).split("=", 1)
    context = chromium.new_context(viewport={"width": width, "height": 1000})
    context.add_cookies([{"name": name, "value": value, "url": origin}])
    page = context.new_page()
    page.goto(f"{origin}/app/#/board/{slug}", wait_until="domcontentloaded")
    return context, page


def open_settings(page):
    page.locator(COORDINATOR).first.click()
    page.get_by_text(SECTION, exact=True).click()


@pytest.mark.parametrize("width", [1280, 390])
def test_owner_sets_role_models_and_escalation_and_they_persist(hub, chromium, width):
    from playwright.sync_api import expect

    api = ProjectsHub(hub)
    assert api.create_board(api.owner, "alpha")[0] == 201
    context, page = open_page(chromium, hub, api.owner, width=width)
    try:
        open_settings(page)
        errors = []
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        for label in ("Orchestrator", "Builder", "Reviewer", "Gate", "Stronger model"):
            expect(page.locator("legend", has_text=label)).to_be_visible()
        add = page.locator("[data-project-model-add]")
        worker = page.locator('select[name="worker_model"]')
        for model in ("example-cheap", "example-strong"):
            add.locator('input[name="provider"]').fill("example-provider")
            add.locator('input[name="model"]').fill(model)
            add.get_by_role("button", name="Add to list").click()
            # The list is redrawn when the add is saved; wait for the new option, not the note.
            expect(
                page.locator('select[name="worker_model"] option', has_text=model)
            ).to_have_count(1)
            if model == "example-cheap":
                # A pick that is not saved yet survives the next add (reviewer and gate too).
                worker.select_option(label="example-provider · example-cheap")
                page.locator('select[name="gate_model"]').select_option(
                    label="example-provider · example-cheap"
                )
        expect(worker).to_have_value("example-provider example-cheap")
        expect(page.locator('select[name="gate_model"]')).to_have_value(
            "example-provider example-cheap"
        )
        # Every model field is a picker over the workspace list; no free-text model input.
        expect(page.locator('input[name="worker_model"]')).to_have_count(0)
        expect(page.locator('input[name="escalation_model"]')).to_have_count(0)
        page.get_by_role("button", name="Save models").click()
        expect(page.get_by_text("Project models saved.")).to_be_visible()
        page.locator('input[name="after_failures"]').fill("2")
        page.locator('input[name="labels"]').fill("security, concurrency")
        page.locator('select[name="escalation_model"]').select_option(
            label="example-provider · example-strong"
        )
        page.get_by_role("button", name="Save escalation").click()
        expect(page.get_by_text("Escalation rule saved.")).to_be_visible()
        page.reload(wait_until="domcontentloaded")
        open_settings(page)
        expect(page.locator('select[name="worker_model"]')).to_have_value(
            "example-provider example-cheap"
        )
        expect(page.locator('input[name="after_failures"]')).to_have_value("2")
        expect(page.locator('input[name="labels"]')).to_have_value("security, concurrency")
        expect(page.locator('select[name="escalation_model"]')).to_have_value(
            "example-provider example-strong"
        )
        assert api.project(api.owner, "alpha")[1]["escalation"]["after_failures"] == 2
        assert errors == []
    finally:
        context.close()


def test_member_cannot_open_or_change_the_settings(hub, chromium):
    from playwright.sync_api import expect

    api = ProjectsHub(hub)
    api.create_board(api.owner, "alpha")
    context, page = open_page(chromium, hub, api.member)
    try:
        expect(page.get_by_text("only a workspace owner can do that")).to_be_visible(timeout=15000)
        expect(page.locator('[data-action="coordinator"]')).to_have_count(0)
        status = page.evaluate(
            """async () => (await fetch("/api/projects/alpha/escalation", {method: "PUT",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({rule: {after_failures: 2, labels: [],
                    model: {provider: "p", model: "m"}}})})).status"""
        )
        assert status == 403
        assert api.project(api.owner, "alpha")[1]["escalation"] is None
    finally:
        context.close()
