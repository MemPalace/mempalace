"""CLI and MCP contracts for the opt-in EmbeddingGemma 2 search surface."""

import json
import sys

import pytest


@pytest.mark.parametrize(
    ("options", "expected_experimental"),
    [
        ([], {}),
        (
            ["--include-media", "--query-task", "code"],
            {"include_media": True, "query_task": "code"},
        ),
    ],
)
def test_cli_parser_dispatches_optional_search_controls(
    monkeypatch, options, expected_experimental
):
    from mempalace import cli, searcher

    captured = []
    monkeypatch.setattr(cli, "_reconfigure_stdio_utf8_on_windows", lambda: None)
    monkeypatch.setattr(cli, "_search_args_forwardable", lambda _args: False)
    monkeypatch.setattr(searcher, "search", lambda **kwargs: captured.append(kwargs))
    monkeypatch.setattr(
        sys,
        "argv",
        ["mempalace", "--palace", "/disposable/palace", "search", "needle", *options],
    )
    monkeypatch.delenv("PYTHONPATH", raising=False)

    cli.main()

    assert len(captured) == 1
    call = captured[0]
    assert call["query"] == "needle"
    assert call["palace_path"] == "/disposable/palace"
    assert {key: call[key] for key in expected_experimental} == expected_experimental
    assert set(call) == {
        "query",
        "palace_path",
        "wing",
        "room",
        "n_results",
        "since",
        "before",
        *expected_experimental,
    }


def test_cli_search_arguments_are_optional_and_keep_legacy_defaults(monkeypatch):
    from mempalace import cli

    captured = []
    monkeypatch.setattr(cli, "cmd_search", lambda args: captured.append(args))
    monkeypatch.setattr(cli, "_reconfigure_stdio_utf8_on_windows", lambda: None)
    monkeypatch.setattr(sys, "argv", ["mempalace", "search", "needle"])
    monkeypatch.delenv("PYTHONPATH", raising=False)

    cli.main()

    assert len(captured) == 1
    assert captured[0].include_media is False
    assert captured[0].query_task == "search"


def test_mcp_search_forwards_only_nondefault_controls_and_serializes_asset_hit(
    monkeypatch, config, kg
):
    from _mcp_server_helpers import _patch_mcp_server
    from mempalace import mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp_server, "_vector_disabled", False)
    captured = []
    asset_result = {
        "query": "find the blue diagram",
        "results": [
            {
                "result_type": "asset",
                "asset_id": "media_abc123",
                "text": "Image asset: blue-diagram.png",
                "media_type": "image",
                "mime_type": "image/png",
                "title": "blue-diagram.png",
                "source_file": "blue-diagram.png",
                "source_path": "/sandbox/corpus/blue-diagram.png",
                "path": "/sandbox/corpus/blue-diagram.png",
                "project": "demo",
                "wing": "demo",
                "room": "media",
                "drawer_id": "drawer-1",
                "available": True,
                "similarity": 0.947,
                "distance": 0.053,
                "embedding_identity": "embeddinggemma2:google/embeddinggemma-2@rev:768:all:retrieval-v1",
                "embedding_dimension": 768,
                "filed_at": "2026-10-06T12:00:00+00:00",
                "authored_at": None,
                "content_date": None,
            }
        ],
        "filters": {"wing": None, "room": None, "source_file": None},
        "ranking": "shared_cosine",
        "query_task": "code",
    }

    def fake_search(*args, **kwargs):
        captured.append((args, kwargs))
        return asset_result

    monkeypatch.setattr(mcp_server, "search_memories", fake_search)

    result = mcp_server.tool_search(
        query="find the blue diagram", include_media=True, query_task="code"
    )

    assert captured[0][1]["include_media"] is True
    assert captured[0][1]["query_task"] == "code"
    serialized = json.dumps(result)
    round_trip = json.loads(serialized)
    assert round_trip["results"][0]["path"] == "/sandbox/corpus/blue-diagram.png"
    assert round_trip["results"][0]["available"] is True
    assert round_trip["results"][0]["embedding_dimension"] == 768

    captured.clear()
    mcp_server.tool_search(query="legacy search")
    kwargs = captured[0][1]
    assert "include_media" not in kwargs
    assert "query_task" not in kwargs


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"include_media": "yes"}, "include_media"),
        ({"query_task": "image"}, "query_task"),
        ({"include_media": True, "cli_compatible": True}, "cli_compatible"),
        ({"query_task": "code", "cli_compatible": True}, "cli_compatible"),
    ],
)
def test_mcp_search_rejects_invalid_controls_before_search(
    monkeypatch, config, kg, arguments, message
):
    from _mcp_server_helpers import _patch_mcp_server
    from mempalace import mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.setattr(
        mcp_server,
        "search_memories",
        lambda *_args, **_kwargs: pytest.fail("invalid options must be rejected before search"),
    )

    result = mcp_server.tool_search(query="needle", **arguments)

    assert message in result["error"]


def test_mcp_search_schema_keeps_new_controls_optional():
    from mempalace.mcp_server import TOOLS

    schema = TOOLS["mempalace_search"]["input_schema"]
    assert schema["required"] == ["query"]
    assert schema["properties"]["include_media"]["type"] == "boolean"
    assert schema["properties"]["query_task"]["enum"] == ["search", "code"]
