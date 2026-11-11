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
        "/api/runners",
        {"label": "bench-1", "projects": ["demo"], "topics": ["worker"], "wings": ["shared"]},
    )
    assert status == 201, (status, body)
    return session


def _open_admin(chromium, hub, session, stall_connections=False):
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
