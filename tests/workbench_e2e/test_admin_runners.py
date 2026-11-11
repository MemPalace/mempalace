"""Real-browser Admin > Runner machines: a registered machine renders, the loading text goes away."""

import json
from http.client import HTTPConnection

from commonplace import sessions
from tests._commonplace_helpers import hub_fixture as hub_fixture


def _post(port, cookie, path, body):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(
        "POST",
        path,
        json.dumps(body),
        {
            "Content-Type": "application/json",
            "Cookie": f"{sessions.COOKIE}={cookie}",
            "Origin": f"http://127.0.0.1:{port}",
        },
    )
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def _owner_with_machine(hub):
    link = sessions.create_login_link(hub.db, "owner@example.com", hub.workspace)
    session = sessions.redeem_login_link(hub.db, link)
    status, body = _post(
        hub.port,
        session,
        "/api/memory",
        {"text": "Synthetic example.", "theme": "shared", "topic": "demo"},
    )
    assert status == 201, (status, body)
    status, body = _post(
        hub.port,
        session,
        "/api/memory",
        {"text": "Synthetic team example.", "theme": "team", "topic": "demo"},
    )
    assert status == 201, (status, body)
    status, body = _post(
        hub.port,
        session,
        "/api/runners",
        {"label": "bench-1", "projects": ["demo"], "topics": ["worker"], "wings": ["shared"]},
    )
    assert status == 201, (status, body)
    return session


def _open_admin(chromium, hub, session, stall_connections=False, fail_wings=False):
    origin = f"http://127.0.0.1:{hub.port}"
    context = chromium.new_context(viewport={"width": 1280, "height": 1000})
    context.add_cookies([{"name": sessions.COOKIE, "value": session, "url": origin}])
    page = context.new_page()
    errors, failed = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on(
        "response",
        lambda r: (
            failed.append((r.status, r.url)) if "/api/" in r.url and r.status >= 400 else None
        ),
    )
    if stall_connections:
        # The wings read goes through the memory backend and can be slow; it never answers here.
        page.route("**/api/connections", lambda route: None)
    if fail_wings:
        page.route("**/api/runners/wings", lambda route: route.abort())
        page.route("**/api/connections", lambda route: route.abort())
    page.goto(origin + "/app/#/admin", wait_until="domcontentloaded")
    return context, page, errors, failed


def test_admin_runner_machines_list_renders_a_registered_machine(hub, chromium):
    from playwright.sync_api import expect

    session = _owner_with_machine(hub)
    context, page, errors, failed = _open_admin(chromium, hub, session)
    try:
        block = page.locator(".runner-block")
        expect(block.get_by_role("heading", name="bench-1", exact=True)).to_be_visible()
        expect(block.get_by_text("Loading runner machines…")).to_have_count(0)
        expect(block.get_by_role("checkbox", name="shared")).to_have_count(2)
        assert not errors, errors
        assert not failed, failed
    finally:
        context.close()


def test_admin_runner_machines_list_renders_while_the_wings_read_is_stuck(hub, chromium):
    from playwright.sync_api import expect

    session = _owner_with_machine(hub)
    context, page, errors, failed = _open_admin(chromium, hub, session, stall_connections=True)
    try:
        block = page.locator(".runner-block")
        expect(block.get_by_role("heading", name="bench-1", exact=True)).to_be_visible(timeout=5000)
        expect(block.get_by_text("Loading runner machines…")).to_have_count(0)
        assert not errors, errors
        assert not failed, failed
    finally:
        context.close()


def _slow_drawer_listing(hub, config, monkeypatch):
    """#451: on a large palace the drawer-by-drawer wing listing never answered in time."""
    from pathlib import Path

    from commonplace.extensions import ToolError
    from commonplace.web import agents

    real = hub.gateway.call_memory_tool

    def call_memory_tool(principal, name, arguments):
        if name == "mempalace_list_wings":
            raise ToolError("Memory wing listing timed out")
        return real(principal, name, arguments)

    monkeypatch.setattr(hub.gateway, "call_memory_tool", call_memory_tool)
    # The in-process worker keeps its palace at the test config path, not the workspace directory.
    palace = Path(config.palace_path) / "chroma.sqlite3"
    monkeypatch.setattr(agents, "_palace_db", lambda gateway, workspace: palace, raising=False)


def _save_wings(page):
    from playwright.sync_api import expect

    machine = page.locator("article.runner-machine", has_text="bench-1")
    form = machine.locator('form[data-runner-form="wings"]')
    return machine, form, expect


def test_admin_runner_wing_editor_adds_a_wing_while_connections_is_stuck(
    hub, chromium, config, monkeypatch
):
    session = _owner_with_machine(hub)
    _slow_drawer_listing(hub, config, monkeypatch)
    context, page, errors, failed = _open_admin(chromium, hub, session, stall_connections=True)
    try:
        machine, form, expect = _save_wings(page)
        expect(form.get_by_role("checkbox", name="team")).to_be_visible(timeout=5000)
        form.get_by_role("checkbox", name="team").check()
        form.get_by_role("button", name="Save wings").click()
        status = page.locator(".runner-block [data-runner-status]")
        expect(status).to_have_text("Wings saved. Attest the machine again if it was reset.")
        expect(machine).to_contain_text("Wings: shared, team")
        assert not errors, errors
        assert not failed, failed
    finally:
        context.close()


def test_admin_runner_wing_editor_stays_usable_when_the_wing_read_fails(
    hub, chromium, config, monkeypatch
):
    session = _owner_with_machine(hub)
    _slow_drawer_listing(hub, config, monkeypatch)
    context, page, errors, _failed = _open_admin(chromium, hub, session, fail_wings=True)
    try:
        machine, form, expect = _save_wings(page)
        expect(form.get_by_role("checkbox", name="shared")).to_be_checked(timeout=5000)
        form.get_by_label("Add wings (comma separated)").fill("team")
        form.get_by_role("button", name="Save wings").click()
        status = page.locator(".runner-block [data-runner-status]")
        expect(status).to_have_text("Wings saved. Attest the machine again if it was reset.")
        expect(machine).to_contain_text("Wings: shared, team")
    finally:
        context.close()
