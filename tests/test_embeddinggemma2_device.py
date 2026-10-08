"""EmbeddingGemma 2 device selection, batch size and context cap, with a mocked torch."""

import json
import logging
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from mempalace import embedding
from mempalace.embeddinggemma2 import (
    _BATCH_SIZE,
    _CONTEXT_TOKENS,
    EmbeddingGemma2EmbeddingFunction,
)


def _torch(cuda=False, mps=False):
    def available(flag):
        if isinstance(flag, Exception):

            def boom():
                raise flag

            return boom
        return lambda: flag

    return SimpleNamespace(
        float32="float32",
        cuda=SimpleNamespace(is_available=available(cuda)),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=available(mps))),
    )


class _FakeModel:
    def __init__(self, dimension=768):
        self.dimension = dimension
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        rows = np.ones((len(texts), self.dimension), dtype=np.float32)
        rows[:, 0] += np.arange(len(texts), dtype=np.float32)
        return rows


def _provider(monkeypatch, *, cuda=False, mps=False, device="auto", batch_size=None):
    monkeypatch.setitem(sys.modules, "torch", _torch(cuda=cuda, mps=mps))
    provider = EmbeddingGemma2EmbeddingFunction(device=device, batch_size=batch_size)
    loads = []

    def load(selected):
        loads.append(selected)
        return _FakeModel()

    monkeypatch.setattr(provider, "_load_model_for", load)
    return provider, loads


@pytest.fixture(autouse=True)
def _fresh_warnings():
    embedding._WARNED.clear()
    embedding._EF_CACHE.clear()
    yield
    embedding._WARNED.clear()
    embedding._EF_CACHE.clear()


# ── device selection ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cuda, mps, expected",
    [(True, False, "cuda"), (True, True, "cuda"), (False, True, "mps"), (False, False, "cpu")],
    ids=["cuda-only", "cuda-and-mps", "mps-only", "neither"],
)
def test_auto_prefers_cuda_then_mps_then_cpu(monkeypatch, cuda, mps, expected):
    provider, loads = _provider(monkeypatch, cuda=cuda, mps=mps)
    assert provider.planned_device() == expected
    assert loads == []  # previewing the device loads nothing
    provider.embed_query(["q"])
    assert loads == [expected]
    assert provider.effective_device == expected


def test_explicit_cuda_runs_on_cuda_when_available(monkeypatch):
    provider, loads = _provider(monkeypatch, cuda=True, device="cuda")
    provider.embed_query(["q"])
    assert loads == ["cuda"]


@pytest.mark.parametrize("device", ["cuda", "mps"])
def test_explicit_accelerator_pytorch_cannot_use_warns_and_runs_on_cpu(monkeypatch, device):
    """Like the ONNX providers: an unavailable accelerator falls back to CPU
    instead of failing, so one config works on a laptop without the GPU."""
    provider, loads = _provider(monkeypatch, device=device)
    assert provider.planned_device() == "cpu"
    with pytest.warns(RuntimeWarning, match=rf"embedding_device='{device}'.*falls back to CPU"):
        provider.embed_query(["q"])
    assert loads == ["cpu"]
    assert provider.effective_device == "cpu"


def test_a_broken_cuda_probe_reads_as_unavailable(monkeypatch):
    provider, loads = _provider(monkeypatch, cuda=RuntimeError("driver too old"))
    provider.embed_query(["q"])
    assert loads == ["cpu"]


def test_a_torch_without_cuda_attribute_still_selects(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False))),
    )
    assert EmbeddingGemma2EmbeddingFunction(device="auto").planned_device() == "cpu"


def test_constructor_accepts_cuda_and_still_rejects_onnx_provider_names():
    assert EmbeddingGemma2EmbeddingFunction(device="CUDA").device == "cuda"
    for device in ("dml", "coreml", "cuda:0"):
        with pytest.raises(ValueError, match="device"):
            EmbeddingGemma2EmbeddingFunction(device=device)


# ── shared embedding_device: ONNX-only values for gemma2, torch-only for ONNX ──


@pytest.fixture
def eg2_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    def write(**values):
        payload = {"embedding_model": "embeddinggemma2", **values}
        (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        embedding._EF_CACHE.clear()

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config_dir))
    for key in (
        "MEMPALACE_EMBEDDING_MODEL",
        "MEMPALACE_EMBEDDING_DEVICE",
        "MEMPALACE_EMBEDDINGGEMMA2_BATCH_SIZE",
        "MEMPALACE_EMBEDDINGGEMMA2_DIMENSION",
        "MEMPALACE_EMBEDDINGGEMMA2_MODALITIES",
        "MEMPALACE_EMBEDDINGGEMMA2_REVISION",
    ):
        monkeypatch.delenv(key, raising=False)
    write()
    return write


@pytest.mark.parametrize("configured", ["dml", "coreml", "DML"])
def test_onnx_only_device_reads_as_auto_for_gemma2_with_one_warning(eg2_config, caplog, configured):
    eg2_config(embedding_device=configured)
    with caplog.at_level(logging.WARNING, logger="mempalace.embedding"):
        ef = embedding.get_embedding_function()
        embedding._EF_CACHE.clear()
        embedding.get_embedding_function()
    assert ef.device == "auto"
    warnings = [r.getMessage() for r in caplog.records if "ONNX Runtime provider" in r.getMessage()]
    assert len(warnings) == 1
    assert f"embedding_device={configured.lower()!r}" in warnings[0]
    assert "uses 'auto'" in warnings[0]


def test_unknown_device_reads_as_cpu_for_gemma2(eg2_config, caplog):
    eg2_config(embedding_device="tpu")
    with caplog.at_level(logging.WARNING, logger="mempalace.embedding"):
        ef = embedding.get_embedding_function()
    assert ef.device == "cpu"
    assert any("is not a known device" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("configured", ["auto", "cuda", "mps", "cpu"])
def test_pytorch_devices_pass_through_for_gemma2(eg2_config, caplog, configured):
    eg2_config(embedding_device=configured)
    with caplog.at_level(logging.WARNING, logger="mempalace.embedding"):
        assert embedding.get_embedding_function().device == configured
    assert not [r for r in caplog.records if "embedding_device" in r.getMessage()]


def _fake_onnxruntime(monkeypatch, providers):
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(get_available_providers=lambda: list(providers)),
    )


@pytest.mark.parametrize(
    "available, expected",
    [
        (["CUDAExecutionProvider", "CPUExecutionProvider"], "cuda"),
        (["CPUExecutionProvider"], "cpu"),
    ],
)
def test_torch_only_mps_reads_as_auto_for_onnx_models(monkeypatch, caplog, available, expected):
    _fake_onnxruntime(monkeypatch, available)
    with caplog.at_level(logging.WARNING, logger="mempalace.embedding"):
        _, effective = embedding._resolve_providers("mps", "minilm")
        embedding._resolve_providers("mps", "minilm")
    assert effective == expected
    warnings = [r.getMessage() for r in caplog.records if "PyTorch device" in r.getMessage()]
    assert len(warnings) == 1


# ── batch size ───────────────────────────────────────────────────────────


def test_default_batch_size_depends_on_the_device():
    provider = EmbeddingGemma2EmbeddingFunction()
    assert provider.batch_size_for("cuda") == 32
    assert provider.batch_size_for("cpu") == _BATCH_SIZE == 4
    assert provider.batch_size_for("mps") == 4
    assert provider.batch_size_for(None) == 4


def test_configured_batch_size_wins_on_every_device():
    provider = EmbeddingGemma2EmbeddingFunction(batch_size=8)
    assert {d: provider.batch_size_for(d) for d in ("cuda", "mps", "cpu")} == {
        "cuda": 8,
        "mps": 8,
        "cpu": 8,
    }


@pytest.mark.parametrize("bad", [0, -1, True, 2.5, "8"])
def test_constructor_rejects_invalid_batch_size(bad):
    with pytest.raises(ValueError, match="batch_size"):
        EmbeddingGemma2EmbeddingFunction(batch_size=bad)


@pytest.mark.parametrize(
    "cuda, configured, expected", [(True, None, 32), (False, None, 4), (True, 16, 16)]
)
def test_encode_uses_the_batch_size_for_the_resolved_device(
    monkeypatch, cuda, configured, expected
):
    provider, _ = _provider(monkeypatch, cuda=cuda, batch_size=configured)
    provider.embed_documents(["a", "b"])
    assert provider._model.calls[0][1]["batch_size"] == expected


def test_batch_size_from_env_and_config(eg2_config, monkeypatch):
    assert embedding.get_embedding_function().batch_size is None
    eg2_config(embeddinggemma2_batch_size=12)
    assert embedding.get_embedding_function().batch_size == 12
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_BATCH_SIZE", "24")
    embedding._EF_CACHE.clear()
    assert embedding.get_embedding_function().batch_size == 24


@pytest.mark.parametrize("raw", ["0", "-4", "abc", ""])
def test_invalid_batch_size_setting_means_the_device_default(eg2_config, monkeypatch, raw):
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_BATCH_SIZE", raw)
    from mempalace.config import MempalaceConfig

    assert MempalaceConfig().embeddinggemma2_batch_size is None


def test_onnx_batch_size_setting_does_not_size_gemma2(eg2_config, monkeypatch):
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA_BATCH_SIZE", "2")
    assert embedding.get_embedding_function().batch_size is None


# ── context cap ──────────────────────────────────────────────────────────


def _load_with(monkeypatch, *, max_seq_length, positions=None):
    class _SentenceTransformer:
        def __init__(self, model_id, **kwargs):
            self.max_seq_length = max_seq_length
            text_config = SimpleNamespace(max_position_embeddings=positions)
            self._first = SimpleNamespace(
                auto_model=SimpleNamespace(config=SimpleNamespace(text_config=text_config))
            )

        def __getitem__(self, index):
            return self._first

    monkeypatch.setitem(sys.modules, "torch", _torch())
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=_SentenceTransformer),
    )
    return EmbeddingGemma2EmbeddingFunction(device="cpu")._load_model_for("cpu")


def test_unbounded_max_seq_length_is_capped_at_the_context_window(monkeypatch):
    # The pinned checkpoint's tokenizer reports model_max_length=1e30 and its
    # text_config max_position_embeddings=262144; the model card says 8,192.
    model = _load_with(monkeypatch, max_seq_length=10**30, positions=262144)
    assert model.max_seq_length == _CONTEXT_TOKENS == 8192


def test_smaller_checkpoint_position_limit_is_used(monkeypatch):
    assert _load_with(monkeypatch, max_seq_length=None, positions=2048).max_seq_length == 2048


def test_a_smaller_existing_limit_is_kept(monkeypatch):
    assert _load_with(monkeypatch, max_seq_length=512, positions=262144).max_seq_length == 512


# ── the device never selects the vector space ───────────────────────────


def test_device_is_not_part_of_the_identity():
    identities = {EmbeddingGemma2EmbeddingFunction(device=d).identity for d in ("cuda", "cpu")}
    assert len(identities) == 1


def test_palace_mined_on_cuda_opens_and_searches_on_a_cpu_only_machine(
    eg2_config, tmp_path, monkeypatch
):
    """The configured device must never be read back from the palace.

    MemPalace creates its collections without a persisted embedding-function
    config (no get_config(), so no device, reaches chroma.sqlite3), and the
    RFC 001 identity in mempalace_embedder.json has no device. A palace filed
    on CUDA therefore opens on a CPU-only box with that box's device."""
    from mempalace import palace as palace_mod
    from mempalace.backends import reset_backends

    loads = []

    def load(self, selected):
        loads.append(selected)
        return _FakeModel()

    monkeypatch.setattr(EmbeddingGemma2EmbeddingFunction, "_load_model_for", load)
    palace_path = str(tmp_path / "palace")
    eg2_config(palace_path=palace_path, embedding_device="cuda")
    monkeypatch.setitem(sys.modules, "torch", _torch(cuda=True))
    reset_backends()
    col = palace_mod.get_collection(palace_path, create=True)
    col.upsert(ids=["d1", "d2"], documents=["alpha", "beta"], metadatas=[{"w": 1}, {"w": 2}])
    assert loads == ["cuda"]
    sidecar = json.loads((tmp_path / "palace" / "mempalace_embedder.json").read_text())
    assert "cuda" not in json.dumps(sidecar)
    import sqlite3

    with sqlite3.connect(str(tmp_path / "palace" / "chroma.sqlite3")) as db:
        configs = [row[0] for row in db.execute("select config_json_str from collections")]
    assert configs and not any("cuda" in (config or "") for config in configs)

    # Same palace on a machine without CUDA: config still says cuda.
    reset_backends()
    palace_mod._VALIDATED_IDENTITY.clear()
    embedding._EF_CACHE.clear()
    monkeypatch.setitem(sys.modules, "torch", _torch(cuda=False))
    with pytest.warns(RuntimeWarning, match="falls back to CPU"):
        reopened = palace_mod.get_collection(palace_path, create=False)
        assert reopened.count() == 2
        hits = reopened.query(query_texts=["alpha"], n_results=2)
    assert loads[-1] == "cpu"
    assert sorted(hits["ids"][0]) == ["d1", "d2"]

    # And with device=cpu configured explicitly: no warning, same palace.
    reset_backends()
    embedding._EF_CACHE.clear()
    eg2_config(palace_path=palace_path, embedding_device="cpu")
    assert palace_mod.get_collection(palace_path, create=False).count() == 2
    reset_backends()


def test_mine_header_names_the_resolved_device(eg2_config, monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", _torch(cuda=True))
    loads = []
    monkeypatch.setattr(
        EmbeddingGemma2EmbeddingFunction,
        "_load_model_for",
        lambda self, d: loads.append(d) or _FakeModel(),
    )
    assert embedding.describe_device() == "embeddinggemma2 (cuda, float32)"
    assert loads == []
    eg2_config(embedding_device="cpu")
    assert embedding.describe_device() == "embeddinggemma2 (cpu, float32)"
