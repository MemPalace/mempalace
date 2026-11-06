"""Opt-in real-model checks; ordinary test runs never download model weights."""

import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MEMPALACE_RUN_EG2_INTEGRATION") != "1",
    reason="set MEMPALACE_RUN_EG2_INTEGRATION=1 to load the real EmbeddingGemma 2 model",
)


def test_real_text_code_and_sandbox_mining(tmp_path, monkeypatch, record_property):
    import numpy as np

    from mempalace.embedding import get_embedding_function
    from mempalace.miner import mine
    from mempalace.palace import get_collection
    from mempalace.searcher import search_memories

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "embeddinggemma2")
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_DIMENSION", "768")
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_MODALITIES", "text")
    ef = get_embedding_function()
    docs = [
        "Bearer tokens authenticate API requests before protected routes run.",
        "The garden contains red roses and tall sunflowers.",
        'def authenticate(request):\n    return request.headers.get("Authorization", "").startswith("Bearer ")',
        'def grow_flowers():\n    return ["rose", "sunflower"]',
    ]
    start = time.perf_counter()
    vectors = np.asarray(ef.embed_documents(docs), dtype=np.float32)
    record_property("first_embedding_seconds", time.perf_counter() - start)
    record_property("device", ef.effective_device)
    query = np.asarray(ef.embed_query(["how are API requests authenticated?"]))
    code_query = np.asarray(ef.embed_code_query(["function that checks bearer token headers"]))
    repeated = np.asarray(ef.embed_query(["how are API requests authenticated?"]))
    assert vectors.shape == (4, 768)
    for arr in (vectors, query, code_query):
        assert np.isfinite(arr).all()
        assert np.allclose(np.linalg.norm(arr, axis=1), 1, atol=1e-5)
    assert np.allclose(query, repeated, atol=1e-5)
    assert (query @ vectors[0]).item() > (query @ vectors[1]).item()
    assert (code_query @ vectors[2]).item() > (code_query @ vectors[3]).item()
    record_property("text_scores", (query @ vectors.T).tolist())
    record_property("code_scores", (code_query @ vectors.T).tolist())

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    contents = {
        "authentication.md": "# API authentication\n" + (docs[0] + "\n") * 6,
        "flowers.md": "# Growing flowers\n" + (docs[1] + "\n") * 6,
        "auth.py": "# Validate API authentication bearer tokens.\n" + docs[2] + "\n",
    }
    for filename, content in contents.items():
        (corpus / filename).write_text(content, encoding="utf-8")
    palace = str(tmp_path / "palace")
    mine(str(corpus), palace, wing_override="eg2_demo")
    collection = get_collection(palace, create=False)
    stored = collection.get(include=["documents", "metadatas", "embeddings"])
    assert collection.count() >= 2
    assert all(len(v) == 768 for v in stored.embeddings)
    assert any("Bearer tokens authenticate" in doc for doc in stored.documents)
    found = search_memories("how are API requests authenticated?", palace, n_results=3)
    assert not found.get("error"), found
    hits = found["results"]
    assert hits
    assert Path(hits[0]["source_path"]).name in {"authentication.md", "auth.py"}
    record_property("sandbox_results", [(hit["source_file"], hit["similarity"]) for hit in hits])


@pytest.mark.skipif(
    os.environ.get("MEMPALACE_RUN_EG2_MULTIMODAL") != "1",
    reason="set MEMPALACE_RUN_EG2_MULTIMODAL=1 for native media inference",
)
def test_real_image_and_mixed_retrieval(tmp_path, monkeypatch, record_property):
    import numpy as np
    from PIL import Image, ImageDraw

    from mempalace.cli import mine_source_adapter
    from mempalace.embedding import get_embedding_function
    from mempalace.palace import get_collection
    from mempalace.searcher import search_memories

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "embeddinggemma2")
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_DIMENSION", "768")
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_MODALITIES", "all")
    corpus = tmp_path / "media"
    corpus.mkdir()
    circle = Image.new("RGB", (512, 512), "white")
    ImageDraw.Draw(circle).ellipse((70, 70, 442, 442), fill="blue")
    circle.save(corpus / "blue-circle.png")
    triangle = Image.new("RGB", (512, 512), "white")
    ImageDraw.Draw(triangle).polygon([(256, 50), (460, 460), (52, 460)], fill="orange")
    triangle.save(corpus / "orange-triangle.png")
    ef = get_embedding_function()
    start = time.perf_counter()
    vectors = np.asarray(
        [ef.embed_image(corpus / name) for name in ("blue-circle.png", "orange-triangle.png")]
    )
    record_property("image_embedding_seconds", time.perf_counter() - start)
    assert vectors.shape == (2, 768)
    assert np.isfinite(vectors).all()
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5)
    query = np.asarray(ef.embed_query(["a large blue circle on a white background"]))
    scores = (query @ vectors.T)[0]
    record_property("image_scores", scores.tolist())
    assert scores[0] > scores[1]
    for extension in ("jpg", "jpeg", "webp"):
        path = corpus / f"circle.{extension}"
        circle.save(path)
        vector = np.asarray(ef.embed_image(path))
        assert np.isfinite(vector).all()
        assert (vector @ vectors[0]).item() > 0.99
        path.unlink()  # Leave the ingestion fixture as the two ranked assets.
    palace = str(tmp_path / "palace")
    drawers = get_collection(palace)
    drawers.upsert(
        documents=["The blue circle diagram illustrates a circular boundary."],
        ids=["circle-note"],
        metadatas=[{"wing": "demo", "room": "general"}],
    )
    assert (
        mine_source_adapter(
            source_name="media",
            source_path=str(corpus),
            palace_path=palace,
            project="demo",
            related_drawer_id="circle-note",
        )
        == 2
    )
    found = search_memories(
        "a large blue circle on a white background", palace, include_media=True, n_results=3
    )
    assert not found.get("error"), found
    assets = [hit for hit in found["results"] if hit["result_type"] == "asset"]
    assert assets[0]["source_file"] == "blue-circle.png"
    assert assets[0]["drawer_id"] == "circle-note"
    assert any(hit["result_type"] == "text" for hit in found["results"])
    record_property(
        "mixed_results", [(hit["source_file"], hit["similarity"]) for hit in found["results"]]
    )


@pytest.mark.skipif(
    os.environ.get("MEMPALACE_RUN_EG2_MULTIMODAL") != "1",
    reason="set MEMPALACE_RUN_EG2_MULTIMODAL=1 for native media inference",
)
def test_real_audio_video_and_local_codecs(tmp_path, monkeypatch, record_property):
    import shutil
    import subprocess
    import sys

    import numpy as np
    from PIL import Image, ImageDraw

    from mempalace.embedding import get_embedding_function

    ffmpeg = shutil.which("ffmpeg")
    if sys.platform != "darwin" or not ffmpeg or not Path("/usr/bin/say").is_file():
        pytest.skip("synthetic speech/video fixtures require macOS say and FFmpeg")
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "embeddinggemma2")
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_DIMENSION", "768")
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_MODALITIES", "all")
    speech = [
        "API requests are authenticated using bearer tokens. Read the authorization header and reject missing or invalid tokens before calling protected routes.",
        "Plant roses and sunflowers in the garden. Water the flowers every morning and prepare the soil before planting new seeds.",
    ]
    for index, text in enumerate(speech):
        subprocess.run(
            ["/usr/bin/say", "-v", "Samantha", "-o", str(tmp_path / f"speech-{index}.aiff"), text],
            check=True,
        )
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(tmp_path / f"speech-{index}.aiff"),
                "-ar",
                "22050",
                "-ac",
                "2",
                str(tmp_path / f"speech-{index}.wav"),
            ],
            check=True,
        )
    for index, shape in enumerate(("circle", "triangle")):
        image = Image.new("RGB", (512, 512), "white")
        draw = ImageDraw.Draw(image)
        if shape == "circle":
            draw.ellipse((70, 70, 442, 442), fill="blue")
        else:
            draw.polygon([(256, 50), (460, 460), (52, 460)], fill="orange")
        image.save(tmp_path / f"shape-{index}.png")
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-loop",
                "1",
                "-i",
                str(tmp_path / f"shape-{index}.png"),
                "-t",
                "3",
                "-r",
                "2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(tmp_path / f"shape-{index}.mp4"),
            ],
            check=True,
        )

    ef = get_embedding_function()
    for kind, filenames, query in (
        (
            "audio",
            ("speech-0.wav", "speech-1.wav"),
            "A person explains bearer token authentication using the authorization header",
        ),
        (
            "video",
            ("shape-0.mp4", "shape-1.mp4"),
            "A video showing a large blue circle on a white background",
        ),
    ):
        start = time.perf_counter()
        vectors = np.asarray([ef.embed_media(tmp_path / name, kind) for name in filenames])
        record_property(f"{kind}_embedding_seconds", time.perf_counter() - start)
        assert vectors.shape == (2, 768)
        assert np.isfinite(vectors).all()
        assert np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5)
        query_vector = np.asarray(ef.embed_query([query]))
        scores = (query_vector @ vectors.T)[0]
        record_property(f"{kind}_scores", scores.tolist())
        assert scores[0] > scores[1]

    # Exercise compressed audio and alternate containers through the provider,
    # rather than merely proving the external fixture encoder accepts them.
    audio_baseline = np.asarray(ef.embed_audio(tmp_path / "speech-0.wav"))
    for extension in ("mp3", "m4a", "flac"):
        target = tmp_path / f"speech-0.{extension}"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(tmp_path / "speech-0.wav"),
                str(target),
            ],
            check=True,
        )
        vector = np.asarray(ef.embed_audio(target))
        assert np.isfinite(vector).all()
        assert (vector @ audio_baseline).item() > 0.97
    for extension in ("mov", "m4v", "webm"):
        target = tmp_path / f"shape-0.{extension}"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(tmp_path / "shape-0.mp4"),
                str(target),
            ],
            check=True,
        )
        vector = np.asarray(ef.embed_video(target))
        assert vector.shape == (768,)
        assert np.isfinite(vector).all()
        assert np.isclose(np.linalg.norm(vector), 1, atol=1e-5)
    record_property("media_device", ef.effective_device)
