"""Content-addressed keys that let drawers with the same text share a vector.

A drawer id names a chunk of one source file, so a forked or re-exported
transcript gets new ids and missed the same-id vector reuse even though its
text is byte-for-byte the old one. ``embed_key`` names what the model computes
instead: the embedder's profile plus the exact input it receives. Two writes
with the same key would get the same vector, so the second can copy the first.

Only the vector is shared. Each drawer keeps its own text and metadata.

An embedder opts in by defining ``embed_profile()`` (a JSON-able dict that
changes whenever its vectors could change). One that is metadata-aware also
defines ``embed_inputs(texts, metadatas)`` returning ``(input_kind,
exact_input)`` per row, so the key covers the title it prepends. An embedder
without ``embed_profile`` gets no keys, and nothing is reused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

EMBED_KEY_FIELD = "embed_key"
EMBED_KEY_DOMAIN = "mempalace.embed.v1"
# Keys per ``$in`` lookup.
LOOKUP_BATCH = 1000
DOCUMENT_INPUT = "document"


def profile_identity(profile: Mapping) -> str:
    """The canonical string of an embedder profile (sorted-key JSON)."""
    return json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def embed_key(identity: str, input_kind: str, exact_input: str) -> str:
    """``sha256(domain \\0 profile \\0 input_kind \\0 exact_input)``, 64 hex digits."""
    data = "\0".join((EMBED_KEY_DOMAIN, identity, input_kind, exact_input))
    return hashlib.sha256(data.encode("utf-8", "surrogatepass")).hexdigest()


def embedder_profile(embedder) -> Optional[dict]:
    """``embedder.embed_profile()``, or ``None`` when it has none or it fails."""
    method = getattr(embedder, "embed_profile", None)
    if not callable(method):
        return None
    try:
        profile = method()
    except Exception:
        logger.debug("embedder profile unavailable; vectors are not shared", exc_info=True)
        return None
    return dict(profile) if profile else None


def embedder_inputs(embedder, texts: list, metadatas: Optional[list]) -> list:
    """``(input_kind, exact_input)`` per text, ``None`` where no key applies.

    ``texts`` must already be what :func:`_embed_texts` hands the embedder
    (lone surrogates replaced).
    """
    method = getattr(embedder, "embed_inputs", None)
    if callable(method):
        return list(method(texts, metadatas))
    return [(DOCUMENT_INPUT, text) if isinstance(text, str) else None for text in texts]


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_FILE_HASHES: dict = {}
_FILE_HASHES_LOCK = threading.Lock()


def file_sha256(path: str) -> str:
    """SHA-256 of a model file, hashed at most once per process per file state.

    A Hugging Face cache stores each large file as a blob named by its
    SHA-256 and links the snapshot to it, so that name is used when present.
    """
    real = os.path.realpath(path)
    name = os.path.basename(real)
    if _SHA256_HEX.match(name):
        return name
    stat = os.stat(real)
    state = (real, stat.st_size, stat.st_mtime_ns)
    with _FILE_HASHES_LOCK:
        cached = _FILE_HASHES.get(state)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with open(real, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    value = digest.hexdigest()
    with _FILE_HASHES_LOCK:
        _FILE_HASHES[state] = value
    return value
