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

    def get_vocab_size(self, with_added_tokens=True):
        return 262145 if with_added_tokens else 262144

    def token_to_id(self, token):
        return 3 if token == "<unk>" else None

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
    # Descending sizes, so size-sorted order is the reverse of arrival order
    # and the assertion below cannot pass on both.
    docs = [f"{'x' * (n - i)} doc {i}" for i in range(n)]
    out = ef(docs)

    assert batch_sizes == [
        embedding._EMBEDDINGGEMMA_BATCH_SIZE,
        embedding._EMBEDDINGGEMMA_BATCH_SIZE,
        6,
    ], f"expected bounded sub-batches, got {batch_sizes}"
    # Sub-batches cover the input in size-sorted order; the scatter in
    # __call__ puts every row back at its own input index afterwards.
    ordered = sorted(docs, key=lambda d: len(d.encode("utf-8")))
    assert ordered != docs, "fixture must not already be in size order"
    assert captured_texts == [embedding._EMBEDDINGGEMMA_PREFIX + d for d in ordered]
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


# Bound before any test can monkeypatch the method, so the width helper
# below measures with the real fake and never re-enters a recorder.
_UNPATCHED_ENCODE_BATCH = _FakeTokenizer.encode_batch


def _record_batches(monkeypatch, sink):
    """Capture the texts handed to each encode_batch call, in order."""

    def recording_encode_batch(self, texts):
        sink.append(list(texts))
        return _UNPATCHED_ENCODE_BATCH(self, texts)

    monkeypatch.setattr(_FakeTokenizer, "encode_batch", recording_encode_batch)


def _fake_padded_width(batches):
    """Total padded token slots the fake tokenizer produces for `batches`.

    Measured by encoding, not by re-deriving the fake's padding rule, so the
    two cannot drift apart and quietly turn the assertion into a tautology.
    """
    tokenizer = _FakeTokenizer()
    return sum(sum(len(e.ids) for e in _UNPATCHED_ENCODE_BATCH(tokenizer, b)) for b in batches)


def test_call_groups_documents_by_size(patched_lazy_load, monkeypatch):
    """Similar-size documents must share a sub-batch.

    encode_batch pads every row to the longest sequence in the sub-batch and
    attention cost per layer is batch x heads x length^2, so interleaving one
    long document with short ones makes the short ones pay the long length
    (#2104). Grouping by size is what keeps that bill proportional to the
    text actually being embedded.
    """
    batches = []
    _record_batches(monkeypatch, batches)
    # One long document per sub-batch's worth of short ones: the pathological
    # arrival order a verbatim transcript sweep produces.
    long_doc = " ".join(["word"] * 200)
    docs = [long_doc if i % _B == 0 else f"short {i}" for i in range(4 * _B)]

    ef = embedding.EmbeddinggemmaONNX()
    out = ef(docs)
    assert len(out) == len(docs)

    for texts in batches:
        sizes = [len(t.encode("utf-8")) for t in texts]
        assert sizes == sorted(sizes), f"sub-batch is not size-grouped: {sizes}"

    prefixed = [embedding._EMBEDDINGGEMMA_PREFIX + d for d in docs]
    arrival_order = [prefixed[s : s + _B] for s in range(0, len(prefixed), _B)]
    assert _fake_padded_width(batches) < _fake_padded_width(arrival_order), (
        "size grouping must lower the total padded width"
    )


def test_call_groups_by_utf8_size_not_character_count(patched_lazy_load, monkeypatch):
    """The key is UTF-8 bytes, because this model is multilingual.

    A CJK document is ~3 bytes per character and roughly a token per
    character, so ordering by character count would file it next to Latin
    documents several times cheaper to embed.
    """
    batches = []
    _record_batches(monkeypatch, batches)
    # Same character count, very different byte count (and token count).
    docs = ["a" * 90] * _B + ["中" * 90] * _B
    ef = embedding.EmbeddinggemmaONNX()
    ef(list(reversed(docs)))

    assert len(batches) == 2, f"expected two sub-batches, got {len(batches)}"
    sizes = [[len(t.encode("utf-8")) for t in b] for b in batches]
    assert max(sizes[0]) < min(sizes[1]), (
        f"CJK documents must not share a sub-batch with Latin ones: {sizes}"
    )


def test_call_size_grouping_is_stable(patched_lazy_load, monkeypatch):
    """Equal-size documents keep their arrival order.

    An unstable sort would make the sub-batch split depend on nothing the
    caller can see, so two identical inputs could take different code paths.
    """
    batches = []
    _record_batches(monkeypatch, batches)
    docs = [f"doc{i:03d}" for i in range(_B + 8)]  # identical size, distinct text
    ef = embedding.EmbeddinggemmaONNX()
    ef(docs)
    captured = [t for b in batches for t in b]
    assert captured == [embedding._EMBEDDINGGEMMA_PREFIX + d for d in docs]


def test_call_keeps_arrival_order_within_a_single_sub_batch(patched_lazy_load, monkeypatch):
    """An input that fits one sub-batch is not reordered.

    Every row pads to the same width either way, so the sort would buy
    nothing and only add keys to compute on the search hot path.
    """
    batches = []
    _record_batches(monkeypatch, batches)
    docs = [f"{'x' * (_B - i)} doc {i}" for i in range(_B)]  # descending size
    ef = embedding.EmbeddinggemmaONNX()
    ef(docs)
    assert batches == [[embedding._EMBEDDINGGEMMA_PREFIX + d for d in docs]]


class _MarkerTokenizer(_FakeTokenizer):
    """Tokenizer whose first token id carries that document's own length.

    ``_FakeTokenizer`` emits all-zero ids padded to one width, so a fake
    session cannot tell its rows apart, which is exactly what an
    order-restoration test has to observe.
    """

    def encode_batch(self, texts):
        widths = [len(t) for t in texts]
        padded = max(widths)

        class _Enc:
            def __init__(self, marker):
                self.ids = [marker] + [0] * (padded - 1)
                self.attention_mask = [1] * marker + [0] * (padded - marker)

        return [_Enc(w) for w in widths]


class _MarkerSession:
    """Emit a vector whose first two dims encode the row's marker id.

    Both dims scale by the same L2 norm, so ``row[0] / row[1]`` survives
    normalization and identifies which document produced the row.
    """

    _WIDTH = 2 * embedding._EMBEDDINGGEMMA_DIM

    def run(self, _output_names, feed):
        ids = feed["input_ids"]
        batch, length = ids.shape
        sent = np.zeros((batch, self._WIDTH), dtype=np.float64)
        sent[:, 0] = ids[:, 0]
        sent[:, 1] = 1.0
        return [np.zeros((batch, length, self._WIDTH), dtype=np.float64), sent]


def _marker_ef(patched_lazy_load, session=None):
    """An EF wired to the marker fakes, with the real lazy load short-circuited."""
    # patched_lazy_load is taken so a future tightening of _lazy_load's
    # early-return cannot turn these tests into a 300 MB model download.
    ef = embedding.EmbeddinggemmaONNX()
    ef._tokenizer = _MarkerTokenizer()
    ef._session = session if session is not None else _MarkerSession()
    ef._output_idx = 1
    ef._np = np
    return ef


def test_call_returns_rows_at_their_input_index(patched_lazy_load):
    """Row i of the result must be the embedding of document i.

    Sub-batching by size reorders the work; ChromaDB zips the returned
    vectors against the ids positionally, so grouping without the matching
    scatter would file every drawer under another drawer's vector. That
    half-applied state is what this pins: arrival order trivially satisfies
    it, so it is the grouping tests that cover the other direction.
    """
    ef = _marker_ef(patched_lazy_load)
    # Strictly descending sizes, so grouping reverses arrival order and an
    # unscattered result would be visibly wrong.
    docs = ["x" * n for n in range(200, 200 - 3 * _B, -1)]
    out = ef(docs)

    assert len(out) == len(docs)
    assert all(row is not None for row in out), "every index must be filled"
    markers = [round(row[0] / row[1]) for row in out]
    assert markers == [len(embedding._EMBEDDINGGEMMA_PREFIX + d) for d in docs]


def test_call_rejects_a_short_row_count_from_the_session(patched_lazy_load):
    """A session returning fewer rows than documents must fail loudly.

    Scattering by index would otherwise leave a None in the result and the
    caller would only trip over it much later, converting to an array.
    """

    class _ShortSession(_MarkerSession):
        def run(self, output_names, feed):
            last_hidden, sent = super().run(output_names, feed)
            return [last_hidden, sent[:-1]]

    ef = _marker_ef(patched_lazy_load, session=_ShortSession())
    with pytest.raises(RuntimeError, match="rows for a"):
        ef(["x" * n for n in range(200, 200 - 2 * _B, -1)])


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
        def __init__(self, preferred_providers=None, intra_op_num_threads=0):
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


def test_out_of_range_added_token_is_remapped_to_unknown(
    patched_lazy_load,
    monkeypatch,
    caplog,
):
    class EncodingWithAddedToken:
        ids = [2, 262144, 1]
        attention_mask = [1, 1, 1]

    def encode_with_added_token(_self, texts):
        return [EncodingWithAddedToken() for _ in texts]

    monkeypatch.setattr(
        _FakeTokenizer,
        "encode_batch",
        encode_with_added_token,
    )

    captured = {}
    fake_session_class = _make_fake_session()

    class BoundsCheckingSession(fake_session_class):
        def run(self, output_names, feed):
            captured["input_ids"] = feed["input_ids"].copy()

            assert np.all(feed["input_ids"] >= 0)
            assert np.all(feed["input_ids"] < 262144)

            return super().run(
                output_names,
                feed,
            )

    import onnxruntime

    monkeypatch.setattr(
        onnxruntime,
        "InferenceSession",
        lambda *_args, **_kwargs: BoundsCheckingSession(),
    )

    caplog.set_level(
        "WARNING",
        logger=embedding.__name__,
    )

    embedding_function = embedding.EmbeddinggemmaONNX()

    result = embedding_function(["literal <image_soft_token> in source"])

    assert captured["input_ids"].tolist() == [[2, 3, 1]]
    assert np.asarray(result).shape == (1, 384)
    assert "remapping to <unk>" in caplog.text
    assert patched_lazy_load["hf_hub_download"] == 3
