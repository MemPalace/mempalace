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
def test_explicit_accelerator_pytorch_cannot_use_warns_and_runs_on_cpu(monkeypatch, caplog, device):
    """Like the ONNX providers: an unavailable accelerator falls back to CPU
    instead of failing, so one config works on a laptop without the GPU. The
    warning is one logger line (stderr), not a second RuntimeWarning copy."""
    provider, loads = _provider(monkeypatch, device=device)
    assert provider.planned_device() == "cpu"
    with caplog.at_level(logging.WARNING, logger="mempalace.embeddinggemma2"):
        provider.embed_query(["q"])
        provider.embed_query(["again"])
    fallbacks = [r.getMessage() for r in caplog.records if "falls back to CPU" in r.getMessage()]
    assert len(fallbacks) == 1 and f"embedding_device='{device}'" in fallbacks[0]
    assert loads == ["cpu"]
    assert provider.effective_device == "cpu"
    assert provider.device_label() == f"cpu; {device} requested but unavailable"


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


@pytest.mark.parametrize("raw", ["0", "-4", "abc", "", "2.5", "1025", "100000"])
def test_invalid_batch_size_env_warns_once_and_means_the_device_default(
    eg2_config, monkeypatch, caplog, raw
):
    import mempalace.config as config_mod

    monkeypatch.setattr(config_mod, "_BATCH_SIZE_WARNED", set())
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_BATCH_SIZE", raw)
    with caplog.at_level(logging.WARNING, logger="mempalace.config"):
        assert config_mod.MempalaceConfig().embeddinggemma2_batch_size is None
        assert config_mod.MempalaceConfig().embeddinggemma2_batch_size is None
    warnings = [r.getMessage() for r in caplog.records if "batch size" in r.getMessage()]
    assert len(warnings) == 1, warnings
    assert "MEMPALACE_EMBEDDINGGEMMA2_BATCH_SIZE" in warnings[0]
    assert "1 to 1024" in warnings[0]


@pytest.mark.parametrize("raw", [True, 8.0, "eight", 0, 4096, [4]])
def test_invalid_batch_size_config_warns_once_and_means_the_device_default(
    eg2_config, monkeypatch, caplog, raw
):
    import mempalace.config as config_mod

    monkeypatch.setattr(config_mod, "_BATCH_SIZE_WARNED", set())
    eg2_config(embeddinggemma2_batch_size=raw)
    with caplog.at_level(logging.WARNING, logger="mempalace.config"):
        assert config_mod.MempalaceConfig().embeddinggemma2_batch_size is None
        assert embedding.get_embedding_function().batch_size_for("cuda") == 32
    warnings = [r.getMessage() for r in caplog.records if "batch size" in r.getMessage()]
    assert len(warnings) == 1 and "embeddinggemma2_batch_size" in warnings[0], warnings


@pytest.mark.parametrize("raw, expected", [("1", 1), (" 64 ", 64), ("1024", 1024)])
def test_batch_size_bounds_are_inclusive(eg2_config, monkeypatch, caplog, raw, expected):
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_BATCH_SIZE", raw)
    from mempalace.config import MempalaceConfig

    with caplog.at_level(logging.WARNING, logger="mempalace.config"):
        assert MempalaceConfig().embeddinggemma2_batch_size == expected
    assert not caplog.records


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


# ── an unusable explicit device is visible before the mine header ───────────


def _record_warnings_and_prints(monkeypatch):
    """Events in order: ("warn", message) from the logger, ("print", text)."""
    import builtins

    events = []

    class _Handler(logging.Handler):
        def emit(self, record):
            events.append(("warn", record.getMessage()))

    handler = _Handler(level=logging.WARNING)
    logging.getLogger("mempalace").addHandler(handler)
    monkeypatch.setattr(
        logging.getLogger("mempalace"), "handlers", [*logging.getLogger("mempalace").handlers]
    )
    real_print = builtins.print

    def recording_print(*args, **kwargs):
        events.append(("print", " ".join(str(a) for a in args)))
        real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", recording_print)
    return events, handler


def test_describe_device_warns_first_and_labels_an_unavailable_explicit_device(
    eg2_config, monkeypatch, caplog
):
    monkeypatch.setitem(sys.modules, "torch", _torch(cuda=False))
    loads = []
    monkeypatch.setattr(
        EmbeddingGemma2EmbeddingFunction,
        "_load_model_for",
        lambda self, d: loads.append(d) or _FakeModel(),
    )
    eg2_config(embedding_device="cuda")
    with caplog.at_level(logging.WARNING, logger="mempalace.embeddinggemma2"):
        label = embedding.describe_device()
        assert embedding.describe_device() == label
        embedding.get_embedding_function().embed_query(["q"])
    assert label == "embeddinggemma2 (cpu; cuda requested but unavailable, float32)"
    fallbacks = [r for r in caplog.records if "falls back to CPU" in r.getMessage()]
    assert len(fallbacks) == 1  # the header preview and the model load share one warning
    assert loads == ["cpu"]
    # Usable devices keep the plain label.
    monkeypatch.setitem(sys.modules, "torch", _torch(cuda=True))
    eg2_config(embedding_device="cuda")
    assert embedding.describe_device() == "embeddinggemma2 (cuda, float32)"
    eg2_config(embedding_device="auto")
    monkeypatch.setitem(sys.modules, "torch", _torch(cuda=False))
    assert embedding.describe_device() == "embeddinggemma2 (cpu, float32)"


def test_mine_prints_the_device_warning_before_the_header(eg2_config, monkeypatch, tmp_path):
    import yaml

    from mempalace.miner import mine

    monkeypatch.setitem(sys.modules, "torch", _torch(cuda=False))
    monkeypatch.setattr(
        EmbeddingGemma2EmbeddingFunction, "_load_model_for", lambda self, d: _FakeModel()
    )
    eg2_config(embedding_device="cuda")
    project = tmp_path / "proj"
    (project / "notes").mkdir(parents=True)
    (project / "notes" / "a.md").write_text("The greenhouse tomatoes need water.\n" * 20)
    with open(project / "mempalace.yaml", "w") as fh:
        yaml.dump({"wing": "garden", "rooms": [{"name": "notes", "description": "N"}]}, fh)
    events, handler = _record_warnings_and_prints(monkeypatch)
    try:
        mine(str(project), str(tmp_path / "palace"), dry_run=True)
    finally:
        logging.getLogger("mempalace").removeHandler(handler)
    warn_at = next(i for i, (kind, text) in enumerate(events) if "falls back to CPU" in text)
    header_at = next(i for i, (kind, text) in enumerate(events) if "MemPalace Mine" in text)
    device_lines = [text for kind, text in events if "Device:" in text]
    assert events[warn_at][0] == "warn" and warn_at < header_at
    assert device_lines == [
        "  Device:  embeddinggemma2 (cpu; cuda requested but unavailable, float32)"
    ]
    assert sum("falls back to CPU" in text for kind, text in events) == 1


def test_status_shows_the_resolved_device(eg2_config, monkeypatch, capsys):
    from mempalace.miner import _print_status

    monkeypatch.setitem(sys.modules, "torch", _torch(cuda=True))
    eg2_config(embedding_device="auto")
    _print_status(3, {"garden": {"notes": 3}})
    assert "  Device:  embeddinggemma2 (cuda, float32)" in capsys.readouterr().out
    monkeypatch.setitem(sys.modules, "torch", _torch(cuda=False))
    eg2_config(embedding_device="cuda")
    _print_status(3, {"garden": {"notes": 3}})
    assert "embeddinggemma2 (cpu; cuda requested but unavailable, float32)" in (
        capsys.readouterr().out
    )
