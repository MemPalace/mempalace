"""
Tests for MiniMax provider routing in benchmark tools.

Verifies model routing, key selection, and serialized Anthropic-compatible
requests without making real API calls.
"""

import importlib.util
import inspect
import json
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def _load_bench(filename):
    spec = importlib.util.spec_from_file_location(filename, _ROOT / "benchmarks" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_locomo = _load_bench("locomo_bench.py")
_longmem = _load_bench("longmemeval_bench.py")


class TestLlmBaseUrl:
    def test_claude_model_returns_anthropic(self):
        assert _locomo._llm_base_url("claude-haiku-4-5-20251001") == "https://api.anthropic.com"

    def test_minimax_flash_returns_minimax(self):
        assert _locomo._llm_base_url("MiniMax-M3.1-Flash") == "https://api.minimax.io/anthropic"

    def test_minimax_m3_returns_minimax(self):
        assert _locomo._llm_base_url("MiniMax-M3") == "https://api.minimax.io/anthropic"

    def test_minimax_base_url_env_override(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic")
        assert _locomo._llm_base_url("MiniMax-M3.1-Flash") == "https://api.minimaxi.com/anthropic"

    def test_longmemeval_minimax_returns_minimax(self):
        assert _longmem._llm_base_url("MiniMax-M3.1-Flash") == "https://api.minimax.io/anthropic"

    def test_unknown_model_returns_anthropic(self):
        assert _locomo._llm_base_url("custom-model") == "https://api.anthropic.com"


class TestLoadApiKey:
    def test_explicit_key_always_wins(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "env-minimax")
        assert _locomo._load_api_key("explicit-key", model="MiniMax-M3.1-Flash") == "explicit-key"

    def test_minimax_model_uses_minimax_key(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
        assert _locomo._load_api_key("", model="MiniMax-M3.1-Flash") == "minimax-secret"

    def test_minimax_model_does_not_use_anthropic_key(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
        assert _locomo._load_api_key("", model="MiniMax-M2.7") == ""

    def test_anthropic_model_uses_anthropic_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
        assert _locomo._load_api_key("", model="claude-sonnet-4-6") == "anthropic-secret"

    def test_ollama_preserves_existing_key_selection(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "existing-key")
        assert (
            _locomo._load_api_key("", model="MiniMax-M3.1-Flash", backend="ollama")
            == "existing-key"
        )

    def test_no_env_returns_empty(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        assert _locomo._load_api_key("", model="MiniMax-M3.1-Flash") == ""


class _Response:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


def test_locomo_flash_request_uses_adaptive_thinking_and_text_block(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update(url=request.full_url, payload=json.loads(request.data), timeout=timeout)
        return _Response(
            {
                "content": [
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "text", "text": "done"},
                ]
            }
        )

    monkeypatch.setattr(_locomo.urllib.request, "urlopen", fake_urlopen)
    assert _locomo._llm_call("prompt", "key", model="MiniMax-M3.1-Flash", max_tokens=32) == "done"
    assert captured["url"] == "https://api.minimax.io/anthropic/v1/messages"
    assert captured["payload"]["model"] == "MiniMax-M3.1-Flash"
    assert captured["payload"]["thinking"] == {"type": "adaptive"}
    assert captured["payload"]["max_tokens"] == 16384


def test_longmemeval_rerank_reads_final_text_after_thinking(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update(url=request.full_url, payload=json.loads(request.data), timeout=timeout)
        return _Response(
            {
                "content": [
                    {"type": "thinking", "thinking": "pick carefully"},
                    {"type": "text", "text": "2"},
                ]
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = _longmem.llm_rerank(
        "question", [0, 1], ["first", "second"], ["id-1", "id-2"], "key", model="MiniMax-M3.1-Flash"
    )
    assert result == [1, 0]
    assert captured["url"] == "https://api.minimax.io/anthropic/v1/messages"
    assert captured["payload"]["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize("module", [_locomo, _longmem])
@pytest.mark.parametrize(
    "model", ["MiniMax-M3.1-Flash", "MiniMax-M3", "MiniMax-M2.7", "MiniMax-custom", "custom"]
)
@pytest.mark.parametrize(
    "endpoint", [None, "https://api.minimaxi.com/anthropic/", "https://custom.invalid/anthropic/"]
)
def test_serialized_rerank_routes_preserve_models(monkeypatch, module, model, endpoint):
    if endpoint:
        monkeypatch.setenv("MINIMAX_BASE_URL", endpoint)
    else:
        monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(request)
        return _Response({"content": [{"type": "text", "text": "2"}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    if module is _locomo:
        assert module.llm_rerank_locomo("q", [0, 1], ["a", "b"], "key", model=model) == [1, 0]
    else:
        assert module.llm_rerank("q", [0, 1], ["a", "b"], ["a", "b"], "key", model=model) == [1, 0]
    request = captured[0]
    base = (endpoint or "https://api.minimax.io/anthropic").rstrip("/")
    if not model.startswith("MiniMax"):
        base = "https://api.anthropic.com"
    assert request.full_url == base + "/v1/messages"
    assert request.get_header("X-api-key") == "key"
    payload = json.loads(request.data)
    assert payload["model"] == model
    assert payload["messages"][0]["role"] == "user"
    assert "service_tier" not in payload
    if model == "MiniMax-M3.1-Flash":
        assert payload["thinking"] == {"type": "adaptive"}
        assert payload["max_tokens"] == 16384
    else:
        assert payload["max_tokens"] == 8
        assert "thinking" not in payload


@pytest.mark.parametrize("module", [_locomo, _longmem])
def test_ollama_request_is_unchanged(monkeypatch, module):
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(request)
        return _Response({"choices": [{"message": {"content": "2"}}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    kwargs = {
        "model": "minimax-m2.7:cloud",
        "backend": "ollama",
        "base_url": "http://localhost:11434",
    }
    if module is _locomo:
        assert module.llm_rerank_locomo("q", [0, 1], ["a", "b"], "key", **kwargs) == [1, 0]
    else:
        assert module.llm_rerank("q", [0, 1], ["a", "b"], ["a", "b"], "key", **kwargs) == [1, 0]
    payload = json.loads(captured[0].data)
    assert captured[0].full_url == "http://localhost:11434/v1/chat/completions"
    assert payload["model"] == "minimax-m2.7:cloud"
    assert payload["max_tokens"] == 1024
    assert payload["temperature"] == 0.0
    assert "thinking" not in payload


def test_existing_defaults_and_multimodal_prompt_are_preserved(monkeypatch):
    assert (
        inspect.signature(_locomo.run_benchmark).parameters["llm_model"].default
        == "claude-sonnet-4-6"
    )
    assert (
        inspect.signature(_longmem.run_benchmark).parameters["llm_model"].default
        == "claude-haiku-4-5-20251001"
    )
    prompt = [
        {"type": "text", "text": "describe"},
        {"type": "image", "source": {"type": "url", "url": "https://example.invalid/image.png"}},
    ]
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(json.loads(request.data))
        return _Response({"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _locomo._llm_call(prompt, "key") == "ok"
    assert captured[0]["model"] == "claude-haiku-4-5-20251001"
    assert captured[0]["messages"][0]["content"] == prompt
    assert captured[0]["max_tokens"] == 32


def test_diary_flash_response(monkeypatch):
    captured = []
    expected = {"topics": ["test"], "summary": "test"}

    def fake_urlopen(request, timeout):
        captured.append(json.loads(request.data))
        return _Response(
            {
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": json.dumps(expected)},
                ]
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert (
        _longmem.diary_ingest_session(
            [{"role": "user", "content": "test"}], "id", "key", model="MiniMax-M3.1-Flash"
        )
        == expected
    )
    assert captured[0]["model"] == "MiniMax-M3.1-Flash"
    assert captured[0]["thinking"] == {"type": "adaptive"}
