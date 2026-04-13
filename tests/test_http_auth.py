"""Verify the HTTP transport enforces the provided auth middleware."""
import pytest
from httpx import AsyncClient, ASGITransport

from mempalace.auth.bearer_static import BearerStaticAuth


@pytest.fixture(autouse=True)
def set_token(monkeypatch):
    monkeypatch.setenv("MEMPALACE_TOKEN", "s3cret")
    yield


@pytest.mark.asyncio
async def test_mcp_post_without_auth_returns_401():
    from mempalace.transport.http import build_app

    app = build_app(auth=BearerStaticAuth())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mcp_post_with_wrong_token_returns_401():
    from mempalace.transport.http import build_app

    app = build_app(auth=BearerStaticAuth())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer wrong"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mcp_post_with_valid_token_returns_200():
    from mempalace.transport.http import build_app

    app = build_app(auth=BearerStaticAuth())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer s3cret"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
    assert resp.status_code == 200
