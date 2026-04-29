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
        def __init__(self, preferred_providers):
            self.preferred_providers = preferred_providers

    monkeypatch.setattr(embedding, "_build_ef_class", lambda thread_cap=0: DummyEF)
    monkeypatch.setattr(
        embedding, "_resolve_providers", lambda device: (["CPUExecutionProvider"], "cpu")
    )
    monkeypatch.delenv("MEMPAL_MAX_THREADS", raising=False)

    first = embedding.get_embedding_function("cpu")
    second = embedding.get_embedding_function("auto")

    assert first is second
    assert first.preferred_providers == ["CPUExecutionProvider"]


def test_describe_device_uses_resolved_effective_device(monkeypatch):
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device: (["CUDAExecutionProvider", "CPUExecutionProvider"], "cuda"),
    )

    assert embedding.describe_device("auto") == "cuda"


# ── MEMPAL_MAX_THREADS: ONNX intra-op cap ────────────────────────────────


def test_read_thread_cap_default(monkeypatch):
    """Unset MEMPAL_MAX_THREADS → default cap of 2."""
    monkeypatch.delenv("MEMPAL_MAX_THREADS", raising=False)
    assert embedding._read_thread_cap() == 2


@pytest.mark.parametrize("value", ["0", "off", "default", "none", "", "  "])
def test_read_thread_cap_disabled_values(monkeypatch, value):
    """Sentinel strings disable the cap (return 0 → ORT defaults)."""
    monkeypatch.setenv("MEMPAL_MAX_THREADS", value)
    assert embedding._read_thread_cap() == 0


@pytest.mark.parametrize("value,expected", [("1", 1), ("2", 2), ("4", 4), ("16", 16)])
def test_read_thread_cap_positive_int(monkeypatch, value, expected):
    monkeypatch.setenv("MEMPAL_MAX_THREADS", value)
    assert embedding._read_thread_cap() == expected


def test_read_thread_cap_bad_value_is_disabled(monkeypatch, caplog):
    """Non-integer input falls back to 0 (no cap) and logs a warning."""
    monkeypatch.setenv("MEMPAL_MAX_THREADS", "banana")
    with caplog.at_level("WARNING"):
        assert embedding._read_thread_cap() == 0
    assert any("MEMPAL_MAX_THREADS" in r.message for r in caplog.records)


def test_read_thread_cap_negative_is_disabled(monkeypatch):
    monkeypatch.setenv("MEMPAL_MAX_THREADS", "-1")
    assert embedding._read_thread_cap() == 0


def test_build_ef_class_no_cap_returns_uncapped_subclass():
    """thread_cap <= 0 → no model override; chromadb defaults apply."""
    cls = embedding._build_ef_class(0)
    assert cls.name() == "default"
    assert not hasattr(cls, "_mempal_thread_cap")


def test_build_ef_class_with_cap_overrides_model():
    """thread_cap > 0 → subclass with capped model and _mempal_thread_cap."""
    cls = embedding._build_ef_class(3)
    assert cls.name() == "default"
    assert cls._mempal_thread_cap == 3


def test_build_ef_class_capped_model_uses_session_options():
    """The capped ``model`` cached_property builds an ORT session with our cap.

    Monkey-patches ``self.ort`` on the embedder instance so we don't download
    the real 90 MB ONNX model; instead we capture the SessionOptions passed to
    ``InferenceSession`` and assert on them.
    """
    cls = embedding._build_ef_class(2)
    instance = cls.__new__(cls)
    instance._preferred_providers = None

    captured: dict = {}

    class _FakeSessionOptions:
        def __init__(self):
            self.intra_op_num_threads = None
            self.inter_op_num_threads = None
            self.log_severity_level = None

    class _FakeORT:
        SessionOptions = _FakeSessionOptions

        @staticmethod
        def InferenceSession(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return "fake-session"

    instance.ort = _FakeORT

    session = instance.model
    assert session == "fake-session"
    so = captured["kwargs"]["sess_options"]
    assert so.intra_op_num_threads == 2
    assert so.inter_op_num_threads == 1
    assert so.log_severity_level == 3
    assert captured["kwargs"]["providers"] == ["CPUExecutionProvider"]


def test_get_embedding_function_cache_keyed_by_thread_cap(monkeypatch):
    """Different MEMPAL_MAX_THREADS values must not share a cached EF."""

    class DummyEF:
        def __init__(self, preferred_providers):
            self.preferred_providers = preferred_providers

    built: list = []

    def fake_build(thread_cap=0):
        built.append(thread_cap)
        return DummyEF

    monkeypatch.setattr(embedding, "_build_ef_class", fake_build)
    monkeypatch.setattr(
        embedding, "_resolve_providers", lambda device: (["CPUExecutionProvider"], "cpu")
    )

    monkeypatch.setenv("MEMPAL_MAX_THREADS", "2")
    first = embedding.get_embedding_function("cpu")
    monkeypatch.setenv("MEMPAL_MAX_THREADS", "4")
    second = embedding.get_embedding_function("cpu")

    assert first is not second
    assert built == [2, 4]
