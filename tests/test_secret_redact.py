"""Tests for mempalace.secret_redact."""

import pytest

from mempalace.secret_redact import contains_secret, redact_secrets

REDACTED = "[REDACTED_SECRET]"
# Obviously synthetic stand-in for a key body: base64 charset so the patterns
# engage, but self-evidently not real key material.
_B64 = "NOTAREALKEYNOTAREALKEY" + "A" * 300


def _pem(label: str = "") -> str:
    head = f"-----BEGIN {label} PRIVATE KEY-----" if label else "-----BEGIN PRIVATE KEY-----"
    tail = f"-----END {label} PRIVATE KEY-----" if label else "-----END PRIVATE KEY-----"
    return f"{head}\n{_B64}\n{tail}"


# A PKCS#8 block carries no label between BEGIN and PRIVATE KEY. It is the
# format Google service-account JSON uses, so a label-requiring pattern misses
# the most commonly leaked key type — this is the regression that matters most.
@pytest.mark.parametrize("label", ["", "RSA", "EC", "OPENSSH", "DSA"])
def test_pem_blocks_are_redacted_with_and_without_a_label(label):
    text = f"here is the key:\n{_pem(label)}\nend of key"
    out = redact_secrets(text)
    assert REDACTED in out
    assert "NOTAREALKEY" not in out
    assert "BEGIN" not in out


def test_truncated_pem_block_is_still_redacted():
    # Transcripts get chunked mid-key, so the END marker is often absent.
    text = "-----BEGIN PRIVATE KEY-----\n" + _B64
    out = redact_secrets(text)
    assert REDACTED in out
    assert "NOTAREALKEY" not in out


@pytest.mark.parametrize("escape", ["\\n", "\\\\n"])
def test_escaped_newline_pem_inside_json_is_redacted_at_any_escape_depth(escape):
    # A key inside JSON carries \n; the same JSON embedded in another JSON
    # document carries \\n. Both have to be redacted.
    text = (
        '{"private_key": "-----BEGIN PRIVATE KEY-----'
        + escape
        + _B64
        + escape
        + '-----END PRIVATE KEY-----"}'
    )
    out = redact_secrets(text)
    assert "NOTAREALKEY" not in out


def test_truncated_double_escaped_block_is_redacted():
    text = "-----BEGIN PRIVATE KEY-----\\\\n" + _B64
    out = redact_secrets(text)
    assert REDACTED in out
    assert "NOTAREALKEY" not in out


def test_private_key_json_field_is_redacted_even_without_pem_markers():
    text = (
        '{"private_key": "' + "z" * 80 + '", "client_email": "svc@example.iam.gserviceaccount.com"}'
    )
    out = redact_secrets(text)
    assert REDACTED in out
    assert "z" * 80 not in out
    # Non-secret sibling fields survive, so the drawer stays useful.
    assert "svc@example.iam.gserviceaccount.com" in out


# Slack fixtures are assembled at import time rather than written as literals.
# A literal in the live token shape trips secret scanners on push — the string
# holds no real credential, but a blocked push is indistinguishable from a real
# leak to the contributor on the other end.
_DIGITS = "123456789012"
_SLACK_FIXTURES = [
    "-".join(["xoxb", _DIGITS, "987654321098", "EXAMPLENOTAREALTOKEN"]),
    "-".join(["xoxp", _DIGITS, "987654321098", "1234567890", "EXAMPLENOTAREALTOKEN"]),
    "-".join(["xapp", "1", "A01234ABCDE", "1234567890123", "EXAMPLENOTAREALTOKEN"]),
    # Shape the module actually requires: prefix plus 10+ trailing characters.
    "xoxb-EXAMPLE-NOT-A-REAL-TOKEN",
]


@pytest.mark.parametrize("token", _SLACK_FIXTURES)
def test_slack_tokens_are_redacted(token):
    out = redact_secrets(f"SLACK_TOKEN={token}")
    assert token not in out
    assert REDACTED in out


def test_bearer_token_is_redacted_but_scheme_is_kept():
    out = redact_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out
    assert out == f"Authorization: Bearer {REDACTED}"


@pytest.mark.parametrize(
    "text",
    [
        "We discussed the xoxb- prefix and how tokens are shaped.",
        "The header is -----BEGIN PRIVATE KEY----- followed by base64.",
        "Set private_key in the config before deploying.",
        "def load_key(path):\n    return open(path).read()",
        "Bearer tokens expire after an hour.",
        "",
    ],
)
def test_discussion_and_ordinary_text_are_left_alone(text):
    assert redact_secrets(text) == text
    assert not contains_secret(text)


def test_contains_secret_detects_a_real_key():
    assert contains_secret(_pem())


def test_multiple_secrets_in_one_chunk_are_all_redacted():
    text = (
        f"first:\n{_pem()}\n"
        f"then a token {_SLACK_FIXTURES[0]}\n"
        "and a header Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\n"
    )
    out = redact_secrets(text)
    assert "NOTAREALKEY" not in out
    assert _SLACK_FIXTURES[0] not in out
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out
    assert out.count(REDACTED) >= 3


def test_redaction_is_idempotent():
    once = redact_secrets(_pem())
    assert redact_secrets(once) == once
