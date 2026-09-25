"""Offline tests for GenericONNXEmbedding and its e5-small preset.

Real models are pulled from HuggingFace on first use, so these tests mock
huggingface_hub.hf_hub_download, tokenizers.Tokenizer, and
onnxruntime.InferenceSession to keep CI fast and network-free — same
approach as test_embeddinggemma.py.

Skipped when the multilingual deps aren't installed (huggingface_hub/
tokenizers/numpy) — CI runs only core deps by default.
"""

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("huggingface_hub")
pytest.importorskip("tokenizers")

import mempalace.embedding as embedding  # noqa: E402  (after importorskip)
from mempalace.generic_onnx_embedding import (  # noqa: E402
    GenericONNXEmbedding,
    _derived_ef_name,
    build_generic_onnx_ef,
    e5_small_ef,
)


@pytest.fixture(autouse=True)
def isolate_embedding_state(monkeypatch):
    monkeypatch.setattr(embedding, "_EF_CACHE", {})
    monkeypatch.setattr(embedding, "_WARNED", set())


class _FakeSession:
    """Stand-in for onnxruntime.InferenceSession.

    Returns a per-token tensor of shape (batch, seq, hidden) whose values
    are the 1-based token position, so masked-mean and cls pooling produce
    different, predictable numbers.
    """

    def __init__(self, *args, input_names=("input_ids", "attention_mask"), hidden=8, **kwargs):
        self._input_names = input_names
        self._hidden = hidden
        self.seen_feeds = []

    def get_inputs(self):
        class _In:
            def __init__(self, name):
                self.name = name

        return [_In(n) for n in self._input_names]

    def run(self, _output_names, feed):
        self.seen_feeds.append(feed)
        ids = feed["input_ids"]
        batch, seq = ids.shape
        pos = np.arange(1, seq + 1, dtype=np.float32)  # 1-based position per token
        hidden = np.tile(pos[None, :, None], (batch, 1, self._hidden))
        return [hidden]


class _FakeTokenizer:
    """Stand-in for tokenizers.Tokenizer that records the texts it encodes.

    Token count equals the text length in characters, capped at the enabled
    truncation length; every row is padded to the longest row in the batch
    with attention-mask zeros, mirroring enable_padding().
    """

    def __init__(self):
        self.encoded_texts = []
        self._max = None

    def enable_padding(self):
        pass

    def enable_truncation(self, max_length):
        self._max = max_length

    def encode_batch(self, texts):
        self.encoded_texts.extend(texts)
        lengths = [min(len(t), self._max or len(t)) for t in texts]
        width = max(lengths) if lengths else 0

        class _Enc:
            def __init__(self, n):
                self.ids = [1] * n + [0] * (width - n)
                self.attention_mask = [1] * n + [0] * (width - n)

        return [_Enc(n) for n in lengths]


@pytest.fixture
def offline_model(monkeypatch):
    """Patch network/model deps; returns the shared fake session and tokenizer."""
    session = _FakeSession()
    tokenizer = _FakeTokenizer()
    monkeypatch.setattr(
        "huggingface_hub.hf_hub_download", lambda repo, filename, **kw: f"/fake/{filename}"
    )
    monkeypatch.setattr("tokenizers.Tokenizer.from_file", staticmethod(lambda path: tokenizer))
    monkeypatch.setattr("onnxruntime.InferenceSession", lambda *a, **kw: session)
    return session, tokenizer


def _make_ef(**overrides):
    kwargs = dict(repo="acme/test-model", onnx_file="model.onnx", ef_name="test_ef")
    kwargs.update(overrides)
    return GenericONNXEmbedding(**kwargs)


# --- construction-time validation (no model load needed) ---


def test_empty_repo_rejected():
    with pytest.raises(ValueError, match="repo"):
        GenericONNXEmbedding(repo="", onnx_file="model.onnx")


def test_bad_pooling_rejected():
    with pytest.raises(ValueError, match="pooling"):
        _make_ef(pooling="max")


def test_bad_batch_size_rejected():
    with pytest.raises(ValueError, match="batch_size"):
        _make_ef(batch_size=0)


# --- embedding behavior against the fake model ---


def test_prefixes_split_by_path(offline_model):
    _, tokenizer = offline_model
    ef = _make_ef(doc_prefix="passage: ", query_prefix="query: ")
    ef(["alpha"])
    ef.embed_query(["alpha"])
    assert tokenizer.encoded_texts == ["passage: alpha", "query: alpha"]


def test_call_accepts_bare_string_and_empty(offline_model):
    ef = _make_ef()
    assert len(ef("alpha")) == 1
    assert ef([]) == []
    assert ef(None) == []
    assert ef.embed_query(None) == []


def test_vectors_l2_normalized(offline_model):
    ef = _make_ef()
    vecs = np.asarray(ef(["alpha", "beta longer text"]))
    norms = np.linalg.norm(vecs, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_mean_pooling_ignores_padding(offline_model):
    """The short row of a padded batch must equal the same row embedded alone."""
    ef = _make_ef()
    padded = ef(["ab", "abcdefghij"])[0]
    alone = ef(["ab"])[0]
    assert np.allclose(padded, alone, atol=1e-6)


def test_cls_pooling_takes_first_token(offline_model):
    ef = _make_ef(pooling="cls", normalize=False)
    vec = np.asarray(ef(["abcd"])[0])
    # Fake hidden state holds the 1-based token position; CLS is position 1.
    assert np.allclose(vec, 1.0)


def test_token_type_ids_fed_only_when_declared(monkeypatch):
    session = _FakeSession(input_names=("input_ids", "attention_mask", "token_type_ids"))
    tokenizer = _FakeTokenizer()
    monkeypatch.setattr("huggingface_hub.hf_hub_download", lambda repo, filename, **kw: "/fake/f")
    monkeypatch.setattr("tokenizers.Tokenizer.from_file", staticmethod(lambda path: tokenizer))
    monkeypatch.setattr("onnxruntime.InferenceSession", lambda *a, **kw: session)
    ef = _make_ef()
    ef(["alpha"])
    assert "token_type_ids" in session.seen_feeds[0]

    plain = _FakeSession()  # declares no token_type_ids
    monkeypatch.setattr("onnxruntime.InferenceSession", lambda *a, **kw: plain)
    ef2 = _make_ef(ef_name="other")
    ef2(["alpha"])
    assert "token_type_ids" not in plain.seen_feeds[0]


def test_batching_preserves_order_and_count(offline_model):
    ef = _make_ef(batch_size=2)
    texts = [f"text-{i}" for i in range(5)]
    vecs = ef(texts)
    assert len(vecs) == 5


# --- identity names ---


def test_e5_preset_identity_and_symmetric_prefix(offline_model):
    _, tokenizer = offline_model
    ef = e5_small_ef()
    assert ef.name() == "e5_small_384"
    ef(["документ"])
    ef.embed_query(["запрос"])
    assert tokenizer.encoded_texts == ["query: документ", "query: запрос"]


def test_derived_ef_name_stable_and_model_sensitive():
    a = _derived_ef_name("intfloat/multilingual-e5-small", "model.onnx")
    b = _derived_ef_name("intfloat/multilingual-e5-small", "model_O4.onnx")
    assert a == "onnx_intfloat_multilingual_e5_small_model_onnx"
    assert a != b  # a different export must trip the identity check


# --- config plumbing (MempalaceConfig unit level) ---


def test_config_onnx_fields_from_file(tmp_path):
    import json

    from mempalace.config import MempalaceConfig

    with open(tmp_path / "config.json", "w") as f:
        json.dump(
            {
                "embedding_onnx_repo": "acme/enc",
                "embedding_onnx_file": "model_q8.onnx",
                "embedding_onnx_pooling": "CLS",
                "embedding_onnx_max_len": 256,
                "embedding_onnx_doc_prefix": "passage: ",
                "embedding_onnx_ef_name": "acme_enc_768",
            },
            f,
        )
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.embedding_onnx_repo == "acme/enc"
    assert cfg.embedding_onnx_file == "model_q8.onnx"
    assert cfg.embedding_onnx_pooling == "cls"
    assert cfg.embedding_onnx_max_len == 256
    # Trailing space of the prefix must survive resolution.
    assert cfg.embedding_onnx_doc_prefix == "passage: "
    assert cfg.embedding_onnx_ef_name == "acme_enc_768"


def test_config_onnx_defaults(tmp_path):
    from mempalace.config import MempalaceConfig

    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.embedding_onnx_repo is None
    assert cfg.embedding_onnx_file == "model.onnx"
    assert cfg.embedding_onnx_subfolder == "onnx"
    assert cfg.embedding_onnx_pooling == "mean"
    assert cfg.embedding_onnx_max_len == 512
    assert cfg.embedding_onnx_doc_prefix == ""
    assert cfg.embedding_onnx_query_prefix == ""
    assert cfg.embedding_onnx_ef_name is None


def test_config_onnx_prefix_env_not_stripped(tmp_path, monkeypatch):
    from mempalace.config import MempalaceConfig

    monkeypatch.setenv("MEMPALACE_EMBEDDING_ONNX_DOC_PREFIX", "query: ")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.embedding_onnx_doc_prefix == "query: "


# --- config-driven resolution (env layer; file config isolated via conftest HOME) ---


def test_build_e5_preset_ignores_config(monkeypatch):
    monkeypatch.delenv("MEMPALACE_EMBEDDING_ONNX_REPO", raising=False)
    ef = build_generic_onnx_ef("e5-small")
    assert ef.name() == "e5_small_384"


def test_build_generic_requires_repo(monkeypatch):
    monkeypatch.delenv("MEMPALACE_EMBEDDING_ONNX_REPO", raising=False)
    with pytest.raises(ValueError, match="embedding_onnx_repo"):
        build_generic_onnx_ef("generic-onnx")


def test_build_generic_reads_env(monkeypatch):
    monkeypatch.setenv("MEMPALACE_EMBEDDING_ONNX_REPO", "acme/enc")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_ONNX_DOC_PREFIX", "query: ")
    ef = build_generic_onnx_ef("generic-onnx")
    assert ef.name() == _derived_ef_name("acme/enc", "model.onnx")
    # Trailing space of the prefix must survive resolution.
    assert ef._doc_prefix == "query: "


def test_build_generic_explicit_ef_name_wins(monkeypatch):
    monkeypatch.setenv("MEMPALACE_EMBEDDING_ONNX_REPO", "acme/enc")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_ONNX_EF_NAME", "my_space_768")
    ef = build_generic_onnx_ef("generic-onnx")
    assert ef.name() == "my_space_768"


# --- factory integration ---


def test_factory_resolves_e5_small_and_caches(offline_model, monkeypatch):
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "e5-small")
    ef1 = embedding.get_embedding_function(device="cpu")
    ef2 = embedding.get_embedding_function(device="cpu")
    assert isinstance(ef1, GenericONNXEmbedding)
    assert ef1.name() == "e5_small_384"
    assert ef1 is ef2  # cached per (providers, model)


def test_factory_resolves_generic_onnx(offline_model, monkeypatch):
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "generic-onnx")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_ONNX_REPO", "acme/enc")
    ef = embedding.get_embedding_function(device="cpu")
    assert isinstance(ef, GenericONNXEmbedding)
