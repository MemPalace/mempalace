"""A bare string is one document, and a failed dimension probe is retried."""

import io
import json
import logging

import pytest

import mempalace.embedding as embedding


@pytest.fixture(autouse=True)
def isolate_embedding_state(monkeypatch):
    monkeypatch.setattr(embedding, "_EF_CACHE", {})
    monkeypatch.setattr(embedding, "_DIM_CACHE", {})


@pytest.mark.parametrize(
    "value, expected",
    [
        ("hello", ["hello"]),
        (["a", "b"], ["a", "b"]),
        (("a", "b"), ["a", "b"]),
        (None, []),
        ([], []),
    ],
)
def test_as_text_batch_treats_a_bare_string_as_one_document(value, expected):
    assert embedding.as_text_batch(value) == expected


def test_minilm_embeds_a_bare_string_as_one_vector(monkeypatch):
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    seen = []

    def fake_forward(self, documents, batch_size=32):
        import numpy as np

        seen.append(list(documents))
        return np.zeros((len(documents), 384), dtype=np.float32)

    monkeypatch.setattr(ONNXMiniLM_L6_V2, "_forward", fake_forward)
    ef = embedding._build_ef_class()()

    assert len(ef("hello")) == 1
    assert len(ef.embed_query("hello")) == 1
    assert seen == [["hello"], ["hello"]]


class _FakeEmbeddingResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_openai_compat_sends_a_bare_string_as_one_input(monkeypatch):
    sent = []

    def fake_urlopen(req, timeout=None):
        payload = json.loads(req.data)
        sent.append(payload["input"])
        body = {
            "data": [{"index": i, "embedding": [0.1, 0.2]} for i in range(len(payload["input"]))]
        }
        return _FakeEmbeddingResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr("mempalace.llm_client.urlopen", fake_urlopen)
    ef = embedding.OpenAICompatEmbeddingFunction("http://127.0.0.1:1234/v1", "local-model")

    assert len(ef("hello")) == 1
    assert sent == [["hello"]]


class _FlakyEmbedder:
    """Fails the first call, as an embedder that is still starting would."""

    def __init__(self):
        self.calls = 0

    def __call__(self, input):  # noqa: A002 — ChromaDB EF protocol
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("embedding server not up yet")
        return [[0.0] * 7 for _ in input]


def test_a_failed_dimension_probe_is_retried_then_cached(monkeypatch, caplog):
    flaky = _FlakyEmbedder()
    monkeypatch.setattr(embedding, "current_model_name", lambda model=None: "openai-compat")
    monkeypatch.setattr(embedding, "get_embedding_function", lambda device=None, model=None: flaky)

    with caplog.at_level(logging.WARNING, logger="mempalace.embedding"):
        assert embedding.probe_dimension() == 0
    assert "dimension probe failed" in caplog.text

    assert embedding.probe_dimension() == 7
    assert embedding.get_embedder_identity().dimension == 7
    assert flaky.calls == 2


def test_a_probe_that_keeps_failing_warns_once(monkeypatch, caplog):
    def broken(input):  # noqa: A002
        raise ConnectionError("embedding server down")

    monkeypatch.setattr(embedding, "current_model_name", lambda model=None: "openai-compat")
    monkeypatch.setattr(embedding, "get_embedding_function", lambda device=None, model=None: broken)

    with caplog.at_level(logging.WARNING, logger="mempalace.embedding"):
        assert embedding.probe_dimension() == 0
        assert embedding.probe_dimension() == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert embedding._DIM_CACHE == {}
