"""Offline tests for EmbeddinggemmaONNX.

The real ONNX model is ~300 MB and pulled from HuggingFace on first use, so
these tests mock huggingface_hub.hf_hub_download, tokenizers.Tokenizer, and
onnxruntime.InferenceSession to keep CI fast and network-free.

Skipped when the multilingual extra isn't installed (huggingface_hub/
tokenizers/numpy) — CI runs only core deps by default.
"""

import sys
import threading
import time

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("huggingface_hub")
pytest.importorskip("tokenizers")

import mempalace.embedding as embedding  # noqa: E402  (after importorskip)


@pytest.fixture(autouse=True)
def isolate_embedding_state(monkeypatch):
    monkeypatch.setattr(embedding, "_EF_CACHE", {})
    monkeypatch.setattr(embedding, "_WARNED", set())


def _make_fake_session(out_dim=768):
    """Fake onnxruntime InferenceSession that returns a deterministic tensor.

    Shape: (batch, out_dim). The values aren't important — tests check shape,
    truncation, and L2-normalization, not numerical correctness.
    """

    class _Output:
        def __init__(self, name):
            self.name = name

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        def get_outputs(self):
            return [_Output("last_hidden_state"), _Output("sentence_embedding")]

        def run(self, _output_names, feed):
            batch = feed["input_ids"].shape[0]
            # Deterministic non-trivial values so L2-norm isn't degenerate.
            sent = np.arange(batch * out_dim, dtype=np.float32).reshape(batch, out_dim) + 1.0
            last_hidden = np.zeros((batch, feed["input_ids"].shape[1], out_dim), dtype=np.float32)
            return [last_hidden, sent]

    return _Session


class _FakeTokenizer:
    """Stand-in for tokenizers.Tokenizer with the methods _lazy_load uses."""

    def __init__(self):
        self._padding_enabled = False
        self._truncation_enabled = False
        self._truncation_max = None

    def enable_padding(self):
        self._padding_enabled = True

    def enable_truncation(self, max_length):
        self._truncation_enabled = True
        self._truncation_max = max_length

    def encode_batch(self, texts):
        class _Enc:
            def __init__(self, n):
                self.ids = [0] * n
                self.attention_mask = [1] * n

        # Same fixed length per batch — real tokenizers pad to the longest.
        max_len = max(len(t.split()) for t in texts)
        return [_Enc(max_len) for _ in texts]


@pytest.fixture
def patched_lazy_load(monkeypatch):
    """Patch the third-party deps imported inside EmbeddinggemmaONNX._lazy_load.

    Returns a dict of recording counters so tests can assert how many times
    each was called (e.g. confirm lazy-load caches after first call).
    """
    calls = {"hf_hub_download": 0, "InferenceSession": 0, "Tokenizer.from_file": 0}

    def fake_download(repo, filename=None, subfolder=None, **kwargs):
        calls["hf_hub_download"] += 1
        return f"/tmp/fake/{subfolder or ''}/{filename}"

    fake_session_cls = _make_fake_session()

    def fake_session_ctor(*args, **kwargs):
        calls["InferenceSession"] += 1
        return fake_session_cls()

    def fake_tokenizer_from_file(_path):
        calls["Tokenizer.from_file"] += 1
        return _FakeTokenizer()

    # huggingface_hub and tokenizers are real packages (installed via the
    # multilingual extra), so we patch the functions in place rather than
    # injecting stub modules.
    import huggingface_hub
    import onnxruntime
    import tokenizers

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    monkeypatch.setattr(onnxruntime, "InferenceSession", fake_session_ctor)
    monkeypatch.setattr(tokenizers.Tokenizer, "from_file", staticmethod(fake_tokenizer_from_file))

    return calls


def test_name_is_stable():
    """ChromaDB persists this on the collection — changing it breaks reads."""
    assert embedding.EmbeddinggemmaONNX.name() == "embeddinggemma_300m"


def test_lazy_load_runs_once(patched_lazy_load):
    ef = embedding.EmbeddinggemmaONNX()
    ef(["one"])
    ef(["two"])
    ef(["three"])
    assert patched_lazy_load["hf_hub_download"] == 3  # model + weights + tokenizer, once
    assert patched_lazy_load["InferenceSession"] == 1
    assert patched_lazy_load["Tokenizer.from_file"] == 1


def test_output_shape_is_truncated_to_384(patched_lazy_load):
    ef = embedding.EmbeddinggemmaONNX()
    out = ef(["one", "two", "three"])
    arr = np.asarray(out)
    assert arr.shape == (3, 384), f"expected (3, 384) after MRL truncation, got {arr.shape}"


def test_output_is_l2_normalized(patched_lazy_load):
    ef = embedding.EmbeddinggemmaONNX()
    out = ef(["hello world", "another sentence"])
    arr = np.asarray(out)
    norms = np.linalg.norm(arr, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5), f"vectors not unit-norm: {norms}"


def test_prefix_is_applied(patched_lazy_load, monkeypatch):
    captured = []
    original_encode_batch = _FakeTokenizer.encode_batch

    def fake_encode_batch(self, texts):
        captured.extend(texts)
        return original_encode_batch(self, texts)

    monkeypatch.setattr(_FakeTokenizer, "encode_batch", fake_encode_batch)
    ef = embedding.EmbeddinggemmaONNX()
    ef(["raw text one", "raw text two"])
    assert all(t.startswith("task: sentence similarity | query: ") for t in captured)
    # And the raw text is preserved after the prefix.
    assert any("raw text one" in t for t in captured)


def test_call_chunks_large_batches(patched_lazy_load, monkeypatch):
    """A large input must be tokenized and run in bounded sub-batches.

    One unchunked session.run over a repair-scale batch (5000 docs) allocates
    attention buffers beyond available RAM and the kernel kills the process
    (#1770) — so __call__ may never see more than _EMBEDDINGGEMMA_BATCH_SIZE
    docs per forward pass.
    """
    batch_sizes = []
    captured_texts = []
    original_encode_batch = _FakeTokenizer.encode_batch

    def recording_encode_batch(self, texts):
        batch_sizes.append(len(texts))
        captured_texts.extend(texts)
        return original_encode_batch(self, texts)

    monkeypatch.setattr(_FakeTokenizer, "encode_batch", recording_encode_batch)
    ef = embedding.EmbeddinggemmaONNX()
    n = embedding._EMBEDDINGGEMMA_BATCH_SIZE * 2 + 6
    docs = [f"doc {i}" for i in range(n)]
    out = ef(docs)

    assert batch_sizes == [
        embedding._EMBEDDINGGEMMA_BATCH_SIZE,
        embedding._EMBEDDINGGEMMA_BATCH_SIZE,
        6,
    ], f"expected bounded sub-batches, got {batch_sizes}"
    # Sub-batches must cover the input in order; combined with the per-chunk
    # extend in __call__ this pins output row order to input order.
    assert captured_texts == [embedding._EMBEDDINGGEMMA_PREFIX + d for d in docs]
    arr = np.asarray(out)
    assert arr.shape == (n, 384), f"chunked outputs must concatenate to (n, 384), got {arr.shape}"
    assert np.allclose(np.linalg.norm(arr, axis=1), 1.0, atol=1e-5)


_B = 32  # mirrors _EMBEDDINGGEMMA_BATCH_SIZE; literal so the cases read plainly


@pytest.mark.parametrize(
    ("n", "expected_batches"),
    [
        (1, [1]),
        (_B, [_B]),
        (_B + 1, [_B, 1]),
        (2 * _B, [_B, _B]),
    ],
)
def test_call_chunk_boundaries(patched_lazy_load, monkeypatch, n, expected_batches):
    """Exact-multiple and off-by-one inputs produce no empty or oversized runs."""
    assert _B == embedding._EMBEDDINGGEMMA_BATCH_SIZE, "update _B alongside the constant"
    batch_sizes = []
    original_encode_batch = _FakeTokenizer.encode_batch

    def recording_encode_batch(self, texts):
        batch_sizes.append(len(texts))
        return original_encode_batch(self, texts)

    monkeypatch.setattr(_FakeTokenizer, "encode_batch", recording_encode_batch)
    ef = embedding.EmbeddinggemmaONNX()
    out = ef([f"doc {i}" for i in range(n)])
    assert batch_sizes == expected_batches
    assert len(out) == n


def test_custom_batch_size_is_honored(patched_lazy_load, monkeypatch):
    """The constructor knob must drive the sub-batch split."""
    batch_sizes = []
    original_encode_batch = _FakeTokenizer.encode_batch

    def recording_encode_batch(self, texts):
        batch_sizes.append(len(texts))
        return original_encode_batch(self, texts)

    monkeypatch.setattr(_FakeTokenizer, "encode_batch", recording_encode_batch)
    ef = embedding.EmbeddinggemmaONNX(batch_size=10)
    out = ef([f"doc {i}" for i in range(24)])
    assert batch_sizes == [10, 10, 4]
    assert len(out) == 24


def test_batch_size_below_one_is_rejected():
    """A zero or negative batch size would loop forever or embed nothing."""
    with pytest.raises(ValueError, match="batch_size"):
        embedding.EmbeddinggemmaONNX(batch_size=0)
    with pytest.raises(ValueError, match="batch_size"):
        embedding.EmbeddinggemmaONNX(batch_size=-3)


def test_call_empty_input_returns_empty(patched_lazy_load):
    """Zero docs must yield zero embeddings without loading the model."""
    ef = embedding.EmbeddinggemmaONNX()
    assert ef([]) == []
    assert ef(None) == []
    assert patched_lazy_load["hf_hub_download"] == 0, "empty input must not trigger the download"


def test_call_bare_string_is_wrapped(patched_lazy_load):
    """A single string is one document, not a sequence of characters."""
    ef = embedding.EmbeddinggemmaONNX()
    out = ef("standalone document")
    assert np.asarray(out).shape == (1, 384)


def test_concurrent_first_calls_load_model_once(patched_lazy_load, monkeypatch):
    """Cold concurrent calls must build exactly one session.

    Instances are shared across threads via _EF_CACHE; without the load
    lock, two cold callers would transiently hold two full model sessions.
    """
    import huggingface_hub

    fixture_download = huggingface_hub.hf_hub_download

    def slow_download(*args, **kwargs):
        time.sleep(0.05)  # widen the race window the lock must close
        return fixture_download(*args, **kwargs)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", slow_download)

    ef = embedding.EmbeddinggemmaONNX()
    barrier = threading.Barrier(2)
    results = [None, None]

    def worker(slot):
        barrier.wait(timeout=5)
        results[slot] = ef([f"doc {slot}"])

    threads = [threading.Thread(target=worker, args=(slot,)) for slot in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert patched_lazy_load["InferenceSession"] == 1
    assert all(r is not None and len(r) == 1 for r in results)


def test_concurrent_get_embedding_function_single_instance(monkeypatch):
    """Concurrent cache misses must converge on one shared EF instance.

    The instance-level load lock is not enough on its own: if the factory's
    check-then-construct is unsynchronized, each thread keeps its own
    instance and each one later loads its own copy of the model.
    """
    monkeypatch.setattr(
        embedding, "_resolve_providers", lambda device: (["CPUExecutionProvider"], "cpu")
    )
    barrier = threading.Barrier(2)
    instances = [None, None]

    def worker(slot):
        barrier.wait(timeout=5)
        instances[slot] = embedding.get_embedding_function(device="cpu", model="embeddinggemma")

    threads = [threading.Thread(target=worker, args=(slot,)) for slot in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert instances[0] is not None, "worker thread did not complete"
    assert instances[0] is instances[1], "factory must hand every thread the same EF"


def test_get_embedding_function_dispatches_to_embeddinggemma(monkeypatch):
    """model='embeddinggemma' must build EmbeddinggemmaONNX, not the MiniLM EF."""
    monkeypatch.setattr(
        embedding, "_resolve_providers", lambda device: (["CPUExecutionProvider"], "cpu")
    )
    ef = embedding.get_embedding_function(device="cpu", model="embeddinggemma")
    assert isinstance(ef, embedding.EmbeddinggemmaONNX)
    assert ef.name() == "embeddinggemma_300m"


def test_cache_key_separates_models(monkeypatch):
    """Switching model must not return the cached EF for the other model.

    The cache key changed from `providers` to `(model, providers)` for exactly
    this reason — without it, the second call would silently reuse the wrong EF.
    """

    class DummyMiniLM:
        def __init__(self, preferred_providers=None):
            self.kind = "minilm"

    monkeypatch.setattr(embedding, "_build_ef_class", lambda: DummyMiniLM)
    monkeypatch.setattr(
        embedding, "_resolve_providers", lambda device: (["CPUExecutionProvider"], "cpu")
    )

    ml = embedding.get_embedding_function(device="cpu", model="minilm")
    eg = embedding.get_embedding_function(device="cpu", model="embeddinggemma")
    ml_again = embedding.get_embedding_function(device="cpu", model="minilm")

    assert ml is ml_again, "minilm should cache-hit on second call"
    assert isinstance(eg, embedding.EmbeddinggemmaONNX), (
        "embeddinggemma should not collide with minilm cache"
    )
    assert ml is not eg


def test_missing_deps_raise_helpful_error(monkeypatch):
    """Multilingual deps now ship in core, but if a user ends up with a broken
    install (uninstalled tokenizers, incompatible pin, etc.) the error should
    tell them how to recover rather than spilling a bare ImportError."""

    # Simulate a user with a broken install: drop tokenizers from sys.modules
    # and block re-import. huggingface_hub and onnxruntime stay importable.
    monkeypatch.setitem(sys.modules, "tokenizers", None)

    ef = embedding.EmbeddinggemmaONNX()
    with pytest.raises(ImportError, match=r"pip install.*mempalace"):
        ef(["anything"])


def test_config_embedding_model_env_override(monkeypatch):
    """MEMPALACE_EMBEDDING_MODEL env var must override the config file default."""
    from mempalace.config import MempalaceConfig

    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "embeddinggemma")
    assert MempalaceConfig().embedding_model == "embeddinggemma"

    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "MiniLM")  # case-insensitive
    assert MempalaceConfig().embedding_model == "minilm"


def test_config_embedding_model_default_is_minilm(monkeypatch):
    """Back-compat: existing installs without explicit config get minilm."""
    from mempalace.config import MempalaceConfig

    monkeypatch.delenv("MEMPALACE_EMBEDDING_MODEL", raising=False)
    assert MempalaceConfig().embedding_model == "minilm"
