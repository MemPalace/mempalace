"""Embedding function factory with hardware acceleration.

Returns a ChromaDB-compatible embedding function bound to a user-selected
backend and device.

Two embedding models are available, selected via ``MEMPALACE_EMBEDDING_MODEL``
or ``embedding_model`` in ``~/.mempalace/config.json``:

* ``minilm`` (default) — ``all-MiniLM-L6-v2``, 384-dim, English-only training.
  ChromaDB's default; what every existing palace was built with.
* ``embeddinggemma`` — ``onnx-community/embeddinggemma-300m-ONNX`` (q8), 384-dim
  via Matryoshka truncation, multilingual (100+ languages). Cross-lingual cos
  ~0.88 on parallel translations vs MiniLM's ~0.35. Recommended for any
  non-English use; onboarding offers it as the default. The ~300 MB ONNX
  model is lazy-downloaded from HuggingFace on first use. Switching models
  on an existing palace requires ``mempalace repair rebuild-index``
  (different vector space).

Independently of model choice, the *backend* that runs the model can be
either ONNX Runtime (default) or, for the ``mps`` device specifically,
PyTorch + sentence-transformers. Switching backend/device does not
invalidate an existing palace — the same model weights and vector space
are used either way.

Supported devices (env ``MEMPALACE_EMBEDDING_DEVICE`` or ``embedding_device``
in ``~/.mempalace/config.json``):

* ``auto`` — prefer ``mps`` ▸ ``cuda`` ▸ ``coreml`` ▸ ``dml``, fall back to CPU
* ``cpu`` — force ONNX Runtime on CPU (the historical default)
* ``cuda`` — NVIDIA GPU via ``onnxruntime-gpu`` (``pip install mempalace[gpu]``)
* ``coreml`` — Apple Neural Engine via ONNX Runtime CoreML provider (macOS)
* ``dml`` — DirectML (Windows / AMD / Intel GPUs)
* ``mps`` — Apple Metal GPU via PyTorch + sentence-transformers
  (``pip install mempalace[mps]``)

Why ``mps`` is its own path: on Apple Silicon, ChromaDB's bundled ONNX
embedding function enables ``CoreMLExecutionProvider`` by default, which
silently falls back op-by-op to CPU for ``all-MiniLM-L6-v2`` because some
ops are not yet implemented in CoreML's MLProgram lowering. The resulting
ANE↔CPU copies cost more than they save (measured 60–256× slowdown vs.
PyTorch MPS on the same hardware, M5, 200 real chunks). Routing
``mps`` through sentence-transformers + PyTorch bypasses CoreML
entirely. The ``coreml`` device is retained for users who want ONNX
Runtime's CoreML provider explicitly (e.g. on an M1 base where the
ANE↔CPU thrash is less pronounced).

The ``mps`` and ``cuda``/``coreml``/``dml`` paths produce embeddings that
differ by ~1e-6 (numerical FP drift between ONNX Runtime and PyTorch
implementations of the same MiniLM weights). This drift is well below the
noise floor of cosine retrieval, so backends can be switched on an
existing palace without re-mining. The HNSW *index* itself is
order- and value-sensitive, so an index built end-to-end on one backend
will not be bit-identical to one built on another (graph neighbor lists
differ); query results stay correct, but exact ``recall@k`` can shift by
a hit or two between runtimes. Pin ``MEMPALACE_EMBEDDING_DEVICE`` if you
need strict reproducibility.

Requesting an unavailable accelerator emits a warning and falls back to
CPU rather than hard-failing — mining must still work on a laptop without
CUDA, MPS, or CoreML.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Sentinel returned in the provider list to mark the PyTorch-MPS path.
# Anything that consumes a (providers, device) pair must treat this as a
# signal to take the sentence-transformers branch rather than passing the
# providers through to ONNX Runtime.
_MPS_SENTINEL = "__mempalace_torch_mps__"

_PROVIDER_MAP = {
    "cpu": ["CPUExecutionProvider"],
    "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    "coreml": ["CoreMLExecutionProvider", "CPUExecutionProvider"],
    "dml": ["DmlExecutionProvider", "CPUExecutionProvider"],
    # "mps" is resolved without consulting onnxruntime — the sentinel exists so
    # cache_key / EF-construction code can stay uniform across backends.
    "mps": [_MPS_SENTINEL, "CPUExecutionProvider"],
}

_DEVICE_EXTRA = {
    "cuda": "mempalace[gpu]",
    "coreml": "mempalace[coreml]",
    "dml": "mempalace[dml]",
    "mps": "mempalace[mps]",
}

# auto-resolution order. MPS is preferred over CoreML on Apple Silicon
# because sentence-transformers + torch.mps avoids the CoreML op-fallback
# thrash that pins ``onnx_default`` to ~2 chunks/s on this model.
_AUTO_ORDER_ONNX = [
    ("CUDAExecutionProvider", "cuda"),
    ("CoreMLExecutionProvider", "coreml"),
    ("DmlExecutionProvider", "dml"),
]

_EF_CACHE: dict = {}
# Check-then-construct on the cache must be atomic: without it, two threads
# resolving the same key each keep their own EF instance, and each instance
# later lazy-loads its own copy of the model.
_EF_CACHE_LOCK = threading.Lock()
_WARNED: set = set()


def _torch_mps_available() -> bool:
    """Return True iff PyTorch is installed AND its MPS backend is usable.

    Wrapped in a broad ``except`` because torch can raise platform-specific
    errors on non-Apple Silicon hosts (NotImplementedError, RuntimeError),
    and we want a clean False fallback rather than a stack trace.
    """
    try:
        import torch

        return (
            hasattr(torch.backends, "mps")
            and torch.backends.mps.is_available()
            and torch.backends.mps.is_built()
        )
    except Exception:
        return False


def _sentence_transformers_available() -> bool:
    """True iff both ``torch`` and ``sentence_transformers`` import cleanly."""
    try:
        import sentence_transformers  # noqa: F401
        import torch  # noqa: F401

        return True
    except ImportError:
        return False


def _resolve_providers(device: str) -> tuple[list, str]:
    """Return ``(provider_list, effective_device)`` for ``device``.

    For ONNX devices the first element of ``provider_list`` is the
    onnxruntime provider name. For the ``mps`` device the first element is
    :data:`_MPS_SENTINEL` and the rest of the system reads ``effective``
    to take the sentence-transformers branch.

    Falls back to CPU (with a one-shot warning) when the requested
    accelerator is not compiled into the installed ``onnxruntime`` or when
    the optional ``mempalace[mps]`` extra is missing.
    """
    device = (device or "auto").strip().lower()

    # MPS is special: it does not consult onnxruntime providers at all.
    if device == "mps":
        return _resolve_mps_or_cpu()

    try:
        import onnxruntime as ort

        available = set(ort.get_available_providers())
    except ImportError:
        return (["CPUExecutionProvider"], "cpu")

    if device == "auto":
        # Prefer MPS on Apple Silicon when the optional extra is installed —
        # CoreML thrashes on this model, so MPS is materially faster even
        # though both target the same physical GPU.
        if _torch_mps_available() and _sentence_transformers_available():
            return ([_MPS_SENTINEL, "CPUExecutionProvider"], "mps")
        for provider, name in _AUTO_ORDER_ONNX:
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


def _resolve_mps_or_cpu() -> tuple[list, str]:
    """Return MPS providers if torch+MPS+ST available, else warn and fall to CPU."""
    if not _sentence_transformers_available():
        if "mps" not in _WARNED:
            logger.warning(
                "embedding_device='mps' requested but the optional 'mps' extra "
                "is not installed — falling back to CPU. Install %s.",
                _DEVICE_EXTRA["mps"],
            )
            _WARNED.add("mps")
        return (["CPUExecutionProvider"], "cpu")
    if not _torch_mps_available():
        if "mps" not in _WARNED:
            logger.warning(
                "embedding_device='mps' requested but torch.backends.mps is "
                "not available on this host — falling back to CPU."
            )
            _WARNED.add("mps")
        return (["CPUExecutionProvider"], "cpu")
    return ([_MPS_SENTINEL, "CPUExecutionProvider"], "mps")


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
# does not change any row's vector.
_EMBEDDINGGEMMA_BATCH_SIZE = 32


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
            self._output_idx = output_idx
            self._tokenizer = tokenizer
            self._np = np
            # Session is assigned last: the unlocked fast path above treats a
            # non-None session as "fully loaded", so every other attribute
            # must already be in place when it becomes visible.
            self._session = session

    def __call__(self, input: str | list[str] | None) -> list[list[float]]:  # noqa: A002 — ChromaDB EF protocol
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
        embeddings: list[list[float]] = []
        # Tokenize and run per sub-batch, not over the whole input: padding
        # is to the longest sequence in the sub-batch, and the ONNX runtime
        # only ever holds batch_size rows of attention buffers at a time
        # (#1770).
        for start in range(0, len(input), self._batch_size):
            chunk = input[start : start + self._batch_size]
            texts = [_EMBEDDINGGEMMA_PREFIX + t for t in chunk]
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
            embeddings.extend((sent_emb / norms).tolist())
        return embeddings

    def embed_query(self, input: list[str]) -> list[list[float]]:  # noqa: A002 — ChromaDB EF protocol
        """Embed query documents (ChromaDB EF protocol)."""
        return self(input)

    def embed_documents(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        """Embed a batch of documents (ChromaDB EF protocol)."""
        return self(input)


def _build_mps_ef(model_name: str = "all-MiniLM-L6-v2"):
    """Build a sentence-transformers EF on torch MPS, named ``"default"``.

    The ``name()`` override mirrors :func:`_build_ef_class` — it lets a
    palace persisted by ChromaDB's bundled default EF reopen with this EF
    without tripping the 1.x ``Embedding function conflict`` guard. The
    embeddings produced agree with ONNX Runtime's to ~1e-6 (same MiniLM
    weights, FP32 vs ANE/MPS arithmetic ordering), well below cosine
    retrieval's noise floor.

    Only builds ``all-MiniLM-L6-v2`` — there is no sentence-transformers
    equivalent wired up for ``embeddinggemma`` here (only its ONNX artifact
    via :class:`EmbeddinggemmaONNX`). Callers must not route
    ``model="embeddinggemma"`` through this function.
    """
    from chromadb.utils.embedding_functions.sentence_transformer_embedding_function import (
        SentenceTransformerEmbeddingFunction,
    )

    class _MempalaceMPS(SentenceTransformerEmbeddingFunction):
        @staticmethod
        def name() -> str:
            return "default"

    return _MempalaceMPS(model_name=model_name, device="mps")


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

    providers, effective = _resolve_providers(device)

    if effective == "mps" and model == "embeddinggemma":
        # _build_mps_ef only has a verified build for all-MiniLM-L6-v2.
        # Silently taking the mps path here would build MiniLM vectors
        # under an EF the palace still expects to be embeddinggemma-shaped
        # — a vector-space correctness bug, not just a missed speedup.
        # Fall back to embeddinggemma's own (CPU) ONNX path instead.
        if "mps-embeddinggemma" not in _WARNED:
            logger.warning(
                "embedding_device='mps' has no embeddinggemma implementation; "
                "using CPU for model='embeddinggemma' instead."
            )
            _WARNED.add("mps-embeddinggemma")
        providers, effective = ["CPUExecutionProvider"], "cpu"

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
            ef = EmbeddinggemmaONNX(preferred_providers=providers, intra_op_num_threads=threads)
        elif providers and providers[0] == _MPS_SENTINEL:
            ef = _build_mps_ef()
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


def describe_device(device: Optional[str] = None) -> str:
    """Return a short human-readable label for the resolved device.

    Used by the miner CLI header so users can see at a glance whether GPU
    acceleration actually engaged.
    """
    if device is None:
        from .config import MempalaceConfig

        device = MempalaceConfig().embedding_device
    _, effective = _resolve_providers(device)
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
