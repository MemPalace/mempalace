"""
scanner.py — Sensitive content detection for MemPalace.

Scans text for common secret patterns (API keys, tokens, passwords,
private keys) and returns findings.  Advisory only — never blocks storage.
"""

import re

PATTERNS = {
    "api_key": re.compile(
        r"(?:sk-(?:proj-|ant-|or-)|AKIA|ghp_|gho_|github_pat_|sk_live_|sk_test_|xoxb-|xoxp-|npm_)"
        r"[A-Za-z0-9_-]{20,}"
    ),
    "bearer_token": re.compile(
        r"Bearer\s+[A-Za-z0-9_-]{20,}", re.IGNORECASE
    ),
    "password_assignment": re.compile(
        r"""(?:password|passwd|pwd)["']?\s*[=:]\s*['"][^'"$][^'"]*['"]""", re.IGNORECASE
    ),
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"
    ),
    "connection_string": re.compile(
        r"(?:mongodb|postgres|mysql|redis)://[^\s'\"]{10,}", re.IGNORECASE
    ),
}


def scan_content(content):
    """Scan content for sensitive patterns.

    Returns a list of dicts: {pattern_name, start, end}.
    Accepts None gracefully (returns empty list).
    """
    if not content:
        return []
    findings = []
    for name, pattern in PATTERNS.items():
        for m in pattern.finditer(content):
            findings.append({
                "pattern_name": name,
                "start": m.start(),
                "end": m.end(),
            })
    return findings


def format_warnings(findings):
    """Format findings into a human-readable warning string.

    Never includes secret content — only pattern names and positions.
    """
    if not findings:
        return ""
    lines = ["WARNING: Sensitive content detected:"]
    for f in findings:
        lines.append(
            f"  - {f['pattern_name']} at chars {f['start']}-{f['end']}"
        )
    return "\n".join(lines)
