"""Tests for mempalace.llm_client.

HTTP is mocked throughout — these tests do not require a running Ollama
or network access. Live-provider smoke tests live outside the unit-test
suite.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from mempalace.llm_client import (
    AnthropicProvider,
    LLMError,
    OllamaProvider,
    OpenAICompatProvider,
    _http_post_json,
    get_provider,
)


# ── factory ─────────────────────────────────────────────────────────────


def test_get_provider_ollama():
    p = get_provider("ollama", "gemma4:e4b")
    assert isinstance(p, OllamaProvider)
    assert p.model == "gemma4:e4b"
    assert p.endpoint == OllamaProvider.DEFAULT_ENDPOINT


def test_get_provider_openai_compat():
    p = get_provider("openai-compat", "foo", endpoint="http://localhost:1234")
    assert isinstance(p, OpenAICompatProvider)


def test_get_provider_anthropic():
    p = get_provider("anthropic", "claude-haiku", api_key="sk-xxx")
    assert isinstance(p, AnthropicProvider)
    assert p.api_key == "sk-xxx"


def test_get_provider_unknown_raises():
    with pytest.raises(LLMError, match="Unknown provider"):
        get_provider("nonsense", "x")


# ── _http_post_json ─────────────────────────────────────────────────────


def test_http_post_json_success():
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"ok": true}'
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock_resp):
        result = _http_post_json("http://x/y", {"a": 1}, {}, timeout=5)
    assert result == {"ok": True}


def test_http_post_json_http_error_wraps_as_llm_error():
    from urllib.error import HTTPError
    import io

    err = HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(b"model missing"))
    with patch("mempalace.llm_client.urlopen", side_effect=err):
        with pytest.raises(LLMError, match="HTTP 404"):
            _http_post_json("http://x", {}, {}, timeout=5)


def test_http_post_json_url_error_wraps_as_llm_error():
    from urllib.error import URLError

    with patch("mempalace.llm_client.urlopen", side_effect=URLError("conn refused")):
        with pytest.raises(LLMError, match="Cannot reach"):
            _http_post_json("http://x", {}, {}, timeout=5)


def test_http_post_json_malformed_response():
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"not json"
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock_resp):
        with pytest.raises(LLMError, match="Malformed"):
            _http_post_json("http://x", {}, {}, timeout=5)


# ── OllamaProvider ──────────────────────────────────────────────────────


def _mock_ollama_chat_response(content: str):
    mock = MagicMock()
    mock.read.return_value = json.dumps({"message": {"content": content}}).encode()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    return mock


def test_ollama_check_available_finds_model():
    tags = {"models": [{"name": "gemma4:e4b"}, {"name": "other:latest"}]}
    mock = MagicMock()
    mock.read.return_value = json.dumps(tags).encode()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock):
        p = OllamaProvider(model="gemma4:e4b")
        ok, msg = p.check_available()
    assert ok
    assert msg == "ok"


def test_ollama_check_available_accepts_latest_suffix():
    tags = {"models": [{"name": "mymodel:latest"}]}
    mock = MagicMock()
    mock.read.return_value = json.dumps(tags).encode()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock):
        p = OllamaProvider(model="mymodel")
        ok, _ = p.check_available()
    assert ok


def test_ollama_check_available_missing_model():
    tags = {"models": [{"name": "other:latest"}]}
    mock = MagicMock()
    mock.read.return_value = json.dumps(tags).encode()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock):
        p = OllamaProvider(model="absent")
        ok, msg = p.check_available()
    assert not ok
    assert "ollama pull absent" in msg


def test_ollama_check_available_unreachable():
    from urllib.error import URLError

    with patch("mempalace.llm_client.urlopen", side_effect=URLError("refused")):
        p = OllamaProvider(model="gemma4:e4b")
        ok, msg = p.check_available()
    assert not ok
    assert "Cannot reach Ollama" in msg


def test_ollama_classify_sends_json_format():
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _mock_ollama_chat_response('{"classifications": []}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        p = OllamaProvider(model="gemma4:e4b")
        resp = p.classify("sys", "user", json_mode=True)

    assert captured["body"]["format"] == "json"
    assert captured["body"]["model"] == "gemma4:e4b"
    assert captured["url"].endswith("/api/chat")
    assert resp.provider == "ollama"
    assert resp.text == '{"classifications": []}'


def test_ollama_classify_empty_content_raises():
    with patch("mempalace.llm_client.urlopen", return_value=_mock_ollama_chat_response("")):
        p = OllamaProvider(model="x")
        with pytest.raises(LLMError, match="Empty response"):
            p.classify("s", "u")


# ── OpenAICompatProvider ────────────────────────────────────────────────


def _mock_openai_response(content: str):
    mock = MagicMock()
    payload = {"choices": [{"message": {"content": content}}]}
    mock.read.return_value = json.dumps(payload).encode()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    return mock


def test_openai_compat_resolves_url_with_v1_suffix():
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["url"] = req.full_url
        return _mock_openai_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        p = OpenAICompatProvider(model="x", endpoint="http://h:1234")
        p.classify("s", "u")
    assert captured["url"] == "http://h:1234/v1/chat/completions"


def test_openai_compat_resolves_url_with_existing_v1():
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["url"] = req.full_url
        return _mock_openai_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        p = OpenAICompatProvider(model="x", endpoint="http://h:1234/v1")
        p.classify("s", "u")
    assert captured["url"] == "http://h:1234/v1/chat/completions"


def test_openai_compat_requires_endpoint():
    p = OpenAICompatProvider(model="x")
    with pytest.raises(LLMError, match="requires --llm-endpoint"):
        p.classify("s", "u")


def test_openai_compat_sends_authorization_when_key_present():
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["auth"] = req.get_header("Authorization")
        return _mock_openai_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        p = OpenAICompatProvider(model="x", endpoint="http://h", api_key="sk-aaa")
        p.classify("s", "u")
    assert captured["auth"] == "Bearer sk-aaa"


def test_openai_compat_uses_env_var_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    p = OpenAICompatProvider(model="x", endpoint="http://h")
    assert p.api_key == "sk-from-env"


def test_openai_compat_sends_response_format_json():
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["body"] = json.loads(req.data.decode())
        return _mock_openai_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        p = OpenAICompatProvider(model="x", endpoint="http://h")
        p.classify("s", "u", json_mode=True)
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_openai_compat_unexpected_shape_raises():
    mock = MagicMock()
    mock.read.return_value = b'{"nothing": "here"}'
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock):
        p = OpenAICompatProvider(model="x", endpoint="http://h")
        with pytest.raises(LLMError, match="Unexpected response shape"):
            p.classify("s", "u")


# ── AnthropicProvider ───────────────────────────────────────────────────


def _mock_anthropic_response(text: str):
    mock = MagicMock()
    payload = {"content": [{"type": "text", "text": text}]}
    mock.read.return_value = json.dumps(payload).encode()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    return mock


def test_anthropic_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = AnthropicProvider(model="claude-haiku")
    ok, msg = p.check_available()
    assert not ok
    assert "ANTHROPIC_API_KEY" in msg


def test_anthropic_reads_env_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    p = AnthropicProvider(model="claude-haiku")
    assert p.api_key == "sk-ant-env"
    ok, _ = p.check_available()
    assert ok


def test_anthropic_classify_sends_version_and_key():
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["api_key"] = req.get_header("X-api-key")
        captured["version"] = req.get_header("Anthropic-version")
        return _mock_anthropic_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        p = AnthropicProvider(model="claude-haiku", api_key="sk-ant-abc")
        resp = p.classify("s", "u")
    assert captured["api_key"] == "sk-ant-abc"
    assert captured["version"] == AnthropicProvider.API_VERSION
    assert resp.text == '{"ok": true}'


def test_anthropic_joins_multiple_text_blocks():
    mock = MagicMock()
    payload = {
        "content": [
            {"type": "text", "text": "part one. "},
            {"type": "text", "text": "part two."},
        ]
    }
    mock.read.return_value = json.dumps(payload).encode()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock):
        p = AnthropicProvider(model="claude-haiku", api_key="sk-ant")
        resp = p.classify("s", "u")
    assert resp.text == "part one. part two."


def test_anthropic_no_key_raises_on_classify(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = AnthropicProvider(model="claude-haiku")
    with pytest.raises(LLMError, match="requires ANTHROPIC_API_KEY"):
        p.classify("s", "u")


# ── is_external_service property (issue #24 — privacy warning support) ──
#
# `is_external_service` is True when this provider's endpoint sends data
# off the user's machine/network. Used by mempalace init to print a
# privacy warning before first run when an external API will receive
# folder content. URL-based heuristic: localhost, 127.x, ::1, .local,
# RFC1918 (10/8, 192.168/16, 172.16-31/12), and IPv6 ULA (fc/fd::) are
# all treated as local. Everything else is treated as external.


def test_ollama_provider_default_endpoint_is_local():
    """OllamaProvider's default endpoint is http://localhost:11434, which
    must be classified as local — no privacy warning fires for the
    typical user running Ollama on their own machine."""
    p = OllamaProvider(model="gemma4:e4b")
    assert p.is_external_service is False, (
        f"Default OllamaProvider endpoint must be local; got "
        f"is_external_service={p.is_external_service} for endpoint={p.endpoint}"
    )


def test_openai_compat_provider_localhost_endpoint_is_local():
    """LM Studio / llama.cpp server / vLLM commonly bind to localhost.
    Those setups must NOT trigger the external-API warning."""
    p = OpenAICompatProvider(model="any", endpoint="http://localhost:1234")
    assert p.is_external_service is False
    p_127 = OpenAICompatProvider(model="any", endpoint="http://127.0.0.1:8000")
    assert p_127.is_external_service is False
    p_lan = OpenAICompatProvider(model="any", endpoint="http://192.168.1.50:1234")
    assert p_lan.is_external_service is False, "LAN (RFC1918) endpoints must be local"


def test_openai_compat_provider_cloud_endpoint_is_external():
    """A user pointing openai-compat at OpenAI's hosted API or any other
    non-local endpoint MUST trigger the external warning."""
    p = OpenAICompatProvider(model="gpt-4o", endpoint="https://api.openai.com")
    assert p.is_external_service is True, (
        f"https://api.openai.com must be classified external; got "
        f"is_external_service={p.is_external_service}"
    )


def test_anthropic_provider_default_endpoint_is_external():
    """AnthropicProvider's default endpoint is https://api.anthropic.com,
    which is always external by definition. The privacy warning MUST
    fire by default for users who pass --llm-provider anthropic."""
    p = AnthropicProvider(model="claude-haiku-4-5", api_key="sk-test")
    assert p.is_external_service is True, (
        f"Default AnthropicProvider endpoint must be external; got "
        f"is_external_service={p.is_external_service} for endpoint={p.endpoint}"
    )


# ── Tailscale CGNAT range (issue #25 follow-up to #24) ──────────────────
#
# Tailscale assigns addresses in 100.64.0.0/10 (CGNAT range): first octet
# always 100, second octet 64-127 inclusive. Users running LM Studio /
# Ollama / any local LLM accessible via Tailscale would currently
# (post-#24, pre-#25) get a wrong privacy warning because the heuristic
# doesn't recognize CGNAT as private. These tests pin the fix.


def test_openai_compat_provider_tailscale_cgnat_endpoint_is_local():
    """Tailscale CGNAT range (100.64.0.0/10) — IPs where the first octet
    is 100 AND the second octet is 64-127 inclusive — must be classified
    as local. Tailscale users running LM Studio on their Tailnet should
    not trigger the external-API warning.
    """
    cases = [
        ("http://100.64.0.1:1234", "start of CGNAT"),
        ("http://100.100.50.50:1234", "middle of CGNAT (typical Tailscale assignment)"),
        ("http://100.127.255.254:1234", "near end of CGNAT"),
    ]
    for endpoint, label in cases:
        p = OpenAICompatProvider(model="any", endpoint=endpoint)
        assert p.is_external_service is False, (
            f"Tailscale CGNAT address {endpoint} ({label}) must be classified "
            f"local; got is_external_service={p.is_external_service}"
        )


def test_openai_compat_provider_outside_tailscale_cgnat_is_external():
    """Addresses in 100.x.x.x that fall OUTSIDE the CGNAT range
    (100.64.0.0 - 100.127.255.255) are public IPs in regular allocated
    space and must remain classified as external. Specifically: anything
    where the second octet is < 64 or > 127.
    """
    cases = [
        ("http://100.0.0.1:1234", "below CGNAT (public)"),
        ("http://100.63.255.255:1234", "just below CGNAT (boundary)"),
        ("http://100.128.0.0:1234", "just above CGNAT (boundary)"),
        ("http://100.255.255.255:1234", "well above CGNAT"),
    ]
    for endpoint, label in cases:
        p = OpenAICompatProvider(model="any", endpoint=endpoint)
        assert p.is_external_service is True, (
            f"Address {endpoint} ({label}) is OUTSIDE Tailscale CGNAT and "
            f"should remain external; got is_external_service={p.is_external_service}"
        )


# ── api_key_source provenance tracking (issue #26) ──────────────────────
#
# Distinguishes whether `api_key` was set via explicit constructor arg
# (= --llm-api-key flag → "flag") vs via environment-variable fallback
# (OPENAI_API_KEY / ANTHROPIC_API_KEY → "env"). cmd_init uses this to
# decide whether to block on a consent prompt: stray env-fallback keys
# require explicit user confirmation; explicit flag-passed keys are
# treated as already-consented.


def test_openai_compat_api_key_source_flag_when_explicit(monkeypatch):
    """When ``api_key`` is passed explicitly to the constructor, the
    provider records ``api_key_source == "flag"`` even if the same env
    var is also set. Flag wins over env."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env-irrelevant")
    p = OpenAICompatProvider(model="x", endpoint="http://h", api_key="sk-from-flag")
    assert p.api_key == "sk-from-flag"
    assert (
        p.api_key_source == "flag"
    ), f"Explicit api_key arg must produce api_key_source='flag'; got {p.api_key_source!r}"


def test_openai_compat_api_key_source_env_when_fallback(monkeypatch):
    """When ``api_key`` arg is None but ``OPENAI_API_KEY`` is set, the
    provider falls back to env and records ``api_key_source == "env"``.
    This is the "stray key" case — user didn't explicitly authorize this
    run to use the env-resolved credential."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    p = OpenAICompatProvider(model="x", endpoint="http://h")
    assert p.api_key == "sk-from-env"
    assert (
        p.api_key_source == "env"
    ), f"Env-fallback api_key must produce api_key_source='env'; got {p.api_key_source!r}"


def test_anthropic_api_key_source_tracking(monkeypatch):
    """AnthropicProvider tracks api_key_source the same way: 'flag' when
    passed explicitly, 'env' when resolved from ANTHROPIC_API_KEY env."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    p_flag = AnthropicProvider(model="claude-haiku", api_key="sk-ant-flag")
    assert (
        p_flag.api_key_source == "flag"
    ), f"Explicit api_key must produce 'flag'; got {p_flag.api_key_source!r}"
    p_env = AnthropicProvider(model="claude-haiku")
    assert p_env.api_key == "sk-ant-env"
    assert (
        p_env.api_key_source == "env"
    ), f"Env-fallback must produce 'env'; got {p_env.api_key_source!r}"


def test_ollama_api_key_source_is_none():
    """Ollama doesn't use api_key at all; ``api_key_source`` should be None."""
    p = OllamaProvider(model="gemma4:e4b")
    assert p.api_key is None
    assert p.api_key_source is None
