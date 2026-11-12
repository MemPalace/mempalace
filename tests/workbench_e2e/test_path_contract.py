"""A card's path contract shows in the card panel of the real app (#389)."""

CONTRACT = ["src/api/**", "docs/*.md"]


def _open(ui, seed, number):
    page = ui.open(f"#/card/{number}")
    page.wait_for_selector("#card-panel:not([hidden]) .detail-h", timeout=8000)
    return page


def test_card_panel_shows_the_path_contract(ui, app):
    number = app.number("A-121")
    service = app.service()
    service.task_update(number, version=service.task_show(number)["version"], paths=CONTRACT)
    page = _open(ui, app, number)
    field = page.locator("#card-panel [data-path-contract]")
    assert field.count() == 1
    assert field.locator("code").all_inner_texts() == CONTRACT
    assert page.locator("#card-panel dt", has_text="Owns paths").count() == 1
    assert ui.errors() == []


def test_card_panel_without_a_contract_has_no_paths_field(ui, app):
    page = _open(ui, app, app.number("A-121"))
    assert page.locator("#card-panel [data-path-contract]").count() == 0
    assert page.locator("#card-panel dt", has_text="Owns paths").count() == 0
