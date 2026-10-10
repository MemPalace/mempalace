"""EmbeddingGemma 2's model-change recovery through repair rebuild-index."""

import json
import os

import pytest

from mempalace.backends.base import EmbedderIdentity, EmbedderIdentityMismatchError
from mempalace.backends.chroma import ChromaBackend
from mempalace.backends._sidecar import write_embedder_sidecar
from mempalace import embedding, palace, repair


class _FakeEmbeddingGemma2:
    dimension = 128
    identity = "embeddinggemma2:google/embeddinggemma-2@test:128:text:retrieval-v1"


@pytest.fixture
def eg2_repair_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "embedding_model": "embeddinggemma2",
                "embeddinggemma2_dimension": 128,
                "embeddinggemma2_modalities": "text",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config_dir))
    for key in (
        "MEMPALACE_EMBEDDING_MODEL",
        "MEMPALACE_EMBEDDINGGEMMA2_DIMENSION",
        "MEMPALACE_EMBEDDINGGEMMA2_MODALITIES",
        "MEMPALACE_EMBEDDINGGEMMA2_REVISION",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _reset_backend_registry():
    from mempalace.backends import reset_backends

    reset_backends()
    yield
    reset_backends()


def test_rebuild_index_migrates_old_identity_and_preserves_drawers(
    tmp_path, monkeypatch, request, eg2_repair_config
):
    from mempalace.config import MempalaceConfig

    assert MempalaceConfig().embedding_model == "embeddinggemma2"
    palace_path = str(tmp_path / "palace")
    os.makedirs(palace_path)
    backend = ChromaBackend()
    request.addfinalizer(backend.close)
    monkeypatch.setattr(ChromaBackend, "_resolve_embedding_function", staticmethod(lambda: None))
    fake_ef = _FakeEmbeddingGemma2()
    monkeypatch.setattr(embedding, "get_embedding_function", lambda **_kwargs: fake_ef)
    from mempalace.backends import embedding_wrapper

    embedded_documents = []

    def _fake_embed_texts(texts, *, query=False, metadatas=None):
        assert query is False
        embedded_documents.append((list(texts), list(metadatas or [])))
        return [[1.0] + [0.0] * 127 for _ in texts]

    monkeypatch.setattr(embedding_wrapper, "_embed_texts", _fake_embed_texts)
    wrapped_for_eg2 = []
    original_wrap = repair._embeddinggemma2_collection

    def _record_wrapper(collection, enabled):
        wrapped_for_eg2.append(enabled)
        return original_wrap(collection, enabled)

    monkeypatch.setattr(repair, "_embeddinggemma2_collection", _record_wrapper)
    monkeypatch.setattr(repair, "check_extraction_safety", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repair, "_post_rebuild_cleanup", lambda *_args, **_kwargs: None)

    client = backend._client(palace_path)
    old_collection = client.create_collection("mempalace_drawers", embedding_function=None)
    original_docs = ["First source body", "Second source body"]
    original_ids = ["drawer-a", "drawer-b"]
    original_metas = [
        {
            "wing": "research",
            "room": "notes",
            "source_file": "/sources/first.txt",
            "chunk_index": 0,
            "title": "First source",
            "filed_at": "2026-10-06T10:00:00",
        },
        {
            "wing": "research",
            "room": "notes",
            "source_file": "/sources/second.txt",
            "chunk_index": 1,
            "title": "Second source",
            "filed_at": "2026-10-06T11:00:00",
        },
    ]
    old_collection.upsert(
        ids=original_ids,
        documents=original_docs,
        metadatas=original_metas,
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    )
    old_identity = EmbedderIdentity(model_name="minilm", dimension=384)
    write_embedder_sidecar(
        os.path.join(palace_path, "mempalace_embedder.json"),
        "mempalace_drawers",
        old_identity,
    )

    # Normal reads correctly reject the old identity. Rebuild must bypass this
    # EF/identity gate only for the snapshot read, then embed with the new model.
    with pytest.raises(EmbedderIdentityMismatchError):
        palace.get_collection(palace_path, create=False)

    repair._rebuild_index_under_lease(
        backend=backend,
        palace_path=palace_path,
        collection_name="mempalace_drawers",
        confirm_truncation_ok=True,
        progress=lambda *_args: None,
    )

    rebuilt = backend._client(palace_path).get_collection(
        "mempalace_drawers", embedding_function=None
    )
    result = rebuilt.get(include=["documents", "metadatas"])
    rows = {
        drawer_id: (doc, meta)
        for drawer_id, doc, meta in zip(result["ids"], result["documents"], result["metadatas"])
    }
    assert set(rows) == set(original_ids)
    for drawer_id, doc, meta in zip(original_ids, original_docs, original_metas):
        got_doc, got_meta = rows[drawer_id]
        assert got_doc == doc
        assert {key: got_meta[key] for key in meta} == meta
    assert wrapped_for_eg2 == [True, True]
    metadata_with_last_modified = [
        {**meta, "last_modified": meta["filed_at"]} for meta in original_metas
    ]
    assert embedded_documents == [
        (original_docs, metadata_with_last_modified),
        (original_docs, metadata_with_last_modified),
    ]

    wrapped = palace.get_collection(palace_path, create=False)
    assert wrapped.get_stored_embedder_identity() == EmbedderIdentity(
        model_name=fake_ef.identity,
        dimension=128,
    )


def test_embeddinggemma2_rebuild_failure_before_swap_preserves_live_collection(
    monkeypatch, eg2_repair_config
):
    fake_ef = _FakeEmbeddingGemma2()
    monkeypatch.setattr(embedding, "get_embedding_function", lambda **_kwargs: fake_ef)

    class _Collection:
        def __init__(self, *, fail_upsert=False, count=1):
            self.fail_upsert = fail_upsert
            self._count = count

        def upsert(self, **_kwargs):
            if self.fail_upsert:
                raise RuntimeError("synthetic staging write failure")

        def count(self):
            return self._count

    temp_collection = _Collection(fail_upsert=True)
    live_collection = _Collection(count=1)

    class _Backend:
        def __init__(self):
            self.deleted = []
            self.live_collection = live_collection

        def create_collection(self, _palace_path, name):
            assert name.endswith("__repair_tmp")
            return temp_collection

        def delete_collection(self, _palace_path, name):
            self.deleted.append(name)
            if name == "drawers":
                self.live_collection = None

    backend = _Backend()
    with pytest.raises(repair.RebuildCollectionError) as excinfo:
        repair._rebuild_collection_via_temp(
            backend,
            "/disposable/palace",
            ["id"],
            ["text"],
            [{"title": "Title"}],
            batch_size=1,
            collection_name="drawers",
            progress=lambda *_args: None,
            embeddinggemma2=True,
        )

    assert excinfo.value.live_replaced is False
    assert backend.deleted == ["drawers__repair_tmp", "drawers__repair_tmp"]
    assert backend.live_collection is live_collection
    assert live_collection.count() == 1


class _FakeMediaEmbeddingGemma2(_FakeEmbeddingGemma2):
    modalities = "all"

    def __init__(self):
        self.calls = []

    def embed_media(self, path, *, media_type):
        self.calls.append((path, media_type))
        return [0.25] + [0.0] * 127


def test_asset_rebuild_uses_native_vectors_and_preserves_source_metadata(tmp_path, monkeypatch):
    from mempalace.palace import ASSETS_COLLECTION_NAME

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"local test reference")
    fake_provider = _FakeMediaEmbeddingGemma2()
    monkeypatch.setattr(repair, "_asset_media_embedder", lambda: fake_provider)
    identity_updates = []
    monkeypatch.setattr(
        repair,
        "_record_rebuilt_embedder_identity",
        lambda collection, palace_path: identity_updates.append((collection, palace_path)),
    )

    class _Collection:
        def __init__(self, name):
            self.name = name
            self.rows = []

        def upsert(self, **kwargs):
            self.rows.append(kwargs)

        def count(self):
            return sum(len(row["ids"]) for row in self.rows)

    class _Backend:
        def __init__(self):
            self.collections = {}
            self.deleted = []

        def create_collection(self, _palace_path, name):
            collection = _Collection(name)
            self.collections[name] = collection
            return collection

        def delete_collection(self, _palace_path, name):
            self.deleted.append(name)
            self.collections.pop(name, None)

    backend = _Backend()
    original_metadata = {
        "asset_id": "media-1",
        "source_path": str(source),
        "source_file": str(source),
        "media_type": "video",
        "title": "Launch recording",
        "embedding_model": "old-model",
        "embedding_identity": "old-model:384",
        "embedding_dimension": 384,
        "embedding_modalities": "text",
    }

    rebuilt = repair._rebuild_collection_via_temp(
        backend,
        str(tmp_path / "palace"),
        ["media-id"],
        ["Video asset: Launch recording"],
        [original_metadata],
        batch_size=1,
        collection_name=ASSETS_COLLECTION_NAME,
        progress=lambda *_args: None,
        embeddinggemma2=True,
    )

    assert rebuilt == 1
    assert fake_provider.calls == [(str(source), "video"), (str(source), "video")]
    # Asset descriptors remain searchable text, while their vectors always
    # come from the media file and their identity metadata is refreshed.
    live_upsert = backend.collections[ASSETS_COLLECTION_NAME].rows[0]
    assert live_upsert["documents"] == ["Video asset: Launch recording"]
    assert live_upsert["embeddings"] == [[0.25] + [0.0] * 127]
    assert live_upsert["ids"] == ["media-id"]
    got_metadata = live_upsert["metadatas"][0]
    for key, value in original_metadata.items():
        if key not in {
            "embedding_model",
            "embedding_identity",
            "embedding_dimension",
            "embedding_modalities",
        }:
            assert got_metadata[key] == value
    assert got_metadata["embedding_model"] == fake_provider.identity
    assert got_metadata["embedding_identity"] == fake_provider.identity
    assert got_metadata["embedding_dimension"] == 128
    assert got_metadata["embedding_modalities"] == "all"
    assert identity_updates == [
        (backend.collections[ASSETS_COLLECTION_NAME], str(tmp_path / "palace"))
    ]


def test_missing_asset_aborts_staging_before_live_replacement(tmp_path, monkeypatch):
    from mempalace.palace import ASSETS_COLLECTION_NAME

    fake_provider = _FakeMediaEmbeddingGemma2()
    monkeypatch.setattr(repair, "_asset_media_embedder", lambda: fake_provider)

    class _Collection:
        def upsert(self, **_kwargs):
            raise AssertionError("missing asset must fail before a staging write")

        def count(self):
            return 1

    class _Backend:
        def __init__(self):
            self.deleted = []
            self.live = object()

        def create_collection(self, _palace_path, name):
            assert name.endswith("__repair_tmp")
            return _Collection()

        def delete_collection(self, _palace_path, name):
            self.deleted.append(name)
            assert name.endswith("__repair_tmp")

    backend = _Backend()
    missing = tmp_path / "missing.png"
    with pytest.raises(repair.RebuildCollectionError) as excinfo:
        repair._rebuild_collection_via_temp(
            backend,
            str(tmp_path / "palace"),
            ["media-id"],
            ["Image asset: missing"],
            [{"source_path": str(missing), "media_type": "image"}],
            batch_size=1,
            collection_name=ASSETS_COLLECTION_NAME,
            progress=lambda *_args: None,
            embeddinggemma2=True,
        )

    assert "restore the file before rebuilding" in str(excinfo.value)
    assert excinfo.value.live_replaced is False
    assert backend.deleted == [f"{ASSETS_COLLECTION_NAME}__repair_tmp"] * 2
    assert fake_provider.calls == []


@pytest.mark.parametrize("collection_name", ["mempalace_drawers", "mempalace_closets"])
def test_sqlite_rebuild_text_uses_metadata_and_records_identity(
    monkeypatch, tmp_path, eg2_repair_config, collection_name
):
    calls = []

    from mempalace.backends import embedding_wrapper

    def embed_documents(texts, *, metadatas=None):
        calls.append((list(texts), list(metadatas or [])))
        return [[1.0] + [0.0] * 127 for _ in texts]

    fake_provider = _FakeEmbeddingGemma2()
    monkeypatch.setattr(embedding, "get_embedding_function", lambda **_kwargs: fake_provider)
    monkeypatch.setattr(embedding_wrapper, "_embed_texts", embed_documents)
    metadata = {"title": "Original title", "wing": "research", "filed_at": "2026-10-06"}
    monkeypatch.setattr(
        repair,
        "extract_via_sqlite",
        lambda *_args: iter([("drawer-id", "Verbatim source body", metadata)]),
    )

    class _Collection:
        def __init__(self):
            self.rows = []
            self.identity = None

        def upsert(self, **kwargs):
            self.rows.append(kwargs)

        def count(self):
            return sum(len(row["ids"]) for row in self.rows)

        def set_embedder_identity(self, identity):
            assert self.count() == 1
            self.identity = identity

    collection = _Collection()
    backend = type("Backend", (), {"create_collection": lambda *_args: collection})()
    dest = str(tmp_path / "dest")
    assert (
        repair._rebuild_one_collection(
            backend=backend,
            source_palace=str(tmp_path / "source"),
            dest_palace=dest,
            collection_name=collection_name,
            batch_size=10,
            archive_path=None,
            counts_so_far={},
        )
        == 1
    )

    row = collection.rows[0]
    assert row["documents"] == ["Verbatim source body"]
    assert row["embeddings"] == [[1.0] + [0.0] * 127]
    assert calls[0][0] == ["Verbatim source body"]
    assert calls[0][1][0]["title"] == "Original title"
    assert collection.identity == EmbedderIdentity(model_name=fake_provider.identity, dimension=128)


def test_sqlite_rebuild_asset_collection_embeds_native_media(monkeypatch, tmp_path):
    from mempalace.palace import ASSETS_COLLECTION_NAME

    source = tmp_path / "photo.jpg"
    source.write_bytes(b"test image")
    fake_provider = _FakeMediaEmbeddingGemma2()
    monkeypatch.setattr(repair, "_asset_media_embedder", lambda: fake_provider)
    identity_updates = []
    monkeypatch.setattr(
        repair,
        "_record_rebuilt_embedder_identity",
        lambda collection, palace_path: identity_updates.append((collection, palace_path)),
    )
    monkeypatch.setattr(
        repair,
        "extract_via_sqlite",
        lambda *_args: iter(
            [
                (
                    "asset-id",
                    "Image asset: Family photo",
                    {"source_path": str(source), "media_type": "image", "title": "Family photo"},
                )
            ]
        ),
    )

    class _Collection:
        def __init__(self):
            self.rows = []

        def upsert(self, **kwargs):
            self.rows.append(kwargs)

        def count(self):
            return sum(len(row["ids"]) for row in self.rows)

    class _Backend:
        def __init__(self):
            self.collection = _Collection()

        def create_collection(self, *_args):
            return self.collection

    backend = _Backend()
    count = repair._rebuild_one_collection(
        backend=backend,
        source_palace=str(tmp_path / "source"),
        dest_palace=str(tmp_path / "dest"),
        collection_name=ASSETS_COLLECTION_NAME,
        batch_size=10,
        archive_path=None,
        counts_so_far={},
    )
    assert count == 1
    assert fake_provider.calls == [(str(source), "image")]
    row = backend.collection.rows[0]
    assert row["ids"] == ["asset-id"]
    assert row["documents"] == ["Image asset: Family photo"]
    assert row["embeddings"] == [[0.25] + [0.0] * 127]
    assert row["metadatas"][0]["title"] == "Family photo"
    assert row["metadatas"][0]["embedding_identity"] == fake_provider.identity
    assert identity_updates == [(backend.collection, str(tmp_path / "dest"))]


def test_recoverable_collections_include_fixed_assets_collection(tmp_path, monkeypatch):
    from mempalace.palace import ASSETS_COLLECTION_NAME

    monkeypatch.setattr(repair, "sqlite_drawer_count", lambda *_args: 1)
    assert ASSETS_COLLECTION_NAME in repair._recoverable_collections(str(tmp_path))


def test_missing_sqlite_asset_refuses_in_place_archive(tmp_path, monkeypatch):
    from mempalace.palace import ASSETS_COLLECTION_NAME

    palace_path = tmp_path / "palace"
    palace_path.mkdir()
    missing_path = tmp_path / "not-present.wav"
    monkeypatch.setattr(
        repair,
        "extract_via_sqlite",
        lambda *_args: iter(
            [("asset-id", "Audio asset: Interview", {"source_path": str(missing_path)})]
        ),
    )
    monkeypatch.setattr(repair, "sqlite_drawer_count", lambda *_args: 1)

    result = repair._rebuild_from_sqlite_locked(
        source_palace=str(palace_path),
        dest_palace=str(palace_path),
        in_place=True,
        batch_size=10,
    )

    assert result == {}
    assert palace_path.is_dir()
    assert not list(tmp_path.glob("palace.pre-rebuild-*"))
    assert ASSETS_COLLECTION_NAME in repair._recoverable_collections(str(palace_path))
