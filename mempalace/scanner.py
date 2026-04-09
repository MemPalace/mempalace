"""
scanner.py — Sensitive content detection for MemPalace.

Scans text for common secret patterns (API keys, tokens, passwords,
private keys) and returns findings.  Advisory only — never blocks storage.
"""

import re

PATTERNS = {
    "api_key": re.compile(
        r"(?:sk-|AKIA|ghp_|gho_|github_pat_)[A-Za-z0-9_-]{20,}"
    ),
    "bearer_token": re.compile(
        r"Bearer\s+[A-Za-z0-9_-]{20,}"
    ),
    "password_assignment": re.compile(
        r"""(?:password|passwd|pwd)["']?\s*[=:]\s*['"][^'"]+['"]""", re.IGNORECASE
    ),
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"
    ),
}


def scan_content(content):
    """Scan content for sensitive patterns.

    Returns a list of dicts: {pattern_name, match, start, end}.
    """
    findings = []
    for name, pattern in PATTERNS.items():
        for m in pattern.finditer(content):
            findings.append({
                "pattern_name": name,
                "match": m.group(),
                "start": m.start(),
                "end": m.end(),
            })
    return findings


def format_warnings(findings):
    """Format findings into a human-readable warning string."""
    if not findings:
        return ""
    lines = ["WARNING: Sensitive content detected:"]
    for f in findings:
        preview = f["match"][:40] + "..." if len(f["match"]) > 40 else f["match"]
        lines.append(f"  - {f['pattern_name']}: {preview}")
    return "\n".join(lines)
