import asyncio
import secrets

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from mempalace.mcp_http_server import FileBackedOAuthProvider


def test_file_backed_oauth_provider_round_trip(tmp_path):
    state_file = tmp_path / "oauth.json"
    provider = FileBackedOAuthProvider(
        issuer_url="https://example.test",
        operator_secret="top-secret",
        state_file=str(state_file),
    )

    client = OAuthClientInformationFull(
        client_id="client-1",
        client_secret="secret-1",
        redirect_uris=["https://chatgpt.com/aip/callback"],
        token_endpoint_auth_method="client_secret_post",
    )
    asyncio.run(provider.register_client(client))

    loaded_client = asyncio.run(provider.get_client("client-1"))
    assert loaded_client is not None
    assert loaded_client.client_id == "client-1"

    consent_url = asyncio.run(
        provider.authorize(
            loaded_client,
            AuthorizationParams(
                state="state-123",
                scopes=["read", "write"],
                code_challenge=secrets.token_urlsafe(24),
                redirect_uri="https://chatgpt.com/aip/callback",
                redirect_uri_provided_explicitly=True,
            ),
        )
    )
    assert consent_url.startswith("https://example.test/authorize/consent?request_id=")
    request_id = consent_url.split("request_id=", 1)[1]
    assert provider.get_pending(request_id) is not None

    try:
        provider.approve_pending(request_id, "wrong-secret")
    except ValueError:
        pass
    else:  # pragma: no cover
        assert False, "expected ValueError for incorrect operator secret"

    pending = provider.get_pending(request_id)
    assert pending is not None
    redirect_url = provider.approve_pending(request_id, "top-secret")
    assert redirect_url.startswith("https://chatgpt.com/aip/callback?")
    assert "code=" in redirect_url
    assert "state=state-123" in redirect_url

    code = redirect_url.split("code=", 1)[1].split("&", 1)[0]
    auth_code = asyncio.run(provider.load_authorization_code(loaded_client, code))
    assert auth_code is not None

    tokens = asyncio.run(provider.exchange_authorization_code(loaded_client, auth_code))
    assert tokens.access_token
    assert tokens.refresh_token

    access_token = asyncio.run(provider.load_access_token(tokens.access_token))
    assert access_token is not None
    assert access_token.client_id == "client-1"

    refresh_token = asyncio.run(provider.load_refresh_token(loaded_client, tokens.refresh_token))
    assert refresh_token is not None
    refreshed = asyncio.run(
        provider.exchange_refresh_token(loaded_client, refresh_token, refresh_token.scopes)
    )
    assert refreshed.access_token != tokens.access_token
