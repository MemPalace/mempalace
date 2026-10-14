from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from dataclasses import replace
import argparse
import contextlib
import sys

from mempalace.media import MediaAssetError, asset_from_path, media_asset_exists, store_media_asset
from mempalace.sources.media import MediaAdapter
from mempalace.sources.base import SourceRef


class _FakeCollection:
    def __init__(self, existing=None):
        self.upserts = []
        self.existing = existing or {}

    def get(self, *, ids, include):
        if ids[0] in self.existing:
            return SimpleNamespace(ids=list(ids), metadatas=[self.existing[ids[0]]])
        return SimpleNamespace(ids=[], metadatas=[])

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)


class _FakeEmbedder:
    dimension = 768
    modalities = "text+vision+audio"
    identity = "embeddinggemma2:test:768:all"

    def __init__(self):
        self.calls = []

    def embed_media(self, path, media_type):
        self.calls.append((path, media_type))
        return np.ones(768, dtype=np.float32)


def test_media_adapter_discovers_metadata_only_and_skips_hidden_and_build_dirs(tmp_path):
    root = tmp_path / "project"
    (root / "assets").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "node_modules").mkdir()
    image = root / "assets" / "cover.png"
    audio = root / "assets" / "sample.wav"
    video = root / "assets" / "clip.mp4"
    image.write_bytes(b"image bytes are never copied")
    audio.write_bytes(b"audio bytes are never copied")
    video.write_bytes(b"video bytes are never copied")
    (root / ".hidden" / "secret.jpg").write_bytes(b"hidden")
    (root / "node_modules" / "ignored.png").write_bytes(b"ignored")

    assets = list(MediaAdapter().ingest(source=SourceRef(local_path=str(root)), palace=None))

    assert [asset.title for asset in assets] == ["clip.mp4", "cover.png", "sample.wav"]
    assert [asset.media_type for asset in assets] == ["video", "image", "audio"]
    assert all(asset.project == "project" for asset in assets)
    assert assets[1].source_file == str(image.resolve())
    assert assets[1].descriptor() == "Image asset: cover.png"
    assert "image bytes" not in assets[1].descriptor()
    assert assets[1].metadata()["wing"] == "project"
    assert assets[1].metadata()["room"] == "media"
    assert (
        assets[1].asset_id
        == asset_from_path(image, source_root=root, media_type="image", project="project").asset_id
    )


def test_media_adapter_respects_project_and_related_drawer_options(tmp_path):
    path = tmp_path / "image.webp"
    path.write_bytes(b"image")

    (asset,) = list(
        MediaAdapter().ingest(
            source=SourceRef(
                local_path=str(path),
                options={"project": "release-7", "related_drawer_id": "drawer-123"},
            ),
            palace=None,
        )
    )

    assert asset.project == "release-7"
    assert asset.related_drawer_id == "drawer-123"
    assert asset.metadata()["related_drawer_id"] == "drawer-123"
    assert all(isinstance(value, (str, int, float, bool)) for value in asset.metadata().values())


def test_audio_asset_reads_duration_from_soundfile_header(tmp_path, monkeypatch):
    assert "duration" in MediaAdapter().describe_schema().fields
    from mempalace.media import asset_from_path

    path = tmp_path / "interview.wav"
    path.write_bytes(b"audio header fixture")
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(info=lambda value: calls.append(value) or SimpleNamespace(duration=42.25)),
    )

    asset = asset_from_path(path, source_root=tmp_path, media_type="audio", project="demo")

    assert asset.duration == 42.25
    assert asset.metadata()["duration"] == 42.25
    assert calls == [str(path.resolve())]


def test_video_discovery_does_not_probe_duration(tmp_path, monkeypatch):
    from mempalace.media import asset_from_path

    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video reference")
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(info=lambda _value: pytest.fail("video discovery must not probe audio")),
    )

    asset = asset_from_path(path, source_root=tmp_path, media_type="video", project="demo")

    assert asset.duration is None
    assert "duration" not in asset.metadata()


@pytest.mark.parametrize(
    "probe",
    [
        None,
        RuntimeError("unsupported format"),
        SimpleNamespace(duration=float("nan")),
        SimpleNamespace(duration=float("inf")),
        SimpleNamespace(duration=-1),
        SimpleNamespace(duration=None),
    ],
)
def test_unavailable_or_invalid_audio_duration_is_omitted(tmp_path, monkeypatch, probe):
    from mempalace.media import asset_from_path

    path = tmp_path / "unknown.m4a"
    path.write_bytes(b"audio bytes")
    if probe is None:
        monkeypatch.setitem(sys.modules, "soundfile", None)
    elif isinstance(probe, Exception):
        monkeypatch.setitem(
            sys.modules,
            "soundfile",
            SimpleNamespace(info=lambda _path: (_ for _ in ()).throw(probe)),
        )
    else:
        monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(info=lambda _path: probe))

    asset = asset_from_path(path, source_root=tmp_path, media_type="audio", project="demo")

    assert asset.duration is None
    assert "duration" not in asset.metadata()


def test_media_adapter_does_not_follow_symlink_files_or_roots(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    link = root / "linked.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert list(MediaAdapter().ingest(source=SourceRef(local_path=str(root)), palace=None)) == []
    with pytest.raises(Exception, match="symbolic link"):
        list(MediaAdapter().ingest(source=SourceRef(local_path=str(link)), palace=None))


def test_store_media_asset_upserts_normalized_768d_vector_with_flat_metadata(tmp_path, monkeypatch):
    from mempalace import config, embedding, palace

    path = tmp_path / "image.png"
    path.write_bytes(b"image")
    asset = asset_from_path(path, source_root=tmp_path, media_type="image", project="demo")
    embedder = _FakeEmbedder()
    collection = _FakeCollection()
    monkeypatch.setattr(
        config,
        "MempalaceConfig",
        lambda *args, **kwargs: SimpleNamespace(embedding_model="embeddinggemma2"),
    )
    monkeypatch.setattr(embedding, "get_embedding_function", lambda **kwargs: embedder)
    monkeypatch.setattr(
        palace,
        "get_collection",
        lambda palace_path, collection_name: collection,
    )

    assert store_media_asset(asset, str(tmp_path / "palace")) == 1
    assert store_media_asset(asset, str(tmp_path / "palace")) == 1

    assert len(collection.upserts) == 2
    first, second = collection.upserts
    assert first["ids"] == [asset.asset_id] == second["ids"]
    assert first["documents"] == ["Image asset: image.png"]
    vector = first["embeddings"][0]
    assert len(vector) == 768
    assert np.linalg.norm(vector) == pytest.approx(1.0)
    metadata = first["metadatas"][0]
    assert metadata["source_path"] == str(path.resolve())
    assert metadata["source_file"] == str(path.resolve())
    assert metadata["embedding_identity"] == embedder.identity
    assert metadata["embedding_dimension"] == 768
    assert metadata["filed_at"] == metadata["indexed_at"]
    assert metadata["last_modified"] == metadata["indexed_at"]
    assert metadata["wing"] == "demo"
    assert metadata["room"] == "media"
    assert all(isinstance(value, (str, int, float, bool)) for value in metadata.values())
    assert embedder.calls == [(str(path), "image"), (str(path), "image")]


def test_store_media_asset_fails_before_storage_for_wrong_provider_or_modality(
    tmp_path, monkeypatch
):
    from mempalace import config, embedding, palace

    path = tmp_path / "audio.wav"
    path.write_bytes(b"audio")
    asset = asset_from_path(path, source_root=tmp_path, media_type="audio", project="demo")
    collection = _FakeCollection()
    monkeypatch.setattr(
        config,
        "MempalaceConfig",
        lambda *args, **kwargs: SimpleNamespace(embedding_model="minilm"),
    )
    monkeypatch.setattr(embedding, "get_embedding_function", lambda **kwargs: pytest.fail("loaded"))
    monkeypatch.setattr(palace, "get_collection", lambda *args, **kwargs: collection)
    with pytest.raises(MediaAssetError, match="requires embedding_model='embeddinggemma2'"):
        store_media_asset(asset, str(tmp_path / "palace"))
    assert collection.upserts == []

    embedder = _FakeEmbedder()
    embedder.modalities = "text+vision"
    monkeypatch.setattr(
        config,
        "MempalaceConfig",
        lambda *args, **kwargs: SimpleNamespace(embedding_model="embeddinggemma2"),
    )
    monkeypatch.setattr(embedding, "get_embedding_function", lambda **kwargs: embedder)
    with pytest.raises(MediaAssetError, match="enable the audio encoder"):
        store_media_asset(asset, str(tmp_path / "palace"))
    assert collection.upserts == []


def test_media_storage_honors_configured_matryoshka_dimension(tmp_path, monkeypatch):
    from mempalace import config, embedding, palace

    path = tmp_path / "image.png"
    path.write_bytes(b"image")
    asset = asset_from_path(path, source_root=tmp_path, media_type="image", project="demo")
    embedder = _FakeEmbedder()
    embedder.dimension = 512
    embedder.embed_media = lambda path, media_type: np.ones(512, dtype=np.float32)
    collection = _FakeCollection()
    monkeypatch.setattr(
        config,
        "MempalaceConfig",
        lambda *args, **kwargs: SimpleNamespace(embedding_model="embeddinggemma2"),
    )
    monkeypatch.setattr(embedding, "get_embedding_function", lambda **kwargs: embedder)
    monkeypatch.setattr(
        palace,
        "get_collection",
        lambda palace_path, collection_name: collection,
    )

    assert store_media_asset(asset, str(tmp_path / "palace")) == 1
    assert len(collection.upserts[0]["embeddings"][0]) == 512
    assert collection.upserts[0]["metadatas"][0]["embedding_dimension"] == 512


def test_media_reingest_preserves_original_filed_at(tmp_path, monkeypatch):
    from mempalace import config, embedding, palace

    path = tmp_path / "image.png"
    path.write_bytes(b"image")
    asset = asset_from_path(path, source_root=tmp_path, media_type="image", project="demo")
    embedder = _FakeEmbedder()
    collection = _FakeCollection(
        existing={asset.asset_id: {"filed_at": "2024-01-02T03:04:05+00:00"}}
    )
    monkeypatch.setattr(
        config,
        "MempalaceConfig",
        lambda *args, **kwargs: SimpleNamespace(embedding_model="embeddinggemma2"),
    )
    monkeypatch.setattr(embedding, "get_embedding_function", lambda **kwargs: embedder)
    monkeypatch.setattr(
        palace,
        "get_collection",
        lambda palace_path, collection_name: collection,
    )

    store_media_asset(asset, str(tmp_path / "palace"))

    metadata = collection.upserts[0]["metadatas"][0]
    assert metadata["filed_at"] == "2024-01-02T03:04:05+00:00"
    assert metadata["indexed_at"] != metadata["filed_at"]
    assert metadata["last_modified"] == metadata["indexed_at"]


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"asset_id": ""}, "asset_id must be a non-empty string"),
        ({"source_file": "relative.png"}, "source_file must be an absolute local path"),
        ({"segment_start": float("nan")}, "segment_start must be a finite"),
        ({"segment_start": -1.0}, "segment_start must be a finite"),
        ({"segment_start": 3.0, "segment_end": 2.0}, "segment_end must be greater"),
        ({"duration": float("nan")}, "duration must be a finite"),
        ({"duration": -1.0}, "duration must be a finite"),
        ({"related_drawer_id": ""}, "related_drawer_id must be a non-empty"),
    ],
)
def test_media_asset_rejects_malformed_fields_and_segments(tmp_path, updates, message):
    path = tmp_path / "asset.png"
    path.write_bytes(b"image")
    asset = asset_from_path(path, source_root=tmp_path, media_type="image", project="demo")

    with pytest.raises(MediaAssetError, match=message):
        replace(asset, **updates)


def test_related_drawer_is_checked_before_media_embedding(tmp_path, monkeypatch):
    from mempalace import config, embedding, palace

    path = tmp_path / "asset.png"
    path.write_bytes(b"image")
    asset = asset_from_path(
        path,
        source_root=tmp_path,
        media_type="image",
        project="demo",
        related_drawer_id="drawer-404",
    )
    embedder = _FakeEmbedder()
    assets = _FakeCollection()
    drawers = _FakeCollection()
    monkeypatch.setattr(
        config,
        "MempalaceConfig",
        lambda *args, **kwargs: SimpleNamespace(embedding_model="embeddinggemma2"),
    )
    monkeypatch.setattr(embedding, "get_embedding_function", lambda **kwargs: embedder)
    monkeypatch.setattr(
        palace,
        "get_collection",
        lambda palace_path, collection_name=None: assets if collection_name else drawers,
    )

    with pytest.raises(MediaAssetError, match="does not exist in this palace"):
        store_media_asset(asset, str(tmp_path / "palace"))
    assert embedder.calls == []
    assert assets.upserts == []

    drawers = _FakeCollection(existing={"drawer-404": {}})
    assert store_media_asset(asset, str(tmp_path / "palace"), drawer_collection=drawers) == 1
    assert assets.upserts[0]["metadatas"][0]["related_drawer_id"] == "drawer-404"


def test_media_reference_survives_missing_file_check(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"image")
    asset = asset_from_path(path, source_root=tmp_path, media_type="image", project="demo")
    assert media_asset_exists(asset)
    path.unlink()
    assert not media_asset_exists(asset)
    assert asset.metadata()["file_exists"] is True


def test_media_builtin_registration_and_fixed_collection_allowlist():
    from mempalace.palace import ASSETS_COLLECTION_NAME, _allowed_wrapper_collection_names
    from mempalace.sources import get_adapter

    assert isinstance(get_adapter("media"), MediaAdapter)
    assert ASSETS_COLLECTION_NAME in _allowed_wrapper_collection_names()


def test_media_adapter_runner_dry_run_counts_assets_without_embedding_or_storage(
    tmp_path, monkeypatch
):
    from mempalace import cli, embedding, palace

    (tmp_path / "asset.jpg").write_bytes(b"image")
    monkeypatch.setattr(
        cli,
        "MempalaceConfig",
        lambda *args, **kwargs: SimpleNamespace(palace_path=str(tmp_path / "palace")),
    )
    monkeypatch.setattr(
        embedding,
        "get_embedding_function",
        lambda **kwargs: pytest.fail("dry run must not load an embedder"),
    )
    monkeypatch.setattr(
        palace,
        "get_collection",
        lambda *args, **kwargs: pytest.fail("dry run must not open a collection"),
    )

    count = cli.mine_source_adapter(
        source_name="media",
        source_path=str(tmp_path),
        palace_path=str(tmp_path / "palace"),
        dry_run=True,
        project="demo",
        related_drawer_id="drawer-1",
    )

    assert count == 1


def test_cli_reports_global_provider_error_instead_of_zero_success(tmp_path, monkeypatch, capsys):
    from mempalace import cli, config, knowledge_graph, palace

    (tmp_path / "asset.png").write_bytes(b"image")

    class _Config:
        palace_path = str(tmp_path / "palace")
        embedding_model = "minilm"

        def __init__(self, palace_path=None):
            self.palace_path = palace_path or self.__class__.palace_path

    class _KnowledgeGraph:
        def __init__(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli, "MempalaceConfig", _Config)
    monkeypatch.setattr(config, "MempalaceConfig", _Config)
    monkeypatch.setattr(
        cli, "_resolve_cli_write_routing_or_exit", lambda *a, **k: SimpleNamespace(use_daemon=False)
    )
    monkeypatch.setattr(palace, "mine_palace_lock", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(palace, "get_collection", lambda *_a, **_k: _FakeCollection())
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", _KnowledgeGraph)
    args = argparse.Namespace(
        dir=str(tmp_path),
        palace=None,
        source="media",
        include_ignored=[],
        mode=None,
        wing=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
        extract="exchange",
        no_gitignore=False,
    )

    with pytest.raises(SystemExit) as exc:
        cli.cmd_mine(args)

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "embedding_model='embeddinggemma2'" in captured.err
    assert "0 media asset(s) written" not in captured.out


def test_store_media_asset_round_trips_through_sqlite_exact(tmp_path, monkeypatch):
    import contextlib

    from mempalace import config, embedding, palace
    from mempalace.backends import get_backend

    path = tmp_path / "asset.png"
    path.write_bytes(b"image")
    asset = asset_from_path(path, source_root=tmp_path, media_type="image", project="demo")
    embedder = _FakeEmbedder()
    monkeypatch.setattr(
        config,
        "MempalaceConfig",
        lambda *args, **kwargs: SimpleNamespace(embedding_model="embeddinggemma2"),
    )
    monkeypatch.setattr(embedding, "get_embedding_function", lambda **kwargs: embedder)
    monkeypatch.setattr(
        palace, "get_backend_for_palace", lambda *args, **kwargs: get_backend("sqlite_exact")
    )
    monkeypatch.setattr(palace, "mine_palace_lock", lambda *_: contextlib.nullcontext())

    palace_path = str(tmp_path / "palace")
    assert store_media_asset(asset, palace_path) == 1
    collection = palace.get_collection(
        palace_path,
        collection_name=palace.ASSETS_COLLECTION_NAME,
        backend="sqlite_exact",
    )
    result = collection.get(ids=[asset.asset_id], include=["embeddings", "metadatas", "documents"])

    assert result.ids == [asset.asset_id]
    assert result.metadatas[0]["media_type"] == "image"
    assert result.metadatas[0]["embedding_dimension"] == 768
    assert len(result.embeddings[0]) == 768


@pytest.mark.parametrize("collection_name", ["mempalace_assets", "mempalace_drawers"])
def test_populated_collection_without_recorded_embedding_identity_fail_closed(
    tmp_path, monkeypatch, collection_name
):
    import contextlib

    from mempalace import palace
    from mempalace.backends import PalaceRef
    from mempalace.backends.sqlite_exact import SQLiteExactBackend
    from mempalace.backends.base import EmbedderIdentityMismatchError

    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "embeddinggemma2")
    monkeypatch.setattr(palace, "mine_palace_lock", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(
        "mempalace.embedding.get_embedding_function", lambda **kwargs: _FakeEmbedder()
    )
    monkeypatch.setattr("mempalace.embedding.current_model_name", lambda: _FakeEmbedder.identity)
    palace._VALIDATED_IDENTITY.clear()
    path = str(tmp_path / "palace")
    raw = SQLiteExactBackend().get_collection(
        palace=PalaceRef(id=path, local_path=path),
        collection_name=collection_name,
        create=True,
    )
    raw.upsert(
        ids=["legacy-asset"],
        documents=["Image asset: old.png"],
        metadatas=[{"media_type": "image"}],
        embeddings=[[1.0] + [0.0] * 767],
    )
    palace._VALIDATED_IDENTITY.clear()

    with pytest.raises(EmbedderIdentityMismatchError, match="no recorded embedding identity"):
        palace.get_collection(
            path,
            collection_name=collection_name,
            backend="sqlite_exact",
            create=False,
        )
