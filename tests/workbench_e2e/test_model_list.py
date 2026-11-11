"""Real-browser check of the project settings model list: two models added in a row."""

from commonplace import sessions
from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext
from tests._commonplace_helpers import hub_fixture as hub_fixture


def test_two_models_can_be_added_in_a_row_and_a_pick_survives(hub, chromium):
    from playwright.sync_api import expect

    owner = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    BoardService(ToolContext(principal=owner, gateway=hub.gateway)).board_add(
        slug="alpha", wing="shared"
    )
    link = sessions.create_login_link(hub.db, "owner@example.com", hub.workspace)
    session = sessions.redeem_login_link(hub.db, link)
    origin = f"http://127.0.0.1:{hub.port}"
    context = chromium.new_context(viewport={"width": 1440, "height": 1000})
    context.add_cookies([{"name": sessions.COOKIE, "value": session, "url": origin}])
    try:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(f"{type(error).__name__}: {error}"))
        page.goto(origin + "/app/#/board/alpha", wait_until="domcontentloaded")
        page.locator('[data-action="coordinator"]:visible').first.click()
        section = page.locator("[data-project-models-section]")
        section.locator("summary").click()
        add = page.locator("[data-project-model-add]")
        status = page.locator("[data-project-model-add-status]")
        main = page.locator('[data-model-role="main"]')
        expect(main).to_be_visible()

        def add_model(provider, model, expected_options):
            add.locator('[name="provider"]').fill(provider)
            add.locator('[name="model"]').fill(model)
            add.get_by_role("button", name="Add to list").click()
            expect(status).to_have_text("Model added to the list.")
            options = main.locator("option")
            expect(options).to_have_count(expected_options)
            assert f"{provider} {model}" in options.evaluate_all(
                "(nodes) => nodes.map((node) => node.value)"
            )

        add_model("claude", "first-model", 2)
        main.select_option("claude first-model")  # an unsaved pick
        # The redraw after the first add left working handlers: a second add works too.
        add_model("codex", "second-model", 3)
        assert main.input_value() == "claude first-model", "the unsaved pick survives the redraw"
        assert "provider=" not in page.url and "model=" not in page.url, page.url
        page.get_by_role("button", name="Save models").click()
        expect(page.locator("[data-project-model-status]")).to_have_text("Project models saved.")
        expect(page.locator('[data-model-stored="main"]')).to_have_text("claude/first-model")
        assert not errors, errors
    finally:
        context.close()
