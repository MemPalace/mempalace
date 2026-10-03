"""Config-driven local ONNX embedding backend — any HuggingFace ONNX model.

The built-in local embedders are fixed choices: ``minilm`` is ChromaDB's
default, ``embeddinggemma`` is a hardcoded second option, and everything else
requires ``openai-compat`` — i.e. running a separate inference server just to
try a different local model. #1563 and #1261 ask for the missing piece:
selecting a local embedding model from configuration, without editing package
source or standing up an endpoint.

:class:`GenericONNXEmbedding` is that piece. It loads any HuggingFace-hosted
ONNX encoder and is parameterized by repo, ONNX file, tokenizer, pooling
strategy, document/query prefixes, and the EF identity name persisted on the
collection. Two entry points:

* ``embedding_model: "e5-small"`` — a bundled preset for
  ``intfloat/multilingual-e5-small`` (384-dim, ~120 MB, strong on
  non-English text; see :func:`e5_small_ef` for measured retrieval numbers).
  Vector width matches the MiniLM-shaped 384-dim collections, but the vector
  *space* differs — switching an existing palace still requires
  ``mempalace repair rebuild-index``.
* ``embedding_model: "generic-onnx"`` — fully config-driven: point
  ``embedding_onnx_repo`` (and friends, see :mod:`mempalace.config`) at any
  encoder repo that ships an ONNX file and a ``tokenizer.json``.

Uses the same lazy-loaded dependencies as ``EmbeddinggemmaONNX``
(``huggingface_hub``, ``tokenizers``, ``numpy``, ``onnxruntime``) — no new
requirements.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_GENERIC_ONNX_BATCH_SIZE = 32

_E5_SMALL_REPO = "intfloat/multilingual-e5-small"
_E5_SMALL_ONNX = "model.onnx"  # fp32; the fp16 export (model_O4) is slower on CPU EPs
_E5_SMALL_MAX_LEN = 512


class GenericONNXEmbedding:
    """ChromaDB-compatible EF running an arbitrary HuggingFace ONNX encoder.

    The class makes no assumptions about the model beyond the standard
    encoder contract: inputs ``input_ids`` / ``attention_mask`` (plus
    ``token_type_ids`` when the graph declares it), first output of shape
    ``(batch, seq, hidden)``. Pooling (``"mean"`` masked-average or
    ``"cls"`` first token) and L2 normalization happen here, so models that
    export only the per-token output work out of the box.

    Prefix handling exists because instruction-tuned retrieval models (the
    e5 family, bge, gte) expect a role marker prepended to the text.
    ``doc_prefix`` is applied on the indexing path (``__call__`` /
    ``embed_documents``), ``query_prefix`` on ``embed_query`` — callers that
    route everything through ``__call__`` get ``doc_prefix`` for both sides,
    which is the symmetric setup the bundled e5 preset relies on.
    """

    def __init__(
        self,
        repo: str,
        onnx_file: str,
        *,
        subfolder: str = "onnx",
        tokenizer_file: str = "tokenizer.json",
        max_len: int = 512,
        pooling: str = "mean",
        doc_prefix: str = "",
        query_prefix: str = "",
        normalize: bool = True,
        ef_name: str = "generic_onnx",
        batch_size: int = _GENERIC_ONNX_BATCH_SIZE,
        preferred_providers=None,
        intra_op_num_threads: int = 0,
    ):
        if not repo:
            raise ValueError("GenericONNXEmbedding requires a HuggingFace repo id")
        if pooling not in ("mean", "cls"):
            raise ValueError(f'pooling must be "mean" or "cls", got {pooling!r}')
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        self._repo = repo
        self._onnx_file = onnx_file
        self._subfolder = subfolder
        self._tokenizer_file = tokenizer_file
        self._max_len = max_len
        self._pooling = pooling
        self._doc_prefix = doc_prefix
        self._query_prefix = query_prefix
        self._normalize = normalize
        self._ef_name = ef_name
        self._batch_size = batch_size
        self._providers = (
            list(preferred_providers) if preferred_providers else ["CPUExecutionProvider"]
        )
        self._intra_op_num_threads = intra_op_num_threads
        self._session = None
        self._tokenizer = None
        self._input_names = None
        self._np = None
        # Instances are shared across threads via the embedding-module EF
        # cache; serialize the one-time model load so concurrent cold calls
        # cannot build (and transiently hold) two full model sessions.
        self._load_lock = threading.Lock()

    def name(self) -> str:
        # ChromaDB persists this on the collection and refuses reads with a
        # mismatched EF — that's the signal that forces users to rebuild_index
        # when switching models. Keep it stable: it must encode the vector
        # space, not the class ("e5_small_384", not "generic_onnx").
        return self._ef_name

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
                    "GenericONNXEmbedding requires huggingface_hub, tokenizers, and "
                    "numpy — these ship with mempalace core, so this error usually "
                    "means one was uninstalled or pinned to an incompatible version. "
                    "Reinstall with: pip install --upgrade --force-reinstall mempalace"
                ) from e

            from .embedding import _intra_op_session_options

            def dl(filename):
                kwargs = {"filename": filename}
                if self._subfolder and self._subfolder != ".":
                    kwargs["subfolder"] = self._subfolder
                return hf_hub_download(self._repo, **kwargs)

            logger.info("Downloading %s/%s (cached after first run)…", self._repo, self._onnx_file)
            model_path = dl(self._onnx_file)
            try:
                # External-data ONNX exports keep weights in a sibling file;
                # best-effort because single-file exports simply don't have one.
                dl(self._onnx_file + "_data")
            except Exception:
                pass
            tokenizer = Tokenizer.from_file(dl(self._tokenizer_file))
            tokenizer.enable_truncation(max_length=self._max_len)
            tokenizer.enable_padding()

            session = ort.InferenceSession(
                model_path,
                sess_options=_intra_op_session_options(self._intra_op_num_threads),
                providers=self._providers,
            )
            self._np = np
            self._tokenizer = tokenizer
            self._input_names = {i.name for i in session.get_inputs()}
            # Session is assigned last: the unlocked fast path above treats a
            # non-None session as "fully loaded", so every other attribute
            # must already be in place when it becomes visible.
            self._session = session

    def _embed(self, texts: list[str], prefix: str) -> list[list[float]]:
        self._lazy_load()
        np = self._np
        out = []
        for start in range(0, len(texts), self._batch_size):
            batch = [prefix + t for t in texts[start : start + self._batch_size]]
            encoded = self._tokenizer.encode_batch(batch)
            ids = np.asarray([e.ids for e in encoded], dtype=np.int64)
            mask = np.asarray([e.attention_mask for e in encoded], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._input_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self._session.run(None, feed)[0]  # (batch, seq, hidden)
            if self._pooling == "cls":
                vec = hidden[:, 0]
            else:
                m = mask[..., None]
                vec = (hidden * m).sum(1) / np.clip(m.sum(1), 1, None)
            if self._normalize:
                vec = vec / (np.linalg.norm(vec, axis=1, keepdims=True) + 1e-9)
            out.append(vec)
        return np.vstack(out).astype("float32").tolist()

    def __call__(self, input: str | list[str] | None) -> list[list[float]]:  # noqa: A002 — ChromaDB EF protocol
        """Embed documents (indexing path) — applies ``doc_prefix``."""
        if isinstance(input, str):
            input = [input]
        return self._embed(input, self._doc_prefix) if input else []

    def embed_documents(self, input: str | list[str] | None) -> list[list[float]]:  # noqa: A002
        return self.__call__(input)

    def embed_query(self, input: str | list[str] | None) -> list[list[float]]:  # noqa: A002
        """Embed queries (search path) — applies ``query_prefix``."""
        if isinstance(input, str):
            input = [input]
        return self._embed(input, self._query_prefix) if input else []


def e5_small_ef(preferred_providers=None, intra_op_num_threads: int = 0) -> GenericONNXEmbedding:
    """Preset for ``intfloat/multilingual-e5-small`` (384-dim, ~120 MB).

    A lighter multilingual alternative to embeddinggemma-300m: same 384-dim
    output width, roughly a quarter of the download, and fp32 CPU inference
    fast enough for hook-time mining on laptops.

    On prefixes: canonical e5 usage prepends ``passage: `` to documents and
    ``query: `` to queries, but the embedding call sites in this codebase
    route both sides through ``__call__``, applying one prefix symmetrically.
    Measured on a real 200k-drawer multilingual (RU-heavy) palace, symmetric
    ``query: `` outperforms both alternatives — MRR 0.754 (R@1 0.65) vs 0.684
    for symmetric ``passage: `` vs 0.536 for minilm on the same corpus — so
    the preset pins ``query: `` on both paths. If call sites later split into
    ``embed_documents`` / ``embed_query``, revisit with the asymmetric pair.
    """
    return GenericONNXEmbedding(
        repo=_E5_SMALL_REPO,
        onnx_file=_E5_SMALL_ONNX,
        max_len=_E5_SMALL_MAX_LEN,
        pooling="mean",
        doc_prefix="query: ",
        query_prefix="query: ",
        normalize=True,
        ef_name="e5_small_384",
        preferred_providers=preferred_providers,
        intra_op_num_threads=intra_op_num_threads,
    )


def _derived_ef_name(repo: str, onnx_file: str) -> str:
    """Default collection identity for a config-driven model.

    Derived from repo + ONNX file so that pointing the config at a different
    model (or a different export of the same model) changes the persisted EF
    name and trips ChromaDB's identity check instead of silently mixing
    vector spaces. Tweaks that change vectors *without* changing the file —
    pooling, max_len, prefixes — are not captured here: set
    ``embedding_onnx_ef_name`` explicitly when experimenting with those.
    """
    raw = f"onnx_{repo}_{onnx_file}"
    return "".join(c if c.isalnum() else "_" for c in raw.lower())


def build_generic_onnx_ef(
    model: str, *, preferred_providers=None, intra_op_num_threads: int = 0
) -> GenericONNXEmbedding:
    """Resolve ``embedding_model`` values handled by this module into an EF.

    ``"e5-small"`` returns the bundled preset. ``"generic-onnx"`` reads the
    ``embedding_onnx_*`` settings from :class:`~mempalace.config.MempalaceConfig`
    (each overridable via the matching ``MEMPALACE_EMBEDDING_ONNX_*`` env var)
    and requires at least ``embedding_onnx_repo``.
    """
    if model == "e5-small":
        return e5_small_ef(
            preferred_providers=preferred_providers,
            intra_op_num_threads=intra_op_num_threads,
        )

    from .config import MempalaceConfig

    cfg = MempalaceConfig()
    repo = cfg.embedding_onnx_repo
    if not repo:
        raise ValueError(
            "embedding_model='generic-onnx' requires a model repo — set "
            "embedding_onnx_repo in ~/.mempalace/config.json or the "
            "MEMPALACE_EMBEDDING_ONNX_REPO env var (e.g. intfloat/multilingual-e5-small)"
        )
    onnx_file = cfg.embedding_onnx_file
    return GenericONNXEmbedding(
        repo=repo,
        onnx_file=onnx_file,
        subfolder=cfg.embedding_onnx_subfolder,
        max_len=cfg.embedding_onnx_max_len,
        pooling=cfg.embedding_onnx_pooling,
        doc_prefix=cfg.embedding_onnx_doc_prefix,
        query_prefix=cfg.embedding_onnx_query_prefix,
        ef_name=cfg.embedding_onnx_ef_name or _derived_ef_name(repo, onnx_file),
        preferred_providers=preferred_providers,
        intra_op_num_threads=intra_op_num_threads,
    )
