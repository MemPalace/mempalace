"""Tests that WAL entries include schema_version and identity fields."""

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_palace(monkeypatch):
    """Point MemPalace at a fresh palace + WAL dir."""
    with tempfile.TemporaryDirectory() as d:
        palace = Path(d) / "palace"
        palace.mkdir()
        wal = Path(d) / "wal"
        wal.mkdir()
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace))
        monkeypatch.setenv("HOME", d)  # so ~/.mempalace/wal resolves here
        yield {"palace": palace, "wal": wal, "home": Path(d)}


def _latest_wal_entry(wal_dir: Path) -> dict:
    files = sorted(wal_dir.glob("*.jsonl"))
    assert files, f"no WAL files in {wal_dir}"
    lines = files[-1].read_text().strip().splitlines()
    assert lines, "WAL file empty"
    return json.loads(lines[-1])


def test_wal_entry_has_schema_version(tmp_palace):
    from mempalace import mcp_server

    mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mempalace_add_drawer",
                "arguments": {
                    "wing": "test",
                    "room": "test",
                    "content": "hello",
                },
            },
        }
    )
    wal_dir = tmp_palace["home"] / ".mempalace" / "wal"
    entry = _latest_wal_entry(wal_dir)
    assert entry.get("schema_version") == "1"


def test_wal_entry_has_identity_field(tmp_palace):
    from mempalace import mcp_server

    mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mempalace_add_drawer",
                "arguments": {"wing": "t", "room": "t", "content": "x"},
            },
        },
        identity="nerdzpc-wsl",
    )
    entry = _latest_wal_entry(tmp_palace["home"] / ".mempalace" / "wal")
    assert entry.get("identity") == "nerdzpc-wsl"


def test_wal_identity_defaults_to_anonymous(tmp_palace):
    from mempalace import mcp_server

    mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mempalace_add_drawer",
                "arguments": {"wing": "t", "room": "t", "content": "x"},
            },
        }
    )
    entry = _latest_wal_entry(tmp_palace["home"] / ".mempalace" / "wal")
    assert entry.get("identity") == "anonymous"
