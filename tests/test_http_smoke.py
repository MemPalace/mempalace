"""Smoke test: HTTP transport starts and /healthz responds."""
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_healthz_returns_ok():
    from mempalace.transport.http import build_app

    app = build_app(auth=None)  # healthz is unauth'd
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
