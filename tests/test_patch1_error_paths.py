"""Error paths around a failing embedder (patch 1, items 1-9).

Each test pins one refusal: what it says, which channel it uses (one
``mempalace:`` line on the CLI, ``isError`` on MCP), and that nothing is
written or deleted when the write it belongs to fails.
"""

import re

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


# ── (2) no chromadb " in upsert." tail on the error text ─────────────────

_CHROMA_TAIL = re.compile(r" in (?:add|get|query|update|upsert|delete)\.$")


@_MINILM
def test_embedder_errors_lose_chromadbs_method_suffix(request, monkeypatch, kg, capfd):
    """chromadb appends " in <method>." to any error raised inside a collection
    call, so the text read '...embedding_api_url is correct. in upsert.'."""
    palace = _dead_endpoint_palace(request, monkeypatch)
    drawer_id = _a_mined_drawer(palace)
    call = _mcp_caller(monkeypatch, palace, kg)
    for name, arguments in (
        ("mempalace_add_drawer", {"wing": "garden", "room": "notes", "content": "Compost."}),
        ("mempalace_update_drawer", {"drawer_id": drawer_id, "content": "Rewritten note."}),
        ("mempalace_search", {"query": "greenhouse tomatoes"}),
    ):
        result, body = call(name, arguments)
        assert result.get("isError") is True, (name, body)
        assert body["details"].endswith("is correct."), (name, body["details"])
        assert not _CHROMA_TAIL.search(body["details"]), (name, body["details"])

    from mempalace.palace import get_collection

    col = get_collection(str(palace), create=False)
    for method, kwargs in (
        ("add", {"ids": ["x"], "documents": ["Compost."]}),
        ("upsert", {"ids": ["x"], "documents": ["Compost."]}),
        ("update", {"ids": [drawer_id], "documents": ["Compost."]}),
        ("query", {"query_texts": ["compost"], "n_results": 1}),
    ):
        with pytest.raises(Exception) as caught:
            getattr(col, method)(**kwargs)
        assert str(caught.value).endswith("is correct."), (method, str(caught.value))
