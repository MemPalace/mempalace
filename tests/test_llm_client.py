"""Tests for mempalace.llm_client.

HTTP is mocked throughout — these tests do not require a running Ollama
or network access. Live-provider smoke tests live outside the unit-test
suite.
"""

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from mempalace.llm_client import (
    AnthropicProvider,
    LLMError,
    OllamaProvider,
    OpenAICompatProvider,
    endpoint_is_local,
    get_provider,
    parse_json_reply,
    post_json,
    require_external_consent,
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


# ── post_json ─────────────────────────────────────────────────────


def test_post_json_success():
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"ok": true}'
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock_resp):
        result = post_json("http://x/y", {"a": 1}, {}, timeout=5)
    assert result == {"ok": True}


def test_post_json_http_error_wraps_as_llm_error():
    import io
    from urllib.error import HTTPError

    err = HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(b"model missing"))
    with patch("mempalace.llm_client.urlopen", side_effect=err):
        with pytest.raises(LLMError, match="HTTP 404"):
            post_json("http://x", {}, {}, timeout=5)


def test_post_json_url_error_wraps_as_llm_error():
    from urllib.error import URLError

    with patch("mempalace.llm_client.urlopen", side_effect=URLError("conn refused")):
        with pytest.raises(LLMError, match="Cannot reach"):
            post_json("http://x", {}, {}, timeout=5)


def test_post_json_malformed_response():
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"not json"
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock_resp):
        with pytest.raises(LLMError, match="Malformed"):
            post_json("http://x", {}, {}, timeout=5)


def test_post_json_http_error_carries_the_status():
    from urllib.error import HTTPError

    err = HTTPError("http://x", 429, "Too Many Requests", {}, None)
    with patch("mempalace.llm_client.urlopen", side_effect=err):
        with pytest.raises(LLMError) as raised:
            post_json("http://x", {}, {}, timeout=5)
    assert raised.value.status == 429


def test_post_json_wraps_protocol_faults_and_bad_urls():
    import http.client

    with patch("mempalace.llm_client.urlopen", side_effect=http.client.BadStatusLine("garbage")):
        with pytest.raises(LLMError, match="Cannot reach"):
            post_json("http://x", {}, {}, timeout=5)
    with pytest.raises(LLMError, match="Cannot reach"):
        post_json("not-a-url", {}, {}, timeout=5)


def test_post_json_sends_a_lone_surrogate_as_replacement_char():
    captured = {}
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"{}"
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False

    def fake_urlopen(req, timeout):
        captured["body"] = req.data
        return mock_resp

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        post_json("http://x", {"input": "stray \udcff byte"}, {}, timeout=5)
    assert "stray � byte" in captured["body"].decode("utf-8")


# ── parse_json_reply ────────────────────────────────────────────────────
# One golden reply per caller's format: kg normalize, rooms propose, corpus
# origin, entity refine and LLM closets.


@pytest.mark.parametrize(
    "reply, expected",
    [
        # kg normalize: a fenced object
        ('```json\n{"facts": [{"index": 1}]}\n```', {"facts": [{"index": 1}]}),
        # rooms propose: an untagged fence after a sentence
        (
            'Here are the rooms:\n```\n{"rooms": [{"name": "infra"}]}\n```',
            {"rooms": [{"name": "infra"}]},
        ),
        # corpus origin: prose before and after the object
        (
            'My answer: {"is_ai_dialogue_corpus": true, "confidence": 0.8} — done.',
            {"is_ai_dialogue_corpus": True, "confidence": 0.8},
        ),
        # entity refine: a top-level list with no wrapper
        ('[{"name": "Y", "label": "PERSON"}]', [{"name": "Y", "label": "PERSON"}]),
        # entity refine: prose brackets before the payload
        (
            'See [note] first. JSON: {"classifications": []}',
            {"classifications": []},
        ),
        # closets: a fence tagged with another language
        (
            '```javascript\n{"topics": ["x"], "quotes": [], "summary": "s"}\n```',
            {"topics": ["x"], "quotes": [], "summary": "s"},
        ),
        # braces and quotes inside strings don't end the object
        ('ok {"summary": "a } b \\" {", "n": 1} tail', {"summary": 'a } b " {', "n": 1}),
        # a reply cut off before its closing fence
        ('```json\n{"topics": ["t"]}', {"topics": ["t"]}),
    ],
)
def test_parse_json_reply_reads_every_callers_format(reply, expected):
    assert parse_json_reply(reply) == expected


def test_parse_json_reply_prefers_an_object_over_a_stray_list_before_it():
    reply = 'Per rule [1] here is the plan: {"facts": [{"index": 1, "object": "Acme"}]}'
    assert parse_json_reply(reply) == {"facts": [{"index": 1, "object": "Acme"}]}
    assert parse_json_reply(reply, expect=(dict, list)) == parse_json_reply(reply)


def test_parse_json_reply_falls_back_to_a_list_when_no_object_parses():
    assert parse_json_reply('prefix [{"name": "Y"}] suffix') == [{"name": "Y"}]


def test_parse_json_reply_expect_skips_values_of_another_type():
    reply = 'Counts: [1, 2]. Result: {"user_name": null}'
    assert parse_json_reply(reply, expect=dict) == {"user_name": None}
    assert parse_json_reply(reply, expect=list) == [1, 2]


@pytest.mark.parametrize(
    "reply",
    [
        # trailing comma: the nested "rooms" list must not stand in for the object
        '{"rooms": [{"slug": "a"}], "assignments": [{"room": "a"}],}',
        # unescaped quote: the quoted code object must not stand in for the reply
        '{"topics": ["cfg"], "quotes": ["[User] set "{"debug": true}" in it"], "summary": "s"}',
        # cut at the token limit, with a complete object quoted inside
        '{"topics": ["cfg"], "quotes": ["{\\"debug\\": true}", "an unfinished quo',
    ],
)
def test_parse_json_reply_never_returns_a_value_nested_in_a_failed_candidate(reply):
    with pytest.raises(ValueError, match="held no JSON"):
        parse_json_reply(reply, expect=dict)


@pytest.mark.parametrize("reply", ["", "   ", "no json here", "{broken", None])
def test_parse_json_reply_raises_when_the_reply_holds_none(reply):
    with pytest.raises(ValueError, match="held no JSON"):
        parse_json_reply(reply)


def test_post_json_drops_the_body_of_an_auth_error():
    from urllib.error import HTTPError

    body = io.BytesIO(b'{"error": "Incorrect API key provided: sk-SECRET1234567"}')
    err = HTTPError("http://x", 401, "Unauthorized", {}, body)
    with patch("mempalace.llm_client.urlopen", side_effect=err):
        with pytest.raises(LLMError) as raised:
            post_json("http://x", {}, {}, timeout=5)
    assert "SECRET" not in str(raised.value)
    assert "Unauthorized" in str(raised.value)


@pytest.mark.parametrize(
    "secret, leak",
    [
        ("token sk-SECRET1234567 rejected", "SECRET1234567"),
        ("header was Bearer abc.DEF-123_xyz", "abc.DEF-123_xyz"),
        ('{"api_key": "LONGSECRETVALUE99"}', "LONGSECRETVALUE99"),
    ],
)
def test_post_json_masks_keys_in_other_error_bodies(secret, leak):
    from urllib.error import HTTPError

    err = HTTPError("http://x", 500, "Server Error", {}, io.BytesIO(secret.encode()))
    with patch("mempalace.llm_client.urlopen", side_effect=err):
        with pytest.raises(LLMError) as raised:
            post_json("http://x", {}, {}, timeout=5)
    assert leak not in str(raised.value)


def test_post_json_masks_a_key_in_the_url():
    from urllib.error import URLError

    url = "http://x/v1?key=SECRETQUERYKEY"
    with patch("mempalace.llm_client.urlopen", side_effect=URLError("refused")):
        with pytest.raises(LLMError) as raised:
            post_json(url, {}, {}, timeout=5)
    assert "SECRETQUERYKEY" not in str(raised.value)


# A configured endpoint can carry its credential in the URL itself.
SECRET_URL = (
    "https://user:hunter2pass@llm.example/v1"
    "?key=SECRETKEY1&api_key=SECRETKEY2&access_token=SECRETKEY3"
)
URL_SECRETS = ("hunter2pass", "SECRETKEY1", "SECRETKEY2", "SECRETKEY3")


def _assert_masked(text):
    for secret in URL_SECRETS:
        assert secret not in text, text
    assert "llm.example" in text, text


def test_post_json_masks_url_userinfo_and_query_credentials():
    from urllib.error import URLError

    with patch("mempalace.llm_client.urlopen", side_effect=URLError("refused")):
        with pytest.raises(LLMError) as raised:
            post_json(SECRET_URL, {}, {}, timeout=5)
    _assert_masked(str(raised.value))


def test_ollama_check_available_masks_endpoint_credentials():
    from urllib.error import URLError

    with patch("mempalace.llm_client.urlopen", side_effect=URLError("refused")):
        ok, msg = OllamaProvider(model="m", endpoint=SECRET_URL).check_available()
    assert not ok
    _assert_masked(msg)


def test_openai_compat_check_available_masks_endpoint_credentials():
    from urllib.error import URLError

    with patch("mempalace.llm_client.urlopen", side_effect=URLError("refused")):
        ok, msg = OpenAICompatProvider(model="m", endpoint=SECRET_URL).check_available()
    assert not ok
    _assert_masked(msg)


def test_openai_compat_empty_model_list_masks_endpoint_credentials():
    mock = MagicMock()
    mock.read.return_value = json.dumps({"data": []}).encode()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock):
        ok, msg = OpenAICompatProvider(model="auto", endpoint=SECRET_URL).check_available()
    assert not ok
    _assert_masked(msg)


def test_openai_compat_unlisted_model_warning_masks_endpoint_credentials(caplog):
    mock = MagicMock()
    mock.read.return_value = json.dumps({"data": [{"id": "other"}]}).encode()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock):
        with caplog.at_level("WARNING", logger="mempalace"):
            ok, _ = OpenAICompatProvider(model="m", endpoint=SECRET_URL).check_available()
    assert ok
    _assert_masked(caplog.text)


def test_external_refusal_masks_endpoint_credentials():
    p = OpenAICompatProvider(model="m", endpoint=SECRET_URL)
    with pytest.raises(LLMError) as raised:
        p.classify("system", "user")
    assert "Refusing" in str(raised.value)
    _assert_masked(str(raised.value))


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
        p = OpenAICompatProvider(model="x", endpoint="http://localhost:1234")
        p.classify("s", "u")
    assert captured["url"] == "http://localhost:1234/v1/chat/completions"


def test_openai_compat_resolves_url_with_existing_v1():
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["url"] = req.full_url
        return _mock_openai_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        p = OpenAICompatProvider(model="x", endpoint="http://localhost:1234/v1")
        p.classify("s", "u")
    assert captured["url"] == "http://localhost:1234/v1/chat/completions"


def test_openai_compat_think_false_sends_reasoning_effort_none():
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["body"] = json.loads(req.data)
        return _mock_openai_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        p = OpenAICompatProvider(model="x", endpoint="http://localhost")
        p.classify("s", "u", think=False)
        assert captured["body"]["reasoning_effort"] == "none"
        p.classify("s", "u")
        assert "reasoning_effort" not in captured["body"]


def test_openai_compat_retries_without_reasoning_effort_on_400():
    from urllib.error import HTTPError

    bodies = []

    def fake_urlopen(req, *, timeout):
        body = json.loads(req.data)
        bodies.append(body)
        if "reasoning_effort" in body:
            raise HTTPError(req.full_url, 400, "unknown field", hdrs=None, fp=None)
        return _mock_openai_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        p = OpenAICompatProvider(model="x", endpoint="http://localhost")
        assert p.classify("s", "u", think=False).text == '{"ok": true}'
    assert len(bodies) == 2 and "reasoning_effort" not in bodies[1]


def test_openai_compat_empty_content_after_length_cutoff_names_the_cause():
    mock = MagicMock()
    payload = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    mock.read.return_value = json.dumps(payload).encode()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock):
        p = OpenAICompatProvider(model="x", endpoint="http://localhost")
        with pytest.raises(LLMError, match="finish_reason=length"):
            p.classify("s", "u")


def _models_response(ids):
    mock = MagicMock()
    mock.read.return_value = json.dumps({"data": [{"id": i} for i in ids]}).encode()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    return mock


def test_openai_compat_auto_model_follows_the_served_model():
    with patch("mempalace.llm_client.urlopen", return_value=_models_response(["qwen3.8-27b"])):
        p = OpenAICompatProvider(model="auto", endpoint="http://localhost:1")
        assert p.check_available() == (True, "ok")
        assert p.model == "qwen3.8-27b"


def test_openai_compat_check_warns_but_passes_on_model_mismatch(caplog):
    # A gateway's listing may be partial or spelled differently; the request
    # decides, so a mismatch is a warning that names the served models.
    with patch("mempalace.llm_client.urlopen", return_value=_models_response(["other"])):
        p = OpenAICompatProvider(model="gone", endpoint="http://localhost:1")
        with caplog.at_level("WARNING", logger="mempalace"):
            ok, msg = p.check_available()
    assert ok and msg == "ok"
    assert "other" in caplog.text and "auto" in caplog.text
    with patch("mempalace.llm_client.urlopen", return_value=_models_response([])):
        assert OpenAICompatProvider(model="x", endpoint="http://localhost:1").check_available()[0]


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
        p = OpenAICompatProvider(model="x", endpoint="http://localhost", api_key="sk-aaa")
        p.classify("s", "u")
    assert captured["auth"] == "Bearer sk-aaa"


def test_openai_compat_uses_env_var_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    p = OpenAICompatProvider(model="x", endpoint="http://localhost")
    assert p.api_key == "sk-from-env"


def test_openai_compat_sends_response_format_json():
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["body"] = json.loads(req.data.decode())
        return _mock_openai_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        p = OpenAICompatProvider(model="x", endpoint="http://localhost")
        p.classify("s", "u", json_mode=True)
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_openai_compat_unexpected_shape_raises():
    mock = MagicMock()
    mock.read.return_value = b'{"nothing": "here"}'
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    with patch("mempalace.llm_client.urlopen", return_value=mock):
        p = OpenAICompatProvider(model="x", endpoint="http://localhost")
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
        p.external_use_accepted = True
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
        p.external_use_accepted = True
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


def test_ollama_provider_defaultendpoint_is_local():
    """OllamaProvider's default endpoint is http://localhost:11434, which
    must be classified as local — no privacy warning fires for the
    typical user running Ollama on their own machine."""
    p = OllamaProvider(model="gemma4:e4b")
    assert p.is_external_service is False, (
        f"Default OllamaProvider endpoint must be local; got "
        f"is_external_service={p.is_external_service} for endpoint={p.endpoint}"
    )


def test_single_label_hostname_is_local_only_when_it_resolves_privately(monkeypatch):
    import socket

    def fake_getaddrinfo(host, *a, **k):
        table = {
            "gpu-box": [("192.168.1.20",), ("fe80::1%en0",)],
            "llm": [("8.8.8.8",)],
            "mixed": [("10.0.0.5",), ("8.8.8.8",)],
        }
        if host not in table:
            raise socket.gaierror("no such host")
        return [(None, None, None, None, addr) for addr in table[host]]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert endpoint_is_local("http://gpu-box:8010")
    assert endpoint_is_local("http://gpu-box:11434/v1")
    # A search domain can expand a bare label to a public host: external.
    assert not endpoint_is_local("http://llm:8010/v1")
    assert not endpoint_is_local("http://mixed:8010/v1")
    # Unresolvable is external too; the consent flag remains the override.
    assert not endpoint_is_local("http://nowhere:8010/v1")
    # An IPv6 literal is dotless too, but it is an address, not a LAN name.
    assert not endpoint_is_local("http://[2606:4700:4700::1111]:8010/v1")
    assert endpoint_is_local("http://[::1]:8010/v1")
    assert not endpoint_is_local("https://api.openai.com")
    assert not endpoint_is_local("http://gpu-box.example.com:8010")


def test_openai_compat_provider_localhostendpoint_is_local():
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


def test_openai_compat_provider_tailscale_cgnatendpoint_is_local():
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
    p = OpenAICompatProvider(model="x", endpoint="http://localhost", api_key="sk-from-flag")
    assert p.api_key == "sk-from-flag"
    assert p.api_key_source == "flag", (
        f"Explicit api_key arg must produce api_key_source='flag'; got {p.api_key_source!r}"
    )


def test_openai_compat_api_key_source_env_when_fallback(monkeypatch):
    """When ``api_key`` arg is None but ``OPENAI_API_KEY`` is set, the
    provider falls back to env and records ``api_key_source == "env"``.
    This is the "stray key" case — user didn't explicitly authorize this
    run to use the env-resolved credential."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    p = OpenAICompatProvider(model="x", endpoint="http://localhost")
    assert p.api_key == "sk-from-env"
    assert p.api_key_source == "env", (
        f"Env-fallback api_key must produce api_key_source='env'; got {p.api_key_source!r}"
    )


def test_anthropic_api_key_source_tracking(monkeypatch):
    """AnthropicProvider tracks api_key_source the same way: 'flag' when
    passed explicitly, 'env' when resolved from ANTHROPIC_API_KEY env."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    p_flag = AnthropicProvider(model="claude-haiku", api_key="sk-ant-flag")
    assert p_flag.api_key_source == "flag", (
        f"Explicit api_key must produce 'flag'; got {p_flag.api_key_source!r}"
    )
    p_env = AnthropicProvider(model="claude-haiku")
    assert p_env.api_key == "sk-ant-env"
    assert p_env.api_key_source == "env", (
        f"Env-fallback must produce 'env'; got {p_env.api_key_source!r}"
    )


def test_ollama_api_key_source_is_none():
    """Ollama doesn't use api_key at all; ``api_key_source`` should be None."""
    p = OllamaProvider(model="gemma4:e4b")
    assert p.api_key is None
    assert p.api_key_source is None


def test_private_range_prefixes_in_a_hostname_are_not_private():
    """A domain that merely starts like a private range is still a domain."""

    for url in (
        "https://10.example.com/v1",
        "https://192.168.example.com/v1",
        "https://172.16.example.com/v1",
        "https://100.64.example.com/v1",
        "https://fd.example.com/v1",
        "https://fdroid.example.org/v1",
        "https://fc-llm.example.net/v1",
    ):
        assert not endpoint_is_local(url), url


def test_private_ip_literals_stay_local_and_neighbours_do_not():

    for url in (
        "http://10.0.0.5:8000",
        "http://172.16.0.1:8000",
        "http://172.31.255.254:8000",
        "http://192.168.1.20:11434",
        "http://100.64.0.1:8000",
        "http://100.127.255.254:8000",
        "http://[fd00::1]:8000",
        "http://[fe80::1]:8000",
        "http://127.0.0.1:8000",
    ):
        assert endpoint_is_local(url), url
    for url in (
        "http://172.32.0.1:8000",
        "http://100.128.0.1:8000",
        "http://100.63.255.255:8000",
        "http://8.8.8.8:8000",
    ):
        assert not endpoint_is_local(url), url


def test_dot_local_names_are_resolved_not_trusted(monkeypatch):
    """Office networks reuse .local for unicast DNS; the suffix proves nothing."""
    import socket

    table = {"studio.local": "192.168.1.9", "corp.local": "8.8.8.8"}

    def fake_getaddrinfo(host, *a, **k):
        if host not in table:
            raise socket.gaierror("no such host")
        return [(None, None, None, None, (table[host], 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert endpoint_is_local("http://studio.local:11434")
    assert not endpoint_is_local("http://corp.local:11434")
    assert not endpoint_is_local("http://gone.local:11434")


def _served_models_request(monkeypatch, *, accepted):
    """The Request an openai-compat model listing sends, for an env-resolved key
    and an external endpoint."""
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    listing = MagicMock()
    listing.read.return_value = json.dumps({"data": [{"id": "m"}]}).encode()
    listing.__enter__.return_value = listing
    listing.__exit__.return_value = False
    p = OpenAICompatProvider(model="auto", endpoint="https://api.example.com")
    assert p.api_key_source == "env" and p.is_external_service
    p.external_use_accepted = accepted
    with patch("mempalace.llm_client.urlopen", return_value=listing) as opened:
        assert p.served_models() == ["m"]
    return opened.call_args[0][0]


def test_served_models_withholds_env_key_from_external_endpoint_without_consent(monkeypatch):
    request = _served_models_request(monkeypatch, accepted=False)
    assert request.get_header("Authorization") is None


def test_served_models_sends_env_key_once_external_use_is_accepted(monkeypatch):
    request = _served_models_request(monkeypatch, accepted=True)
    assert request.get_header("Authorization") == "Bearer env-key"


def test_classify_does_not_contact_external_endpoint_with_env_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")

    def boom(*_args, **_kwargs):
        raise AssertionError("contacted an external endpoint")

    with patch("mempalace.llm_client.urlopen", side_effect=boom):
        provider = OpenAICompatProvider(model="x", endpoint="https://api.openai.com/v1")
        with pytest.raises(LLMError, match="non-local"):
            provider.classify("system", "user text")


def test_classify_sends_env_key_when_external_use_is_accepted(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["auth"] = req.get_header("Authorization")
        return _mock_openai_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        provider = OpenAICompatProvider(model="x", endpoint="https://api.openai.com/v1")
        provider.external_use_accepted = True
        assert provider.classify("s", "u").text == '{"ok": true}'
    assert captured["auth"] == "Bearer sk-from-env"


def test_classify_refuses_external_endpoint_until_accepted_even_with_explicit_key(monkeypatch):
    monkeypatch.delenv("MEMPALACE_ACCEPT_EXTERNAL_LLM", raising=False)
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["auth"] = req.get_header("Authorization")
        return _mock_openai_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        provider = OpenAICompatProvider(
            model="x", endpoint="https://api.openai.com/v1", api_key="sk-flag"
        )
        with pytest.raises(LLMError, match="--accept-external-llm"):
            provider.classify("s", "u")
        assert captured == {}
        assert require_external_consent(provider, accept=True)
        provider.classify("s", "u")
    assert captured["auth"] == "Bearer sk-flag"


def test_ollama_classify_refuses_unaccepted_external_endpoint(monkeypatch):
    monkeypatch.delenv("MEMPALACE_ACCEPT_EXTERNAL_LLM", raising=False)
    with patch("mempalace.llm_client.urlopen", side_effect=AssertionError("contacted")):
        provider = OllamaProvider(model="m", endpoint="https://ollama.example.com")
        with pytest.raises(LLMError, match="MEMPALACE_ACCEPT_EXTERNAL_LLM"):
            provider.classify("s", "u")


@pytest.mark.parametrize("flag", ["1", "on", "Yes"])
def test_consent_env_var_accepts_without_a_prompt(monkeypatch, flag):
    monkeypatch.setenv("MEMPALACE_ACCEPT_EXTERNAL_LLM", flag)
    provider = OllamaProvider(model="m", endpoint="https://ollama.example.com")
    assert require_external_consent(provider)
    assert provider.external_use_accepted


def test_trailing_dot_localhost_is_local_and_backslash_netloc_is_not():
    assert endpoint_is_local("http://localhost.:11434/v1")
    assert not endpoint_is_local("http://evil.com\\@localhost:11434/v1")


def test_docker_host_is_resolved_not_trusted(monkeypatch):
    import socket

    answers = {"host.docker.internal": "192.168.65.254"}

    def fake_getaddrinfo(host, *a, **k):
        if host not in answers:
            raise socket.gaierror("no such host")
        return [(None, None, None, None, (answers[host], 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert endpoint_is_local("http://host.docker.internal:11434")
    # Outside Docker the same name can resolve anywhere.
    answers["host.docker.internal"] = "8.8.8.8"
    assert not endpoint_is_local("http://host.docker.internal:11434")


def test_resolution_uses_the_hostname_with_its_trailing_dot(monkeypatch):
    import socket

    looked_up = []

    def fake_getaddrinfo(host, *a, **k):
        looked_up.append(host)
        # A search domain could make the bare name resolve privately while
        # the absolute name urllib connects to is public.
        address = "192.168.1.20" if host == "gpu-box" else "8.8.8.8"
        return [(None, None, None, None, (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert not endpoint_is_local("http://gpu-box.:11434")
    assert looked_up == ["gpu-box."]


def test_classify_sends_env_key_to_local_endpoint(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured["auth"] = req.get_header("Authorization")
        return _mock_openai_response('{"ok": true}')

    with patch("mempalace.llm_client.urlopen", side_effect=fake_urlopen):
        provider = OpenAICompatProvider(model="x", endpoint="http://127.0.0.1:11434/v1")
        provider.classify("s", "u")
    assert captured["auth"] == "Bearer sk-from-env"


def test_anthropic_classify_does_not_contact_with_env_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")

    def boom(*_args, **_kwargs):
        raise AssertionError("contacted Anthropic")

    with patch("mempalace.llm_client.urlopen", side_effect=boom):
        provider = AnthropicProvider(model="claude-haiku")
        with pytest.raises(LLMError, match="non-local"):
            provider.classify("s", "u")
