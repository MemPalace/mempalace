"""Tests for mempalace.classifier — T0/T2/T3 block classification."""

from mempalace.classifier import classify_block, is_hard_t0, HARD_T0_REASONS


# ─── T0: structural ──────────────────────────────────────────────────────────


def test_code_fence_is_t0():
    text = "```python\ndef foo():\n    return 1\n```"
    result = classify_block(text)
    assert result.decision == "T0"
    assert "code_fence" in result.reasons
    assert is_hard_t0(result)


def test_indented_code_is_t0():
    text = "    def foo():\n        return 1\n    x = foo()"
    result = classify_block(text)
    assert result.decision == "T0"
    assert "indented_code" in result.reasons


def test_json_is_t0():
    text = '{"name": "alice", "age": 30, "roles": ["admin", "user"]}'
    result = classify_block(text)
    assert result.decision == "T0"
    assert "json_structure" in result.reasons


def test_yaml_is_t0():
    text = "name: alice\nrole: admin\nversion: 1.2"
    result = classify_block(text)
    assert result.decision == "T0"


def test_sql_strong_anchor_is_t0():
    text = "SELECT id, name FROM users WHERE active = true ORDER BY created_at"
    result = classify_block(text)
    assert result.decision == "T0"
    assert "sql_content" in result.reasons


def test_sql_prose_false_positive_rejected():
    # "where" + "from" in normal prose should not trigger SQL
    text = "I came from the store where they sell apples."
    result = classify_block(text)
    # May still be T0 via other signals, but should NOT be tagged sql_content
    assert "sql_content" not in result.reasons


# ─── T0: guardrails ──────────────────────────────────────────────────────────


def test_python_traceback_is_t0():
    text = (
        "Traceback (most recent call last):\n"
        '  File "foo.py", line 42, in bar\n'
        "    raise ValueError('boom')\n"
        "ValueError: boom"
    )
    result = classify_block(text)
    assert result.decision == "T0"
    assert "error_traceback" in result.reasons


def test_http_error_is_t0():
    text = "The request failed with HTTP/1.1 401 Unauthorized — username is required."
    result = classify_block(text)
    assert result.decision == "T0"
    assert "http_error" in result.reasons


def test_explicit_failure_is_t0():
    text = "Authentication failed: invalid credentials for user admin."
    result = classify_block(text)
    assert result.decision == "T0"
    assert "failure_signature" in result.reasons


# ─── T0: content types ──────────────────────────────────────────────────────


def test_api_key_openai_style_is_t0():
    text = "Use key sk-abc123DEF456ghi789JKL012mno345PQR for the request."
    result = classify_block(text)
    assert result.decision == "T0"
    assert "api_key" in result.reasons


def test_url_is_soft_t0():
    text = "See the docs at https://example.com/docs/v2 for details."
    result = classify_block(text)
    assert result.decision == "T0"
    assert "url" in result.reasons
    # url is a soft T0 reason
    assert not is_hard_t0(result) or "url" not in HARD_T0_REASONS


# ─── T2 / T3 prose ───────────────────────────────────────────────────────────


def test_short_prose_is_t2():
    text = "We decided to ship the change today."
    result = classify_block(text)
    assert result.decision == "T2"
    assert result.reasons == []


def test_long_prose_is_t3():
    text = (
        "we spent a lot of time discussing the tradeoffs between the two "
        "approaches and eventually settled on the simpler option because it "
        "was easier to reason about and required less ongoing maintenance."
    )
    result = classify_block(text)
    assert result.decision == "T3"
    assert result.reasons == []


def test_empty_string_is_t2():
    result = classify_block("")
    assert result.decision == "T2"
    assert result.confidence == 0.0


def test_whitespace_only_is_t2():
    result = classify_block("   \n\n  \t")
    assert result.decision == "T2"


# ─── Confidence ──────────────────────────────────────────────────────────────


def test_confidence_grows_with_reasons():
    # Text that triggers multiple T0 reasons should have higher confidence
    mixed = (
        "Traceback (most recent call last):\n"
        '  File "app.py", line 10, in main\n'
        "```python\n"
        "def main():\n"
        "    pass\n"
        "```"
    )
    single = "Traceback (most recent call last):\n  File \"a.py\", line 1, in b\n    x()"
    multi_result = classify_block(mixed)
    single_result = classify_block(single)
    assert multi_result.confidence >= single_result.confidence


def test_t0_confidence_capped_at_0_95():
    # Many-reason text should not exceed the cap
    text = (
        "```python\ndef f():\n    return {'a': 1}\n```\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in f\n'
        "HTTP/1.1 500 Internal Server Error\n"
        "Execution failed: Connection refused\n"
        "https://example.com sk-" + "a" * 30 + "\n"
        "SELECT * FROM users WHERE id = 1 ORDER BY name"
    )
    result = classify_block(text)
    assert result.decision == "T0"
    assert result.confidence <= 0.95
