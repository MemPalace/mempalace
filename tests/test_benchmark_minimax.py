"""
Tests for MiniMax provider routing in benchmark tools.

Verifies model routing, key selection, and serialized Anthropic-compatible
requests without making real API calls.
"""

import importlib.util
import json
import urllib.request
from pathlib import Path

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

    def test_ollama_does_not_read_provider_key(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax-secret")
        assert _locomo._load_api_key("", model="MiniMax-M3.1-Flash", backend="ollama") == ""

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
