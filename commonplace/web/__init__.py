"""The local, cookie-authenticated Commonplace web app."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

from commonplace import events, plans, sessions, sync_feed, urls
from commonplace.http_util import query, require_principal, send_json

STATIC = Path(__file__).parent / "static"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def _response(h, status, body=b"", content_type="text/plain; charset=utf-8", headers=None):
    h.send_response(status)
    h.send_header("Content-Type", content_type)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("Content-Security-Policy", CSP)
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "no-referrer")
    for name, value in (headers or {}).items():
        h.send_header(name, value)
    h.end_headers()
    h.wfile.write(body)


def _static(h):
    path = unquote(urlsplit(h.path).path)
    relative = path.removeprefix("/app").lstrip("/")
    if (
        "\\" in relative
        or "\x00" in relative
        or any(part in ("..", ".") for part in relative.split("/"))
    ):
        _response(h, 404, b"Not found")
        return
    target = (STATIC / relative).resolve()
    if not target.is_relative_to(STATIC.resolve()):
        _response(h, 404, b"Not found")
        return
    if not target.is_file():
        # Extensionless app paths are SPA routes. Missing assets stay real 404s.
        if Path(relative).suffix or relative.startswith(("api/", "fonts/")):
            _response(h, 404, b"Not found")
            return
        target = (STATIC / "index.html").resolve()
        if not target.is_relative_to(STATIC.resolve()):
            _response(h, 404, b"Not found")
            return
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if target.suffix == ".js":
        mime = "text/javascript"
    if mime.startswith("text/"):
        mime += "; charset=utf-8"
    _response(h, 200, target.read_bytes(), mime)


def install(gateway):
    def login(h):
        token = query(h).get("t", "")
        if not token:
            _response(h, 302, headers={"Location": "/app/"})
            return
        session = sessions.redeem_login_link(gateway.db, token)
        if not session or sessions.session_principal(gateway.db, session) is None:
            _response(h, 302, headers={"Location": "/app/?signin=expired"})
            return
        _response(
            h,
            302,
            headers={
                "Location": "/app/",
                "Set-Cookie": sessions.set_cookie(session, gateway.secure_cookies),
            },
        )

    def logout(h):
        if require_principal(gateway, h) is None:
            return
        session = sessions.cookie_value(h.headers.get("Cookie"))
        if session:
            sessions.end_session(gateway.db, session)
        h.close_connection = True
        _response(h, 204, headers={"Set-Cookie": sessions.clear_cookie()})

    def me(h):
        principal = require_principal(gateway, h)
        if principal is not None:
            send_json(
                h,
                200,
                {
                    "user": principal.user_id,
                    "email": principal.user_email,
                    "workspace": principal.workspace_id,
                    "workspace_name": gateway.db.workspace(principal.workspace_id)["name"],
                    "actor": principal.actor,
                    "people": _people(gateway.db, principal.workspace_id),
                },
            )

    def plan(h):
        principal = require_principal(gateway, h)
        if principal is None:
            return
        if not plans.enabled():
            send_json(h, 200, {"enabled": False})
            return
        account = plans.workspace_account(gateway.db, principal.workspace_id)
        current = plans.get_plan(gateway.db, account) if account else None
        used = (
            gateway.storage_meter.used(
                principal.workspace_id, gateway.workspace_dir(principal.workspace_id)
            )
            if account
            else 0
        )
        send_json(
            h,
            200,
            {
                "enabled": True,
                "plan": current.name if current else None,
                "storage": {"used": used, "limit": current.storage_bytes if current else None},
                "transcription": {
                    "used_today": plans.transcription_used(gateway.db, account) if account else 0,
                    "limit": current.transcription_seconds if current else None,
                },
                "sync": {
                    "interval_seconds": plans.sync_interval(gateway.db, account) if account else 0
                },
            },
        )

    def feed(h):
        principal = require_principal(gateway, h)
        if principal is None:
            return
        if query(h).get("feed") == "sync":
            sync_feed.handle(gateway, h, principal)
            return
        # Events carry labels without theme ACLs. Restrict unfilterable metadata
        # to people and agents already permitted to read the entire workspace.
        if not principal.person and not (
            principal.policy.has_scope("events:read") and principal.policy.sees_all_wings
        ):
            send_json(h, 403, {"error": "Workspace event access required"})
            return
        try:
            seq = int(query(h).get("after", "0"))
            if not 0 <= seq <= 9223372036854775807:
                raise ValueError
        except ValueError:
            send_json(h, 400, {"error": "Invalid event cursor"})
            return
        # The snapshot and read share the lock. The contract's seq is the latest
        # sequence, so return every unread event rather than skipping a batch.
        with gateway.db._lock:
            latest = events.latest_seq(gateway.db, principal.workspace_id)
            batch = events.after(gateway.db, principal.workspace_id, min(seq, latest), latest or 1)
        send_json(h, 200, {"events": batch, "seq": latest})

    def connections(h):
        principal = require_principal(gateway, h)
        if principal is None:
            return
        if principal.role != "owner":
            send_json(h, 403, {"error": "Owner access required"})
            return
        from commonplace.extensions import ToolError
        from commonplace.policy import canonical_wing, is_private_wing

        from .agents import connection_view

        rows = gateway.db.connections(principal.workspace_id)
        try:
            known = gateway.call_memory_tool(principal, "mempalace_list_wings", {}).get("wings", {})
        except ToolError:
            # Connection management remains available while memory is offline.
            known = {}
        wings = sorted(
            {
                canonical_wing(wing)
                for wing in known
                if not is_private_wing(wing)
                and principal.policy.can_read_wing(wing)
                and not any(c in wing for c in "*?[]")
            }
        )
        send_json(
            h,
            200,
            {
                "connections": [connection_view(row) for row in rows],
                "wings": wings,
                "mcp_url": urls.AGENT_URL + "/mcp",
            },
        )

    from . import agents

    agents.install(gateway)

    gateway.add_route("GET", "/app/login", login)
    gateway.add_route("POST", "/app/logout", logout)
    gateway.add_route("GET", "/api/me", me)
    gateway.add_route("GET", "/api/plan", plan)
    gateway.add_route("GET", "/api/events", feed)
    gateway.add_route("GET", "/api/connections", connections)
    gateway.add_prefix_route("GET", "/app/", _static)


def _people(db, workspace_id) -> dict:
    """``user:<id>`` -> the name the app shows: the user's name, else the
    part of the email before the @."""
    people = {}
    for member in db.members(workspace_id):
        name = db.user(member["id"])["name"] or member["email"].split("@", 1)[0]
        people[f"user:{member['id']}"] = name
    return people
