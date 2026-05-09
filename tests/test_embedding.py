import pytest

import mempalace.embedding as embedding


@pytest.fixture(autouse=True)
def isolate_embedding_state(monkeypatch):
    monkeypatch.setattr(embedding, "_EF_CACHE", {})
    monkeypatch.setattr(embedding, "_WARNED", set())


def test_auto_picks_cuda(monkeypatch):
    monkeypatch.setattr(
        "onnxruntime.get_available_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )

    assert embedding._resolve_providers("auto") == (
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "cuda",
    )


def test_auto_falls_to_cpu(monkeypatch):
    monkeypatch.setattr("onnxruntime.get_available_providers", lambda: ["CPUExecutionProvider"])

    assert embedding._resolve_providers("auto") == (["CPUExecutionProvider"], "cpu")


def test_cuda_missing_warns_with_gpu_extra(monkeypatch, caplog):
    monkeypatch.setattr("onnxruntime.get_available_providers", lambda: ["CPUExecutionProvider"])

    assert embedding._resolve_providers("cuda") == (["CPUExecutionProvider"], "cpu")
    assert "mempalace[gpu]" in caplog.text


def test_coreml_missing_warns_with_coreml_extra(monkeypatch, caplog):
    monkeypatch.setattr("onnxruntime.get_available_providers", lambda: ["CPUExecutionProvider"])

    assert embedding._resolve_providers("coreml") == (["CPUExecutionProvider"], "cpu")
    assert "mempalace[coreml]" in caplog.text


def test_dml_missing_warns_with_dml_extra(monkeypatch, caplog):
    monkeypatch.setattr("onnxruntime.get_available_providers", lambda: ["CPUExecutionProvider"])

    assert embedding._resolve_providers("dml") == (["CPUExecutionProvider"], "cpu")
    assert "mempalace[dml]" in caplog.text


def test_unknown_device_warns_once(monkeypatch, caplog):
    monkeypatch.setattr("onnxruntime.get_available_providers", lambda: ["CPUExecutionProvider"])

    assert embedding._resolve_providers("bogus") == (["CPUExecutionProvider"], "cpu")
    assert embedding._resolve_providers("bogus") == (["CPUExecutionProvider"], "cpu")
    assert caplog.text.count("Unknown embedding_device") == 1


def test_onnxruntime_import_error_falls_back_to_cpu(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "onnxruntime":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert embedding._resolve_providers("cuda") == (["CPUExecutionProvider"], "cpu")


def test_offline_embedding_function_has_query_compat_shims(monkeypatch):
    ef = embedding.OfflineLocalEmbeddingFunction(model_path="/opt/models/paraphrase-multilingual-MiniLM-L12-v2")

    class DummyModel:
        def encode(self, input, convert_to_numpy=True, show_progress_bar=False):
            assert convert_to_numpy is True
            assert show_progress_bar is False
            return [[float(len(x))] * 3 for x in input]

    monkeypatch.setattr(ef, "_load_model", lambda: DummyModel())

    assert ef.embed_documents(["ab", "c"]) == [[2.0, 2.0, 2.0], [1.0, 1.0, 1.0]]
    assert ef.embed_query("ab") == [2.0, 2.0, 2.0]
    assert ef.embed_query(["ab", "c"]) == [[2.0, 2.0, 2.0], [1.0, 1.0, 1.0]]


def test_get_embedding_function_caches_by_model_path(monkeypatch):
    class DummyConfig:
        @property
        def embedding_model_path(self):
            return "/opt/models/paraphrase-multilingual-MiniLM-L12-v2"

    class DummyEF:
        def __init__(self, model_path=None):
            self.model_path = model_path

    monkeypatch.setattr(embedding, "MempalaceConfig", DummyConfig)
    monkeypatch.setattr(embedding, "OfflineLocalEmbeddingFunction", DummyEF)

    first = embedding.get_embedding_function("cpu")
    second = embedding.get_embedding_function("auto")

    assert first is second
    assert first.model_path == "/opt/models/paraphrase-multilingual-MiniLM-L12-v2"


def test_describe_device_uses_resolved_effective_device(monkeypatch):
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device: (["CUDAExecutionProvider", "CPUExecutionProvider"], "cuda"),
    )

    assert embedding.describe_device("auto") == "cuda"
