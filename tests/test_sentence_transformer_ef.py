"""Offline tests for the sentence-transformers embedding path (#442).

``sentence-transformers`` is an optional dependency (the ``[multilingual]``
extra), so everything here stubs it via ``sys.modules`` — the suite must pass
on a stock ``[dev]`` environment with no torch installed.
"""

import sys
import types

import pytest

import mempalace.embedding as embedding


@pytest.fixture(autouse=True)
def isolate_embedding_state(monkeypatch):
    monkeypatch.setattr(embedding, "_EF_CACHE", {})
    monkeypatch.setattr(embedding, "_WARNED", set())
    monkeypatch.setattr(embedding, "_DIM_CACHE", {})


@pytest.fixture
def stub_sentence_transformers(monkeypatch):
    """Make ``import sentence_transformers`` succeed without the real package."""
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", types.ModuleType("sentence_transformers")
    )


@pytest.fixture
def no_sentence_transformers(monkeypatch):
    """Make ``import sentence_transformers`` fail even when installed."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)


@pytest.fixture
def recording_st_ef(monkeypatch):
    """Replace chromadb's SentenceTransformerEmbeddingFunction with a recorder."""
    calls = []

    class _FakeSTEF:
        def __init__(self, model_name=None, device=None, **kwargs):
            calls.append({"model_name": model_name, "device": device, **kwargs})
            self.model_name = model_name
            self.device = device

        def __call__(self, input):  # noqa: A002 — ChromaDB EF protocol
            return [[0.1, 0.2, 0.3] for _ in input]

    import chromadb.utils.embedding_functions as ef_mod

    monkeypatch.setattr(ef_mod, "SentenceTransformerEmbeddingFunction", _FakeSTEF, raising=False)
    return calls


def _fake_torch(monkeypatch, *, cuda=False, mps=False):
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps))
    monkeypatch.setitem(sys.modules, "torch", torch)


# ── is_sentence_transformer_model ──────────────────────────────────────


def test_builtin_names_are_not_st_models():
    assert not embedding.is_sentence_transformer_model("minilm")
    assert not embedding.is_sentence_transformer_model("embeddinggemma")
    assert not embedding.is_sentence_transformer_model("")
    assert not embedding.is_sentence_transformer_model(None)
    # Config lowercases before this is reached, but direct callers may not.
    assert not embedding.is_sentence_transformer_model("  MiniLM ")


def test_hf_repo_ids_are_st_models():
    assert embedding.is_sentence_transformer_model("intfloat/multilingual-e5-base")
    assert embedding.is_sentence_transformer_model("baai/bge-m3")


# ── ensure_model_dependencies ──────────────────────────────────────────


def test_ensure_deps_noop_for_builtin(no_sentence_transformers):
    embedding.ensure_model_dependencies("minilm")
    embedding.ensure_model_dependencies("embeddinggemma")
    embedding.ensure_model_dependencies(None)


def test_ensure_deps_raises_actionable_import_error(no_sentence_transformers):
    with pytest.raises(ImportError, match=r"mempalace\[multilingual\]"):
        embedding.ensure_model_dependencies("intfloat/multilingual-e5-base")


def test_ensure_deps_raises_dedicated_subclass(no_sentence_transformers):
    # Error arms in mcp_server / searcher / layers match this class so that
    # unrelated ImportErrors (e.g. a missing backend driver) are not
    # mislabeled with the [multilingual] hint.
    with pytest.raises(embedding.EmbeddingDependencyError):
        embedding.ensure_model_dependencies("intfloat/multilingual-e5-base")


def test_missing_dependency_fails_loud_not_silent(no_sentence_transformers):
    # Pre-#442 behavior for unrecognized model names was a silent fallback to
    # the default EF — the palace was then embedded with a different model
    # than config (and the identity sidecar) claimed.
    with pytest.raises(ImportError, match="sentence-transformers"):
        embedding.get_embedding_function(device="cpu", model="baai/bge-m3")


# ── dispatch + caching ─────────────────────────────────────────────────


def test_dispatch_builds_st_ef(stub_sentence_transformers, recording_st_ef, monkeypatch):
    monkeypatch.setattr(embedding, "_resolve_torch_device", lambda device: "cpu")

    ef = embedding.get_embedding_function(device="cpu", model="intfloat/multilingual-e5-base")

    # Construction is lazy — no model load at EF resolution (collection-open
    # paths like MCP status / hooks must stay inside the startup budgets).
    assert recording_st_ef == []
    # chromadb persists this on the collection and calls it at open — it must
    # not trigger the load, and must match the real chromadb class name.
    assert ef.name() == "sentence_transformer"
    assert recording_st_ef == []

    vectors = ef(["hello"])
    assert recording_st_ef == [{"model_name": "intfloat/multilingual-e5-base", "device": "cpu"}]
    assert vectors == [[0.1, 0.2, 0.3]]


def test_st_ef_loads_once_across_calls(stub_sentence_transformers, recording_st_ef, monkeypatch):
    monkeypatch.setattr(embedding, "_resolve_torch_device", lambda device: "cpu")

    ef = embedding.get_embedding_function(device="cpu", model="intfloat/multilingual-e5-base")
    ef(["a"])
    ef.embed_query(["b"])
    ef.embed_documents(["c"])
    assert len(recording_st_ef) == 1


def test_st_ef_load_failure_raises_model_error(stub_sentence_transformers, monkeypatch):
    # A bad model id fails with OSError/RepositoryNotFoundError — not
    # ImportError — and must surface as an actionable EmbeddingModelError,
    # never a silent fallback to the default model.
    import chromadb.utils.embedding_functions as ef_mod

    class _Boom:
        def __init__(self, **kwargs):
            raise OSError("Repository Not Found")

    monkeypatch.setattr(ef_mod, "SentenceTransformerEmbeddingFunction", _Boom, raising=False)
    monkeypatch.setattr(embedding, "_resolve_torch_device", lambda device: "cpu")

    ef = embedding.get_embedding_function(device="cpu", model="no-such/model")
    with pytest.raises(embedding.EmbeddingModelError, match="no-such/model"):
        ef(["x"])


def test_st_ef_cached_per_model_and_device(
    stub_sentence_transformers, recording_st_ef, monkeypatch
):
    monkeypatch.setattr(embedding, "_resolve_torch_device", lambda device: "cpu")

    a1 = embedding.get_embedding_function(device="cpu", model="intfloat/multilingual-e5-base")
    a2 = embedding.get_embedding_function(device="cpu", model="intfloat/multilingual-e5-base")
    b = embedding.get_embedding_function(device="cpu", model="baai/bge-m3")

    assert a1 is a2
    assert a1 is not b


def test_builtin_dispatch_unaffected(monkeypatch):
    # minilm still routes to the ONNX path — no sentence-transformers needed.
    built = {}

    class _FakeONNX:
        def __init__(self, preferred_providers=None, intra_op_num_threads=0):
            built["providers"] = preferred_providers

    monkeypatch.setattr(embedding, "_build_ef_class", lambda: _FakeONNX)

    ef = embedding.get_embedding_function(device="cpu", model="minilm")

    assert isinstance(ef, _FakeONNX)
    assert built["providers"] == ["CPUExecutionProvider"]


def test_probe_dimension_works_for_st_models(
    stub_sentence_transformers, recording_st_ef, monkeypatch
):
    monkeypatch.setattr(embedding, "_resolve_torch_device", lambda device: "cpu")

    assert embedding.probe_dimension(device="cpu", model="intfloat/multilingual-e5-base") == 3


def test_current_model_name_matches_config_normalization():
    # Identity comparisons must use the exact normalization config uses:
    # builtins lowercase, ST ids / checkpoint paths verbatim. Otherwise an
    # identity recorded from config and one derived from an explicit model
    # argument could disagree on case alone → false mismatch.
    assert embedding.current_model_name("BAAI/bge-m3") == "BAAI/bge-m3"
    assert embedding.current_model_name("  MiniLM ") == "minilm"
    assert embedding.current_model_name("EmbeddingGemma") == "embeddinggemma"


# ── _resolve_torch_device ──────────────────────────────────────────────


def test_torch_device_auto_prefers_cuda(monkeypatch):
    _fake_torch(monkeypatch, cuda=True, mps=True)
    assert embedding._resolve_torch_device("auto") == "cuda"


def test_torch_device_auto_mps_requires_arm64(monkeypatch):
    _fake_torch(monkeypatch, cuda=False, mps=True)
    monkeypatch.setattr(embedding.platform, "machine", lambda: "arm64")
    assert embedding._resolve_torch_device("auto") == "mps"
    # Intel Macs report mps.is_available() == True but MPS does not work.
    monkeypatch.setattr(embedding.platform, "machine", lambda: "x86_64")
    assert embedding._resolve_torch_device("auto") == "cpu"


def test_torch_device_coreml_maps_to_mps(monkeypatch):
    _fake_torch(monkeypatch, cuda=False, mps=True)
    monkeypatch.setattr(embedding.platform, "machine", lambda: "arm64")
    assert embedding._resolve_torch_device("coreml") == "mps"


def test_torch_device_unavailable_falls_back_to_cpu_with_warning(monkeypatch, caplog):
    _fake_torch(monkeypatch, cuda=False, mps=False)
    with caplog.at_level("WARNING"):
        assert embedding._resolve_torch_device("cuda") == "cpu"
    assert "falling back to CPU" in caplog.text


def test_torch_device_dml_falls_back_to_cpu(monkeypatch, caplog):
    _fake_torch(monkeypatch, cuda=True, mps=False)
    with caplog.at_level("WARNING"):
        assert embedding._resolve_torch_device("dml") == "cpu"
    assert "falling back to CPU" in caplog.text


def test_torch_device_warns_once(monkeypatch, caplog):
    _fake_torch(monkeypatch, cuda=False, mps=False)
    with caplog.at_level("WARNING"):
        embedding._resolve_torch_device("cuda")
        embedding._resolve_torch_device("cuda")
    assert caplog.text.count("falling back to CPU") == 1


def test_torch_missing_means_cpu(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    assert embedding._resolve_torch_device("auto") == "cpu"


def test_torch_device_explicit_cpu_stays_cpu_without_warning(monkeypatch, caplog):
    _fake_torch(monkeypatch, cuda=True, mps=True)
    with caplog.at_level("WARNING"):
        assert embedding._resolve_torch_device("cpu") == "cpu"
    assert "falling back" not in caplog.text


# ── describe_device ────────────────────────────────────────────────────


def test_describe_device_st_model_reports_torch_device(monkeypatch):
    _fake_torch(monkeypatch, cuda=True)
    assert embedding.describe_device("auto", model="baai/bge-m3") == "cuda"


def test_describe_device_builtin_model_reports_onnx_device(monkeypatch):
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device, model=None: (["CPUExecutionProvider"], "cpu"),
    )
    assert embedding.describe_device("auto", model="minilm") == "cpu"


# ── chroma resolver must not swallow embedding errors ──────────────────


def test_chroma_resolver_reraises_dependency_error(monkeypatch):
    from mempalace.backends.chroma import ChromaBackend

    def _boom():
        raise embedding.EmbeddingDependencyError("pip install 'mempalace[multilingual]'")

    monkeypatch.setattr(embedding, "get_embedding_function", _boom)
    with pytest.raises(embedding.EmbeddingDependencyError, match="multilingual"):
        ChromaBackend._resolve_embedding_function()


def test_chroma_resolver_reraises_model_error(monkeypatch):
    from mempalace.backends.chroma import ChromaBackend

    def _boom():
        raise embedding.EmbeddingModelError("Failed to load sentence-transformers model")

    monkeypatch.setattr(embedding, "get_embedding_function", _boom)
    with pytest.raises(embedding.EmbeddingModelError):
        ChromaBackend._resolve_embedding_function()


def test_chroma_resolver_still_swallows_other_errors(monkeypatch):
    from mempalace.backends.chroma import ChromaBackend

    def _boom():
        raise RuntimeError("anything else")

    monkeypatch.setattr(embedding, "get_embedding_function", _boom)
    assert ChromaBackend._resolve_embedding_function() is None


def test_chroma_resolver_swallows_unrelated_import_errors(monkeypatch):
    # A bare ImportError that is NOT the embedding-dependency error (e.g. a
    # broken transitive import) keeps the pre-existing fallback-to-default
    # behavior instead of being mislabeled with the [multilingual] hint.
    from mempalace.backends.chroma import ChromaBackend

    def _boom():
        raise ImportError("No module named 'somethingelse'")

    monkeypatch.setattr(embedding, "get_embedding_function", _boom)
    assert ChromaBackend._resolve_embedding_function() is None


# ── MCP server surfaces the install hint, not "Backend open failed" ────


def test_mcp_get_collection_reports_missing_dependency(monkeypatch, tmp_path):
    import mempalace.mcp_server as mcp_server
    from mempalace.backends.chroma import ChromaBackend

    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_client_cache", None)
    monkeypatch.setattr(mcp_server, "_collection_cache", None)
    monkeypatch.setattr(mcp_server, "_collection_open_error", None)
    monkeypatch.setattr(mcp_server, "_get_client", lambda: object())

    def _boom():
        raise embedding.EmbeddingDependencyError(
            "embedding_model='baai/bge-m3' requires the sentence-transformers package"
        )

    monkeypatch.setattr(ChromaBackend, "_resolve_embedding_function", staticmethod(_boom))

    assert mcp_server._get_collection(create=True) is None
    err = mcp_server._collection_open_error
    assert err is not None
    assert err["error"] == "Embedding dependency missing"
    assert "sentence-transformers" in err["details"]
    assert "mempalace[multilingual]" in err["hint"]


def test_mcp_get_collection_missing_dependency_generic_backend(monkeypatch, tmp_path):
    # The non-chroma branch routes through palace.get_collection; its
    # EmbeddingDependencyError arm must surface the same error shape.
    import mempalace.mcp_server as mcp_server
    import mempalace.palace as palace

    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_selected_backend_name", lambda: "sqlite_exact")
    monkeypatch.setattr(mcp_server, "_collection_cache", None)
    monkeypatch.setattr(mcp_server, "_collection_open_error", None)

    def _boom(*args, **kwargs):
        raise embedding.EmbeddingDependencyError(
            "embedding_model='baai/bge-m3' requires the sentence-transformers package"
        )

    monkeypatch.setattr(palace, "get_collection", _boom)

    assert mcp_server._get_collection(create=True) is None
    err = mcp_server._collection_open_error
    assert err is not None
    assert err["error"] == "Embedding dependency missing"
    assert "sentence-transformers" in err["details"]
    assert "mempalace[multilingual]" in err["hint"]


def test_mcp_get_collection_unrelated_import_error_not_mislabeled(monkeypatch, tmp_path):
    # A missing backend driver (e.g. pymilvus) raises plain ImportError and
    # must NOT be labeled with the [multilingual] hint.
    import mempalace.mcp_server as mcp_server
    import mempalace.palace as palace

    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_selected_backend_name", lambda: "milvus")
    monkeypatch.setattr(mcp_server, "_collection_cache", None)
    monkeypatch.setattr(mcp_server, "_collection_open_error", None)

    def _boom(*args, **kwargs):
        raise ImportError("No module named 'pymilvus'")

    monkeypatch.setattr(palace, "get_collection", _boom)

    assert mcp_server._get_collection(create=True) is None
    err = mcp_server._collection_open_error
    assert err is not None
    assert err["error"] != "Embedding dependency missing"


# ── MCP search path surfaces the install hint (#442 review) ────────────


def test_search_memories_surfaces_missing_dependency(monkeypatch, tmp_path):
    import mempalace.searcher as searcher

    def _boom(*args, **kwargs):
        raise embedding.EmbeddingDependencyError(
            "embedding_model='baai/bge-m3' requires the sentence-transformers package"
        )

    monkeypatch.setattr(searcher, "get_collection", _boom)

    result = searcher.search_memories("query", str(tmp_path))
    assert result["error"] == "Embedding dependency missing"
    assert "sentence-transformers" in result["details"]
    assert "mempalace[multilingual]" in result["hint"]


# ── wake-up layers name the real cause, not "No palace found" ──────────


def test_layer1_names_missing_dependency(monkeypatch, tmp_path):
    import mempalace.layers as layers

    def _boom(*args, **kwargs):
        raise embedding.EmbeddingDependencyError(
            "embedding_model='baai/bge-m3' requires the sentence-transformers package"
        )

    monkeypatch.setattr(layers, "_get_collection", _boom)

    text = layers.Layer1(palace_path=str(tmp_path)).generate()
    assert "Palace unavailable" in text
    assert "sentence-transformers" in text
    assert "No palace found" not in text


# ── config normalization: builtins lowercase, everything else verbatim ──


def test_config_preserves_case_for_st_models(tmp_path):
    from mempalace.config import MempalaceConfig

    cfg = MempalaceConfig(config_dir=tmp_path)
    cfg.set_embedding_model("BAAI/bge-m3")
    # Local checkpoint paths are case-sensitive on Linux — the value must
    # round-trip verbatim for non-builtin ids.
    assert cfg.embedding_model == "BAAI/bge-m3"


def test_config_still_lowercases_builtin_names(tmp_path):
    from mempalace.config import MempalaceConfig

    cfg = MempalaceConfig(config_dir=tmp_path)
    cfg.set_embedding_model("  MiniLM ")
    assert cfg.embedding_model == "minilm"
    cfg.set_embedding_model("EmbeddingGemma")
    assert cfg.embedding_model == "embeddinggemma"


def test_env_var_case_preserved_for_st_models(monkeypatch, tmp_path):
    from mempalace.config import MempalaceConfig

    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "/Models/MyCheckpoint")
    assert MempalaceConfig(config_dir=tmp_path).embedding_model == "/Models/MyCheckpoint"
