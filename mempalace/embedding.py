"""Embedding function factory with hardware acceleration.

Returns a ChromaDB-compatible embedding function bound to a user-selected
ONNX Runtime execution provider.

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

import logging
from typing import Optional

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
        hf_hub_download(
            _EMBEDDINGGEMMA_REPO, subfolder="onnx", filename=_EMBEDDINGGEMMA_ONNX + "_data"
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

    def embed_query(self, input: list[str]) -> list[list[float]]:  # noqa: A002 — ChromaDB EF protocol
        """Embed query documents (ChromaDB EF protocol)."""
        return self(input)

    def embed_documents(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        """Embed a batch of documents (ChromaDB EF protocol)."""
        return self(input)


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
    """Return a short human-readable label for the resolved device.

    Used by the miner CLI header so users can see at a glance whether GPU
    acceleration actually engaged.
    """
    if device is None:
        from .config import MempalaceConfig

        device = MempalaceConfig().embedding_device
    _, effective = _resolve_providers(device)
    return effective
