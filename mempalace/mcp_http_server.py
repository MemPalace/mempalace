#!/usr/bin/env python3
"""
Remote MemPalace MCP server over Streamable HTTP / SSE.

This exposes the existing MemPalace MCP tool handlers behind an HTTP transport
that can be used by ChatGPT developer mode or the OpenAI Responses API.
"""

import argparse
import json
import hmac
import inspect
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MemPalace remote MCP server")
    parser.add_argument(
        "--palace",
        metavar="PATH",
        help="Path to the palace directory (overrides config file and env var)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind (default: 8000)",
    )
    parser.add_argument(
        "--transport",
        choices=["streamable-http", "sse"],
        default="streamable-http",
        help="Remote MCP transport to expose (default: streamable-http)",
    )
    parser.add_argument(
        "--path",
        default="/mcp",
        help="Streamable HTTP path (default: /mcp)",
    )
    parser.add_argument(
        "--sse-path",
        default="/sse",
        help="SSE path (default: /sse)",
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="Return JSON responses for streamable HTTP instead of SSE streams",
    )
    parser.add_argument(
        "--oauth-issuer-url",
        help="Enable OAuth and set the public HTTPS base URL for auth endpoints",
    )
    parser.add_argument(
        "--oauth-secret-file",
        default="~/.mempalace/mcp_http_auth_secret",
        help="Path to the operator secret used on the OAuth consent screen",
    )
    parser.add_argument(
        "--oauth-state-file",
        default="~/.mempalace/mcp_http_oauth.json",
        help="Path to the persisted OAuth state store",
    )
    return parser.parse_args()


def _ensure_remote_runtime() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit(
            "mempalace-mcp-http requires Python 3.10+ because the MCP HTTP SDK "
            "does not support Python 3.9."
        )


def _load_fastmcp():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised manually
        raise SystemExit(
            'Missing remote MCP dependencies. Install with: pip install "mempalace[mcp-http]"'
        ) from exc
    return FastMCP


def _load_tool_registry(palace_path: Optional[str] = None):
    os.environ["MEMPALACE_DISABLE_STDIO_REDIRECT"] = "1"
    if palace_path:
        os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(os.path.expanduser(palace_path))
    from . import mcp_server

    return mcp_server


def _expand_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _append_query_params(url: str, params: Dict[str, str]) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _ensure_parent_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


class FileBackedOAuthProvider:
    def __init__(
        self,
        issuer_url: str,
        operator_secret: str,
        state_file: str,
        access_ttl_seconds: int = 86400,
        refresh_ttl_seconds: int = 2592000,
    ):
        from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken

        self.AccessToken = AccessToken
        self.AuthorizationCode = AuthorizationCode
        self.RefreshToken = RefreshToken
        self.issuer_url = issuer_url.rstrip("/")
        self.operator_secret = operator_secret
        self.state_file = state_file
        self.access_ttl_seconds = access_ttl_seconds
        self.refresh_ttl_seconds = refresh_ttl_seconds
        _ensure_parent_dir(self.state_file)

    def _load_state(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(self.state_file):
            return {
                "clients": {},
                "pending": {},
                "auth_codes": {},
                "access_tokens": {},
                "refresh_tokens": {},
            }
        with open(self.state_file, encoding="utf-8") as f:
            data = json.load(f)
        for key in ("clients", "pending", "auth_codes", "access_tokens", "refresh_tokens"):
            data.setdefault(key, {})
        return data

    def _save_state(self, state: Dict[str, Dict[str, Any]]) -> None:
        tmp_path = f"{self.state_file}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp_path, self.state_file)
        try:
            os.chmod(self.state_file, 0o600)
        except OSError:
            pass

    async def get_client(self, client_id: str):
        from mcp.shared.auth import OAuthClientInformationFull

        state = self._load_state()
        raw = state["clients"].get(client_id)
        if not raw:
            return None
        return OAuthClientInformationFull.model_validate(raw)

    async def register_client(self, client_info) -> None:
        state = self._load_state()
        state["clients"][client_info.client_id] = client_info.model_dump(mode="json")
        self._save_state(state)

    async def authorize(self, client, params) -> str:
        request_id = secrets.token_urlsafe(24)
        state = self._load_state()
        state["pending"][request_id] = {
            "client_id": client.client_id,
            "client_name": client.client_name or client.client_id,
            "scopes": params.scopes or [],
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
            "state": params.state,
            "created_at": int(time.time()),
        }
        self._save_state(state)
        return f"{self.issuer_url}/authorize/consent?request_id={request_id}"

    async def load_authorization_code(self, client, authorization_code: str):
        state = self._load_state()
        raw = state["auth_codes"].get(authorization_code)
        if not raw:
            return None
        return self.AuthorizationCode.model_validate(raw)

    async def exchange_authorization_code(self, client, authorization_code):
        from mcp.shared.auth import OAuthToken

        now = int(time.time())
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        state = self._load_state()
        state["auth_codes"].pop(authorization_code.code, None)
        state["access_tokens"][access_token] = self.AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + self.access_ttl_seconds,
            resource=authorization_code.resource,
        ).model_dump(mode="json")
        state["refresh_tokens"][refresh_token] = self.RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + self.refresh_ttl_seconds,
        ).model_dump(mode="json")
        self._save_state(state)
        return OAuthToken(
            access_token=access_token,
            expires_in=self.access_ttl_seconds,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_refresh_token(self, client, refresh_token: str):
        state = self._load_state()
        raw = state["refresh_tokens"].get(refresh_token)
        if not raw:
            return None
        return self.RefreshToken.model_validate(raw)

    async def exchange_refresh_token(self, client, refresh_token, scopes: list[str]):
        from mcp.shared.auth import OAuthToken

        now = int(time.time())
        new_access_token = secrets.token_urlsafe(32)
        new_refresh_token = secrets.token_urlsafe(32)
        state = self._load_state()
        state["refresh_tokens"].pop(refresh_token.token, None)
        state["access_tokens"][new_access_token] = self.AccessToken(
            token=new_access_token,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + self.access_ttl_seconds,
        ).model_dump(mode="json")
        state["refresh_tokens"][new_refresh_token] = self.RefreshToken(
            token=new_refresh_token,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + self.refresh_ttl_seconds,
        ).model_dump(mode="json")
        self._save_state(state)
        return OAuthToken(
            access_token=new_access_token,
            expires_in=self.access_ttl_seconds,
            refresh_token=new_refresh_token,
            scope=" ".join(scopes),
        )

    async def load_access_token(self, token: str):
        state = self._load_state()
        raw = state["access_tokens"].get(token)
        if not raw:
            return None
        return self.AccessToken.model_validate(raw)

    async def revoke_token(self, token) -> None:
        state = self._load_state()
        token_value = getattr(token, "token", None)
        if token_value:
            state["access_tokens"].pop(token_value, None)
            state["refresh_tokens"].pop(token_value, None)
            self._save_state(state)

    def get_pending(self, request_id: str) -> Optional[Dict[str, Any]]:
        state = self._load_state()
        return state["pending"].get(request_id)

    def approve_pending(self, request_id: str, submitted_secret: str) -> str:
        if not hmac.compare_digest(self.operator_secret, submitted_secret):
            raise ValueError("Invalid operator secret")

        state = self._load_state()
        pending = state["pending"].pop(request_id, None)
        if not pending:
            raise KeyError(request_id)

        auth_code = secrets.token_urlsafe(32)
        state["auth_codes"][auth_code] = self.AuthorizationCode(
            code=auth_code,
            scopes=pending["scopes"],
            expires_at=time.time() + 600,
            client_id=pending["client_id"],
            code_challenge=pending["code_challenge"],
            redirect_uri=pending["redirect_uri"],
            redirect_uri_provided_explicitly=pending["redirect_uri_provided_explicitly"],
            resource=pending["resource"],
        ).model_dump(mode="json")
        self._save_state(state)

        redirect_params = {"code": auth_code}
        if pending.get("state"):
            redirect_params["state"] = pending["state"]
        return _append_query_params(pending["redirect_uri"], redirect_params)


def _load_operator_secret(secret_file: str) -> str:
    env_secret = os.environ.get("MEMPALACE_MCP_AUTH_SECRET")
    if env_secret:
        return env_secret.strip()

    path = _expand_path(secret_file)
    if not os.path.exists(path):
        raise SystemExit(
            "OAuth auth requested but no operator secret was found. "
            f"Create {path} with a long random secret, or set MEMPALACE_MCP_AUTH_SECRET."
        )
    with open(path, encoding="utf-8") as f:
        secret = f.read().strip()
    if not secret:
        raise SystemExit(f"OAuth operator secret file is empty: {path}")
    return secret


def _annotation_for_property(prop_schema: Dict[str, Any], required: bool) -> Any:
    declared_type = prop_schema.get("type")
    mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    base_type = mapping.get(declared_type, Any)
    if required:
        return base_type
    return Optional[base_type]


def _make_tool_wrapper(
    tool_name: str,
    description: str,
    input_schema: Dict[str, Any],
    handler: Callable[..., Dict[str, Any]],
) -> Callable[..., Dict[str, Any]]:
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))
    parameters = []
    annotations: Dict[str, Any] = {"return": Dict[str, Any]}

    for arg_name, prop_schema in properties.items():
        annotation = _annotation_for_property(prop_schema, arg_name in required)
        parameters.append(
            inspect.Parameter(
                arg_name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty if arg_name in required else None,
                annotation=annotation,
            )
        )
        annotations[arg_name] = annotation

    async def wrapper(**kwargs: Any) -> Dict[str, Any]:
        return handler(**kwargs)

    wrapper.__name__ = tool_name
    wrapper.__qualname__ = tool_name
    wrapper.__doc__ = description
    wrapper.__annotations__ = annotations
    wrapper.__signature__ = inspect.Signature(
        parameters=parameters,
        return_annotation=Dict[str, Any],
    )
    return wrapper


def build_server(
    palace_path: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    path: str = "/mcp",
    sse_path: str = "/sse",
    json_response: bool = False,
    oauth_issuer_url: Optional[str] = None,
    oauth_secret_file: str = "~/.mempalace/mcp_http_auth_secret",
    oauth_state_file: str = "~/.mempalace/mcp_http_oauth.json",
):
    _ensure_remote_runtime()
    FastMCP = _load_fastmcp()
    auth_kwargs: Dict[str, Any] = {}
    from mcp.types import ToolAnnotations

    core = _load_tool_registry(palace_path)

    read_only_tools = {
        "mempalace_status",
        "mempalace_list_wings",
        "mempalace_list_rooms",
        "mempalace_get_taxonomy",
        "mempalace_search",
        "mempalace_check_duplicate",
        "mempalace_get_drawer",
        "mempalace_list_drawers",
        "mempalace_get_aaak_spec",
        "mempalace_kg_query",
        "mempalace_kg_timeline",
        "mempalace_kg_stats",
        "mempalace_diary_read",
        "mempalace_memories_filed_away",
        "mempalace_find_tunnels",
        "mempalace_follow_tunnels",
        "mempalace_graph_stats",
        "mempalace_list_tunnels",
        "mempalace_traverse",
    }

    oauth_provider = None
    if oauth_issuer_url:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

        operator_secret = _load_operator_secret(oauth_secret_file)
        oauth_provider = FileBackedOAuthProvider(
            issuer_url=oauth_issuer_url,
            operator_secret=operator_secret,
            state_file=_expand_path(oauth_state_file),
        )
        auth_kwargs = {
            "auth_server_provider": oauth_provider,
            "auth": AuthSettings(
                issuer_url=oauth_issuer_url,
                client_registration_options=ClientRegistrationOptions(enabled=True),
                resource_server_url=f"{oauth_issuer_url.rstrip('/')}{path}",
            ),
        }

    server = FastMCP(
        name="mempalace",
        instructions=(
            "MemPalace remote MCP server. Tools provide verbatim local-memory read/write "
            "operations against the configured palace."
        ),
        host=host,
        port=port,
        streamable_http_path=path,
        sse_path=sse_path,
        json_response=json_response,
        **auth_kwargs,
    )

    if oauth_provider is not None:

        @server.custom_route("/authorize/consent", methods=["GET", "POST"], include_in_schema=False)
        async def oauth_consent(request: Request) -> Response:
            if request.method == "GET":
                request_id = request.query_params.get("request_id", "")
                pending = oauth_provider.get_pending(request_id)
                if not pending:
                    return HTMLResponse("<h1>Invalid or expired authorization request</h1>", status_code=404)
                scopes = ", ".join(pending["scopes"]) if pending["scopes"] else "none"
                html = f"""<!doctype html>
<html><body style="font-family: sans-serif; max-width: 42rem; margin: 2rem auto;">
<h1>Authorize MemPalace</h1>
<p><strong>Client:</strong> {pending["client_name"]}</p>
<p><strong>Scopes:</strong> {scopes}</p>
<p>Enter the MemPalace operator secret to approve this ChatGPT app.</p>
<form method="post">
  <input type="hidden" name="request_id" value="{request_id}">
  <input type="password" name="secret" autofocus style="width: 100%; max-width: 28rem;">
  <div style="margin-top: 1rem;">
    <button type="submit">Approve</button>
  </div>
</form>
</body></html>"""
                return HTMLResponse(html)

            form = await request.form()
            request_id = str(form.get("request_id", ""))
            submitted_secret = str(form.get("secret", ""))
            try:
                redirect_url = oauth_provider.approve_pending(request_id, submitted_secret)
            except KeyError:
                return HTMLResponse("<h1>Invalid or expired authorization request</h1>", status_code=404)
            except ValueError:
                return HTMLResponse("<h1>Authorization failed</h1><p>Invalid operator secret.</p>", status_code=401)
            return RedirectResponse(redirect_url, status_code=302)

    for tool_name, tool_spec in core.TOOLS.items():
        wrapper = _make_tool_wrapper(
            tool_name=tool_name,
            description=tool_spec["description"],
            input_schema=tool_spec["input_schema"],
            handler=tool_spec["handler"],
        )
        server.add_tool(
            wrapper,
            name=tool_name,
            description=tool_spec["description"],
            annotations=(
                ToolAnnotations(readOnlyHint=True) if tool_name in read_only_tools else None
            ),
            structured_output=True,
        )

    return server


def main() -> None:
    args = _parse_args()
    server = build_server(
        palace_path=args.palace,
        host=args.host,
        port=args.port,
        path=args.path,
        sse_path=args.sse_path,
        json_response=args.json_response,
        oauth_issuer_url=args.oauth_issuer_url,
        oauth_secret_file=args.oauth_secret_file,
        oauth_state_file=args.oauth_state_file,
    )
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
