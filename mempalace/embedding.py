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
  (different vector space). Its ``session.run()`` sub-batch size (32 docs by
  default, #1770) is overridable via ``MEMPALACE_EMBEDDINGGEMMA_BATCH_SIZE``
  or ``embeddinggemma_batch_size`` in ``config.json`` for palaces whose
  drawers are long enough that the default sub-batch exceeds available
  memory (#2330).
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
The same applies to an accelerator that runs but computes the model wrongly:
``embeddinggemma`` on CoreML returns NaN or all-zero vectors without raising,
so ``auto`` never selects CoreML for it and an explicitly requested one is
rejected by a witness embedding at load time.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
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

# Providers that must never be picked *automatically* for a given model,
# keyed by model name.
#
# embeddinggemma × CoreML: CoreML claims only ~280 of the quantized graph's
# 1647 nodes, split across 100+ partitions, and the result is corrupt —
# last_hidden_state comes back all-NaN, so the pooled sentence_embedding is
# NaN or all-zero (which one depends on how that run partitioned) — with no
# error raised. Since embedding_device defaults to "auto" and CoreML sits
# ahead of CPU, every Apple Silicon user running this model would otherwise
# embed degenerate vectors silently; a `repair rebuild-index` would write them
# over the whole palace. CoreML is also ~2x slower here when it does run
# (7.9 vs 16.0 docs/s on an M4 Max), so nothing is lost by skipping it.
# An explicit embedding_device=coreml is still honoured — the witness probe in
# EmbeddinggemmaONNX._lazy_load catches it and falls back to CPU.
_AUTO_PROVIDER_DENYLIST = {
    "embeddinggemma": {"CoreMLExecutionProvider"},
}

_EF_CACHE: dict = {}
# Check-then-construct on the cache must be atomic: without it, two threads
# resolving the same key each keep their own EF instance, and each instance
# later lazy-loads its own copy of the model.
_EF_CACHE_LOCK = threading.Lock()
_WARNED: set = set()


def _resolve_providers(device: str, model: Optional[str] = None) -> tuple[list, str]:
    """Return ``(provider_list, effective_device)`` for ``device``.

    Falls back to CPU (with a one-shot warning) when the requested
    accelerator is not compiled into the installed ``onnxruntime``.

    ``model`` gates ``_AUTO_PROVIDER_DENYLIST``: a provider known to produce
    wrong results for that model is skipped under ``auto``. Explicit device
    requests are left alone — a user who names an accelerator gets it.
    """
    device = (device or "auto").strip().lower()
    denied = _AUTO_PROVIDER_DENYLIST.get((model or "").strip().lower(), frozenset())

    try:
        import onnxruntime as ort

        available = set(ort.get_available_providers())
    except ImportError:
        return (["CPUExecutionProvider"], "cpu")

    if device == "auto":
        for provider, name in _AUTO_ORDER:
            if provider in available and provider not in denied:
                return ([provider, "CPUExecutionProvider"], name)
        return (["CPUExecutionProvider"], "cpu")

    requested = _PROVIDER_MAP.get(device)
    if requested is None:
        if device not in _WARNED:
            logger.warning("Unknown embedding_device %r -- falling back to cpu", device)
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


def _intra_op_session_options(intra_op_num_threads: int):
    """Build ORT ``SessionOptions`` capping the intra-op thread pool (#1068).

    Returns ``None`` when ``intra_op_num_threads <= 0`` so the caller leaves
    ORT at its default (≈ physical core count). ChromaDB's embedder ignores
    ``OMP_NUM_THREADS`` — ORT owns its own intra-op pool, settable only via
    ``SessionOptions`` at session construction — so a cap has to be threaded
    through here rather than via the environment.
    """
    if not intra_op_num_threads or intra_op_num_threads <= 0:
        return None
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.intra_op_num_threads = intra_op_num_threads
    return so


def _resolve_intra_op_threads() -> int:
    """Read the configured ORT intra-op thread cap (``0`` = uncapped, #1068)."""
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().embedding_threads
    except Exception:
        logger.debug("embedding_threads resolution failed; leaving ORT default", exc_info=True)
        return 0


def _resolve_embeddinggemma_batch_size() -> int:
    """Read the configured EmbeddingGemma sub-batch size (#2330)."""
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().embeddinggemma_batch_size
    except Exception:
        logger.debug(
            "embeddinggemma_batch_size resolution failed; using the %d default",
            _EMBEDDINGGEMMA_BATCH_SIZE,
            exc_info=True,
        )
        return _EMBEDDINGGEMMA_BATCH_SIZE


def _build_ef_class():
    """Subclass ``ONNXMiniLM_L6_V2`` with name ``"default"``.

    Why the rename: ChromaDB 1.5 persists the EF identity on the collection
    and rejects reads that pass a differently-named EF (``onnx_mini_lm_l6_v2``
    vs ``default``). The vectors and model are identical — only the
    ``name()`` tag differs — so spoofing the name lets one EF class serve
    palaces created with ``DefaultEmbeddingFunction`` *and* palaces we
    create ourselves, with the same GPU-capable ``preferred_providers``.
    """
    from functools import cached_property

    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    class _MempalaceONNX(ONNXMiniLM_L6_V2):
        def __init__(self, preferred_providers=None, intra_op_num_threads=0):
            super().__init__(preferred_providers=preferred_providers)
            self._intra_op_num_threads = intra_op_num_threads

        @staticmethod
        def name() -> str:
            return "default"

        @cached_property
        def model(self):
            # Upstream builds the InferenceSession with no intra-op thread cap,
            # so ORT defaults its pool to the physical core count and a
            # background mine pins every core (#1068). Rebuild the session the
            # same way upstream does (same SessionOptions, same CoreML pruning,
            # same model path) but with our cap applied. If upstream's
            # internals shift, fall back to its uncapped build so embedding
            # still works.
            cap = getattr(self, "_intra_op_num_threads", 0)
            if not cap or cap <= 0:
                return super().model
            try:
                ort = self.ort
                providers = self._preferred_providers or ort.get_available_providers()
                providers = [p for p in providers if p != "CoreMLExecutionProvider"]
                so = ort.SessionOptions()
                so.log_severity_level = 3
                so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                so.intra_op_num_threads = cap
                return ort.InferenceSession(
                    os.path.join(self.DOWNLOAD_PATH, self.EXTRACTED_FOLDER_NAME, "model.onnx"),
                    providers=providers,
                    sess_options=so,
                )
            except Exception:
                logger.warning(
                    "thread-capped ORT session build failed; using ORT defaults",
                    exc_info=True,
                )
                return super().model

    return _MempalaceONNX


# Embeddinggemma-300m ONNX (q8) — 100+ languages, MRL-truncated to 384 dims so
# it drops into existing ChromaDB collections without a schema change. Lazy:
# the model (~300 MB) downloads on first call and is cached by huggingface_hub.
_EMBEDDINGGEMMA_REPO = "onnx-community/embeddinggemma-300m-ONNX"
_EMBEDDINGGEMMA_ONNX = "model_quantized.onnx"
_EMBEDDINGGEMMA_PREFIX = "task: sentence similarity | query: "
_EMBEDDINGGEMMA_DIM = 384  # Matryoshka truncation — first 384 dims of the 768
_EMBEDDINGGEMMA_MAX_LEN = 2048
# Default docs per session.run. The ONNX graph has no internal batching,
# so one unchunked run over a repair-scale batch (5000 docs, repair.py/
# cli.py) allocates attention buffers that grow with batch size and
# superlinearly with padded length (score tensors are batch x heads x
# len^2 per layer), and the kernel OOM-kills the process (#1770). 32
# matches the internal batch size of chromadb's ONNXMiniLM_L6_V2, whose
# chunked _forward survives the same call sites. embeddinggemma's
# sentence_embedding output is attention-masked, so sub-batch padding
# does not change any row's vector. __call__ decides which documents share
# a sub-batch by size rather than by arrival order (#2104), because the run
# is priced on that padded length and not on the document count.
_EMBEDDINGGEMMA_BATCH_SIZE = 32
# Short document embedded once per model load to prove the execution provider
# actually computes (see _embeddinggemma_session_is_healthy).
_EMBEDDINGGEMMA_WITNESS = "mempalace embedding provider health check"


def _sanitize_embeddinggemma_input_ids(tokenizer, input_ids, np):
    """Replace tokenizer-only IDs that the text ONNX model cannot embed."""
    model_vocab_size = tokenizer.get_vocab_size(with_added_tokens=False)
    out_of_range = (input_ids < 0) | (input_ids >= model_vocab_size)

    if not np.any(out_of_range):
        return input_ids

    unknown_token_id = tokenizer.token_to_id("<unk>")
    if unknown_token_id is None or not 0 <= unknown_token_id < model_vocab_size:
        raise RuntimeError(
            "EmbeddingGemma tokenizer produced token IDs outside the ONNX "
            "text vocabulary, but no valid <unk> token is available"
        )

    invalid_ids = sorted({int(token_id) for token_id in input_ids[out_of_range]})
    warning_key = (
        "embeddinggemma-out-of-range-token-ids",
        model_vocab_size,
        tuple(invalid_ids),
    )

    if warning_key not in _WARNED:
        logger.warning(
            "EmbeddingGemma tokenizer produced token IDs outside the ONNX "
            "text vocabulary (size=%d): %s; remapping to <unk> (%d)",
            model_vocab_size,
            invalid_ids,
            unknown_token_id,
        )
        _WARNED.add(warning_key)

    sanitized = input_ids.copy()
    sanitized[out_of_range] = unknown_token_id
    return sanitized


def _embeddinggemma_forward(session, tokenizer, output_idx, np, texts):
    """Run one sub-batch through the ONNX graph.

    Returns the MRL-truncated, *unnormalized* ``sentence_embedding`` rows.
    Shared by ``__call__`` and the provider witness probe so both exercise
    exactly the same path — a probe that ran a different graph would not
    prove anything about the vectors we hand back.
    """
    encs = tokenizer.encode_batch([_EMBEDDINGGEMMA_PREFIX + text for text in texts])
    input_ids = np.asarray([e.ids for e in encs], dtype=np.int64)
    input_ids = _sanitize_embeddinggemma_input_ids(tokenizer, input_ids, np)
    attention_mask = np.asarray([e.attention_mask for e in encs], dtype=np.int64)
    outputs = session.run(None, {"input_ids": input_ids, "attention_mask": attention_mask})
    return outputs[output_idx][:, :_EMBEDDINGGEMMA_DIM]


def _embeddinggemma_session_is_healthy(session, tokenizer, output_idx, np) -> bool:
    """Embed a witness string and check the vector is usable.

    An execution provider that only partially supports the graph can return
    NaN or all-zero rows without raising (CoreML does exactly this on Apple
    Silicon). Both are indistinguishable from a healthy vector once stored,
    so the provider is checked once, at load, before anything is embedded.
    """
    try:
        vectors = _embeddinggemma_forward(
            session, tokenizer, output_idx, np, [_EMBEDDINGGEMMA_WITNESS]
        )
        norm = float(np.linalg.norm(vectors))
    except Exception:
        logger.warning(
            "EmbeddingGemma witness embedding failed; treating the provider as unusable",
            exc_info=True,
        )
        return False
    # NaN/Inf fail the finite check; an all-zero vector fails the > 0 check.
    return bool(np.isfinite(norm)) and norm > 0.0


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

    def __init__(
        self,
        preferred_providers=None,
        batch_size: int = _EMBEDDINGGEMMA_BATCH_SIZE,
        intra_op_num_threads: int = 0,
    ):
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        self._providers = (
            list(preferred_providers) if preferred_providers else ["CPUExecutionProvider"]
        )
        self._batch_size = batch_size
        self._intra_op_num_threads = intra_op_num_threads
        self._session = None
        self._tokenizer = None
        self._np = None
        self._output_idx = None
        # Instances are shared across threads via _EF_CACHE; serialize the
        # one-time model load so concurrent cold calls cannot build (and
        # transiently hold) two full model sessions.
        self._load_lock = threading.Lock()

    def _lazy_load(self) -> None:
        if self._session is not None:
            return
        with self._load_lock:
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
            hf_hub_download(
                _EMBEDDINGGEMMA_REPO, subfolder="onnx", filename=_EMBEDDINGGEMMA_ONNX + "_data"
            )
            tok_path = hf_hub_download(_EMBEDDINGGEMMA_REPO, filename="tokenizer.json")

            session = ort.InferenceSession(
                model_path,
                sess_options=_intra_op_session_options(self._intra_op_num_threads),
                providers=self._providers,
            )
            out_names = [o.name for o in session.get_outputs()]
            # Model card: sentence_embedding is the pooled output (last_hidden_state
            # is the per-token output we don't want).
            output_idx = (
                out_names.index("sentence_embedding") if "sentence_embedding" in out_names else 1
            )

            tokenizer = Tokenizer.from_file(tok_path)
            tokenizer.enable_padding()
            tokenizer.enable_truncation(max_length=_EMBEDDINGGEMMA_MAX_LEN)

            # Accelerators can compute this graph wrongly rather than refuse
            # it, so an accelerated session has to prove itself before it
            # embeds anything. CPU-only sessions skip the probe: CPU is the
            # fallback, so a check there could only add a forward pass to
            # every cold start.
            if any(p != "CPUExecutionProvider" for p in self._providers):
                if not _embeddinggemma_session_is_healthy(session, tokenizer, output_idx, np):
                    logger.warning(
                        "Embedding provider %s returned a degenerate vector (NaN or "
                        "all-zero) for EmbeddingGemma — falling back to "
                        "CPUExecutionProvider. Set embedding_device to 'cpu' in "
                        "~/.mempalace/config.json to skip this check.",
                        self._providers[0],
                    )
                    session = ort.InferenceSession(
                        model_path,
                        sess_options=_intra_op_session_options(self._intra_op_num_threads),
                        providers=["CPUExecutionProvider"],
                    )
                    if not _embeddinggemma_session_is_healthy(session, tokenizer, output_idx, np):
                        # No provider left to fall back to. Raising loses this
                        # process's embeddings; continuing would write vectors
                        # that are unsearchable and indistinguishable from
                        # healthy ones once in the palace.
                        raise RuntimeError(
                            "EmbeddingGemma produced a degenerate vector on "
                            "CPUExecutionProvider — refusing to embed rather than store "
                            "unusable vectors. Reinstall onnxruntime, or switch "
                            "embedding_model in ~/.mempalace/config.json."
                        )
                    self._providers = ["CPUExecutionProvider"]

            self._output_idx = output_idx
            self._tokenizer = tokenizer
            self._np = np
            # Session is assigned last: the unlocked fast path above treats a
            # non-None session as "fully loaded", so every other attribute
            # must already be in place when it becomes visible.
            self._session = session

    def __call__(self, input: str | list[str] | None) -> list[list[float]]:  # noqa: A002 — ChromaDB EF protocol
        """Embed ``input``, returning one vector per document in input order.

        Documents are grouped by size before the sub-batch split. The
        tokenizer pads every row of a sub-batch to the longest sequence in
        it, and attention cost per layer is batch x heads x length^2, so one
        long document drags a whole sub-batch up to its own length. Without
        grouping the bill is set by arrival order: a verbatim transcript
        whose long tool results sit between one-line replies pays the long
        length for nearly every row (#2104).

        An input that fits a single sub-batch is left in arrival order: every
        row pads to the same width either way, so the keys would buy nothing
        on the one-document search path.

        Regrouping does not change what a row means. The model's
        ``sentence_embedding`` output is attention-masked, so padding never
        enters a row's values; what does move is float32 rounding, because a
        different padded width changes the reduction order inside the GEMMs.
        Measured against the same documents embedded in arrival order, that
        residual peaks at one float32 ULP (1.2e-07 absolute, cosine
        0.99999992).

        The key is UTF-8 byte length rather than character count: this model
        is multilingual, and bytes per token vary far less across scripts
        than characters per token do. The sort is stable, so equal-size
        documents keep arrival order and the split stays reproducible.
        """
        if isinstance(input, str):
            # A bare string would be iterated character by character below,
            # silently producing one garbage vector per character.
            input = [input]
        if input is None or len(input) == 0:
            # None or zero docs: nothing to embed; skip the lazy model
            # download. len() over truthiness so an array-like documents
            # sequence is not rejected by ambiguous-truth-value semantics.
            return []
        self._lazy_load()
        np = self._np
        # One sub-batch pads identically whatever the order, so the sort is
        # only worth its keys once the input splits into several.
        order: range | list[int] = range(len(input))
        if len(input) > self._batch_size:
            order = sorted(range(len(input)), key=lambda i: len(input[i].encode("utf-8")))
        # Row i is filled by the sub-batch that carries document i. ``order``
        # is a permutation of every index, so no placeholder survives; callers
        # (ChromaDB included) zip the result against their ids positionally.
        embeddings: list[list[float] | None] = [None] * len(input)
        # Tokenize and run per sub-batch, not over the whole input: the ONNX
        # runtime only ever holds batch_size rows of attention buffers at a
        # time (#1770).
        for start in range(0, len(order), self._batch_size):
            idxs = order[start : start + self._batch_size]
            sent_emb = _embeddinggemma_forward(
                self._session,
                self._tokenizer,
                self._output_idx,
                np,
                [input[i] for i in idxs],
            )
            # L2-normalize so cosine similarity == dot product (matches what the
            # MTEB methodology assumes; ChromaDB's distance is configured for it).
            norms = np.linalg.norm(sent_emb, axis=1, keepdims=True) + 1e-12
            rows = (sent_emb / norms).tolist()
            if len(rows) != len(idxs):
                # zip would truncate silently and leave a None in the result,
                # which only surfaces far downstream in the caller's array
                # conversion. Fail on the sub-batch that came back short.
                raise RuntimeError(
                    f"embeddinggemma returned {len(rows)} rows for a {len(idxs)}-document sub-batch"
                )
            for row_index, row in zip(idxs, rows):
                embeddings[row_index] = row
        return embeddings

    def embed_query(self, input: list[str]) -> list[list[float]]:  # noqa: A002 — ChromaDB EF protocol
        """Embed query documents (ChromaDB EF protocol)."""
        return self(input)

    def embed_documents(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        """Embed a batch of documents (ChromaDB EF protocol)."""
        return self(input)


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
        import http.client
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
            # ValueError covers an invalid/missing URL scheme and json.JSONDecodeError;
            # http.client.HTTPException covers low-level protocol faults (BadStatusLine,
            # IncompleteRead) common with local/overloaded servers.
            except (HTTPError, URLError, OSError, http.client.HTTPException, ValueError) as e:
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

        if not isinstance(data, dict):
            raise EmbeddingAPIError(
                f"Embedding API at {self._url} returned a non-object response: {data}"
            )
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

    providers, effective = _resolve_providers(device, model)
    cache_key = (model, tuple(providers))
    cached = _EF_CACHE.get(cache_key)  # lock-free fast path; dict.get is GIL-atomic
    if cached is not None:
        return cached
    with _EF_CACHE_LOCK:
        cached = _EF_CACHE.get(cache_key)
        if cached is not None:
            return cached

        threads = _resolve_intra_op_threads()
        if model == "embeddinggemma":
            ef = EmbeddinggemmaONNX(
                preferred_providers=providers,
                intra_op_num_threads=threads,
                batch_size=_resolve_embeddinggemma_batch_size(),
            )
        else:
            # Default: minilm (or anything we don't recognize — back-compat win).
            ef_cls = _build_ef_class()
            ef = ef_cls(preferred_providers=providers, intra_op_num_threads=threads)

        _EF_CACHE[cache_key] = ef
    logger.info(
        "Embedding function initialized (model=%s device=%s providers=%s)",
        model,
        effective,
        providers,
    )
    return ef


def describe_device(device: Optional[str] = None, model: Optional[str] = None) -> str:
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
        if model is None:
            # The resolved device depends on the model (_AUTO_PROVIDER_DENYLIST),
            # so the label would otherwise name a provider we won't use.
            model = cfg.embedding_model
    _, effective = _resolve_providers(device, model)
    return effective


# Probed vector widths, keyed by resolved model name. Populated once per
# process the first time an identity is resolved for a model.
_DIM_CACHE: dict = {}


def current_model_name(model: Optional[str] = None) -> str:
    """Resolve the canonical embedder model name (cheap, no model load).

    This is the configured ``embedding_model`` (``"minilm"`` /
    ``"embeddinggemma"`` / ...), not the embedding function's internal
    ``name()`` (which is spoofed to ``"default"`` for ChromaDB compatibility).
    """
    if model is not None:
        return str(model).strip().lower()
    from .config import MempalaceConfig

    return MempalaceConfig().embedding_model


def probe_dimension(device: Optional[str] = None, model: Optional[str] = None) -> int:
    """Return the embedder's output dimension by embedding a short probe.

    Model-agnostic — works for any model without a hardcoded table — and
    cached per resolved model name so the probe is paid at most once per
    process. Returns ``0`` if the probe fails (treated as "dimension unknown"
    by the identity check, so a probe failure never blocks normal operation).
    """
    name = current_model_name(model)
    cached = _DIM_CACHE.get(name)
    if cached is not None:
        return cached
    try:
        ef = get_embedding_function(device=device, model=model)
        vectors = ef(input=["probe"])
        dim = len(vectors[0]) if vectors and vectors[0] is not None else 0
    except Exception:
        logger.debug("Embedding dimension probe failed for model=%s", name, exc_info=True)
        dim = 0
    _DIM_CACHE[name] = dim
    return dim


def get_embedder_identity(device: Optional[str] = None, model: Optional[str] = None):
    """Resolve the current embedder identity (RFC 001).

    ``model_name`` from config (cheap); ``dimension`` from a cached one-time
    probe. Returns an :class:`~mempalace.backends.base.EmbedderIdentity`.
    """
    from .backends.base import EmbedderIdentity

    return EmbedderIdentity(
        model_name=current_model_name(model),
        dimension=probe_dimension(device=device, model=model),
    )
