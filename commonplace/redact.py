"""Shared storage and display redactor interface."""

import re

MAX_CHARS = 16_000
KINDS = (
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

_EDGE = r"A-Za-z0-9_\-"
KEY = (
    r"[A-Za-z0-9_.-]{0,32}"
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|auth|lease)"
    r"[A-Za-z0-9_.-]{0,32}"
)
_SECRET_KEY = re.compile(KEY, re.IGNORECASE)
_BEGIN_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----")
_MARKER = re.compile(r"(\[(?:redacted|omitted):(?:" + "|".join(KINDS) + r")\])")
_VALUE = r"""(?:"[^"\n]{1,512}"|'[^'\n]{1,512}'|[^\s"',;]{4,512})"""


def _token(shape):
    return rf"(?<![{_EDGE}])(?P<value>{shape})(?![{_EDGE}])"


# Order is part of the contract. A named value group preserves labels and
# delimiters; the replacement itself never contains a credential hint.
_PATTERNS = tuple(
    (kind, re.compile(pattern))
    for kind, pattern in (
        ("cmmn_oauth", _token(r"(?:cmmn_at_[A-Za-z0-9_-]{43}|cmmn_rt_[A-Za-z0-9_-]{64})")),
        ("cmmn_token", _token(r"cmmn_[A-Za-z0-9_-]{43}")),
        (
            "session_cookie",
            r'(?i:\b(?:cmmn_session|cmmn_oauth|cmmn_signin)=)(?P<value>[^;\s"\\,]{1,4096})',
        ),
        ("login_link", r'[?&]t=(?P<value>[^&#\s"\\]{1,4096})'),
        (
            "push_topic",
            r"""(?:/|(?i:\btopic)[\s:="']{0,4})(?P<value>cp[A-Za-z0-9_-]{40})(?![A-Za-z0-9_-])""",
        ),
        ("aws_key", r"(?<![A-Z0-9])(?P<value>(?:AKIA|ASIA)[A-Z0-9]{16})(?![A-Z0-9])"),
        (
            "github_token",
            _token(r"(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})"),
        ),
        ("gitlab_token", _token(r"glpat-[A-Za-z0-9_-]{20,4096}")),
        ("anthropic_key", _token(r"sk-ant-[A-Za-z0-9_-]{20,4096}")),
        ("openai_key", _token(r"sk-(?!ant-)[A-Za-z0-9_-]{20,4096}")),
        (
            "slack_token",
            _token(
                r"(?:xox[baprs]-[A-Za-z0-9-]{10,4096}|"
                r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{1,4096})"
            ),
        ),
        ("stripe_key", _token(r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,4096}")),
        ("google_api_key", _token(r"AIza[A-Za-z0-9_-]{35}")),
        ("npm_token", _token(r"npm_[A-Za-z0-9]{36}")),
        (
            "jwt",
            rf"(?<![{_EDGE}.])(?P<value>eyJ[{_EDGE}]{{5,4096}}\."
            rf"[{_EDGE}]{{8,4096}}\.[{_EDGE}]{{8,4096}})(?![{_EDGE}.])",
        ),
        ("bearer", r"(?i)\b(?P<value>Bearer\s{1,4}[A-Za-z0-9._~+/=-]{8,4096})"),
        ("basic_auth", r"(?i)\b(?P<value>Basic\s{1,4}[A-Za-z0-9+/=]{8,4096})"),
        (
            "auth_header",
            r"(?i)\b(?:Authorization|Proxy-Authorization|X-Api-Key|X-Auth-Token|Private-Token)"
            r":\s{0,4}(?P<value>[^\s\r\n][^\r\n]{0,4095})",
        ),
        (
            "cookie_header",
            r"(?i)\b(?:Cookie|Set-Cookie):\s{0,4}(?P<value>[^\s\r\n][^\r\n]{0,4095})",
        ),
        (
            "url_credentials",
            r"\b[A-Za-z][A-Za-z0-9+.-]{0,63}://"
            r"(?P<value>[^\s/:@]{1,4096}:[^\s/@]{1,4096}|[A-Za-z0-9_-]{20,4096})@",
        ),
        (
            "query_secret",
            r"(?i)[?&](?:token|access_token|refresh_token|id_token|key|api_key|apikey|secret|"
            r"client_secret|password|passwd|pwd|sig|signature|auth|code|code_verifier|state|nonce)"
            r"=(?P<value>[^&#\s]{1,4096})",
        ),
        (
            "json_secret",
            rf'(?i)\\?"{KEY}\\?"\s{{0,4}}:\s{{0,4}}\\?"(?P<value>[^"\\\n]{{4,4096}})\\?"',
        ),
        (
            "assignment",
            rf"(?i)(?<![A-Za-z0-9_.-]){KEY}\s{{0,4}}[:=]\s{{0,4}}(?P<value>{_VALUE})",
        ),
        ("cli_flag", rf"(?i)--{KEY}(?:=|\s{{1,4}})(?P<value>{_VALUE})"),
        ("random_token", _token(r"(?:[A-Za-z0-9_-]{43}|[0-9a-f]{32})")),
    )
)


def _private_keys(text):
    """Consume each block once, including an incomplete final block."""
    parts, pos = [], 0
    while match := _BEGIN_KEY.search(text, pos):
        end = text.find("-----END", match.end())
        newline = text.find("\n", end) if end != -1 else -1
        stop = len(text) if newline == -1 else newline
        parts.extend((text[pos : match.start()], "[redacted:private_key]"))
        pos = stop
    parts.append(text[pos:])
    return "".join(parts), bool(pos)


def redact(text) -> tuple[str, list[str]]:
    """Redact bounded strings and report unique kinds in pattern order."""
    if not isinstance(text, str):
        return text, []
    if len(text) > MAX_CHARS:
        return "[omitted:too_long]", ["too_long"]

    text, private = _private_keys(text)
    kinds = ["private_key"] if private else []
    for kind, pattern in _PATTERNS:
        found = False

        def replace(match, kind=kind):
            nonlocal found
            found = True
            start, end = match.span("value")
            return (
                match.string[match.start() : start]
                + f"[redacted:{kind}]"
                + match.string[end : match.end()]
            )

        # Protect both incoming and newly produced markers from every later
        # rule, including broad header, assignment and JSON value patterns.
        parts = _MARKER.split(text)
        for index in range(0, len(parts), 2):
            parts[index] = pattern.sub(replace, parts[index])
        text = "".join(parts)
        if found:
            kinds.append(kind)
    return text, kinds


def redact_value(obj) -> tuple[object, list[str]]:
    """Copy JSON containers, replacing entire values under secret keys."""
    if isinstance(obj, dict):
        clean, kinds = {}, set()
        for key, value in obj.items():
            if isinstance(key, str) and _SECRET_KEY.fullmatch(key):
                if isinstance(value, str) and _MARKER.fullmatch(value):
                    clean[key] = value
                else:
                    clean[key] = "[redacted:json_secret_key]"
                    kinds.add("json_secret_key")
            else:
                clean[key], found = redact_value(value)
                kinds.update(found)
        return clean, [kind for kind in KINDS if kind in kinds]
    if isinstance(obj, list):
        clean, kinds = [], set()
        for value in obj:
            item, found = redact_value(value)
            clean.append(item)
            kinds.update(found)
        return clean, [kind for kind in KINDS if kind in kinds]
    return redact(obj)
