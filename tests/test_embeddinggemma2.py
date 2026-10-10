"""Offline tests for the standalone EmbeddingGemma 2 provider."""

import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from mempalace.embeddinggemma2 import (
    DEFAULT_REVISION,
    MODEL_ID,
    EmbeddingGemma2EmbeddingFunction,
    EmbeddingOutputError,
    _config_kwargs,
)


class _FakeModel:
    def __init__(self, dimension=768, behavior=None):
        self.dimension = dimension
        self.behavior = behavior
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        if self.behavior:
            result = self.behavior(texts, kwargs)
            if result is not None:
                return result
        return np.ones((len(texts), self.dimension), dtype=np.float32)


def _ready_provider(monkeypatch, model=None):
    provider = EmbeddingGemma2EmbeddingFunction(device="cpu")
    fake = model or _FakeModel()
    provider._model = fake
    provider._resolved_device = "cpu"
    monkeypatch.setattr(provider, "_load_and_check", lambda: None)
    return provider, fake


@pytest.mark.parametrize("dimension", [768, 512, 256, 128])
def test_dimension_and_unit_norm(dimension, monkeypatch):
    provider = EmbeddingGemma2EmbeddingFunction(dimension=dimension, device="cpu")
    model = _FakeModel(dimension=dimension)
    provider._model = model
    provider._resolved_device = "cpu"
    monkeypatch.setattr(provider, "_load_and_check", lambda: None)
    vectors = provider.embed_query(["q"])
    assert len(vectors) == 1
    assert len(vectors[0]) == dimension
    assert np.linalg.norm(vectors[0]) == pytest.approx(1.0)
    assert model.calls[0][1]["truncate_dim"] == dimension
    assert model.calls[0][1]["normalize_embeddings"] is True


@pytest.mark.parametrize("dimension", [0, 384, 1024, True, "768"])
def test_rejects_unsupported_dimensions(dimension):
    with pytest.raises(ValueError, match="dimension"):
        EmbeddingGemma2EmbeddingFunction(dimension=dimension)


@pytest.mark.parametrize("modalities", ["text", "text+vision", "text+audio", "all"])
def test_selective_config_kwargs(modalities):
    expected = {
        "text": {"vision_config": None, "audio_config": None},
        "text+vision": {"audio_config": None},
        "text+audio": {"vision_config": None},
        "all": {},
    }
    assert _config_kwargs(modalities) == expected[modalities]


@pytest.mark.parametrize("modalities", ["vision", "text+video", "", None])
def test_rejects_unsupported_modalities(modalities):
    with pytest.raises(ValueError, match="modalities"):
        EmbeddingGemma2EmbeddingFunction(modalities=modalities)


@pytest.mark.parametrize("device", ["dml", "coreml", "cuda:0", "mps:0", "", None])
def test_rejects_unsupported_devices(device):
    with pytest.raises(ValueError, match="device"):
        EmbeddingGemma2EmbeddingFunction(device=device)


def test_model_load_is_lazy_and_uses_pinned_revision_float32(monkeypatch):
    captured = {}

    class _Torch:
        float32 = "float32"

    class _SentenceTransformer:
        def __init__(self, model_id, **kwargs):
            captured.update(model_id=model_id, kwargs=kwargs)

    monkeypatch.setitem(sys.modules, "torch", _Torch())
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=_SentenceTransformer),
    )
    provider = EmbeddingGemma2EmbeddingFunction(modalities="text+vision", device="cpu")
    assert provider._model is None
    model = provider._load_model_for("cpu")
    assert model is not None
    assert captured["model_id"] == MODEL_ID
    assert captured["kwargs"] == {
        "device": "cpu",
        "revision": DEFAULT_REVISION,
        "model_kwargs": {"dtype": "float32"},
        "config_kwargs": {"audio_config": None},
    }


def test_document_metadata_title_and_source_basename_prompts(monkeypatch):
    provider, model = _ready_provider(monkeypatch)
    vectors = provider.embed_documents(
        ["body A", "body B", "body C"],
        metadatas=[{"title": "A title"}, {"source_file": "/repo/path/file.py"}, {}],
    )
    assert len(vectors) == 3
    assert model.calls[0][0] == ["body C"]
    assert model.calls[0][1]["prompt_name"] == "Document"
    assert model.calls[1][0] == ["title: A title | text: body A", "title: file.py | text: body B"]
    assert model.calls[1][1]["prompt"] == ""


def test_query_routes_use_asymmetric_search_and_code_prompts(monkeypatch):
    provider, model = _ready_provider(monkeypatch)
    provider.embed_query("find a decision")
    provider.embed_code_query("find auth handling")
    assert model.calls[0][0] == ["find a decision"]
    assert model.calls[0][1]["prompt_name"] == "SearchQuery"
    assert model.calls[1][0] == ["find auth handling"]
    assert model.calls[1][1]["prompt_name"] == "CodeRetrieval"


def test_chroma_call_embeds_documents_and_empty_inputs_skip_model(monkeypatch):
    provider, model = _ready_provider(monkeypatch)
    assert provider([]) == []
    assert provider.embed_query([]) == []
    assert model.calls == []
    assert len(provider("one document")) == 1
    assert model.calls[0][1]["prompt_name"] == "Document"


def test_metadata_count_must_match_documents(monkeypatch):
    provider, model = _ready_provider(monkeypatch)
    with pytest.raises(ValueError, match="metadatas length"):
        provider.embed_documents(["one", "two"], metadatas=[{}])
    with pytest.raises(ValueError, match="metadatas length"):
        provider.embed_documents(["one", "two"], metadatas={"title": "one"})
    assert model.calls == []


def test_single_document_accepts_single_metadata_mapping(monkeypatch):
    provider, model = _ready_provider(monkeypatch)
    provider.embed_documents("body", metadatas={"title": "One"})
    assert model.calls[0][0] == ["title: One | text: body"]
    assert model.calls[0][1]["prompt"] == ""


@pytest.mark.parametrize(
    "raw",
    [
        np.zeros((1, 768), dtype=np.float32),
        np.full((1, 768), np.nan, dtype=np.float32),
        np.ones((1, 767), dtype=np.float32),
        np.ones((2, 768), dtype=np.float32),
        np.ones(768, dtype=np.float32),
    ],
)
def test_rejects_zero_nonfinite_or_wrong_shape_vectors(raw, monkeypatch):
    provider, _ = _ready_provider(monkeypatch, _FakeModel(behavior=lambda *_: raw))
    with pytest.raises(EmbeddingOutputError):
        provider.embed_query(["q"])


def test_config_round_trip_and_identity_include_vector_space():
    provider = EmbeddingGemma2EmbeddingFunction(
        dimension=256, modalities="text+audio", device="mps"
    )
    config = provider.get_config()
    rebuilt = EmbeddingGemma2EmbeddingFunction.build_from_config(config)
    assert rebuilt.get_config() == config
    assert rebuilt.name() == "embeddinggemma2"
    assert rebuilt.default_space() == "cosine"
    assert rebuilt.supported_spaces() == ["cosine"]
    assert rebuilt.is_legacy() is False
    assert rebuilt.identity == (
        f"embeddinggemma2:{MODEL_ID}@{DEFAULT_REVISION}:256:text+audio:retrieval-v1"
    )


def test_chroma_config_validation_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown"):
        EmbeddingGemma2EmbeddingFunction.validate_config({"device": "cpu", "token": "x"})


def test_mps_witness_is_repeatable_and_falls_back_if_invalid(monkeypatch):
    provider = EmbeddingGemma2EmbeddingFunction(device="auto")
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)),
        ),
    )
    devices = []

    class _InvalidMPS(_FakeModel):
        def encode(self, texts, **kwargs):
            if devices[-1] == "mps":
                return np.zeros((len(texts), self.dimension), dtype=np.float32)
            return super().encode(texts, **kwargs)

    def load(device):
        devices.append(device)
        return _InvalidMPS()

    monkeypatch.setattr(provider, "_load_model_for", load)
    with pytest.warns(RuntimeWarning, match="falling back to CPU"):
        provider._load_and_check()
    assert devices == ["mps", "cpu"]
    assert provider._resolved_device == "cpu"


def test_mps_model_initialization_runtime_failure_falls_back(monkeypatch):
    provider = EmbeddingGemma2EmbeddingFunction(device="auto")
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)),
        ),
    )
    devices = []

    def load(device):
        devices.append(device)
        if device == "mps":
            raise RuntimeError("MPS backend initialization failed")
        return _FakeModel()

    monkeypatch.setattr(provider, "_load_model_for", load)
    with pytest.warns(RuntimeWarning, match="model initialization"):
        provider._load_and_check()
    assert devices == ["mps", "cpu"]
    assert provider.effective_device == "cpu"


def test_mps_model_configuration_failure_is_not_hidden(monkeypatch):
    provider = EmbeddingGemma2EmbeddingFunction(device="auto")
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)),
        ),
    )
    monkeypatch.setattr(
        provider,
        "_load_model_for",
        lambda _device: (_ for _ in ()).throw(RuntimeError("invalid config")),
    )
    with pytest.raises(RuntimeError, match="invalid config"):
        provider._load_and_check()


def test_mps_batch_failure_retries_same_batch_once_on_cpu(monkeypatch):
    provider = EmbeddingGemma2EmbeddingFunction(device="auto")
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)),
        ),
    )
    devices = []
    batches = []

    class _Model(_FakeModel):
        def encode(self, texts, **kwargs):
            batches.append((devices[-1], list(texts), kwargs.get("prompt_name")))
            if devices[-1] == "mps" and texts[0] != "mempalace embedding provider health check":
                raise RuntimeError("MPS runtime operation failed")
            return super().encode(texts, **kwargs)

    monkeypatch.setattr(
        provider, "_load_model_for", lambda device: devices.append(device) or _Model()
    )
    with pytest.warns(RuntimeWarning, match="retrying this batch on CPU"):
        vectors = provider.embed_query(["same batch"])
    assert devices == ["mps", "cpu"]
    assert ("mps", ["same batch"], "SearchQuery") in batches
    assert ("cpu", ["same batch"], "SearchQuery") in batches
    assert len(vectors) == 1


@pytest.mark.parametrize("media_type", ["image", "video"])
def test_corrupt_media_runtime_error_keeps_healthy_mps_model(
    monkeypatch, tmp_path, caplog, media_type
):
    def encode(texts, _kwargs):
        if isinstance(texts[0], dict):
            raise RuntimeError("Could not decode input file: Invalid data found")

    provider, model = _ready_provider(monkeypatch, _FakeModel(behavior=encode))
    provider.modalities = "all"
    provider._resolved_device = "mps"
    reloads = []
    monkeypatch.setattr(
        provider, "_load_model_for", lambda device: reloads.append(device) or _FakeModel()
    )
    path = tmp_path / ("damaged.png" if media_type == "image" else "damaged.mp4")
    path.write_bytes(b"invalid media")

    with pytest.raises(RuntimeError, match="Could not decode input file"):
        provider.embed_media(path, media_type)

    assert reloads == []
    assert provider.effective_device == "mps"
    assert provider._model is model
    assert "retrying" not in caplog.text
    assert len(provider.embed_query(["healthy next query"])[0]) == 768


def test_concurrent_first_call_waits_for_mps_witness(monkeypatch):
    provider = EmbeddingGemma2EmbeddingFunction(device="auto")
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)),
        ),
    )
    witness_started = threading.Event()
    release_witness = threading.Event()
    query_finished = threading.Event()

    class _BlockingWitness(_FakeModel):
        def encode(self, texts, **kwargs):
            if texts == ["mempalace embedding provider health check"]:
                witness_started.set()
                assert release_witness.wait(timeout=2)
            return super().encode(texts, **kwargs)

    model = _BlockingWitness()
    monkeypatch.setattr(provider, "_load_model_for", lambda _device: model)
    first = threading.Thread(target=lambda: provider.embed_query(["first"]))
    second = threading.Thread(
        target=lambda: (provider.embed_query(["second"]), query_finished.set())
    )
    first.start()
    assert witness_started.wait(timeout=1)
    second.start()
    assert not query_finished.wait(timeout=0.05)
    release_witness.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert query_finished.is_set()
    assert model.calls[-2][0] == ["first"]
    assert model.calls[-1][0] == ["second"]


def test_cpu_invalid_vectors_fail_without_retry(monkeypatch):
    provider = EmbeddingGemma2EmbeddingFunction(device="cpu")
    model = _FakeModel(behavior=lambda *_: np.zeros((1, 768), dtype=np.float32))
    provider._model = model
    provider._resolved_device = "cpu"
    monkeypatch.setattr(provider, "_load_and_check", lambda: None)
    with pytest.raises(EmbeddingOutputError, match="zero"):
        provider.embed_query(["q"])
    assert len(model.calls) == 1


def test_image_embedding_accepts_local_path_and_pil_image(tmp_path, monkeypatch):
    from PIL import Image

    provider, model = _ready_provider(monkeypatch, _FakeModel())
    provider.modalities = "text+vision"
    image_path = tmp_path / "local.png"
    image_path.write_bytes(b"handled by mocked model")

    path_vector = provider.embed_image(image_path)
    image = Image.new("RGB", (3, 3), color="red")
    pil_vector = provider.embed_media(image, media_type="image")

    assert len(path_vector) == 768 and len(pil_vector) == 768
    path_input, path_kwargs = model.calls[0]
    assert path_input == [{"image": str(image_path.resolve())}]
    assert path_kwargs["prompt"] == ""
    assert model.calls[1][0] == [{"image": image}]


def test_audio_dict_is_downmixed_and_resampled_before_embedding(monkeypatch):
    provider, model = _ready_provider(monkeypatch, _FakeModel())
    provider.modalities = "text+audio"
    stereo = np.ones((8, 2), dtype=np.float32)

    vector = provider.embed_audio({"array": stereo, "sampling_rate": 8_000})

    assert len(vector) == 768
    payload, kwargs = model.calls[0]
    audio = payload[0]["audio"]
    assert audio["sampling_rate"] == 16_000
    assert audio["array"].dtype == np.float32
    assert audio["array"].shape == (16,)
    assert kwargs["prompt"] == ""


def test_audio_path_is_decoded_to_mono_16khz(tmp_path, monkeypatch):
    from scipy.io import wavfile

    provider, model = _ready_provider(monkeypatch, _FakeModel())
    provider.modalities = "text+audio"
    audio_path = tmp_path / "clip.wav"
    wavfile.write(audio_path, 8_000, np.full((8, 2), 1024, dtype=np.int16))

    provider.embed_media(audio_path, "audio")

    decoded = model.calls[0][0][0]["audio"]
    assert decoded["sampling_rate"] == 16_000
    assert decoded["array"].shape == (16,)
    assert decoded["array"].dtype == np.float32


def test_m4a_uses_torchcodec_when_soundfile_cannot_decode(tmp_path, monkeypatch):
    import types

    provider, model = _ready_provider(monkeypatch, _FakeModel())
    provider.modalities = "text+audio"
    audio_path = tmp_path / "clip.m4a"
    audio_path.write_bytes(b"mock media")
    decoder_calls = []

    soundfile = types.ModuleType("soundfile")

    def fail_soundfile(*args, **kwargs):
        raise RuntimeError("unsupported AAC container")

    soundfile.read = fail_soundfile

    class _AudioDecoder:
        def __init__(self, path, *, sample_rate, num_channels):
            decoder_calls.append((path, sample_rate, num_channels))

        def get_all_samples(self):
            return SimpleNamespace(data=np.arange(12, dtype=np.float32)[None, :])

    torchcodec = types.ModuleType("torchcodec")
    torchcodec.__path__ = []
    decoders = types.ModuleType("torchcodec.decoders")
    decoders.AudioDecoder = _AudioDecoder
    monkeypatch.setitem(sys.modules, "soundfile", soundfile)
    monkeypatch.setitem(sys.modules, "torchcodec", torchcodec)
    monkeypatch.setitem(sys.modules, "torchcodec.decoders", decoders)

    provider.embed_media(audio_path, "audio")

    assert decoder_calls == [(str(audio_path.resolve()), 16_000, 1)]
    decoded = model.calls[0][0][0]["audio"]
    assert decoded["sampling_rate"] == 16_000
    assert decoded["array"].shape == (12,)
    assert decoded["array"].dtype == np.float32
    np.testing.assert_array_equal(decoded["array"], np.arange(12, dtype=np.float32))


def test_audio_path_keeps_soundfile_samples_when_decode_succeeds(tmp_path, monkeypatch):
    import types

    provider, model = _ready_provider(monkeypatch, _FakeModel())
    provider.modalities = "text+audio"
    audio_path = tmp_path / "source.flac"
    audio_path.write_bytes(b"mock media")
    samples = np.linspace(-0.5, 0.5, 12, dtype=np.float32)[:, None]
    soundfile = types.ModuleType("soundfile")
    soundfile.read = lambda *args, **kwargs: (samples, 16_000)
    monkeypatch.setitem(sys.modules, "soundfile", soundfile)

    provider.embed_media(audio_path, "audio")

    decoded = model.calls[0][0][0]["audio"]["array"]
    np.testing.assert_array_equal(decoded, samples[:, 0])


def test_m4a_without_soundfile_or_torchcodec_has_actionable_error(tmp_path, monkeypatch):
    audio_path = tmp_path / "clip.m4a"
    audio_path.write_bytes(b"mock media")
    monkeypatch.setitem(sys.modules, "soundfile", None)
    monkeypatch.setitem(sys.modules, "torchcodec", None)
    monkeypatch.setitem(sys.modules, "torchcodec.decoders", None)

    with pytest.raises(ImportError, match=r"Install mempalace\[multimodal\]"):
        EmbeddingGemma2EmbeddingFunction._load_audio(audio_path)


def test_m4a_uses_torchcodec_when_soundfile_is_unavailable(tmp_path, monkeypatch):
    import types

    audio_path = tmp_path / "clip.m4a"
    audio_path.write_bytes(b"mock media")

    class _AudioDecoder:
        def __init__(self, path, *, sample_rate, num_channels):
            assert path == str(audio_path.resolve())
            assert sample_rate == 16_000
            assert num_channels == 1

        def get_all_samples(self):
            return SimpleNamespace(data=np.ones((1, 16), dtype=np.float32))

    torchcodec = types.ModuleType("torchcodec")
    torchcodec.__path__ = []
    decoders = types.ModuleType("torchcodec.decoders")
    decoders.AudioDecoder = _AudioDecoder
    monkeypatch.setitem(sys.modules, "soundfile", None)
    monkeypatch.setitem(sys.modules, "torchcodec", torchcodec)
    monkeypatch.setitem(sys.modules, "torchcodec.decoders", decoders)

    audio = EmbeddingGemma2EmbeddingFunction._load_audio(audio_path)

    assert audio["sampling_rate"] == 16_000
    assert audio["array"].shape == (16,)
    assert audio["array"].dtype == np.float32


def test_torchcodec_rejects_unexpected_multichannel_shape(tmp_path, monkeypatch):
    import types

    audio_path = tmp_path / "clip.m4a"
    audio_path.write_bytes(b"mock media")
    soundfile = types.ModuleType("soundfile")
    soundfile.read = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad input"))

    class _AudioDecoder:
        def __init__(self, *args, **kwargs):
            pass

        def get_all_samples(self):
            return SimpleNamespace(data=np.ones((2, 12), dtype=np.float32))

    torchcodec = types.ModuleType("torchcodec")
    torchcodec.__path__ = []
    decoders = types.ModuleType("torchcodec.decoders")
    decoders.AudioDecoder = _AudioDecoder
    monkeypatch.setitem(sys.modules, "soundfile", soundfile)
    monkeypatch.setitem(sys.modules, "torchcodec", torchcodec)
    monkeypatch.setitem(sys.modules, "torchcodec.decoders", decoders)

    with pytest.raises(ValueError, match=r"shape \(2, 12\), expected mono samples"):
        EmbeddingGemma2EmbeddingFunction._load_audio(audio_path)


def test_video_embedding_uses_local_path_without_prefix(tmp_path, monkeypatch):
    provider, model = _ready_provider(monkeypatch, _FakeModel())
    provider.modalities = "all"
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"handled by mocked model")

    assert len(provider.embed_video(video_path)) == 768
    payload, kwargs = model.calls[0]
    assert payload == [{"video": str(video_path.resolve())}]
    assert kwargs["prompt"] == ""


@pytest.mark.parametrize(
    ("modalities", "media_type"),
    [("text", "image"), ("text+vision", "audio"), ("text+audio", "video")],
)
def test_media_embedding_requires_matching_configured_modality(modalities, media_type):
    provider = EmbeddingGemma2EmbeddingFunction(modalities=modalities, device="cpu")
    with pytest.raises(ValueError, match="requires modalities"):
        provider.embed_media("missing", media_type)


def test_media_paths_reject_remote_urls_and_missing_files(tmp_path):
    with pytest.raises(ValueError, match="remote media URLs"):
        EmbeddingGemma2EmbeddingFunction._local_media_path("https://example.test/a.png", "image")
    with pytest.raises(FileNotFoundError, match="image file does not exist"):
        EmbeddingGemma2EmbeddingFunction._local_media_path(tmp_path / "absent.png", "image")


def test_embed_media_rejects_unknown_media_type():
    provider = EmbeddingGemma2EmbeddingFunction(modalities="all", device="cpu")
    with pytest.raises(ValueError, match="media_type"):
        provider.embed_media("x", "document")
