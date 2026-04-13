"""Parity test: HTTP transport returns the same responses as stdio for fixture queries."""
import json
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

QUERIES = json.loads(Path("tests/fixtures/parity_queries.json").read_text())
REFERENCES = [
    json.loads(line)
    for line in Path("tests/fixtures/parity_stdio_responses.jsonl").read_text().splitlines()
]


@pytest.mark.asyncio
@pytest.mark.parametrize("query,reference", list(zip(QUERIES, REFERENCES, strict=True)))
async def test_http_matches_stdio(query, reference):
    from mempalace.transport.http import build_app

    app = build_app(auth=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": reference["id"],
                "method": "tools/call",
                "params": query,
            },
        )
    assert resp.status_code == 200
    http_response = resp.json()
    assert http_response.get("result") == reference.get("result"), (
        f"parity mismatch for {query['name']}"
    )
