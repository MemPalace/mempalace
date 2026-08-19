import pytest

import mempalace.embedding as embedding


@pytest.fixture(autouse=True)
def isolate_embedding_state(monkeypatch):
    monkeypatch.setattr(embedding, "_EF_CACHE", {})
    monkeypatch.setattr(embedding, "_WARNED", set())
    # Default-off MPS for the onnx-focused tests below so they keep their
    # original meaning even on an Apple Silicon dev machine where the [mps]
    # extra is installed. MPS-specific tests re-enable these explicitly.
    monkeypatch.setattr(embedding, "_torch_mps_available", lambda: False)
    monkeypatch.setattr(embedding, "_sentence_transformers_available", lambda: False)


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


# ---------------------------------------------------------------------------
# embedding -> backend handoff
#
# These live in this module on purpose: conftest's autouse
# ``_stable_embedding_function_for_tests`` replaces
# ``embedding_wrapper._embed_texts`` outright for every other test module, so a
# defect in the real function is invisible there. ``test_embedding`` is in
# ``_REAL_EMBEDDING_TEST_MODULES`` and runs unstubbed.
# ---------------------------------------------------------------------------


class _NumpyEmbeddingFunction:
    """Mimics the real EF contract: a list of float32 ``np.ndarray`` rows.

    Both shipped embedders (ChromaDB's ONNX MiniLM and EmbeddingGemma) return
    numpy arrays, not Python lists — that difference is the whole point here.
    """

    def __init__(self, dim: int = 8):
        self.dim = dim

    def __call__(self, input):
        import numpy as np

        return [np.full(self.dim, 0.1, dtype=np.float32) for _ in list(input or [])]


def test_embed_texts_returns_plain_python_floats(monkeypatch):
    """``list(ndarray)`` yields ``np.float32`` scalars, which ChromaDB rejects.

    Regression for the default (chroma) backend failing every write with
    "Expected embeddings to be a list of floats or ints, a list of lists, a
    numpy array, or a list of numpy arrays" once chroma began declaring
    ``requires_explicit_embeddings`` and routing through EmbeddingCollection.
    """
    from mempalace.backends import embedding_wrapper as ew

    monkeypatch.setattr(
        embedding, "get_embedding_function", lambda *_, **__: _NumpyEmbeddingFunction()
    )

    vectors = ew._embed_texts(["hello", "world"])

    assert len(vectors) == 2
    for row in vectors:
        assert isinstance(row, list)
        assert all(type(x) is float for x in row), f"got {type(row[0])}, not builtin float"


def test_embedding_collection_upsert_accepts_numpy_backed_vectors(tmp_path, monkeypatch):
    """End-to-end: a real Chroma collection must accept what the wrapper emits.

    Asserting on float types alone would not catch a future ChromaDB tightening
    its accepted shapes, so drive an actual upsert + read-back.
    """
    from mempalace.backends.chroma import ChromaBackend
    from mempalace.backends.base import PalaceRef
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    monkeypatch.setattr(
        embedding, "get_embedding_function", lambda *_, **__: _NumpyEmbeddingFunction()
    )

    backend = ChromaBackend()
    palace = tmp_path / "palace"
    ref = PalaceRef(id=str(palace), local_path=str(palace))
    try:
        inner = backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
        col = EmbeddingCollection(inner)

        col.upsert(documents=["verbatim drawer text"], ids=["drawer-1"], metadatas=[{"wing": "w"}])

        assert col.get(ids=["drawer-1"]).documents == ["verbatim drawer text"]
    finally:
        backend.close()


def test_embed_texts_handles_plain_sequence_embedders(monkeypatch):
    """The ``float(x)`` fallback must convert plain sequences, not just ndarrays.

    ``_embed_texts`` branches on ``hasattr(v, "tolist")``. The numpy side is
    covered above, but the fallback exists for embedders that hand back plain
    sequences (custom/BYO EFs, and rows that arrive as tuples), and nothing
    exercised it — so a regression there would surface only in the field, on a
    non-default embedder, as the same ChromaDB ``ValueError``.

    Yields ``Decimal`` rather than ``float`` so the assertion proves a real
    conversion happened rather than passing values through unchanged.
    """
    from decimal import Decimal

    from mempalace.backends import embedding_wrapper as ew

    class _PlainSequenceEmbeddingFunction:
        def __call__(self, input):
            return [(Decimal("0.5"), Decimal("0.25")) for _ in list(input or [])]

    monkeypatch.setattr(
        embedding, "get_embedding_function", lambda *_, **__: _PlainSequenceEmbeddingFunction()
    )

    vectors = ew._embed_texts(["a", "b"])

    assert vectors == [[0.5, 0.25], [0.5, 0.25]]
    for row in vectors:
        assert isinstance(row, list)
        assert all(type(x) is float for x in row), f"got {type(row[0])}, not builtin float"


def test_embed_texts_short_circuits_on_empty_input(monkeypatch):
    """Empty input must return ``[]`` without constructing an embedding function.

    Callers pass empty batches (a drawer set fully filtered by dedup), and
    loading the EF is the expensive part — on the ONNX default it spins up a
    native session. Guards the early return so it cannot be refactored away.
    """
    from mempalace.backends import embedding_wrapper as ew

    def _explode(*_, **__):
        raise AssertionError("get_embedding_function must not be called for an empty batch")

    monkeypatch.setattr(embedding, "get_embedding_function", _explode)

    assert ew._embed_texts([]) == []


# ---------------------------------------------------------------------------
# MPS device — sentence-transformers / torch.mps branch
# ---------------------------------------------------------------------------
#
# Why MPS is its own path: ChromaDB's bundled ONNXMiniLM_L6_V2 enables
# CoreMLExecutionProvider by default, which silently falls back op-by-op
# to CPU for all-MiniLM-L6-v2 on Apple Silicon. The ANE↔CPU copies cost
# more than they save (measured 60-256x slowdown). Routing through
# sentence-transformers + torch.mps bypasses CoreML entirely and runs
# the model directly on the Metal GPU.


def test_mps_explicit_resolves_to_sentinel_when_available(monkeypatch):
    """Explicit device='mps' returns the MPS sentinel when torch+ST+MPS line up."""
    monkeypatch.setattr(embedding, "_torch_mps_available", lambda: True)
    monkeypatch.setattr(embedding, "_sentence_transformers_available", lambda: True)

    providers, effective = embedding._resolve_providers("mps")
    assert providers[0] == embedding._MPS_SENTINEL
    assert effective == "mps"


def test_mps_missing_extra_warns_and_falls_to_cpu(monkeypatch, caplog):
    """device='mps' without the [mps] extra installed -> CPU + actionable warning."""
    monkeypatch.setattr(embedding, "_torch_mps_available", lambda: False)
    monkeypatch.setattr(embedding, "_sentence_transformers_available", lambda: False)

    providers, effective = embedding._resolve_providers("mps")
    assert providers == ["CPUExecutionProvider"]
    assert effective == "cpu"
    assert "mempalace[mps]" in caplog.text


def test_mps_st_installed_but_no_metal_warns(monkeypatch, caplog):
    """ST installed but torch.backends.mps unavailable (e.g. Linux/Intel Mac)."""
    monkeypatch.setattr(embedding, "_sentence_transformers_available", lambda: True)
    monkeypatch.setattr(embedding, "_torch_mps_available", lambda: False)

    providers, effective = embedding._resolve_providers("mps")
    assert providers == ["CPUExecutionProvider"]
    assert effective == "cpu"
    assert "torch.backends.mps" in caplog.text


def test_auto_prefers_mps_over_coreml_when_torch_mps_available(monkeypatch):
    """The whole point of this PR: MPS wins on Apple Silicon, even if CoreML works."""
    monkeypatch.setattr(embedding, "_torch_mps_available", lambda: True)
    monkeypatch.setattr(embedding, "_sentence_transformers_available", lambda: True)
    monkeypatch.setattr(
        "onnxruntime.get_available_providers",
        lambda: ["CoreMLExecutionProvider", "CPUExecutionProvider"],
    )

    providers, effective = embedding._resolve_providers("auto")
    assert effective == "mps"
    assert providers[0] == embedding._MPS_SENTINEL


def test_auto_falls_to_coreml_when_torch_unavailable(monkeypatch):
    """Without the [mps] extra, auto on Apple Silicon should still use CoreML."""
    monkeypatch.setattr(embedding, "_torch_mps_available", lambda: False)
    monkeypatch.setattr(embedding, "_sentence_transformers_available", lambda: False)
    monkeypatch.setattr(
        "onnxruntime.get_available_providers",
        lambda: ["CoreMLExecutionProvider", "CPUExecutionProvider"],
    )

    providers, effective = embedding._resolve_providers("auto")
    assert effective == "coreml"
    assert providers == ["CoreMLExecutionProvider", "CPUExecutionProvider"]


def test_get_embedding_function_routes_mps_to_st_branch(monkeypatch):
    """When the resolver returns the MPS sentinel, get_embedding_function must
    take the sentence-transformers path (``_build_mps_ef``), not the ONNX path."""

    class DummySTEF:
        pass

    onnx_called = False

    def fake_build_ef_class():
        nonlocal onnx_called
        onnx_called = True

        class _Dummy:
            def __init__(self, **_):
                pass

        return _Dummy

    monkeypatch.setattr(embedding, "_build_ef_class", fake_build_ef_class)
    monkeypatch.setattr(embedding, "_build_mps_ef", lambda: DummySTEF())
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device: ([embedding._MPS_SENTINEL, "CPUExecutionProvider"], "mps"),
    )

    ef = embedding.get_embedding_function("mps")
    assert isinstance(ef, DummySTEF)
    assert onnx_called is False  # ONNX class was never built — proves routing


def test_get_embedding_function_caches_mps_branch(monkeypatch):
    """The MPS branch must hit the same ``_EF_CACHE`` as the ONNX branch."""

    class DummySTEF:
        pass

    monkeypatch.setattr(embedding, "_build_mps_ef", lambda: DummySTEF())
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device: ([embedding._MPS_SENTINEL, "CPUExecutionProvider"], "mps"),
    )

    a = embedding.get_embedding_function("mps")
    b = embedding.get_embedding_function("mps")
    assert a is b


def test_embeddinggemma_mps_falls_back_to_cpu_onnx(monkeypatch, caplog):
    """MPS must never build MiniLM vectors for an EmbeddingGemma palace."""
    captured = {}

    class DummyGemma:
        def __init__(self, preferred_providers=None, intra_op_num_threads=0):
            captured["providers"] = preferred_providers

    monkeypatch.setattr(embedding, "EmbeddinggemmaONNX", DummyGemma)
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device: ([embedding._MPS_SENTINEL, "CPUExecutionProvider"], "mps"),
    )
    monkeypatch.setattr(
        embedding,
        "_build_mps_ef",
        lambda: (_ for _ in ()).throw(AssertionError("must not build MiniLM MPS EF")),
    )

    ef = embedding.get_embedding_function("mps", "embeddinggemma")

    assert isinstance(ef, DummyGemma)
    assert captured["providers"] == ["CPUExecutionProvider"]
    assert "using CPU" in caplog.text


def test_describe_device_reports_model_compatible_mps_fallback(monkeypatch):
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device: ([embedding._MPS_SENTINEL, "CPUExecutionProvider"], "mps"),
    )

    assert embedding.describe_device("mps", "minilm") == "mps"
    assert embedding.describe_device("mps", "embeddinggemma") == "cpu"


def test_unknown_device_does_not_match_mps_sentinel(monkeypatch, caplog):
    """Regression guard: arbitrary unknown strings must not accidentally route to MPS."""
    monkeypatch.setattr("onnxruntime.get_available_providers", lambda: ["CPUExecutionProvider"])
    providers, effective = embedding._resolve_providers("__mempalace_torch_mps__")
    assert effective == "cpu"
    assert providers == ["CPUExecutionProvider"]


def test_torch_mps_available_returns_bool_without_torch(monkeypatch):
    """Helper must not raise when torch is missing — important for non-Apple CI."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("no torch in this venv")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert embedding._torch_mps_available() is False
