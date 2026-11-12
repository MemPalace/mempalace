"""Interactive handoff integration checks; no external HTTP or Memory worker."""

import base64
import hashlib
import http.client
import json
import socket
import struct
import time

import pytest

from commonplace import sessions
from tests.e2e_handoff.ui_server import (
    SCRIPT_MESSAGE,
    SUCCESSOR_MESSAGE,
    Browser,
    Devtools,
    WebSocket,
    form_body,
    lightpanda,
    lightpanda_binary,
    ui_server,
)


@pytest.fixture
def browser_tools(tmp_path):
    binary = lightpanda_binary()
    if not binary:
        pytest.skip("Lightpanda binary missing (~/.local/bin/lightpanda)")
    # Resolve the browser before constructing the real Hub or any stores.
    with lightpanda(binary, tmp_path) as tools:
        yield tools


@pytest.fixture
def app(browser_tools, tmp_path):
    with ui_server(tmp_path / "data") as server:
        yield server


@pytest.fixture
def owner(app, browser_tools):
    browser = Browser(browser_tools)
    try:
        browser.login(app, app.owner_login_link)
        yield browser
    finally:
        browser.close()


def setting_state(browser):
    return browser.evaluate(
        "(() => {const form = document.querySelector('form.setting');"
        "const toggle = form.querySelector('[name=enabled]'); return {"
        "enabled: toggle.checked, disabled: toggle.disabled, "
        "csrf: form.querySelector('[name=csrf]').value, "
        "buttons: form.querySelectorAll('button').length};})()"
    )


def browser_session(app, browser):
    cookies = browser.call("Network.getCookies", {"urls": [app.origin + "/app/settings/memory"]})
    matching = [item for item in cookies["cookies"] if item["name"] == sessions.COOKIE]
    assert len(matching) == 1
    assert matching[0]["httpOnly"] is True
    return matching[0]["value"]


def post_form(app, browser, fields, *, headers=None):
    cookie = f"{sessions.COOKIE}={browser_session(app, browser)}"
    connection = http.client.HTTPConnection("127.0.0.1", app.hub.port, timeout=5)
    try:
        connection.request(
            "POST",
            "/app/settings/memory",
            form_body(fields),
            {
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                **(headers or {}),
            },
        )
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def test_owner_toggle_override_and_member_read_only(app, owner, browser_tools):
    assert app.owner_token.startswith("cmmn_")
    raw_session = browser_session(app, owner)
    assert raw_session != app.owner_token
    principal = sessions.session_principal(app.hub.db, raw_session)
    assert principal is not None and principal.person is True
    assert principal.role == "owner"
    assert principal.workspace_id == app.hub.workspace
    assert principal.policy.has_scope("handoff:write")
    assert not principal.policy.has_scope("admin")
    assert owner.evaluate("document.querySelector('.brand').getAttribute('href')") == "/app/"
    assert owner.evaluate("document.querySelector('form[action=\"/app/logout\"]') === null")
    state = setting_state(owner)
    assert state["enabled"] is False
    assert state["disabled"] is False
    assert len(state["csrf"]) == 64
    status, settings = app.api("/api/settings/handoff")
    assert status == 200
    assert settings["effective"]["enabled"] is False
    assert settings["effective"]["source"] == "default"

    owner.submit("form.setting", {"enabled": True})
    owner.navigate(app.origin + "/app/settings/memory")
    assert setting_state(owner)["enabled"] is True
    status, settings = app.api("/api/settings/handoff")
    assert status == 200
    assert settings["effective"]["enabled"] is True
    assert settings["effective"]["threshold"] == 0.8

    owner.submit("form.add-override", {"scope": "task", "ref": "#142", "enabled": False})
    owner.navigate(app.origin + "/app/settings/memory")
    assert owner.evaluate(
        "(() => {const row = document.querySelector('form.override'); return {"
        "scope: row.querySelector('[name=scope]').value, "
        "ref: row.querySelector('[name=ref]').value, "
        "enabled: row.querySelector('[name=enabled]').checked};})()"
    ) == {"scope": "task", "ref": "#142", "enabled": False}
    status, settings = app.api("/api/settings/handoff?task=%23142")
    assert status == 200
    assert settings["effective"]["enabled"] is False
    assert settings["effective"]["source"] == "task"
    assert any(
        row["scope"] == "task" and row["ref"] == "#142" and row["enabled"] is False
        for row in settings["rows"]
    )
    assert app.api("/api/settings/handoff")[1]["effective"]["enabled"] is True

    # Lightpanda supports one context at a time. Disposing it destroys the
    # owner's cookies; the member acquires a new session by logging in itself.
    owner.close()
    member = Browser(browser_tools)
    try:
        member.login(app, app.member_login_link)
        member_session = browser_session(app, member)
        assert member_session not in (raw_session, app.member_token)
        member_principal = sessions.session_principal(app.hub.db, member_session)
        assert member_principal is not None and member_principal.person is True
        assert member_principal.role == "member"
        # a member keeps the owner's grant except the owner-only scopes (#285: for_role drops private:read)
        assert member_principal.policy == principal.policy.for_role("member")
        readonly = setting_state(member)
        assert readonly["enabled"] is True
        assert readonly["disabled"] is True
        assert readonly["buttons"] == 0
        assert member.evaluate("document.querySelector('form.add-override') === null")
        assert member.evaluate("document.querySelector('[name=threshold]').disabled")
        assert member.evaluate(
            "Array.from(document.querySelectorAll('.override')).map(row => ({"
            "ref: row.querySelector('strong').textContent, "
            "disabled: row.querySelector('[name=enabled]').disabled, "
            "enabled: row.querySelector('[name=enabled]').checked}))"
        ) == [{"ref": "#142", "disabled": True, "enabled": False}]
        before = app.api("/api/settings/handoff")[1]
        status, body = post_form(
            app,
            member,
            {"csrf": readonly["csrf"], "scope": "workspace", "action": "save", "threshold": "80"},
        )
        assert status == 404
        assert "Not found" in body
        assert app.api("/api/settings/handoff")[1] == before
        assert app.api("/api/settings/handoff?task=%23142")[1]["effective"]["enabled"] is False
    finally:
        member.close()


@pytest.mark.parametrize("path", ["/app/settings/memory", "/app/cards/142", "/app/threads/missing"])
def test_signed_out_handoff_pages_use_shared_login(app, path):
    connection = http.client.HTTPConnection("127.0.0.1", app.hub.port, timeout=5)
    try:
        # An API token does not stand in for the web app's session cookie.
        connection.request("GET", path, headers={"Authorization": f"Bearer {app.owner_token}"})
        response = connection.getresponse()
        assert response.status == 303
        assert response.getheader("Location") == "/app/login"
        assert response.getheader("Set-Cookie") is None
        assert response.read() == b""
    finally:
        connection.close()


def test_card_links_failure_and_verbatim_script(app, owner):
    owner.navigate(app.origin + "/app/cards/142")
    assert owner.evaluate("document.querySelector('h1').textContent") == "#142"
    assert owner.evaluate("document.querySelector('.state').textContent") == "handed off"
    links = owner.evaluate(
        "Array.from(document.querySelectorAll('.timeline a')).map(link => "
        "({text: link.textContent, href: link.href}))"
    )
    assert links == [
        {"text": app.old_path.rsplit("/", 1)[1], "href": app.origin + app.old_path},
        {"text": "Codex successor", "href": app.origin + app.successor_path},
    ]
    assert owner.evaluate("document.querySelector('.meter').getAttribute('aria-valuenow')") == (
        "162400"
    )
    assert owner.evaluate(
        "Array.from(document.querySelectorAll('.preview dt')).map(node => node.textContent)"
    ) == ["Provenance", "Goal", "Status"]
    assert owner.evaluate(
        "document.querySelectorAll('.preview div')[1].querySelector('pre span').textContent"
    ) == ("Finish #142 with every word preserved.")
    owner.evaluate("document.querySelector('.timeline a').click()")
    owner.wait(
        f"location.pathname === {json.dumps(app.old_path)} && document.readyState === 'complete'"
    )
    messages = owner.evaluate(
        "Array.from(document.querySelectorAll('pre span')).map(node => node.textContent)"
    )
    assert messages == ["Finish #142 with every word preserved.", SCRIPT_MESSAGE]
    assert owner.evaluate("typeof window.handoffExecuted") == "undefined"
    assert owner.evaluate("document.querySelectorAll('script').length") == 0

    owner.navigate(app.origin + "/app/cards/142")
    owner.evaluate("document.querySelectorAll('.timeline a')[1].click()")
    owner.wait(
        f"location.pathname === {json.dumps(app.successor_path)} && document.readyState === 'complete'"
    )
    assert owner.evaluate("document.querySelector('pre span').textContent") == SUCCESSOR_MESSAGE

    owner.navigate(app.origin + "/app/cards/143")
    assert owner.evaluate("document.querySelector('h1').textContent") == "#143"
    assert owner.evaluate("document.querySelector('.state').textContent") == "handoff failed"
    assert owner.evaluate("document.querySelector('.warn-text').textContent") == "compacted instead"
    assert owner.evaluate("document.querySelectorAll('.timeline a').length") == 0
    assert owner.evaluate("document.querySelector('.meter') === null")


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"csrf": "0" * 64}, 400),
        ({"threshold": "nan"}, 400),
        ({"scope": "task", "ref": "not-a-card"}, 400),
        ({"action": "unknown"}, 400),
    ],
)
def test_invalid_owner_forms_leave_settings_off(app, owner, changes, expected):
    fields = {
        "csrf": setting_state(owner)["csrf"],
        "scope": "workspace",
        "action": "save",
        "threshold": "80",
        "enabled": "on",
    }
    fields.update(changes)
    before = app.api("/api/settings/handoff")[1]
    status, body = post_form(app, owner, fields)
    assert status == expected
    assert "Invalid form" in body
    assert app.api("/api/settings/handoff")[1] == before


@pytest.mark.parametrize(
    "headers",
    [{"Origin": "https://other.example"}, {"Sec-Fetch-Site": "cross-site"}],
)
def test_cross_site_owner_forms_leave_settings_off(app, owner, headers):
    fields = {
        "csrf": setting_state(owner)["csrf"],
        "scope": "workspace",
        "action": "save",
        "threshold": "80",
        "enabled": "on",
    }
    before = app.api("/api/settings/handoff")[1]
    status, body = post_form(app, owner, fields, headers=headers)
    assert status == 400
    assert "Invalid form" in body
    assert app.api("/api/settings/handoff")[1] == before


def frame(payload, *, first=0x81):
    length = len(payload)
    if length < 126:
        return bytes([first, length]) + payload
    if length < 65536:
        return bytes([first, 126]) + struct.pack("!H", length) + payload
    return bytes([first, 127]) + struct.pack("!Q", length) + payload


@pytest.fixture
def wire_pair():
    local, remote = socket.socketpair()
    remote.settimeout(1)
    wire = WebSocket.__new__(WebSocket)
    wire.sock, wire.buffer, wire.timeout = local, bytearray(), 1
    try:
        yield wire, remote
    finally:
        wire.close()
        remote.close()


def test_socket_fragmentation_events_and_ping(wire_pair):
    wire, remote = wire_pair
    remote.sendall(
        frame(b'{"method":"event"}')
        + frame(b'{"id":999,"result":{}}')
        + frame(b'{"id":', first=0x01)
        + frame(b"hi", first=0x89)
        + frame(b"", first=0x8A)
        + frame(b'1,"result":{"ok":true}}', first=0x80)
    )
    tools = Devtools.__new__(Devtools)
    tools.wire, tools.identifier = wire, 0
    assert tools.call("Test.method") == {"ok": True}
    outgoing = remote.recv(4096)
    assert outgoing[1] & 0x80  # Commands and the pong must be masked.
    offset = 6 + (outgoing[1] & 127)
    assert outgoing[offset] == 0x8A
    assert outgoing[offset + 1] == 0x82
    mask = outgoing[offset + 2 : offset + 6]
    assert bytes(value ^ mask[i % 4] for i, value in enumerate(outgoing[offset + 6 :])) == b"hi"


@pytest.mark.parametrize("size", [1, 200, 70000])
def test_socket_outgoing_mask_and_lengths(wire_pair, size):
    wire, remote = wire_pair
    wire.send(b"x" * size)
    header = remote.recv(2)
    assert header[0] == 0x81 and header[1] & 128
    length = header[1] & 127
    if length == 126:
        length = struct.unpack("!H", remote.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", remote.recv(8))[0]
    assert length == size
    mask = remote.recv(4)
    payload = bytearray()
    while len(payload) < size:
        payload.extend(remote.recv(size - len(payload)))
    assert bytes(value ^ mask[i % 4] for i, value in enumerate(payload)) == b"x" * size


@pytest.mark.parametrize("size", [200, 70000])
def test_socket_incoming_extended_lengths(wire_pair, size):
    wire, _ = wire_pair
    wire.buffer.extend(frame(json.dumps({"text": "x" * size}).encode()))
    assert wire.receive(time.monotonic() + 1) == {"text": "x" * size}


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"\x81\x80", "flags"),
        (b"\xc1\x00", "flags"),
        (b"\x89\x7e\x00\x7e", "control"),
        (b"\x09\x00", "control"),
        (b"\x80\x00", "sequence"),
        (b"\x82\x00", "sequence"),
        (b"\x81\x7f" + struct.pack("!Q", 9 * 1024 * 1024), "Oversized"),
    ],
)
def test_socket_rejects_invalid_frames(wire_pair, data, error):
    wire, _ = wire_pair
    wire.buffer.extend(data)
    with pytest.raises(ValueError, match=error):
        wire.receive(time.monotonic() + 1)


def test_socket_close_and_timeout(wire_pair):
    wire, remote = wire_pair
    with pytest.raises(TimeoutError):
        wire.receive(time.monotonic() - 1)
    remote.sendall(frame(b"", first=0x88))
    with pytest.raises(ConnectionError, match="close"):
        wire.receive(time.monotonic() + 1)


def test_protocol_error_response(wire_pair):
    wire, remote = wire_pair
    tools = Devtools.__new__(Devtools)
    tools.wire, tools.identifier = wire, 0
    remote.sendall(frame(b'{"id":1,"error":{"message":"missing method"}}'))
    with pytest.raises(RuntimeError, match="missing method"):
        tools.call("Missing.method")


@pytest.mark.parametrize("valid", [True, False])
def test_socket_validates_upgrade(monkeypatch, valid):
    class UpgradeSocket:
        def __init__(self):
            self.closed = False

        def sendall(self, request):
            headers = dict(
                line.split(": ", 1) for line in request.decode().split("\r\n")[1:] if ": " in line
            )
            digest = hashlib.sha1(
                (headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
            accept = base64.b64encode(digest).decode() if valid else "wrong"
            self.response = (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: keep-alive,Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode() + frame(b'{"ok":true}')

        def settimeout(self, timeout):
            pass

        def recv(self, size):
            response, self.response = self.response, b""
            return response

        def close(self):
            self.closed = True

    connection = UpgradeSocket()
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: connection)
    if valid:
        wire = WebSocket("ws://127.0.0.1:1234/")
        assert wire.receive(time.monotonic() + 1) == {"ok": True}
        wire.close()
    else:
        with pytest.raises(ValueError, match="upgrade"):
            WebSocket("ws://127.0.0.1:1234/")
    assert connection.closed


def test_socket_rejects_non_loopback():
    with pytest.raises(ValueError, match="loopback"):
        WebSocket("ws://example.com:1234/")
