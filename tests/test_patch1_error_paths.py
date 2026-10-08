"""Error paths around a failing embedder (patch 1, items 1-9).

Each test pins one refusal: what it says, which channel it uses (one
``mempalace:`` line on the CLI, ``isError`` on MCP), and that nothing is
written or deleted when the write it belongs to fails.
"""

import pytest

from test_embedding_model_fallback import (
    _mcp_caller,
    _openai_compat_palace_with_a_dead_endpoint,
    unknown_model_palace,  # noqa: F401  (fixture)
)


def _MINILM(test):
    test = pytest.mark.usefixtures("unknown_model_palace")(test)
    return pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)(test)


def _dead_endpoint_palace(request, monkeypatch):
    palace = request.getfixturevalue("unknown_model_palace")
    return _openai_compat_palace_with_a_dead_endpoint(palace, monkeypatch)


def _closet_count(palace) -> int:
    from mempalace.palace import get_closets_collection

    return get_closets_collection(str(palace), create=False).count()


def _a_mined_drawer(palace):
    from mempalace.palace import get_collection

    got = get_collection(str(palace), create=False).get(include=["metadatas"], limit=1)
    assert got["ids"] and got["metadatas"][0].get("source_file"), got
    return got["ids"][0]


# ── (1) update_drawer keeps the closets when the update fails ────────────


@_MINILM
@pytest.mark.parametrize(
    "content",
    ["Rewritten note.", "The rewritten note runs long. " * 60],
    ids=["update", "rechunk-upsert"],
)
def test_a_failed_update_drawer_keeps_the_source_closets(request, monkeypatch, kg, content):
    """MCP update_drawer purged the source file's closets before the write,
    so a dead endpoint left the drawer unchanged and its closets gone."""
    palace = _dead_endpoint_palace(request, monkeypatch)
    closets = _closet_count(palace)
    assert closets > 0
    drawer_id = _a_mined_drawer(palace)

    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call("mempalace_update_drawer", {"drawer_id": drawer_id, "content": content})
    assert result.get("isError") is True, body
    assert body["error_class"] == "EmbeddingAPIError", body
    assert _closet_count(palace) == closets
