"""Embedding function factory with hardware acceleration.

Returns a ChromaDB-compatible embedding function — either a local ONNX model
bound to a user-selected ONNX Runtime execution provider, or an
OpenAI-compatible HTTP ``/v1/embeddings`` endpoint.

Three embedding-model options are available, selected via
``MEMPALACE_EMBEDDING_MODEL`` or ``embedding_model`` in
``~/.mempalace/config.json``:

* ``minilm`` (default) — ``all-MiniLM-L6-v2``, 384-dim, English-only training.
  ChromaDB's default; what every existing palace was built with.
* ``embeddinggemma`` — ``onnx-community/embeddinggemma-300m-ONNX`` (q8), 384-dim
  via Matryoshka truncation, multilingual (100+ languages). Cross-lingual cos
  ~0.88 on parallel translations vs MiniLM's ~0.35. Recommended for any
  non-English use; onboarding offers it as the default. The ~300 MB ONNX
  model is lazy-downloaded from HuggingFace on first use. Switching models
  on an existing palace requires ``mempalace repair rebuild-index``
  (different vector space).
* ``openai-compat`` — embeddings served by any OpenAI-compatible
  ``/v1/embeddings`` endpoint (LM Studio, llama.cpp, vLLM, Ollama's OpenAI
  shim, or a self-hosted server) instead of a local ONNX model. Useful for
  larger / multilingual embedders (e.g. Qwen3-Embedding) or GPU offload.
  Endpoint settings are read from ``config.json`` as ``embedding_api_url`` /
  ``embedding_api_model`` / ``embedding_api_key`` (each overridable via the
  matching ``MEMPALACE_EMBEDDING_API_*`` env var). Vectors are L2-normalized
  for the cosine collection; the dimension is whatever the server returns, so
  switching to/from this backend also requires ``mempalace repair
  rebuild-index``. Stays local when the endpoint is on your machine/LAN.

Supported devices (env ``MEMPALACE_EMBEDDING_DEVICE`` or ``embedding_device``
in ``~/.mempalace/config.json``):

* ``auto`` — prefer CUDA ▸ CoreML ▸ DirectML, fall back to CPU
* ``cpu`` — force CPU (the historical default)
* ``cuda`` — NVIDIA GPU via ``onnxruntime-gpu`` (``pip install mempalace[gpu]``)
* ``coreml`` — Apple Neural Engine (macOS)
* ``dml`` — DirectML (Windows / AMD / Intel GPUs)

Requesting an unavailable accelerator emits a warning and falls back to CPU
rather than hard-failing — mining must still work on a laptop without CUDA.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from .version import __version__

logger = logging.getLogger(__name__)

_PROVIDER_MAP = {
    "cpu": ["CPUExecutionProvider"],
    "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    "coreml": ["CoreMLExecutionProvider", "CPUExecutionProvider"],
    "dml": ["DmlExecutionProvider", "CPUExecutionProvider"],
}

_DEVICE_EXTRA = {
    "cuda": "mempalace[gpu]",
    "coreml": "mempalace[coreml]",
    "dml": "mempalace[dml]",
}

_AUTO_ORDER = [
    ("CUDAExecutionProvider", "cuda"),
    ("CoreMLExecutionProvider", "coreml"),
    ("DmlExecutionProvider", "dml"),
]

_EF_CACHE: dict = {}
_WARNED: set = set()


def _resolve_providers(device: str) -> tuple[list, str]:
    """Return ``(provider_list, effective_device)`` for ``device``.

    Falls back to CPU (with a one-shot warning) when the requested
    accelerator is not compiled into the installed ``onnxruntime``.
    """
    device = (device or "auto").strip().lower()

    try:
        import onnxruntime as ort

        available = set(ort.get_available_providers())
    except ImportError:
        return (["CPUExecutionProvider"], "cpu")

    if device == "auto":
        for provider, name in _AUTO_ORDER:
            if provider in available:
                return ([provider, "CPUExecutionProvider"], name)
        return (["CPUExecutionProvider"], "cpu")

    requested = _PROVIDER_MAP.get(device)
    if requested is None:
        if device not in _WARNED:
            logger.warning("Unknown embedding_device %r — falling back to cpu", device)
            _WARNED.add(device)
        return (["CPUExecutionProvider"], "cpu")

    preferred = requested[0]
    if preferred == "CPUExecutionProvider":
        return (requested, "cpu")

    if preferred not in available:
        if device not in _WARNED:
            extra = _DEVICE_EXTRA.get(device, "the matching mempalace extra for your device")
            logger.warning(
                "embedding_device=%r requested but %s is not installed — "
                "falling back to CPU. Install %s.",
                device,
                preferred,
                extra,
            )
            _WARNED.add(device)
        return (["CPUExecutionProvider"], "cpu")

    return (requested, device)


def _build_ef_class():
    """Subclass ``ONNXMiniLM_L6_V2`` with name ``"default"``.

    Why the rename: ChromaDB 1.5 persists the EF identity on the collection
    and rejects reads that pass a differently-named EF (``onnx_mini_lm_l6_v2``
    vs ``default``). The vectors and model are identical — only the
    ``name()`` tag differs — so spoofing the name lets one EF class serve
    palaces created with ``DefaultEmbeddingFunction`` *and* palaces we
    create ourselves, with the same GPU-capable ``preferred_providers``.
    """
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    class _MempalaceONNX(ONNXMiniLM_L6_V2):
        @staticmethod
        def name() -> str:
            return "default"

    return _MempalaceONNX


# Embeddinggemma-300m ONNX (q8) — 100+ languages, MRL-truncated to 384 dims so
# it drops into existing ChromaDB collections without a schema change. Lazy:
# the model (~300 MB) downloads on first call and is cached by huggingface_hub.
_EMBEDDINGGEMMA_REPO = "onnx-community/embeddinggemma-300m-ONNX"
_EMBEDDINGGEMMA_ONNX = "model_quantized.onnx"
_EMBEDDINGGEMMA_PREFIX = "task: sentence similarity | query: "
_EMBEDDINGGEMMA_DIM = 384  # Matryoshka truncation — first 384 dims of the 768
_EMBEDDINGGEMMA_MAX_LEN = 2048


class EmbeddinggemmaONNX:
    """ChromaDB-compatible EF using embeddinggemma-300m ONNX (q8, MRL→384d).

    Cross-lingual cosine similarity on parallel-translated text averages 0.88
    across DE/FR/HI/IT/KO/RU vs 0.35 for ``all-MiniLM-L6-v2``. Output dim is
    truncated to 384 via Matryoshka Representation Learning so the model is a
    drop-in replacement for the MiniLM-shaped 384-dim collections ChromaDB
    creates by default — same vector width, no schema change.

    Switching an existing palace from minilm → embeddinggemma still requires
    re-embedding (different vector space) — collections persist the EF name
    and ChromaDB rejects mismatched reads. Run ``mempalace repair rebuild-index``.
    """

    @staticmethod
    def name() -> str:
        # ChromaDB persists this on the collection and refuses reads with a
        # mismatched EF — that's the signal that forces users to rebuild_index
        # when switching models. Keep it stable.
        return "embeddinggemma_300m"

    def __init__(self, preferred_providers=None):
        self._providers = (
            list(preferred_providers) if preferred_providers else ["CPUExecutionProvider"]
        )
        self._session = None
        self._tokenizer = None
        self._np = None
        self._output_idx = None

    def _lazy_load(self) -> None:
        if self._session is not None:
            return
        try:
            import numpy as np
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
        except ImportError as e:
            raise ImportError(
                "EmbeddinggemmaONNX requires huggingface_hub, tokenizers, and "
                "numpy — these ship with mempalace core, so this error usually "
                "means one was uninstalled or pinned to an incompatible version. "
                "Reinstall with: pip install --upgrade --force-reinstall mempalace"
            ) from e

        logger.info(
            "Downloading %s/%s (cached after first run)…",
            _EMBEDDINGGEMMA_REPO,
            _EMBEDDINGGEMMA_ONNX,
        )
        model_path = hf_hub_download(
            _EMBEDDINGGEMMA_REPO, subfolder="onnx", filename=_EMBEDDINGGEMMA_ONNX
        )
        tok_path = hf_hub_download(_EMBEDDINGGEMMA_REPO, filename="tokenizer.json")

        self._session = ort.InferenceSession(model_path, providers=self._providers)
        out_names = [o.name for o in self._session.get_outputs()]
        # Model card: sentence_embedding is the pooled output (last_hidden_state
        # is the per-token output we don't want).
        self._output_idx = (
            out_names.index("sentence_embedding") if "sentence_embedding" in out_names else 1
        )

        tokenizer = Tokenizer.from_file(tok_path)
        tokenizer.enable_padding()
        tokenizer.enable_truncation(max_length=_EMBEDDINGGEMMA_MAX_LEN)
        self._tokenizer = tokenizer
        self._np = np

    def __call__(self, input):  # noqa: A002 — ChromaDB EF protocol uses `input`
        self._lazy_load()
        np = self._np
        texts = [_EMBEDDINGGEMMA_PREFIX + t for t in input]
        encs = self._tokenizer.encode_batch(texts)
        input_ids = np.asarray([e.ids for e in encs], dtype=np.int64)
        attention_mask = np.asarray([e.attention_mask for e in encs], dtype=np.int64)
        outputs = self._session.run(
            None, {"input_ids": input_ids, "attention_mask": attention_mask}
        )
        sent_emb = outputs[self._output_idx][:, :_EMBEDDINGGEMMA_DIM]
        # L2-normalize so cosine similarity == dot product (matches what the
        # MTEB methodology assumes; ChromaDB's distance is configured for it).
        norms = np.linalg.norm(sent_emb, axis=1, keepdims=True) + 1e-12
        return (sent_emb / norms).tolist()


# ── OpenAI-compatible embedding API ──────────────────────────────────────
# Fetch embeddings from an OpenAI-compatible ``/v1/embeddings`` server
# (LM Studio, llama.cpp, vLLM, Ollama's OpenAI shim, or any compatible
# endpoint) instead of running a model locally. Selected by
# ``embedding_model == "openai-compat"``. Connection settings (URL, model,
# optional key) are resolved by :class:`~mempalace.config.MempalaceConfig`
# as the single source of truth — see ``embedding_api_url`` /
# ``embedding_api_model`` / ``embedding_api_key`` (each env-overridable).
_EF_API_BATCH = 64
_EF_API_TIMEOUT = 120


class EmbeddingAPIError(RuntimeError):
    """Raised when the embedding API is unreachable or returns an invalid body.

    Module-specific subclass mirroring ``llm_client.LLMError`` so callers can
    distinguish embedding-endpoint failures; subclasses ``RuntimeError`` so
    existing ``except RuntimeError`` paths still catch it.
    """


class OpenAICompatEmbeddingFunction:
    """ChromaDB-compatible EF backed by an OpenAI-compatible ``/v1/embeddings``
    endpoint (LM Studio, llama.cpp, vLLM, Ollama's OpenAI shim, etc.).

    Selected via ``embedding_model == "openai-compat"``. Vectors are produced
    server-side and fetched over HTTP, which changes the vector space — so
    ``name()`` encodes the model id: ChromaDB persists the EF name on the
    collection and rejects mismatched reads, the signal to run ``mempalace
    repair rebuild-index`` after changing model/endpoint. stdlib ``urllib``
    only, no new dependency.
    """

    def __init__(self, base_url: str, model: str, api_key: Optional[str] = None):
        self._url = self._resolve_url(base_url)
        self._model = model
        self._api_key = api_key

    @staticmethod
    def _resolve_url(base_url: str) -> str:
        """Accept a base host, a ``/v1`` base, or a full endpoint URL.

        Mirrors ``llm_client.OpenAICompatProvider._resolve_url`` so both sides
        treat an ``http://host:port`` endpoint the same way.
        """
        url = base_url.rstrip("/")
        if url.endswith("/embeddings"):
            return url
        if url.endswith("/v1"):
            return f"{url}/embeddings"
        return f"{url}/v1/embeddings"

    def name(self) -> str:
        # Encode the model so switching it changes the persisted EF identity
        # and forces a rebuild_index (vectors from a different model/space are
        # not interchangeable). ChromaDB compares this on every read.
        return f"openai_compat_emb_{self._model}".replace("/", "_")

    def embed_query(self, input):  # noqa: A002 — ChromaDB EF protocol uses `input`
        # ChromaDB 1.5 dispatches query embedding through embed_query (add uses
        # __call__). Mirror the EmbeddingFunction protocol default: same path.
        return self(input)

    def __call__(self, input):  # noqa: A002 — ChromaDB EF protocol uses `input`
        import json
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen

        headers = {
            "Content-Type": "application/json",
            # Some hosted (Cloudflare-fronted) endpoints 403 the default
            # ``Python-urllib`` User-Agent — send our own (see issue #1570).
            "User-Agent": f"mempalace/{__version__}",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        out: list = []
        texts = list(input)
        for start in range(0, len(texts), _EF_API_BATCH):
            batch = texts[start : start + _EF_API_BATCH]
            # encoding_format=float is explicit so a server that defaults to
            # base64 doesn't hand back strings we'd mis-parse as vectors.
            payload = {"model": self._model, "input": batch, "encoding_format": "float"}
            req = Request(self._url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            try:
                with urlopen(req, timeout=_EF_API_TIMEOUT) as resp:
                    data = json.loads(resp.read())
            except (HTTPError, URLError, OSError, json.JSONDecodeError) as e:
                raise EmbeddingAPIError(
                    f"Embedding API request to {self._url} failed: {e}. Check that the "
                    f"server is reachable and MEMPALACE_EMBEDDING_API_URL / embedding_api_url "
                    f"is correct."
                ) from e
            out.extend(self._vectors_from_response(data, len(batch)))
        return out

    def _vectors_from_response(self, data, n: int) -> list:
        """Validate one ``/v1/embeddings`` response and return L2-normed vectors.

        Guards every way a non-conformant server could corrupt the store
        silently: a missing/short ``data`` array, response ``index`` values
        that aren't the contiguous ``0..n-1`` batch positions (sorting then
        zipping positionally would otherwise misalign vectors with texts), and
        malformed / ragged / base64 embedding payloads. All failures raise
        :class:`EmbeddingAPIError` naming the endpoint rather than a cryptic
        numpy error — a silent wrong result would break the 100%-recall promise.
        """
        import numpy as np

        rows = data.get("data")
        if not isinstance(rows, list):
            raise EmbeddingAPIError(
                f"Embedding API at {self._url} returned no 'data' array: {data.get('error', data)}"
            )
        if len(rows) != n:
            raise EmbeddingAPIError(
                f"Embedding API at {self._url} returned {len(rows)} embeddings for {n} inputs"
            )
        # The endpoint may return rows out of order — sort by index, then
        # require the indices to be exactly 0..n-1 so positional alignment is
        # provably correct (a server using absolute or duplicate indices would
        # otherwise pass the count check yet map vectors to the wrong texts).
        try:
            rows = sorted(rows, key=lambda d: d.get("index", -1))
            indices = [r.get("index") for r in rows]
        except AttributeError as e:
            raise EmbeddingAPIError(
                f"Embedding API at {self._url} returned non-object rows: {e}"
            ) from e
        if indices != list(range(n)):
            raise EmbeddingAPIError(
                f"Embedding API at {self._url} returned non-contiguous or duplicate "
                f"'index' values; cannot align embeddings with inputs"
            )
        try:
            arr = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
        except (KeyError, TypeError, ValueError) as e:
            raise EmbeddingAPIError(
                f"Embedding API at {self._url} returned malformed embeddings: {e}"
            ) from e
        if arr.ndim != 2:
            raise EmbeddingAPIError(
                f"Embedding API at {self._url} returned non-vector embeddings (shape {arr.shape})"
            )
        # L2-normalize so cosine == dot product (collection uses
        # hnsw:space=cosine), matching EmbeddinggemmaONNX above.
        norms = np.linalg.norm(arr, axis=1, keepdims=True) + 1e-12
        return (arr / norms).tolist()


def get_embedding_function(device: Optional[str] = None, model: Optional[str] = None):
    """Return a cached embedding function for the requested device + model.

    ``device=None`` reads :attr:`MempalaceConfig.embedding_device`;
    ``model=None`` reads :attr:`MempalaceConfig.embedding_model`.
    The returned function is shared across calls with the same resolved
    provider list + model so we only pay model-load cost once per process.
    """
    if device is None or model is None:
        from .config import MempalaceConfig

        cfg = MempalaceConfig()
        if device is None:
            device = cfg.embedding_device
        if model is None:
            model = cfg.embedding_model

    # OpenAI-compatible embedding API: bypasses local ONNX entirely. Checked
    # before device→provider resolution since it needs no hardware accelerator.
    if model == "openai-compat":
        from .config import MempalaceConfig

        cfg = MempalaceConfig()
        url = cfg.embedding_api_url
        if not url:
            raise ValueError(
                "embedding_model='openai-compat' requires an endpoint — set "
                "embedding_api_url in ~/.mempalace/config.json or the "
                "MEMPALACE_EMBEDDING_API_URL env var (e.g. http://host:port)"
            )
        api_model = cfg.embedding_api_model
        if not api_model:
            raise ValueError(
                "embedding_model='openai-compat' requires a model — set "
                "embedding_api_model in ~/.mempalace/config.json or the "
                "MEMPALACE_EMBEDDING_API_MODEL env var"
            )
        api_key = cfg.embedding_api_key
        # Include a fingerprint of the key (never the raw secret) so a token
        # rotation busts the cache in long-lived processes (e.g. MCP server).
        key_fp = hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]
        cache_key = ("openai-compat", url, api_model, key_fp)
        cached = _EF_CACHE.get(cache_key)
        if cached is not None:
            return cached
        ef = OpenAICompatEmbeddingFunction(base_url=url, model=api_model, api_key=api_key)
        _EF_CACHE[cache_key] = ef
        logger.info(
            "Embedding function initialized (openai-compat url=%s model=%s)", url, api_model
        )
        return ef

    providers, effective = _resolve_providers(device)
    cache_key = (model, tuple(providers))
    cached = _EF_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if model == "embeddinggemma":
        ef = EmbeddinggemmaONNX(preferred_providers=providers)
    else:
        # Default: minilm (or anything we don't recognize — back-compat win).
        ef_cls = _build_ef_class()
        ef = ef_cls(preferred_providers=providers)

    _EF_CACHE[cache_key] = ef
    logger.info(
        "Embedding function initialized (model=%s device=%s providers=%s)",
        model,
        effective,
        providers,
    )
    return ef


def describe_device(device: Optional[str] = None) -> str:
    """Return a short human-readable label for the resolved embedding backend.

    Used by the miner CLI header / MCP status so users can see at a glance
    whether GPU acceleration engaged — or, for the ``openai-compat`` backend,
    that embeddings are served by a remote endpoint rather than local hardware
    (in which case the ``embedding_device`` accelerator label is irrelevant).
    """
    if device is None:
        from .config import MempalaceConfig

        cfg = MempalaceConfig()
        if cfg.embedding_model == "openai-compat":
            url = cfg.embedding_api_url
            return f"openai-compat ({url})" if url else "openai-compat"
        device = cfg.embedding_device
    _, effective = _resolve_providers(device)
    return effective
