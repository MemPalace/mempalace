"""Shared-vector ranking and opt-in public search contracts without model downloads."""

import math

import pytest

from mempalace import embedding
from mempalace.palace import get_collection
from mempalace.searcher import search_memories


class _Provider:
    dimension = 768
    modalities = "all"
    identity = "embeddinggemma2:test-model:768:all:retrieval-v1"

    def __init__(self):
        self.calls = []

    def embed_query(self, input):
        self.calls.append("search")
        return [[1.0] + [0.0] * 767 for _ in input]

    def embed_code_query(self, input):
        self.calls.append("code")
        return [[0.0, 1.0] + [0.0] * 766 for _ in input]


@pytest.fixture(params=["chroma", "sqlite_exact"])
def indexed_media(tmp_path, monkeypatch, request):
    from mempalace.palace import ASSETS_COLLECTION_NAME

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "embeddinggemma2")
    monkeypatch.setenv("MEMPALACE_BACKEND", request.param)
    provider = _Provider()
    monkeypatch.setattr(embedding, "get_embedding_function", lambda **_: provider)
    from mempalace.backends import embedding_wrapper

    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts, **_: provider.embed_query(texts)
    )
    palace = str(tmp_path / "palace")
    drawers = get_collection(palace)
    drawers.upsert(
        documents=["Authentication implementation stored verbatim."],
        ids=["drawer-1"],
        metadatas=[
            {"wing": "demo", "room": "general", "source_file": "auth.py", "filed_at": "2026-10-01"}
        ],
        embeddings=[[0.8, 0.6] + [0.0] * 766],
    )
    image = tmp_path / "screen.png"
    image.write_bytes(b"indexed by mocked provider")
    assets = get_collection(palace, collection_name=ASSETS_COLLECTION_NAME)
    assets.upsert(
        documents=["Image asset: screen.png"],
        ids=["asset-1"],
        metadatas=[
            {
                "media_type": "image",
                "mime_type": "image/png",
                "title": "screen.png",
                "project": "demo",
                "wing": "demo",
                "room": "general",
                "filed_at": "2026-10-02",
                "source_path": str(image),
                "source_file": str(image),
                "related_drawer_id": "drawer-1",
                "embedding_identity": provider.identity,
                "embedding_dimension": 768,
            }
        ],
        embeddings=[[0.95, math.sqrt(1 - 0.95**2)] + [0.0] * 766],
    )
    yield palace, provider, image


def test_shared_cosine_merges_text_and_asset_and_retains_missing_path(indexed_media):
    palace, provider, image = indexed_media
    found = search_memories("find the screenshot", palace, include_media=True, n_results=5)
    assert not found.get("error"), found
    assert [r["result_type"] for r in found["results"]] == ["asset", "text"]
    first = found["results"][0]
    assert first["similarity"] == 0.95
    assert first["path"] == str(image)
    assert first["drawer_id"] == "drawer-1"
    assert first["available"] is True
    assert provider.calls == ["search"]
    image.unlink()
    missing = search_memories("find the screenshot", palace, include_media=True)["results"][0]
    assert missing["available"] is False
    assert missing["path"] == str(image)


def test_code_task_and_date_scope(indexed_media):
    palace, provider, _ = indexed_media
    found = search_memories(
        "authentication function", palace, include_media=True, query_task="code"
    )
    assert found["results"][0]["result_type"] == "text"
    assert provider.calls == ["code"]
    recent = search_memories("screenshot", palace, include_media=True, since="2026-10-02")
    assert len(recent["results"]) == 1
    assert recent["results"][0]["result_type"] == "asset"
    assert not search_memories("screenshot", palace, include_media=True, wing="elsewhere")[
        "results"
    ]


def test_default_search_excludes_assets_and_keeps_legacy_hit_schema(indexed_media):
    palace, _, _ = indexed_media
    found = search_memories("authentication", palace)
    assert found["results"]
    assert all("result_type" not in hit and "asset_id" not in hit for hit in found["results"])


def test_media_search_refuses_disabled_vector_index(indexed_media):
    palace, provider, _ = indexed_media
    found = search_memories("screenshot", palace, include_media=True, vector_disabled=True)
    assert "disabled" in found["error"]
    assert provider.calls == []


@pytest.mark.parametrize("query_task", ["search", "code"])
def test_media_query_releases_request_lock_only_during_inference(
    indexed_media, monkeypatch, query_task
):
    palace, provider, _ = indexed_media
    events = []

    class _Hook:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, *_args):
            events.append("exit")

    method = "embed_code_query" if query_task == "code" else "embed_query"
    original = getattr(provider, method)

    def encode(input):
        assert events == ["enter"]
        events.append("inference")
        return original(input)

    monkeypatch.setattr(provider, method, encode)
    embedding.set_embedding_section_hook(lambda: _Hook())
    try:
        found = search_memories("screenshot", palace, include_media=True, query_task=query_task)
    finally:
        embedding.set_embedding_section_hook(None)
    assert not found.get("error"), found
    assert found["results"]
    assert events == ["enter", "inference", "exit"]
