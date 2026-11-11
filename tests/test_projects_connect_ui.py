"""Connect conflicts remain visible in the project UI after dialogs close."""

import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace/web/static"
CONNECT = "/api/projects/ledger/orchestrator/connect"
OWN = "Give this project its own memory section"
SHARED = "Allow this agent to read and write all shared memory"
SHARED_CONFLICT = {
    "error": "shared_wing",
    "explanation": "This project uses shared memory. An agent could read and change all of it.",
    "fix": {"path": "/api/projects/ledger/wing", "label": OWN},
}
OTHER_CONFLICT = {"error": "The orchestrator connection changed; try again"}


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def connect_page(browser):
    project = {
        "board": "ledger",
        "name": "Ledger",
        "can_configure": True,
        "main": {
            "name": "orchestrator:ledger",
            "connected": False,
            "seen": False,
            "connectable": True,
        },
        "memory": {"shared": True, "wing": "shared"},
        "models": {},
    }
    state = {"conflicts": [SHARED_CONFLICT], "posts": []}
    with browser.new_context(viewport={"width": 1280, "height": 1000}) as context:
        page = context.new_page()

        def route_request(route):
            request = route.request
            path = urlsplit(request.url).path
            if path.startswith("/app/"):
                asset = STATIC / path.removeprefix("/app/")
                route.fulfill(
                    body=asset.read_bytes(),
                    content_type=mimetypes.guess_type(asset.name)[0] or "application/octet-stream",
                )
            elif path == "/":
                # Use the shipped styles and real API client, with synthetic HTTP responses.
                styles = "".join(
                    f'<link rel="stylesheet" href="/app/{name}">'
                    for name in ("app.css", "ui.css", "workbench.css")
                )
                route.fulfill(
                    content_type="text/html",
                    body=f"<!doctype html>{styles}<main id='project'></main>",
                )
            elif request.method == "POST":
                state["posts"].append((path, request.post_data_json))
                if path == CONNECT:
                    conflict = state["conflicts"].pop(0) if state["conflicts"] else OTHER_CONFLICT
                    route.fulfill(status=409, json=conflict)
                else:
                    assert path == "/api/projects/ledger/wing"
                    route.fulfill(
                        json={
                            **project,
                            "changed": True,
                            "memory": {"shared": False, "wing": "project_ledger"},
                        }
                    )
            elif path == "/api/projects/ledger":
                route.fulfill(json=project)
            elif path == "/api/connections":
                route.fulfill(json={"connections": []})
            elif path == "/api/boards/ledger/cards":
                route.fulfill(json={"cards": []})
            else:
                assert path == "/api/chat-settings"
                route.fulfill(json={"front": {"provider": "none"}})

        page.route("**/*", route_request)
        page.goto("http://project.test/")
        yield page, state


def start_connect(page, entry):
    page.evaluate(
        """async (entry) => {
            const ui = await import('/app/projects.js');
            const {api, escape: e} = await import('/app/api.js');
            if (entry === 'settings') {
                await ui.mountProject(document.querySelector('#project'), {
                    slug: 'ledger', api, e, settings: true, restart: false,
                });
            } else {
                ui.openConnectDialog({slug: 'ledger', api, e});
            }
        }""",
        entry,
    )
    button = page.locator(
        "[data-project-connect]" if entry == "settings" else "[data-project-create]"
    )
    button.click()
    return button


@pytest.mark.parametrize("entry", ["settings", "intro"])
@pytest.mark.parametrize("dismiss", ["cancel", "escape", "close"])
def test_shared_wing_cancel_keeps_conflict_and_next_step(connect_page, entry, dismiss):
    from playwright.sync_api import expect

    page, state = connect_page
    button = start_connect(page, entry)
    dialog = page.get_by_role("dialog", name="Project memory", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name=OWN, exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name=SHARED, exact=True)).to_be_visible()
    if dismiss == "cancel":
        dialog.get_by_role("button", name="Cancel", exact=True).click()
    elif dismiss == "escape":
        page.keyboard.press("Escape")
    else:
        dialog.evaluate("dialog => dialog.close()")
    expect(dialog).to_have_count(0)
    expect(button).to_be_enabled()
    assert state["posts"] == [(CONNECT, {})], "dismissal must not grant or change memory"
    status = page.locator("[data-project-connect-status]")
    text = status.inner_text()
    assert "shared memory" in text.lower(), f"missing shared-memory conflict: {text!r}"
    assert OWN in text and SHARED in text, f"missing next steps: {text!r}"
    assert status.is_visible(), "the conflict must be visible after the dialog closes"


@pytest.mark.parametrize("entry", ["settings", "intro"])
def test_replacement_409_cancel_keeps_conflict_and_next_step(connect_page, entry):
    from playwright.sync_api import expect

    page, state = connect_page
    state["conflicts"] = [{"error": "replaces current orchestrator"}]
    button = start_connect(page, entry)
    dialog = page.get_by_role("alertdialog", name="Replace the orchestrator?", exact=True)
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(button).to_be_enabled()
    status = page.locator("[data-project-connect-status]")
    text = status.inner_text()
    assert "already has an orchestrator" in text
    assert "confirm its replacement" in text
    assert status.is_visible()
    assert state["posts"] == [(CONNECT, {})]


def test_other_connect_409_shows_reason_in_orchestrator_section(connect_page):
    from playwright.sync_api import expect

    page, state = connect_page
    state["conflicts"] = [OTHER_CONFLICT]
    button = start_connect(page, "settings")
    expect(button).to_be_enabled()
    status = page.locator("[data-project-connect-status]")
    text = status.inner_text()
    assert "Could not connect the agent." in text
    assert OTHER_CONFLICT["error"] in text, f"missing conflict reason: {text!r}"
    assert status.is_visible(), "the other 409 must be visible without a dialog"
    assert status.locator("..").get_by_role("heading", name="Orchestrator", exact=True).count()


@pytest.mark.parametrize("choice", ["own", "shared"])
def test_shared_wing_choices_keep_existing_retry_rules(connect_page, choice):
    from playwright.sync_api import expect

    page, state = connect_page
    state["conflicts"] = [SHARED_CONFLICT, OTHER_CONFLICT]
    button = start_connect(page, "settings")
    page.locator(f'[data-wing-choice="{choice}"]').click()
    expect(button).to_be_enabled()
    expected = [(CONNECT, {})]
    if choice == "own":
        expected += [("/api/projects/ledger/wing", {}), (CONNECT, {})]
    else:
        expected += [(CONNECT, {"allow_shared": True})]
    assert state["posts"] == expected
