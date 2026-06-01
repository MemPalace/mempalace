"""Tests for the OpenAI-compatible embedding API backend (issue #1559).

Covers ``OpenAICompatEmbeddingFunction``, the ``embedding_model ==
"openai-compat"`` selection branch in ``get_embedding_function``, and the
``MempalaceConfig`` properties that are the single source of truth for the
endpoint settings. No server required — ``urllib.request.urlopen`` is mocked.
"""

import json

import pytest

import mempalace.embedding as embedding
from mempalace.config import MempalaceConfig


@pytest.fixture(autouse=True)
def isolate_embedding_cache(monkeypatch):
    monkeypatch.setattr(embedding, "_EF_CACHE", {})


# ── Fake HTTP layer ───────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _fake_urlopen(*, dim=4, one_hot=False, shuffle=False, captured=None):
    """Return a urlopen stand-in that echoes one embedding per input text.

    ``one_hot`` makes each vector a unit vector at its own index (so order is
    observable); ``shuffle`` reverses the returned rows to prove the EF
    re-sorts by ``index``; ``captured`` collects the Request objects.
    """

    def fake(req, timeout=None):
        if captured is not None:
            captured.append(req)
        body = json.loads(req.data.decode())
        n = len(body["input"])
        rows = []
        for i in range(n):
            if one_hot:
                vec = [0.0] * max(dim, n)
                vec[i] = 1.0
            else:
                vec = [float(i + 1)] * dim
            rows.append({"index": i, "embedding": vec})
        if shuffle:
            rows = list(reversed(rows))
        return _FakeResp(json.dumps({"data": rows, "model": body["model"]}).encode())

    return fake


# ── OpenAICompatEmbeddingFunction ─────────────────────────────────────────


def test_resolve_url_variants():
    ef = embedding.OpenAICompatEmbeddingFunction
    assert ef("http://h:8420", "m")._url == "http://h:8420/v1/embeddings"
    assert ef("http://h:8420/", "m")._url == "http://h:8420/v1/embeddings"
    assert ef("http://h:8420/v1", "m")._url == "http://h:8420/v1/embeddings"
    assert ef("http://h:8420/v1/embeddings", "m")._url == "http://h:8420/v1/embeddings"


def test_name_encodes_model():
    ef = embedding.OpenAICompatEmbeddingFunction
    assert ef("http://h", "small").name() == "openai_compat_emb_small"
    # HF-style ids with slashes are flattened to a safe identifier
    assert ef("http://h", "Qwen/Qwen3-Embedding-0.6B").name() == (
        "openai_compat_emb_Qwen_Qwen3-Embedding-0.6B"
    )


def test_embeds_and_l2_normalizes(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(dim=4))
    ef = embedding.OpenAICompatEmbeddingFunction("http://h:8420", "small")
    out = ef(["a", "b"])
    assert len(out) == 2
    assert len(out[0]) == 4
    for vec in out:
        assert abs(sum(x * x for x in vec) ** 0.5 - 1.0) < 1e-6


def test_sorts_response_by_index(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(one_hot=True, shuffle=True))
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    out = ef(["x", "y", "z"])
    # Server returned rows reversed; the EF must realign by index so out[i]
    # is the one-hot vector for position i.
    for i, vec in enumerate(out):
        assert max(range(len(vec)), key=lambda j: vec[j]) == i


def test_sends_bearer_header_when_key_set(monkeypatch):
    captured = []
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(captured=captured))
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m", api_key="sk-secret")
    ef(["a"])
    assert captured[0].get_header("Authorization") == "Bearer sk-secret"


def test_no_auth_header_without_key(monkeypatch):
    captured = []
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(captured=captured))
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    ef(["a"])
    assert captured[0].get_header("Authorization") is None


def test_batches_large_input(monkeypatch):
    captured = []
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(captured=captured))
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    out = ef([f"t{i}" for i in range(130)])  # > _EF_API_BATCH (64)
    assert len(out) == 130
    assert len(captured) == 3  # 64 + 64 + 2


def test_embed_query_delegates_to_call(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(dim=4))
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    assert ef.embed_query(["q"]) == ef(["q"])


def test_raises_on_count_mismatch(monkeypatch):
    def short(req, timeout=None):
        return _FakeResp(json.dumps({"data": [{"index": 0, "embedding": [1.0]}]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", short)
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    with pytest.raises(RuntimeError, match="embeddings for"):
        ef(["a", "b"])


def test_raises_on_transport_error(monkeypatch):
    from urllib.error import URLError

    def boom(req, timeout=None):
        raise URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    with pytest.raises(RuntimeError, match="failed"):
        ef(["a"])


# ── get_embedding_function selection branch ───────────────────────────────


class _FakeCfg:
    def __init__(self, url=None, model=None, key=None, embedding_model="openai-compat"):
        self.embedding_api_url = url
        self.embedding_api_model = model
        self.embedding_api_key = key
        self.embedding_model = embedding_model


def test_get_embedding_function_selects_openai_compat(monkeypatch):
    monkeypatch.setattr(
        "mempalace.config.MempalaceConfig", lambda *a, **k: _FakeCfg("http://h:8420", "small")
    )
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(dim=4))
    ef = embedding.get_embedding_function(device="cpu", model="openai-compat")
    assert isinstance(ef, embedding.OpenAICompatEmbeddingFunction)
    assert ef.name() == "openai_compat_emb_small"
    assert len(ef(["hi"])[0]) == 4


def test_openai_compat_requires_url(monkeypatch):
    monkeypatch.setattr("mempalace.config.MempalaceConfig", lambda *a, **k: _FakeCfg(None, "small"))
    with pytest.raises(ValueError, match="requires an endpoint"):
        embedding.get_embedding_function(device="cpu", model="openai-compat")


def test_openai_compat_requires_model(monkeypatch):
    monkeypatch.setattr(
        "mempalace.config.MempalaceConfig", lambda *a, **k: _FakeCfg("http://h", None)
    )
    with pytest.raises(ValueError, match="requires a model"):
        embedding.get_embedding_function(device="cpu", model="openai-compat")


# ── MempalaceConfig endpoint settings (single source of truth) ────────────


def test_config_api_url_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPALACE_EMBEDDING_API_URL", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"embedding_api_url": "http://host:8420"}))
    assert MempalaceConfig(config_dir=str(tmp_path)).embedding_api_url == "http://host:8420"


def test_config_api_env_overrides_file(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"embedding_api_url": "http://from-config"}))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_API_URL", "  http://from-env  ")
    assert MempalaceConfig(config_dir=str(tmp_path)).embedding_api_url == "http://from-env"


def test_config_api_unset_is_none(tmp_path, monkeypatch):
    for var in ("MEMPALACE_EMBEDDING_API_URL", "MEMPALACE_EMBEDDING_API_MODEL"):
        monkeypatch.delenv(var, raising=False)
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.embedding_api_url is None
    assert cfg.embedding_api_model is None


def test_config_api_blank_value_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPALACE_EMBEDDING_API_MODEL", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"embedding_api_model": "   "}))
    assert MempalaceConfig(config_dir=str(tmp_path)).embedding_api_model is None


def test_config_api_model_and_key_preserve_case(tmp_path, monkeypatch):
    for var in ("MEMPALACE_EMBEDDING_API_MODEL", "MEMPALACE_EMBEDDING_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "config.json").write_text(
        json.dumps({"embedding_api_model": "Qwen3-Embedding", "embedding_api_key": "AbC-XyZ"})
    )
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.embedding_api_model == "Qwen3-Embedding"
    assert cfg.embedding_api_key == "AbC-XyZ"


def test_config_api_blank_env_falls_through_to_file(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"embedding_api_url": "http://from-config"}))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_API_URL", "   ")  # blank must not mask the file value
    assert MempalaceConfig(config_dir=str(tmp_path)).embedding_api_url == "http://from-config"


# ── request shape + malformed-response hardening (review findings) ────────


def test_request_targets_v1_embeddings_with_expected_body(monkeypatch):
    captured = []
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(captured=captured))
    embedding.OpenAICompatEmbeddingFunction("http://h:8420", "small")(["a", "b"])
    req = captured[0]
    assert req.full_url == "http://h:8420/v1/embeddings"
    assert req.get_header("Content-type") == "application/json"
    # Custom User-Agent so Cloudflare-fronted endpoints don't 403 us (#1570).
    assert req.get_header("User-agent", "").startswith("mempalace/")
    assert json.loads(req.data) == {
        "model": "small",
        "input": ["a", "b"],
        "encoding_format": "float",
    }


def test_embedding_api_error_is_runtimeerror():
    assert issubclass(embedding.EmbeddingAPIError, RuntimeError)


def test_raises_on_missing_embedding_key(monkeypatch):
    def bad(req, timeout=None):
        return _FakeResp(json.dumps({"data": [{"index": 0}]}).encode())  # no "embedding"

    monkeypatch.setattr("urllib.request.urlopen", bad)
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    with pytest.raises(embedding.EmbeddingAPIError, match="malformed embeddings"):
        ef(["a"])


def test_raises_on_non_contiguous_indices(monkeypatch):
    # Count matches (2 rows for 2 inputs) but the indices are absolute, not
    # 0..n-1 — sort+positional-zip would silently misalign vectors with texts.
    def bad(req, timeout=None):
        rows = [{"index": 64, "embedding": [1.0]}, {"index": 65, "embedding": [2.0]}]
        return _FakeResp(json.dumps({"data": rows}).encode())

    monkeypatch.setattr("urllib.request.urlopen", bad)
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    with pytest.raises(embedding.EmbeddingAPIError, match="non-contiguous"):
        ef(["a", "b"])


def test_raises_on_http_protocol_exception(monkeypatch):
    # BadStatusLine / IncompleteRead — common with local/overloaded servers.
    from http.client import HTTPException

    def boom(req, timeout=None):
        raise HTTPException("incomplete read")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    with pytest.raises(embedding.EmbeddingAPIError, match="failed"):
        ef(["a"])


def test_raises_on_value_error_from_urlopen(monkeypatch):
    # urlopen raises ValueError on an invalid/missing URL scheme.
    def boom(req, timeout=None):
        raise ValueError("unknown url type")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    with pytest.raises(embedding.EmbeddingAPIError, match="failed"):
        ef(["a"])


def test_raises_on_non_object_response(monkeypatch):
    def bad(req, timeout=None):
        return _FakeResp(json.dumps([1, 2, 3]).encode())  # JSON list, not an object

    monkeypatch.setattr("urllib.request.urlopen", bad)
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    with pytest.raises(embedding.EmbeddingAPIError, match="non-object response"):
        ef(["a"])


def test_raises_and_surfaces_server_error_body(monkeypatch):
    # HTTP 200 with an OpenAI-style error envelope (no "data") — surface it.
    def err(req, timeout=None):
        return _FakeResp(json.dumps({"error": {"message": "model not found"}}).encode())

    monkeypatch.setattr("urllib.request.urlopen", err)
    ef = embedding.OpenAICompatEmbeddingFunction("http://h", "m")
    with pytest.raises(embedding.EmbeddingAPIError, match="model not found"):
        ef(["a"])


def test_get_embedding_function_caches_instance(monkeypatch):
    monkeypatch.setattr(
        "mempalace.config.MempalaceConfig", lambda *a, **k: _FakeCfg("http://h:8420", "small", "k")
    )
    a = embedding.get_embedding_function(device="cpu", model="openai-compat")
    b = embedding.get_embedding_function(device="cpu", model="openai-compat")
    assert a is b


def test_describe_device_reports_openai_compat_endpoint(monkeypatch):
    monkeypatch.setattr(
        "mempalace.config.MempalaceConfig",
        lambda *a, **k: _FakeCfg("http://10.0.0.1:8420", "small"),
    )
    assert embedding.describe_device() == "openai-compat (http://10.0.0.1:8420)"


def test_describe_device_openai_compat_without_url(monkeypatch):
    monkeypatch.setattr("mempalace.config.MempalaceConfig", lambda *a, **k: _FakeCfg(None, "small"))
    assert embedding.describe_device() == "openai-compat"
