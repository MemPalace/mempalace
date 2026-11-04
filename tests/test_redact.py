"""U1's pure redaction contract; all credential-shaped values are synthetic."""

import json
import re
import secrets
import string
from pathlib import Path

import pytest

from commonplace import redact as redactor

FROZEN_KINDS = (
    "private_key",
    "cmmn_oauth",
    "cmmn_token",
    "session_cookie",
    "login_link",
    "push_topic",
    "aws_key",
    "github_token",
    "gitlab_token",
    "anthropic_key",
    "openai_key",
    "slack_token",
    "stripe_key",
    "google_api_key",
    "npm_token",
    "jwt",
    "bearer",
    "basic_auth",
    "auth_header",
    "cookie_header",
    "url_credentials",
    "query_secret",
    "json_secret",
    "assignment",
    "cli_flag",
    "random_token",
    "json_secret_key",
    "too_long",
)
CORPUS = json.loads((Path(__file__).parent / "fixtures/console_secrets.json").read_text())


def test_kinds_never_shrink():
    assert isinstance(redactor.KINDS, tuple)
    assert set(FROZEN_KINDS) <= set(redactor.KINDS)
    assert len(set(redactor.KINDS)) == len(redactor.KINDS)
    assert redactor.MAX_CHARS == 16_000


def expand(template):
    values = []

    def replace(match):
        alphabet = {
            "urlsafe": string.ascii_letters + string.digits + "_-",
            "alnum": string.ascii_letters + string.digits,
            "upper": string.ascii_uppercase + string.digits,
            "hex": string.hexdigits[:16],
            "base64": string.ascii_letters + string.digits + "+/",
        }[match[1]]
        value = "".join(secrets.choice(alphabet) for _ in range(int(match[2])))
        values.append(value)
        return value

    return re.sub(r"\{(urlsafe|alnum|upper|hex|base64):(\d+)\}", replace, template), values


@pytest.mark.parametrize("row", CORPUS["templates"], ids=lambda row: row["kind"])
def test_frozen_corpus_in_every_context(row):
    text, values = expand(row["template"])
    assert values
    for context in row["contexts"]:
        if context == "object":
            clean, kinds = redactor.redact_value({"api_token": text})
            rendered = json.dumps(clean)
        else:
            wrapped = {
                "prose": text,
                "json": json.dumps({"message": text}),
                "json_in_string": json.dumps({"message": json.dumps({"message": text})}),
            }[context]
            rendered, kinds = redactor.redact(wrapped)
        assert all(value not in rendered for value in values)
        assert row["kind"] in kinds
        assert f"[redacted:{row['kind']}]" in rendered


@pytest.mark.parametrize("text", CORPUS["negative_controls"])
def test_negative_controls_survive(text):
    assert redactor.redact(text) == (text, [])


@pytest.mark.parametrize("value", [None, True, 5, 3.5, b"password=synthetic", ("text",)])
def test_non_strings_are_returned_unchanged(value):
    clean, kinds = redactor.redact(value)
    assert clean is value and kinds == []


def test_patterns_run_in_contract_order_and_kinds_are_unique():
    assert [kind for kind, _ in redactor._PATTERNS] == list(FROZEN_KINDS[1:-2])
    token = "cmmn_at_" + secrets.token_urlsafe(32)
    text = f'password="{token}" ; {token} ; --api-key opaque-value'
    clean, kinds = redactor.redact(text)
    assert kinds == ["cmmn_oauth", "cli_flag"]
    assert clean.count("[redacted:cmmn_oauth]") == 2
    assert "[redacted:cli_flag]" in clean
    assert token not in clean


@pytest.mark.parametrize("label", ["", "RSA ", "EC ", "ENCRYPTED ", "OPENSSH ", "A" * 39 + " "])
@pytest.mark.parametrize("block", ["", " BLOCK"])
def test_procedural_private_key_scan(label, block):
    text = (
        f"before\n-----BEGIN {label}PRIVATE KEY{block}-----\n"
        f"synthetic body\n-----END {label}PRIVATE KEY{block}----- trailing on END line\nafter"
    )
    assert redactor.redact(text) == ("before\n[redacted:private_key]\nafter", ["private_key"])


@pytest.mark.parametrize("tail", ["synthetic body", "-----END PRIVATE KEY-----"])
def test_private_key_to_end_without_closing_line_or_newline(tail):
    assert redactor.redact("before -----BEGIN PRIVATE KEY-----\n" + tail) == (
        "before [redacted:private_key]",
        ["private_key"],
    )


def test_private_key_scanner_resumes_after_each_end_line():
    block = "-----BEGIN PRIVATE KEY-----\nbody\n-----END PRIVATE KEY-----\n"
    assert redactor.redact(block + "between\n" + block + "after") == (
        "[redacted:private_key]\nbetween\n[redacted:private_key]\nafter",
        ["private_key"],
    )


@pytest.mark.parametrize("kind", FROZEN_KINDS)
def test_markers_are_never_rematched(kind):
    marker = f"[redacted:{kind}]"
    for text in (marker, f'{{"password": "{marker}"}}', f"Authorization: {marker}"):
        assert redactor.redact(text) == (text, [])
    assert redactor.redact_value({"password": marker}) == ({"password": marker}, [])


@pytest.mark.parametrize("container", [False, True])
def test_value_starting_with_marker_redacts_suffix(container):
    text = "[redacted:assignment] suffix password=synthetic"
    if container:
        assert redactor.redact_value({"password": text}) == (
            {"password": "[redacted:json_secret_key]"},
            ["json_secret_key"],
        )
    else:
        assert redactor.redact(text) == (
            "[redacted:assignment] suffix password=[redacted:assignment]",
            ["assignment"],
        )


@pytest.mark.parametrize(
    "prefix, size, kind", [("cmmn_", 43, "cmmn_token"), ("ghp_", 36, "github_token")]
)
@pytest.mark.parametrize("container", [False, True])
def test_leading_marker_does_not_exempt_token_suffix(prefix, size, kind, container):
    text = "[redacted:assignment] " + prefix + "Z" * size
    expected = "[redacted:assignment] [redacted:" + kind + "]"
    if container:
        assert redactor.redact_value({"text": text}) == ({"text": expected}, [kind])
    else:
        assert redactor.redact(text) == (expected, [kind])


def test_embedded_markers_do_not_hide_other_secrets():
    token = secrets.token_urlsafe(32)
    text = f"before [redacted:bearer] then {token} after"
    clean, kinds = redactor.redact(text)
    assert clean == "before [redacted:bearer] then [redacted:random_token] after"
    assert kinds == ["random_token"]
    assert redactor.redact(clean) == (clean, [])


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "PASSWD",
        "pwd",
        "client_secret",
        "lease_token",
        "api-key",
        "AccessKey",
        "prefix.private_key.suffix",
        "credential",
        "AUTH",
        "lease",
        "prefix.token-suffix",
    ],
)
@pytest.mark.parametrize("value", [None, 5, False, "x", ["nested"], {"nested": "value"}])
def test_dict_secret_keys_replace_entire_value(key, value):
    obj = {key: value}
    assert redactor.redact_value(obj) == ({key: "[redacted:json_secret_key]"}, ["json_secret_key"])
    assert obj[key] is value


def test_redact_value_recurses_without_mutating_input():
    token = "cmmn_" + secrets.token_urlsafe(32)
    original = {"rows": [{"text": token, "password": ["keep private"]}, token, 5], 7: True}
    snapshot = json.dumps(original)
    clean, kinds = redactor.redact_value(original)
    assert clean == {
        "rows": [
            {"text": "[redacted:cmmn_token]", "password": "[redacted:json_secret_key]"},
            "[redacted:cmmn_token]",
            5,
        ],
        7: True,
    }
    assert kinds == ["cmmn_token", "json_secret_key"]
    assert json.dumps(original) == snapshot
    assert clean is not original and clean["rows"] is not original["rows"]


@pytest.mark.parametrize("cookie", ["cmmn_session", "cmmn_oauth", "cmmn_signin"])
def test_cookie_value_only_and_cookie_separator(cookie):
    assert redactor.redact(f"{cookie}=opaque-value; other=ok") == (
        f"{cookie}=[redacted:session_cookie]; other=ok",
        ["session_cookie"],
    )


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "key",
        "api_key",
        "apikey",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "sig",
        "signature",
        "auth",
        "code",
        "code_verifier",
        "state",
        "nonce",
    ],
)
@pytest.mark.parametrize("separator", ["?", "&"])
def test_query_secret_names_and_delimiters(key, separator):
    text = f"/path{separator}{key}=opaque-value&ordinary=ok#fragment"
    assert redactor.redact(text) == (
        f"/path{separator}{key}=[redacted:query_secret]&ordinary=ok#fragment",
        ["query_secret"],
    )


@pytest.mark.parametrize("separator", ["?", "&"])
def test_login_link_precedes_query_and_random_rules(separator):
    token = secrets.token_urlsafe(32)
    assert redactor.redact(f"/app/login{separator}t={token}&next=ok") == (
        f"/app/login{separator}t=[redacted:login_link]&next=ok",
        ["login_link"],
    )


@pytest.mark.parametrize("prefix", ["/", "topic ", "topic=", "topic: ", '"topic":"'])
def test_push_topic_contexts(prefix):
    topic = "cp" + secrets.token_urlsafe(30)
    assert redactor.redact(prefix + topic) == (prefix + "[redacted:push_topic]", ["push_topic"])


@pytest.mark.parametrize(
    "prefix,length,kind",
    [("cmmn_at_", 43, "cmmn_oauth"), ("cmmn_rt_", 64, "cmmn_oauth"), ("cmmn_", 43, "cmmn_token")],
)
def test_cmmn_token_edges_and_exact_lengths(prefix, length, kind):
    token = prefix + "A" * length
    assert redactor.redact(f"({token})") == (f"([redacted:{kind}])", [kind])
    for text in (token + "A", "A" + token, token[:-1]):
        assert redactor.redact(text) == (text, [])


@pytest.mark.parametrize("prefix", ["ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"])
def test_github_prefixes(prefix):
    assert redactor.redact(prefix + "A" * 36) == ("[redacted:github_token]", ["github_token"])


@pytest.mark.parametrize("prefix", ["sk_live_", "sk_test_", "rk_live_", "rk_test_"])
def test_stripe_prefixes(prefix):
    assert redactor.redact(prefix + "A" * 24) == ("[redacted:stripe_key]", ["stripe_key"])


def test_slack_webhook_url():
    text = "https://hooks.slack.com/services/T00000000/B00000000/synthetic-value"
    assert redactor.redact(text) == ("[redacted:slack_token]", ["slack_token"])


@pytest.mark.parametrize(
    "name,kind",
    [
        ("Authorization", "auth_header"),
        ("Proxy-Authorization", "auth_header"),
        ("X-Api-Key", "auth_header"),
        ("X-Auth-Token", "auth_header"),
        ("Private-Token", "auth_header"),
        ("Cookie", "cookie_header"),
        ("Set-Cookie", "cookie_header"),
    ],
)
def test_headers_replace_value_through_line_end(name, kind):
    assert redactor.redact(f"{name}: opaque value; another=value\nnext line") == (
        f"{name}: [redacted:{kind}]\nnext line",
        [kind],
    )


@pytest.mark.parametrize("scheme", ["https", "postgresql", "git+ssh"])
@pytest.mark.parametrize("credentials", ["user:synthetic", "A" * 20])
def test_url_credentials_preserve_host_and_path(scheme, credentials):
    assert redactor.redact(f"{scheme}://{credentials}@example.invalid/path") == (
        f"{scheme}://[redacted:url_credentials]@example.invalid/path",
        ["url_credentials"],
    )


@pytest.mark.parametrize(
    "text",
    [
        'password="x"',
        "pwd='x'",
        "token: bare-value",
        "API_KEY=bare-value",
        "--password=bare-value",
        "--api-key 'x'",
    ],
)
def test_assignments_and_flags(text):
    # KEY allows '-' in its prefix, so the earlier assignment rule also
    # covers --KEY=value. Space-separated flags reach the cli_flag rule.
    kind = "cli_flag" if text.startswith("--") and "=" not in text else "assignment"
    clean, kinds = redactor.redact(text)
    assert kinds == [kind] and f"[redacted:{kind}]" in clean
    assert "bare-value" not in clean and "'x'" not in clean and '"x"' not in clean


@pytest.mark.parametrize(
    "context", [lambda text: text, json.dumps, lambda text: json.dumps({"message": text})]
)
def test_json_secret_in_nested_json_strings(context):
    text = context('{"lease_token": "synthetic-value"}')
    clean, kinds = redactor.redact(text)
    assert kinds == ["json_secret"] and "synthetic-value" not in clean
    assert "[redacted:json_secret]" in clean


def test_token_hint_false_positives_are_intentional():
    text = "the token_hint cmmn_…abcd is shown"
    assert redactor.redact(text) == (text, [])
    assert redactor.redact('"token_hint": "cmmn_…abcd"')[1] == ["json_secret"]
    assert redactor.redact("token_hint: cmmn_…abcd")[1] == ["assignment"]


@pytest.mark.parametrize(
    "source,make,context,kind",
    [
        (
            "webhooks.py:279",
            lambda: secrets.token_urlsafe(32),
            lambda value: json.dumps({"secret": value}),
            "json_secret",
        ),
        (
            "runner/jobs.py:306",
            lambda: secrets.token_urlsafe(32),
            lambda value: json.dumps({"lease_token": value}),
            "json_secret",
        ),
        (
            "handoff/service.py:164",
            lambda: secrets.token_hex(16),
            lambda value: json.dumps({"lease_token": value}),
            "json_secret",
        ),
        (
            "sessions.py:116",
            lambda: secrets.token_urlsafe(32),
            lambda value: "/app/login?t=" + value,
            "login_link",
        ),
        (
            "sessions.py:175",
            lambda: secrets.token_urlsafe(32),
            lambda value: "cmmn_session=" + value,
            "session_cookie",
        ),
        (
            "email_auth.py:180",
            lambda: secrets.token_urlsafe(32),
            lambda value: "/app/login?t=" + value,
            "login_link",
        ),
        (
            "oauth/__init__.py:198",
            lambda: secrets.token_urlsafe(32),
            lambda value: "cmmn_oauth=" + value,
            "session_cookie",
        ),
        (
            "oauth/__init__.py:199",
            lambda: secrets.token_urlsafe(32),
            lambda value: "cmmn_oauth=" + value,
            "session_cookie",
        ),
        (
            "social/__init__.py:157",
            lambda: secrets.token_urlsafe(32),
            lambda value: "?state=" + value,
            "query_secret",
        ),
        (
            "social/__init__.py:157",
            lambda: secrets.token_urlsafe(32),
            lambda value: "?code_verifier=" + value,
            "query_secret",
        ),
        (
            "social/__init__.py:157",
            lambda: secrets.token_urlsafe(32),
            lambda value: "?nonce=" + value,
            "query_secret",
        ),
        (
            "oauth/store.py:231",
            lambda: secrets.token_urlsafe(32),
            lambda value: "?code=" + value,
            "query_secret",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_unprefixed_sources_including_n9_authorization_code(source, make, context, kind):
    value = make()
    clean, kinds = redactor.redact(context(value))
    assert value not in clean and f"[redacted:{kind}]" in clean
    # Contextual rules run before the bare-token fallback.
    assert kinds == [kind], source
    assert redactor.redact(value) == ("[redacted:random_token]", ["random_token"])


def test_n9_oauth_client_id_is_not_a_credential():
    # oauth/__init__.py:127 uses token_urlsafe(24), not token_urlsafe(32).
    client_id = "cmmn_client_" + secrets.token_urlsafe(24)
    for text in (client_id, "?client_id=" + client_id, json.dumps({"client_id": client_id})):
        assert redactor.redact(text) == (text, [])
    assert redactor.redact_value({"client_id": client_id}) == ({"client_id": client_id}, [])


def test_random_token_edges():
    for token in (secrets.token_urlsafe(32), secrets.token_hex(16)):
        assert redactor.redact(f"({token})") == ("([redacted:random_token])", ["random_token"])
        for text in ("x" + token, token + "x", "_" + token, token + "-"):
            assert redactor.redact(text) == (text, [])


def test_oversized_input_is_omitted_before_any_scan(monkeypatch):
    def forbidden_scan(_):
        raise AssertionError("oversized input was scanned")

    monkeypatch.setattr(redactor, "_private_keys", forbidden_scan)
    for text in ("x" * (redactor.MAX_CHARS + 1), "x" * 1_000_000):
        assert redactor.redact(text) == ("[omitted:too_long]", ["too_long"])


def test_exact_maximum_length_is_scanned():
    text = "a." * 8000
    assert len(text) == redactor.MAX_CHARS
    assert redactor.redact(text) == (text, [])
    assert redactor.redact("-" * 16_000) == ("-" * 16_000, [])


def test_three_hundred_unterminated_private_key_begins_return():
    text = "-----BEGIN PRIVATE KEY-----\n" * 300
    assert redactor.redact(text) == ("[redacted:private_key]", ["private_key"])


def test_regex_quantifiers_are_bounded_at_4096():
    for pattern in [redactor._BEGIN_KEY, redactor._MARKER, redactor._SECRET_KEY] + [
        p for _, p in redactor._PATTERNS
    ]:
        # Strip character classes and escaped literals before inspecting the
        # quantifiers (a + in a base64 character class is not a quantifier).
        operators = re.sub(r"\[(?:\\.|[^\]\\])*\]|\\.", "", pattern.pattern)
        assert "*" not in operators and "+" not in operators
        for quantifier in re.findall(r"\{([^}]+)\}", operators):
            bounds = quantifier.split(",")
            assert all(bound.isdigit() for bound in bounds)
            assert max(map(int, bounds)) <= 4096


def test_unrecognized_marker_does_not_exempt_assignment():
    assert redactor.redact("password=[redacted:hunterhunter]") == (
        "password=[redacted:assignment]",
        ["assignment"],
    )


def test_unrecognized_marker_does_not_exempt_secret_value():
    assert redactor.redact_value({"password": "[redacted:hunterhunter]"}) == (
        {"password": "[redacted:json_secret_key]"},
        ["json_secret_key"],
    )
