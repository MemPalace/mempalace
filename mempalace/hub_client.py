"""Small authenticated client for forwarding JSON-RPC to a live Palace hub."""

from __future__ import annotations

import http.client
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Mapping

from ._env import env_bool, env_str
from .stored_text import EXACT_WIRE_HEADER, dumps_internal

logger = logging.getLogger(__name__)

HUB_FORWARD_ENV = "MEMPALACE_HUB_FORWARD"
HUB_PROXY_TIMEOUT_S = 600.0
HUB_HEALTH_TIMEOUT_S = 0.75

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


# MCP tools whose results depend on the search settings above.
SEARCH_TOOL_NAMES = frozenset({"mempalace_search", "mempalace_check_duplicate"})


def search_settings_overridden(backend: str | None = None) -> bool:
    """Whether this process asks for search settings a hub may not be using.

    An explicit backend, or any env var in ``SEARCH_OVERRIDE_ENV_VARS``, means
    the caller chose settings itself; the hub searches with what it resolved
    when it started, so such a search stays local.
    """
    if backend:
        return True
    return any(env_str(name) for name in SEARCH_OVERRIDE_ENV_VARS)


def search_fingerprint_matches(info: Mapping, palace_path: str) -> bool:
    """Whether a hub's registered search fingerprint equals this process's.

    Resolving the fingerprint opens the palace package, so the light stdio
    proxy leaves this check to processes that load storage anyway.
    """
    from .config import MempalaceConfig

    current = MempalaceConfig(palace_path=palace_path).search_config_fingerprint
    return info.get("search_config_fingerprint") == current


def search_setting_in_scope(scope, backend: str, embedding_model: str) -> bool:
    """Whether a ``SEARCH_SETTINGS`` row applies to this backend and model."""
    if scope is None:
        return True
    kind, value = scope
    current = backend if kind == "backend" else embedding_model
    if value.startswith("!"):
        return current != value[1:]
    return current == value


def forwarding_disabled() -> bool:
    """Whether ``MEMPALACE_HUB_FORWARD`` turns hub forwarding off."""
    return not env_bool(HUB_FORWARD_ENV, True)


def hub_request_headers() -> dict[str, str]:
    """Headers for a MemPalace process calling a hub's MCP endpoint.

    Asks for the exact text, stray source bytes included (see
    ``stored_text.dumps_internal``); the caller shows it to its own client
    the client's way.
    """
    return {"Content-Type": "application/json", EXACT_WIRE_HEADER: "1"}


# What a failed hub call can raise before or while the hub answers.
FORWARD_ERRORS = (
    urllib.error.URLError,
    OSError,
    TimeoutError,
    ValueError,
    http.client.HTTPException,
)


def proxy_error_response(request: Mapping, base_url: str, exc: BaseException):
    """The JSON-RPC error for a request the hub may have run but did not answer.

    None for a notification, which is owed no response.
    """
    if request.get("id") is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {
            "code": -32000,
            "message": f"palace hub proxy failed: {exc}",
            "data": {
                "hub": base_url,
                "hint": (
                    "The palace hub did not complete this request. Mutating tools "
                    "are not replayed locally — the hub may still be executing the "
                    "call. Check the hub process, then retry."
                ),
            },
        },
    }


class HubClient:
    """The live hub serving one palace on this machine, and the way to call it.

    Every request carries the exact-wire header. The bearer token comes from
    the palace's local token candidates; a hub serving loopback without a
    token gets none.
    """

    def __init__(self, palace_path: str, info: Mapping):
        from . import server_registry

        self.palace_path = palace_path
        self.info = info
        self.base_url = server_registry.client_base_url(info)

    @classmethod
    def discover(cls, palace_path: str | None, *, include_self: bool = False) -> HubClient | None:
        """The hub serving ``palace_path``, unless forwarding is off or none is alive.

        A server process skips its own record so it never forwards to itself;
        the CLI, which is never the hub, passes ``include_self``.
        """
        if forwarding_disabled() or not palace_path:
            return None
        try:
            from . import server_registry

            info = server_registry.read_live_serverinfo(palace_path)
            if not info or (not include_self and info.get("pid") == os.getpid()):
                return None
            return cls(palace_path, info)
        except Exception:
            logger.debug("hub discovery failed", exc_info=True)
            return None

    @property
    def pid(self):
        return self.info.get("pid")

    @property
    def capabilities(self) -> list:
        return list(self.info.get("capabilities") or [])

    @property
    def read_only(self) -> bool:
        return bool(self.info.get("read_only"))

    def healthy(self, timeout: float = HUB_HEALTH_TIMEOUT_S) -> bool:
        """Whether ``/healthz`` answers 200 within ``timeout``."""
        request = urllib.request.Request(f"{self.base_url}/healthz", headers=hub_request_headers())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def post(self, request: Mapping, *, timeout: float = HUB_PROXY_TIMEOUT_S):
        """POST one JSON-RPC request to ``/mcp``; None for an empty body (a notification)."""
        body = dumps_internal(request).encode("utf-8")
        with self._open(f"{self.base_url}/mcp", body, timeout) as response:
            raw = response.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def call_tool(self, name: str, arguments: Mapping, *, timeout: float = HUB_PROXY_TIMEOUT_S):
        """Run one MCP tool in the hub; the raw JSON-RPC response."""
        return self.post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": dict(arguments)},
            },
            timeout=timeout,
        )

    def _open(self, url: str, body: bytes, timeout: float):
        """Open one request, retrying only a pre-acceptance HTTP 401.

        At most two distinct local credentials exist. A 401 means the hub's
        authentication gate rejected the request before dispatch, so trying
        the second credential is safe even for mutating calls. Every other
        failure is raised at once and never replayed.
        """
        from . import server_registry

        *earlier, last = server_registry.load_server_tokens(self.palace_path) or ("",)
        for token in earlier:
            try:
                return urllib.request.urlopen(self._request(url, body, token), timeout=timeout)
            except urllib.error.HTTPError as exc:
                if exc.code != 401:
                    raise
                exc.close()
        return urllib.request.urlopen(self._request(url, body, last), timeout=timeout)

    @staticmethod
    def _request(url: str, body: bytes, token: str):
        headers = hub_request_headers()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return urllib.request.Request(url, data=body, headers=headers)


def tool_result(payload: Mapping):
    """The JSON a ``tools/call`` response carries in its first text block.

    Raises KeyError, IndexError, TypeError or ValueError when it has none.
    """
    return json.loads(payload["result"]["content"][0]["text"])
