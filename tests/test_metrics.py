"""Metrics endpoint exposes Prometheus format."""
import pytest
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
async def test_metrics_exposes_prometheus_format():
    from mempalace.transport.http import build_app

    app = build_app(auth=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "mempalace_http_requests_total" in body
