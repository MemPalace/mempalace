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


def test_get_embedding_function_caches_by_resolved_provider_tuple(monkeypatch):
    class DummyEF:
        def __init__(self, preferred_providers, intra_op_num_threads=0):
            self.preferred_providers = preferred_providers

    monkeypatch.setattr(embedding, "_build_ef_class", lambda: DummyEF)
    monkeypatch.setattr(
        embedding, "_resolve_providers", lambda device: (["CPUExecutionProvider"], "cpu")
    )

    first = embedding.get_embedding_function("cpu", "minilm")
    second = embedding.get_embedding_function("auto", "minilm")

    assert first is second
    assert first.preferred_providers == ["CPUExecutionProvider"]


def test_intra_op_session_options_caps_threads():
    so = embedding._intra_op_session_options(3)
    assert so is not None
    assert so.intra_op_num_threads == 3


def test_intra_op_session_options_uncapped_returns_none():
    assert embedding._intra_op_session_options(0) is None
    assert embedding._intra_op_session_options(-1) is None


def test_get_embedding_function_threads_cap_passed_to_minilm_ef(monkeypatch):
    captured = {}

    class DummyEF:
        def __init__(self, preferred_providers, intra_op_num_threads=0):
            captured["threads"] = intra_op_num_threads

    monkeypatch.setattr(embedding, "_build_ef_class", lambda: DummyEF)
    monkeypatch.setattr(
        embedding, "_resolve_providers", lambda device: (["CPUExecutionProvider"], "cpu")
    )
    monkeypatch.setattr(embedding, "_resolve_intra_op_threads", lambda: 2)

    embedding.get_embedding_function("cpu", "minilm")

    assert captured["threads"] == 2


def test_get_embedding_function_threads_cap_passed_to_embeddinggemma(monkeypatch):
    captured = {}

    class DummyGemma:
        def __init__(self, preferred_providers=None, intra_op_num_threads=0):
            captured["threads"] = intra_op_num_threads

    monkeypatch.setattr(embedding, "EmbeddinggemmaONNX", DummyGemma)
    monkeypatch.setattr(
        embedding, "_resolve_providers", lambda device: (["CPUExecutionProvider"], "cpu")
    )
    monkeypatch.setattr(embedding, "_resolve_intra_op_threads", lambda: 4)

    embedding.get_embedding_function("cpu", "embeddinggemma")

    assert captured["threads"] == 4


def test_minilm_ef_model_override_applies_thread_cap(monkeypatch):
    """The ``_MempalaceONNX.model`` override must construct the ORT session
    with the configured ``intra_op_num_threads`` (#1068). We stub
    ``InferenceSession`` to capture the ``SessionOptions`` it receives, so the
    test never downloads or loads the real model."""
    import onnxruntime as ort

    captured = {}

    def fake_session(model_path, providers=None, sess_options=None):
        captured["sess_options"] = sess_options
        captured["providers"] = providers
        return object()

    monkeypatch.setattr(ort, "InferenceSession", fake_session)

    ef_cls = embedding._build_ef_class()
    ef = ef_cls(preferred_providers=["CPUExecutionProvider"], intra_op_num_threads=2)
    _ = ef.model  # triggers the cached_property build

    assert captured["sess_options"] is not None
    assert captured["sess_options"].intra_op_num_threads == 2
    assert "CoreMLExecutionProvider" not in captured["providers"]


def test_minilm_ef_model_override_falls_back_when_uncapped(monkeypatch):
    """With no cap (0), the override must defer to the parent build via
    ``super().model`` — not reach into ``cached_property`` internals (#1068
    review). Proves super() resolves the parent descriptor without error."""
    import onnxruntime as ort

    captured = {}

    def fake_session(model_path, providers=None, sess_options=None):
        captured["sess_options"] = sess_options
        return object()

    monkeypatch.setattr(ort, "InferenceSession", fake_session)

    ef_cls = embedding._build_ef_class()
    ef = ef_cls(preferred_providers=["CPUExecutionProvider"], intra_op_num_threads=0)
    session = ef.model  # cap <= 0 → super().model (upstream builder)

    assert session is not None
    # Upstream leaves intra_op at ORT's default (0 = unset), confirming we
    # deferred to it rather than applying our cap.
    assert captured["sess_options"].intra_op_num_threads == 0


def test_describe_device_uses_resolved_effective_device(monkeypatch):
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device: (["CUDAExecutionProvider", "CPUExecutionProvider"], "cuda"),
    )

    assert embedding.describe_device("auto") == "cuda"
