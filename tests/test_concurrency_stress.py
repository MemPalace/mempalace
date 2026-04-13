"""Stress test: 100 concurrent writes produce 100 WAL entries, no corruption."""
import asyncio
import json
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
@pytest.mark.slow
async def test_100_concurrent_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path / "palace"))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "palace").mkdir()

    from mempalace.transport.http import build_app

    app = build_app(auth=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async def one_write(i: int):
            resp = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": i,
                    "method": "tools/call",
                    "params": {
                        "name": "mempalace_add_drawer",
                        "arguments": {
                            "wing": "stress",
                            "room": "test",
                            "content": f"stress-write-{i}",
                        },
                    },
                },
            )
            assert resp.status_code == 200

        await asyncio.gather(*[one_write(i) for i in range(100)])

    wal_files = sorted((tmp_path / ".mempalace" / "wal").glob("*.jsonl"))
    assert wal_files, "no WAL file produced"
    entries = []
    for f in wal_files:
        entries.extend(json.loads(line) for line in f.read_text().splitlines() if line)
    assert len(entries) == 100, f"expected 100 WAL entries, got {len(entries)}"
    assert all(e.get("schema_version") == "1" for e in entries)
