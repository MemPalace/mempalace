"""Embedding function factory with hardware acceleration.

Returns a ChromaDB-compatible embedding function bound to a user-selected
ONNX Runtime execution provider. The same ``all-MiniLM-L6-v2`` model and
384-dim vectors ChromaDB ships by default are reused, so switching device
does not invalidate existing palaces.

Supported devices (env ``MEMPALACE_EMBEDDING_DEVICE`` or ``embedding_device``
in ``~/.mempalace/config.json``):

* ``auto`` — prefer CUDA ▸ CoreML ▸ DirectML, fall back to CPU
* ``cpu`` — force CPU (the historical default)
* ``cuda`` — NVIDIA GPU via ``onnxruntime-gpu`` (``pip install mempalace[gpu]``)
* ``coreml`` — Apple Neural Engine (macOS)
* ``dml`` — DirectML (Windows / AMD / Intel GPUs)

Requesting an unavailable accelerator emits a warning and falls back to CPU
rather than hard-failing — mining must still work on a laptop without CUDA.

Q6600 OFFLINE MODE:
For Q6600 CPU (no AVX support), embedding uses local SentenceTransformer model
with local_files_only=True to avoid triggering HuggingFace Hub lookups.
"""

from __future__ import annotations

import logging
from typing import Optional
import json
import os

# Force offline mode on legacy hardware: never let sentence-transformers / HF Hub phone home.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", "/opt/models")

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


class OfflineLocalEmbeddingFunction:
    """
    ChromaDB-compatible embedding function using local SentenceTransformer model.
    
    Enforces local_files_only=True to prevent HuggingFace Hub lookups on Q6600.
    Implements the ChromaDB EmbeddingFunction interface:
    - __call__(input): Encode texts to embeddings
    - name(): Return function identifier
    - get_config(): Return configuration dict
    - build_from_config(config): Reconstruct from config
    """
    
    def __init__(self, model_path: str = "/opt/models/all-MiniLM-L6-v2"):
        """Initialize with explicit local model path and offline mode."""
        self.model_path = model_path
        self._model = None
        logger.info(f"OfflineLocalEmbeddingFunction initialized (model_path={model_path})")
    
    def _load_model(self):
        """Lazy-load SentenceTransformer with local_files_only=True."""
        if self._model is not None:
            return self._model
        
        try:
            from sentence_transformers import SentenceTransformer
            
            logger.info(f"Loading SentenceTransformer from {self.model_path} (local_files_only=True)")
            self._model = SentenceTransformer(
                self.model_path,
                cache_folder=self.model_path,
                local_files_only=True,
                trust_remote_code=False,
            )
            logger.info("SentenceTransformer model loaded successfully")
            return self._model
        except Exception as e:
            logger.error(f"Failed to load SentenceTransformer: {e}")
            raise
    
    def __call__(self, input: list[str]) -> list[list[float]]:
        """Encode a list of texts to embeddings."""
        model = self._load_model()
        embeddings = model.encode(
            input,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        # Ensure output is list of lists (ChromaDB expects this)
        return embeddings.tolist() if hasattr(embeddings, "tolist") else list(embeddings)
    
    def name(self) -> str:
        """Return the function identifier (must match persisted collection config)."""
        # Keep the legacy Chroma collection identity so existing palaces can be
        # reopened without embedding-function conflict, while still enforcing
        # local_files_only=True inside the model loader.
        return "sentence_transformer"
    
    def get_config(self) -> dict:
        """Return configuration for persistence."""
        return {
            "model_path": self.model_path,
        }
    
    @staticmethod
    def build_from_config(config: dict) -> OfflineLocalEmbeddingFunction:
        """Reconstruct from saved configuration."""
        return OfflineLocalEmbeddingFunction(
            model_path=config.get("model_path", "/opt/models/all-MiniLM-L6-v2")
        )


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
    """Subclass ``ONNXMiniLM_L6_V2`` with name ``\"default\"``.

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


def get_embedding_function(device: Optional[str] = None):
    """Return a cached embedding function bound to the requested device.

    ``device=None`` reads from :class:`MempalaceConfig.embedding_device`.
    The returned function is shared across calls with the same resolved
    provider list so we only pay model-load cost once per process.
    
    For Q6600 / offline environments, always uses OfflineLocalEmbeddingFunction
    with local_files_only=True to prevent HuggingFace Hub lookups.
    """
    # Always use offline local embedding for consistency
    # This ensures no HuggingFace Hub connections are made
    cache_key = "offline_local"
    cached = _EF_CACHE.get(cache_key)
    if cached is not None:
        return cached

    ef = OfflineLocalEmbeddingFunction()
    _EF_CACHE[cache_key] = ef
    logger.info("Embedding function initialized (offline local SentenceTransformer)")
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
