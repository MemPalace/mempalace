"""Strip credential-shaped strings out of text before it is persisted.

Mined conversation transcripts routinely contain real credentials — a shell
session that echoed a service-account JSON, a curl command with an auth header,
a token pasted for debugging. Once a chunk is embedded into the vector store the
secret is durable: it survives on disk, is returned by search, and gets copied
into any export or backup.

This module is deliberately regex-only and dependency-free so it can run inline
on the ingest path without a network call or model load.
"""

import re

_REDACTED = "[REDACTED_SECRET]"

# PEM blocks. Two things matter here beyond the obvious:
#   * The label between BEGIN and PRIVATE KEY is optional. PKCS#8 keys — the
#     format Google service-account JSON uses — are plain
#     "-----BEGIN PRIVATE KEY-----", with no RSA/EC/OPENSSH word. A pattern that
#     requires a label silently misses the single most common leaked key type.
#   * The END marker is optional. Transcripts get chunked mid-key, so a block
#     may be truncated; we still redact the base64 body we can see rather than
#     failing to match and writing the fragment out verbatim.
_PEM_BEGIN = r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
_PATTERNS = [
    # Complete PEM block.
    (re.compile(_PEM_BEGIN + r"[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----"), _REDACTED),
    # Truncated PEM block: header plus a long base64 run, escaped \n included.
    (re.compile(_PEM_BEGIN + r"(?:\\n|[A-Za-z0-9+/=\s]){40,}"), _REDACTED),
    # JSON private_key field, whether or not the body looks like PEM.
    (
        re.compile(r'"private_key"\s*:\s*"(?:\\.|[^"\\]){40,}"'),
        '"private_key": ' + f'"{_REDACTED}"',
    ),
    # Slack tokens: bot, user, app-level, legacy workspace variants.
    (re.compile(r"\bxox[abpres]-[A-Za-z0-9-]{10,}"), _REDACTED),
    (re.compile(r"\bxapp-[A-Za-z0-9-]{10,}"), _REDACTED),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{20,}={0,2}"), "Bearer " + _REDACTED),
]


def redact_secrets(text: str) -> str:
    """Return ``text`` with credential-shaped substrings replaced.

    Conservative by construction: every pattern requires either a structural
    marker (a PEM header, a known token prefix, an ``Authorization`` scheme) or a
    minimum secret length, so ordinary prose and code are left alone.
    """
    if not text or (
        "-----BEGIN" not in text
        and "xox" not in text
        and "xapp-" not in text
        and "private_key" not in text
        and "earer " not in text
    ):
        # Cheap bail-out: the miner calls this once per chunk, and the vast
        # majority of chunks contain no credential marker at all.
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def contains_secret(text: str) -> bool:
    """True if ``redact_secrets`` would change ``text``."""
    return redact_secrets(text) != text
