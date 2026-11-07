"""Public-client OAuth authorization for the MCP resource.

Install with ``install(gateway)``. Discovery follows RFC 9728/RFC 8414;
authorization uses exact registered redirects and PKCE S256 only. See
https://modelcontextprotocol.io/specification/latest/basic/authorization.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import secrets
from html import escape
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from .. import import_grant, sessions, urls
from . import store
from .http import OAuthError, body, callback, query, redirect, send, send_error, send_json
from .store import OAuthAuthenticator

__all__ = ["OAuthAuthenticator", "install"]

_BROWSER_COOKIE = "cmmn_oauth"
_CSS = """*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;
background:#0b0c0b;color:#e8e9e5;font:14px/1.5 ui-monospace,Consolas,monospace;padding:20px}
main{background:#161816;width:min(100%,440px);padding:28px;border-top:4px solid #f7941d}
h1{margin:0 0 16px;font-size:24px;overflow-wrap:anywhere}p{margin:0 0 24px;overflow-wrap:anywhere}
form{display:flex;gap:6px;flex-wrap:wrap}button,input{font:inherit;border:0;border-radius:0;padding:12px}
button{background:#202320;color:#e8e9e5;cursor:pointer}button[value=allow],button[type=submit]{background:#f7941d;color:#0b0c0b}
input{background:#050605;color:#e8e9e5;min-width:0;flex:1}button:focus-visible,input:focus-visible{outline:2px solid #e8e9e5;outline-offset:3px}
a.provider{display:block;padding:12px;background:#202320;color:inherit;text-decoration:none;margin-bottom:6px}
a.provider+form{margin-top:24px}a.provider:focus-visible{outline:2px solid #e8e9e5;outline-offset:3px}
"""


def _page(h, content: str, *, status=200, headers=()) -> None:
    style_hash = base64.b64encode(hashlib.sha256(_CSS.encode()).digest()).decode("ascii")
    csp = (
        "default-src 'none'; style-src 'sha256-" + style_hash + "'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    html = (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Commonplace</title><style>" + _CSS + "</style><main>" + content + "</main></html>"
    )
    send(
        h,
        status,
        html.encode("utf-8"),
        media="text/html; charset=utf-8",
        headers=(("Content-Security-Policy", csp), ("X-Frame-Options", "DENY"), *headers),
    )


def _valid_redirect(uri) -> bool:
    if (
        not isinstance(uri, str)
        or not uri
        or len(uri) > 2048
        or "#" in uri
        or "\\" in uri
        or any(ord(c) <= 32 or ord(c) >= 127 for c in uri)
    ):
        return False
    try:
        parts = urlsplit(uri)
        host = parts.hostname
        if not host or parts.username is not None or parts.password is not None:
            return False
        if parts.port is not None and not 0 < parts.port < 65536:
            return False
        if parts.scheme == "https":
            return True
        if parts.scheme != "http":
            return False
        if host == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _register(db, data: dict) -> dict:
    redirects = data.get("redirect_uris")
    if (
        not isinstance(redirects, list)
        or not 1 <= len(redirects) <= 20
        or not all(_valid_redirect(uri) for uri in redirects)
    ):
        raise OAuthError("invalid_redirect_uri")
    name = data.get("client_name", "Application")
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 120
        or any(ord(c) < 32 for c in name)
    ):
        raise OAuthError("invalid_client_metadata")
    if data.get("token_endpoint_auth_method", "none") != "none":
        raise OAuthError("invalid_client_metadata", "Only public clients are supported")
    for key, default, allowed in (
        (
            "grant_types",
            ["authorization_code", "refresh_token"],
            {"authorization_code", "refresh_token"},
        ),
        ("response_types", ["code"], {"code"}),
    ):
        value = data.get(key, default)
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(v, str) or v not in allowed for v in value)
        ):
            raise OAuthError("invalid_client_metadata")
    if "authorization_code" not in data.get("grant_types", ["authorization_code"]):
        raise OAuthError("invalid_client_metadata")
    if "scope" in data:
        if not isinstance(data["scope"], str):
            raise OAuthError("invalid_client_metadata")
        store.scope(data["scope"])
    client_id = "cmmn_client_" + secrets.token_urlsafe(24)
    with db._lock:
        db._conn.execute(
            "INSERT INTO oauth_clients(client_id, client_name, redirect_uris, created_at) VALUES (?,?,?,?)",
            (client_id, name.strip(), json.dumps(list(dict.fromkeys(redirects))), store.now()),
        )
    return {
        "client_id": client_id,
        "client_id_issued_at": store.now(),
        "client_name": name.strip(),
        "redirect_uris": list(dict.fromkeys(redirects)),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": " ".join(store.OAUTH_DEFAULT_SCOPES),
    }


def _response(parameters, **values):
    if "state" in parameters:
        values["state"] = parameters["state"]
    values["iss"] = urls.AGENT_URL
    return callback(parameters["redirect_uri"], values)


def _validate_authorization(db, parameters):
    client = store.client(db, parameters.get("client_id", ""))
    if parameters.get("redirect_uri") not in json.loads(client["redirect_uris"]):
        raise OAuthError("invalid_request", "Redirect URI is not registered")
    # Only errors after exact client/redirect validation may leave this server.
    try:
        if parameters.get("response_type") != "code":
            raise OAuthError("unsupported_response_type")
        if parameters.get("code_challenge_method") != "S256":
            raise OAuthError("invalid_request", "PKCE S256 is required")
        challenge = parameters.get("code_challenge", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
            raise OAuthError("invalid_request", "Invalid PKCE challenge")
        decoded = base64.urlsafe_b64decode(challenge + "=")
        if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != challenge:
            raise OAuthError("invalid_request", "Invalid PKCE challenge")
        parameters["resource"] = store.resource(parameters)
        parameters["scope"] = store.scope(parameters.get("scope"))
    except OAuthError as exc:
        return client, _response(parameters, error=exc.error, error_description=exc.description)
    return client, None


def _browser_cookie(h) -> str:
    cookies = SimpleCookie()
    try:
        cookies.load(h.headers.get("Cookie", ""))
    except CookieError:
        return ""
    value = cookies[_BROWSER_COOKIE].value if _BROWSER_COOKIE in cookies else ""
    return value if re.fullmatch(r"[A-Za-z0-9_-]{32,100}", value) else ""


def _person(gateway, h):
    principal = gateway.principal_for(h)
    return principal if principal and principal.person else None


def _show_authorize(gateway, h, *, status=200):
    parameters = query(h.path)
    client, error_url = _validate_authorization(gateway.db, parameters)
    if error_url:
        redirect(h, error_url)
        return
    principal = _person(gateway, h)
    session = sessions.cookie_value(h.headers.get("Cookie")) or ""
    browser = _browser_cookie(h) or secrets.token_urlsafe(32)
    request = secrets.token_urlsafe(32)
    # HTTP also permits absolute-form request targets. Never copy their
    # authority into a form action or the post-login return redirect.
    original_query = urlsplit(h.path).query
    authorize_path = "/oauth/authorize" + ("?" + original_query if original_query else "")
    with gateway.db._lock:
        gateway.db._conn.execute("DELETE FROM oauth_requests WHERE expires_at <= ?", (store.now(),))
        gateway.db._conn.execute(
            "INSERT INTO oauth_requests(request_hash, parameters, authorize_path, browser_hash, "
            "session_hash, user_id, workspace_id, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                store.digest(request),
                json.dumps(parameters),
                authorize_path,
                store.digest(browser),
                store.digest(session) if principal else "",
                principal.user_id if principal else "",
                principal.workspace_id if principal else "",
                store.now() + store.REQUEST_SECONDS,
            ),
        )
    hidden = f'<input type="hidden" name="request" value="{escape(request, quote=True)}">'
    action = escape(authorize_path, quote=True)
    if principal:
        workspace = gateway.db.workspace(principal.workspace_id)["name"]
        consent = ""
        if import_grant.IMPORT_SCOPE in parameters["scope"].split():
            if import_grant.eligible(gateway.db, principal.user_id, principal.workspace_id):
                consent = (
                    '<label><input type="checkbox" name="grant_import" value="1"> '
                    f"{escape(import_grant.SCOPE_TEXT)}</label>"
                )
            else:
                consent = f"<p>{escape(import_grant.OWNER_ONLY_TEXT)}</p>"
        if import_grant.READER_SCOPE in parameters["scope"].split():
            if import_grant.eligible(gateway.db, principal.user_id, principal.workspace_id):
                consent += (
                    '<label><input type="checkbox" name="grant_import_read" value="1"> '
                    f"{escape(import_grant.READER_TEXT)}</label>"
                    f"<small>{escape(import_grant.READER_DETAIL)}</small>"
                )
            else:
                consent += f"<p>{escape(import_grant.OWNER_ONLY_TEXT)}</p>"
        content = (
            f"<h1>{escape(client['client_name'])}</h1><p>{escape(workspace)}</p>"
            f'<form method="post" action="{action}">{hidden}{consent}'
            '<button name="decision" value="allow">Allow</button>'
            '<button name="decision" value="deny">Deny</button></form>'
        )
        headers = ()
    else:
        cookie = f"{_BROWSER_COOKIE}={browser}; Path=/oauth/authorize; HttpOnly; SameSite=Lax; Max-Age=600"
        if gateway.secure_cookies:
            cookie += "; Secure"
        sign_in = getattr(gateway, "sign_in", None)
        links = "".join(
            f'<a class="provider" href="{escape(link, quote=True)}">Continue with {escape(name)}</a>'
            for name, link in (sign_in.links(authorize_path) if sign_in else [])
        )
        content = (
            links + f'<form method="post" action="{action}">{hidden}'
            '<input name="login_token" aria-label="Sign in token" type="password" required autocomplete="off">'
            '<button type="submit">Sign in</button></form>'
        )
        headers = (("Set-Cookie", cookie),)
    _page(h, content, status=status, headers=headers)


def _submit_authorize(gateway, h):
    principal = _person(gateway, h)
    if principal is None and not _browser_cookie(h):
        raise OAuthError("invalid_request", "Sign in required", 401)
    data = body(h)
    request = data.get("request", "")
    if not request or len(request) > 200:
        raise OAuthError("invalid_request", "Invalid authorization form", 403)
    db = gateway.db
    with db._lock, db._tx() as conn:
        pending = conn.execute(
            "SELECT * FROM oauth_requests WHERE request_hash = ?", (store.digest(request),)
        ).fetchone()
        if pending is None or pending["expires_at"] <= store.now():
            raise OAuthError("invalid_request", "Authorization form expired", 403)
        if not pending["session_hash"]:
            if not secrets.compare_digest(
                pending["browser_hash"], store.digest(_browser_cookie(h))
            ):
                raise OAuthError("invalid_request", "Invalid authorization form", 403)
            if not data.get("login_token"):
                raise OAuthError("invalid_request", "Sign in required", 401)
            # sessions.redeem_login_link uses this same reentrant DB lock. Its
            # schema is installed before serving; don't run its DDL inside our tx.
            conn.execute(
                "DELETE FROM oauth_requests WHERE request_hash = ?", (pending["request_hash"],)
            )
            login = True
        else:
            session = sessions.cookie_value(h.headers.get("Cookie")) or ""
            if (
                principal is None
                or principal.user_id != pending["user_id"]
                or principal.workspace_id != pending["workspace_id"]
                or not secrets.compare_digest(pending["session_hash"], store.digest(session))
            ):
                raise OAuthError("invalid_request", "Invalid authorization form", 403)
            if data.get("decision") not in ("allow", "deny"):
                raise OAuthError("invalid_request")
            parameters = json.loads(pending["parameters"])
            client, error_url = _validate_authorization(db, parameters)
            conn.execute(
                "DELETE FROM oauth_requests WHERE request_hash = ?", (pending["request_hash"],)
            )
            if error_url:
                location = error_url
            elif import_grant.IMPORT_SCOPE in parameters["scope"].split() and not {
                "memory:read",
                "memory:write",
            }.issubset(parameters["scope"].split()):
                location = _response(parameters, error="invalid_scope")
            elif (
                import_grant.READER_SCOPE in parameters["scope"].split()
                and "memory:read" not in parameters["scope"].split()
            ):
                location = _response(parameters, error="invalid_scope")
            elif data["decision"] == "deny":
                location = _response(parameters, error="access_denied")
            else:
                granted = parameters["scope"].split()
                if import_grant.IMPORT_SCOPE in granted and not (
                    data.get("grant_import") == "1"
                    and import_grant.eligible(db, principal.user_id, principal.workspace_id)
                ):
                    granted.remove(import_grant.IMPORT_SCOPE)
                if import_grant.READER_SCOPE in granted and not (
                    data.get("grant_import_read") == "1"
                    and import_grant.eligible(db, principal.user_id, principal.workspace_id)
                ):
                    granted.remove(import_grant.READER_SCOPE)
                if not granted:
                    location = _response(parameters, error="invalid_scope")
                else:
                    code = store.create_code(
                        conn, client, principal, dict(parameters, scope=" ".join(granted))
                    )
                    location = _response(parameters, code=code)
            login = False
    if login:
        session = sessions.redeem_login_link(db, data["login_token"])
        if session is None:
            # A mistyped or expired login token gets a fresh browser-bound form.
            h.path = pending["authorize_path"]
            _show_authorize(gateway, h, status=401)
            return
        redirect(
            h,
            pending["authorize_path"],
            status=303,
            headers=(("Set-Cookie", sessions.set_cookie(session, gateway.secure_cookies)),),
        )
    else:
        redirect(h, location, status=303)


def _protected_metadata():
    return {
        "resource": urls.mcp_url(),
        "authorization_servers": [urls.AGENT_URL],
        "scopes_supported": list(store.OAUTH_DEFAULT_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "Commonplace",
    }


def _server_metadata():
    return {
        "issuer": urls.AGENT_URL,
        "authorization_endpoint": urls.AGENT_URL + "/oauth/authorize",
        "token_endpoint": urls.AGENT_URL + "/oauth/token",
        "registration_endpoint": urls.AGENT_URL + "/oauth/register",
        "revocation_endpoint": urls.AGENT_URL + "/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(store.SCOPES),
        "authorization_response_iss_parameter_supported": True,
    }


def install(gateway) -> None:
    if getattr(gateway, "_commonplace_oauth_installed", False):
        return
    urls.ensure_schema(gateway.db)
    store.ensure_schema(gateway.db)
    gateway.authenticators.append(OAuthAuthenticator(gateway.db))
    gateway.www_authenticate = (
        'Bearer resource_metadata="' + urls.AGENT_URL + '/.well-known/oauth-protected-resource"'
    )

    def endpoint(method, path):
        def register(fn):
            def handle(h):
                try:
                    fn(h)
                except OAuthError as exc:
                    send_error(h, exc)

            gateway.add_route(method, path, handle)
            return handle

        return register

    @endpoint("GET", "/.well-known/oauth-protected-resource")
    @endpoint("GET", "/.well-known/oauth-protected-resource/mcp")
    def protected_metadata(h):
        send_json(h, 200, _protected_metadata())

    @endpoint("GET", "/.well-known/oauth-authorization-server")
    def server_metadata(h):
        send_json(h, 200, _server_metadata())

    @endpoint("POST", "/oauth/register")
    def register(h):
        send_json(h, 201, _register(gateway.db, body(h, as_json=True)))

    @endpoint("GET", "/oauth/authorize")
    def authorize_get(h):
        _show_authorize(gateway, h)

    @endpoint("POST", "/oauth/authorize")
    def authorize_post(h):
        _submit_authorize(gateway, h)

    @endpoint("POST", "/oauth/token")
    def token(h):
        if h.headers.get("Authorization"):
            raise OAuthError("invalid_client", "Only public clients are supported")
        data = body(h)
        if data.get("grant_type") == "authorization_code":
            result = store.exchange(gateway.db, data)
        elif data.get("grant_type") == "refresh_token":
            result = store.rotate(gateway.db, data)
        else:
            raise OAuthError("unsupported_grant_type")
        send_json(h, 200, result)

    @endpoint("POST", "/oauth/revoke")
    def revoke(h):
        if h.headers.get("Authorization"):
            raise OAuthError("invalid_client")
        store.revoke(gateway.db, body(h))
        send(h, 200)

    def short_link(h):
        location = urls.resolve_link(gateway.db, urlsplit(h.path).path.removeprefix("/s/"))
        if location is None:
            send_json(h, 404, {"error": "not_found"})
        else:
            redirect(h, location)

    gateway.add_prefix_route("GET", "/s/", short_link)
    gateway._commonplace_oauth_installed = True
