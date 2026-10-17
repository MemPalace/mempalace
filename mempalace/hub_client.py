"""Small authenticated client for forwarding JSON-RPC to a live Palace hub."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Mapping

logger = logging.getLogger(__name__)

HUB_FORWARD_ENV = "MEMPALACE_HUB_FORWARD"
HUB_PROXY_TIMEOUT_S = 600.0

# Every setting that changes what a hub search returns, as
# (MempalaceConfig attribute, env vars that override it, scope). The CLI keeps
# a search local while any of these env vars is set, and the hub's registered
# search fingerprint hashes the resolved values in scope, so a setting added
# here reaches both checks. Scope is None (always), ("backend", name), or
# ("embedding", model); a model prefixed with "!" means every other model.
SEARCH_SETTINGS = (
    ("collection_name", (), None),
    ("lang_explicit", ("MEMPALACE_LANG", "MEMPAL_LANG"), None),
    ("hybrid_rank_vector_weight", ("MEMPALACE_HYBRID_VECTOR_WEIGHT",), None),
    ("hybrid_rank_bm25_weight", ("MEMPALACE_HYBRID_BM25_WEIGHT",), None),
    ("embedding_api_key", ("MEMPALACE_EMBEDDING_API_KEY",), ("embedding", "openai-compat")),
    ("embedding_api_model", ("MEMPALACE_EMBEDDING_API_MODEL",), ("embedding", "openai-compat")),
    ("embedding_api_url", ("MEMPALACE_EMBEDDING_API_URL",), ("embedding", "openai-compat")),
    (
        "embeddinggemma2_dimension",
        ("MEMPALACE_EMBEDDINGGEMMA2_DIMENSION",),
        ("embedding", "embeddinggemma2"),
    ),
    (
        "embeddinggemma2_modalities",
        ("MEMPALACE_EMBEDDINGGEMMA2_MODALITIES",),
        ("embedding", "embeddinggemma2"),
    ),
    (
        "embeddinggemma2_revision",
        ("MEMPALACE_EMBEDDINGGEMMA2_REVISION",),
        ("embedding", "embeddinggemma2"),
    ),
    ("embedding_device", ("MEMPALACE_EMBEDDING_DEVICE",), ("embedding", "!openai-compat")),
    ("embedding_threads", ("MEMPALACE_EMBEDDING_THREADS",), ("embedding", "!openai-compat")),
    ("qdrant_api_key", ("MEMPALACE_QDRANT_API_KEY",), ("backend", "qdrant")),
    ("qdrant_namespace", ("MEMPALACE_QDRANT_NAMESPACE",), ("backend", "qdrant")),
    ("qdrant_timeout", ("MEMPALACE_QDRANT_TIMEOUT",), ("backend", "qdrant")),
    ("qdrant_url", ("MEMPALACE_QDRANT_URL",), ("backend", "qdrant")),
    ("milvus_consistency_level", ("MEMPALACE_MILVUS_CONSISTENCY_LEVEL",), ("backend", "milvus")),
    ("milvus_db_name", ("MEMPALACE_MILVUS_DB_NAME",), ("backend", "milvus")),
    ("milvus_namespace", ("MEMPALACE_MILVUS_NAMESPACE",), ("backend", "milvus")),
    ("milvus_token", ("MEMPALACE_MILVUS_TOKEN",), ("backend", "milvus")),
    ("milvus_uri", ("MEMPALACE_MILVUS_URI",), ("backend", "milvus")),
    ("pgvector_dsn", ("MEMPALACE_PGVECTOR_DSN",), ("backend", "pgvector")),
    ("pgvector_namespace", ("MEMPALACE_PGVECTOR_NAMESPACE",), ("backend", "pgvector")),
    (
        "pgvector_shared_namespace",
        ("MEMPALACE_PGVECTOR_SHARED_NAMESPACE",),
        ("backend", "pgvector"),
    ),
)

# Env vars that pick the backend or the embedder itself. The fingerprint hashes
# the resolved backend and model directly rather than through the table.
SEARCH_SELECTOR_ENV_VARS = (
    "MEMPALACE_BACKEND",
    "MEMPALACE_BACKEND_EXPLICIT",
    "MEMPALACE_EMBEDDING_MODEL",
)

SEARCH_OVERRIDE_ENV_VARS = SEARCH_SELECTOR_ENV_VARS + tuple(
    name for _attr, env_names, _scope in SEARCH_SETTINGS for name in env_names
)


def search_setting_in_scope(scope, backend: str, embedding_model: str) -> bool:
    """Whether a ``SEARCH_SETTINGS`` row applies to this backend and model."""
    if scope is None:
        return True
    kind, value = scope
    current = backend if kind == "backend" else embedding_model
    if value.startswith("!"):
        return current != value[1:]
    return current == value


def _forwarding_disabled() -> bool:
    return os.environ.get(HUB_FORWARD_ENV, "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def discover_hub(palace_path: str | None) -> tuple[str, dict[str, str]] | None:
    """Return the authenticated endpoint for another live hub, if available."""

    if _forwarding_disabled() or not palace_path:
        return None
    try:
        from . import server_registry

        info = server_registry.read_live_serverinfo(palace_path)
        if not info or info.get("pid") == os.getpid():
            return None
        base_url = server_registry.client_base_url(info)
        headers = {"Content-Type": "application/json"}
        token = server_registry.load_server_token(palace_path)
    except Exception:
        logger.debug("hub discovery failed", exc_info=True)
        return None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return base_url, headers


def forward_json_rpc(
    base_url: str,
    headers: Mapping[str, str],
    request: Mapping,
    *,
    timeout: float = HUB_PROXY_TIMEOUT_S,
):
    """POST one JSON-RPC request to the hub; return ``None`` for an empty body."""

    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    http_request = urllib.request.Request(f"{base_url}/mcp", data=body, headers=dict(headers))
    with urllib.request.urlopen(http_request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))
