"""
Tests for MiniMax provider routing in benchmark tools.

Verifies that _llm_base_url and _load_api_key correctly route to MiniMax
vs Anthropic endpoints based on model name, without making real API calls.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# Load benchmark modules directly since benchmarks/ has no __init__.py
_ROOT = Path(__file__).parent.parent


def _load_bench(filename):
    spec = importlib.util.spec_from_file_location(filename, _ROOT / "benchmarks" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_locomo = _load_bench("locomo_bench.py")
_longmem = _load_bench("longmemeval_bench.py")


# ── _llm_base_url ──────────────────────────────────────────────────────────────


class TestLlmBaseUrl:
    def test_claude_model_returns_anthropic(self):
        assert _locomo._llm_base_url("claude-haiku-4-5-20251001") == "https://api.anthropic.com"

    def test_claude_sonnet_returns_anthropic(self):
        assert _locomo._llm_base_url("claude-sonnet-4-6") == "https://api.anthropic.com"

    def test_minimax_m2_7_returns_minimax(self):
        assert _locomo._llm_base_url("MiniMax-M2.7") == "https://api.minimax.io/anthropic"

    def test_minimax_m3_returns_minimax(self):
        assert _locomo._llm_base_url("MiniMax-M3") == "https://api.minimax.io/anthropic"

    def test_minimax_highspeed_returns_minimax(self):
        assert _locomo._llm_base_url("MiniMax-M2.7-highspeed") == "https://api.minimax.io/anthropic"

    def test_minimax_base_url_env_override(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic")
        assert _locomo._llm_base_url("MiniMax-M2.7") == "https://api.minimaxi.com/anthropic"

    def test_longmemeval_minimax_returns_minimax(self):
        assert _longmem._llm_base_url("MiniMax-M2.7") == "https://api.minimax.io/anthropic"

    def test_longmemeval_minimax_m3_returns_minimax(self):
        assert _longmem._llm_base_url("MiniMax-M3") == "https://api.minimax.io/anthropic"

    def test_longmemeval_claude_returns_anthropic(self):
        assert _longmem._llm_base_url("claude-haiku-4-5-20251001") == "https://api.anthropic.com"

    def test_unknown_model_returns_anthropic(self):
        assert _locomo._llm_base_url("gpt-4o") == "https://api.anthropic.com"


# ── _load_api_key ─────────────────────────────────────────────────────────────


class TestLoadApiKey:
    def test_explicit_key_always_wins(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic")
        monkeypatch.setenv("MINIMAX_API_KEY", "env-minimax")
        assert _locomo._load_api_key("explicit-key", model="MiniMax-M2.7") == "explicit-key"
        assert _locomo._load_api_key("explicit-key", model="claude-sonnet-4-6") == "explicit-key"

    def test_minimax_model_uses_minimax_key(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax-secret")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert _locomo._load_api_key("", model="MiniMax-M2.7") == "minimax-secret"

    def test_minimax_m3_uses_minimax_key(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax-secret")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert _locomo._load_api_key("", model="MiniMax-M3") == "minimax-secret"

    def test_minimax_model_falls_back_to_anthropic_if_no_minimax_key(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
        assert _locomo._load_api_key("", model="MiniMax-M2.7") == "anthropic-secret"

    def test_anthropic_model_uses_anthropic_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax-secret")
        assert _locomo._load_api_key("", model="claude-sonnet-4-6") == "anthropic-secret"

    def test_no_env_returns_empty(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        assert _locomo._load_api_key("", model="MiniMax-M2.7") == ""

    def test_longmemeval_minimax_key(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax-secret")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert _longmem._load_api_key("", model="MiniMax-M2.7") == "minimax-secret"

    def test_longmemeval_anthropic_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        assert _longmem._load_api_key("", model="claude-haiku-4-5-20251001") == "anthropic-secret"

    def test_no_model_falls_back_to_anthropic(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
        assert _locomo._load_api_key("") == "anthropic-secret"

