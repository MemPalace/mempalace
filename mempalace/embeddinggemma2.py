"""Local Sentence Transformers provider for Google's EmbeddingGemma 2.

The model is loaded only on first use. Documents and search queries use the
model's distinct task prompts, and document metadata can supply a title.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import warnings
from pathlib import Path, PurePath
from typing import Any, Optional

logger = logging.getLogger(__name__)

MODEL_ID = "google/embeddinggemma-2"
DEFAULT_REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"
SUPPORTED_DIMENSIONS = frozenset({768, 512, 256, 128})
SUPPORTED_MODALITIES = frozenset({"text", "text+vision", "text+audio", "all"})
SUPPORTED_DEVICES = frozenset({"auto", "mps", "cpu"})
_BATCH_SIZE = 4
_WITNESS = "mempalace embedding provider health check"


class EmbeddingOutputError(RuntimeError):
    """Raised when the model returns vectors that cannot safely be stored."""


def _config_kwargs(modalities: str) -> dict[str, Any]:
    """Return the selective encoder settings documented by the model card."""
    if modalities == "text":
        return {"vision_config": None, "audio_config": None}
    if modalities == "text+vision":
        return {"audio_config": None}
    if modalities == "text+audio":
        return {"vision_config": None}
    return {}


def _as_texts(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        texts = [value]
    else:
        texts = list(value)
    if any(not isinstance(text, str) for text in texts):
        raise TypeError("EmbeddingGemma 2 accepts text strings only")
    return texts


def _metadata_title(metadata: Optional[dict]) -> Optional[str]:
    if not isinstance(metadata, dict):
        return None
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    source_file = metadata.get("source_file")
    if isinstance(source_file, str) and source_file.strip():
        name = PurePath(source_file.replace("\\", "/")).name
        if name:
            return name
    source_path = metadata.get("source_path")
    if isinstance(source_path, str) and source_path.strip():
        name = PurePath(source_path.replace("\\", "/")).name
        if name:
            return name
    return None


class EmbeddingGemma2EmbeddingFunction:
    """Chroma-compatible local EmbeddingGemma 2 function.

    ``__call__`` and :meth:`embed_documents` produce corpus vectors. Use
    :meth:`embed_query` for natural-language retrieval and
    :meth:`embed_code_query` for code retrieval. Media helpers accept local
    image, audio, and video inputs when the configured modality includes the
    corresponding encoder.
    """

    def __init__(
        self,
        dimension: int = 768,
        modalities: str = "text",
        device: str = "auto",
        revision: Optional[str] = None,
    ):
        if isinstance(dimension, bool) or dimension not in SUPPORTED_DIMENSIONS:
            raise ValueError(
                f"dimension must be one of {sorted(SUPPORTED_DIMENSIONS)}, got {dimension!r}"
            )
        modalities = str(modalities).strip().lower()
        if modalities not in SUPPORTED_MODALITIES:
            raise ValueError(
                f"modalities must be one of {sorted(SUPPORTED_MODALITIES)}, got {modalities!r}"
            )
        device = str(device).strip().lower()
        if device not in SUPPORTED_DEVICES:
            raise ValueError(f"device must be one of {sorted(SUPPORTED_DEVICES)}, got {device!r}")

        self.dimension = int(dimension)
        self.modalities = modalities
        self.device = device
        self.revision = str(revision or DEFAULT_REVISION).strip()
        if not self.revision:
            raise ValueError("revision must be a non-empty model revision")
        self._model = None
        self._resolved_device: Optional[str] = None
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    @staticmethod
    def name() -> str:
        """Stable Chroma embedding-function class name."""
        return "embeddinggemma2"

    @staticmethod
    def default_space() -> str:
        return "cosine"

    @staticmethod
    def supported_spaces() -> list[str]:
        return ["cosine"]

    @staticmethod
    def validate_config(config: dict) -> None:
        if not isinstance(config, dict):
            raise ValueError("EmbeddingGemma 2 config must be an object")
        allowed = {"dimension", "modalities", "device", "revision"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(f"unknown EmbeddingGemma 2 config keys: {sorted(unknown)}")

    @staticmethod
    def build_from_config(config: dict) -> "EmbeddingGemma2EmbeddingFunction":
        EmbeddingGemma2EmbeddingFunction.validate_config(config)
        return EmbeddingGemma2EmbeddingFunction(**config)

    def get_config(self) -> dict:
        return {
            "dimension": self.dimension,
            "modalities": self.modalities,
            "device": self.device,
            "revision": self.revision,
        }

    @property
    def effective_device(self) -> Optional[str]:
        """Resolved runtime device after the lazy model load, if loaded."""
        return self._resolved_device

    def is_legacy(self) -> bool:
        return False

    @property
    def identity(self) -> str:
        return (
            f"embeddinggemma2:{MODEL_ID}@{self.revision}:{self.dimension}:"
            f"{self.modalities}:retrieval-v1"
        )

    def _select_device(self, torch) -> str:
        if self.device == "cpu":
            return "cpu"
        mps_available = bool(
            getattr(getattr(torch, "backends", None), "mps", None)
            and torch.backends.mps.is_available()
        )
        if self.device == "mps":
            if not mps_available:
                raise RuntimeError("device='mps' requested, but PyTorch MPS is unavailable")
            return "mps"
        return "mps" if mps_available else "cpu"

    def _load_model_for(self, device: str):
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "EmbeddingGemma 2 requires sentence-transformers>=6.1.0, "
                "transformers>=5.19.0, and torch. Install the EmbeddingGemma 2 extra."
            ) from exc

        model = SentenceTransformer(
            MODEL_ID,
            device=device,
            revision=self.revision,
            model_kwargs={"dtype": torch.float32},
            config_kwargs=_config_kwargs(self.modalities),
        )
        return model

    def _encode_raw(self, inputs, *, prompt_name: Optional[str] = None, prompt=None):
        kwargs = {
            "batch_size": _BATCH_SIZE,
            "show_progress_bar": False,
            "convert_to_numpy": True,
            "convert_to_tensor": False,
            "precision": "float32",
            "truncate_dim": self.dimension,
            "normalize_embeddings": True,
        }
        if prompt_name is not None:
            kwargs["prompt_name"] = prompt_name
        if prompt is not None:
            kwargs["prompt"] = prompt
        return self._model.encode(inputs, **kwargs)

    def _validate_and_normalize(self, raw, expected_rows: int):
        import numpy as np

        try:
            vectors = np.asarray(raw, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise EmbeddingOutputError(
                f"EmbeddingGemma 2 returned malformed vectors: {exc}"
            ) from exc
        if vectors.ndim != 2:
            raise EmbeddingOutputError(
                f"EmbeddingGemma 2 returned shape {vectors.shape}, expected a 2D matrix"
            )
        if vectors.shape[0] != expected_rows:
            raise EmbeddingOutputError(
                f"EmbeddingGemma 2 returned {vectors.shape[0]} vectors for {expected_rows} inputs"
            )
        if vectors.shape[1] != self.dimension:
            raise EmbeddingOutputError(
                f"EmbeddingGemma 2 returned dimension {vectors.shape[1]}, expected {self.dimension}"
            )
        if not np.isfinite(vectors).all():
            raise EmbeddingOutputError("EmbeddingGemma 2 returned NaN or infinite vector values")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if not np.isfinite(norms).all() or (norms <= 0).any():
            raise EmbeddingOutputError("EmbeddingGemma 2 returned a zero or invalid vector")
        return (vectors / norms).astype(np.float32, copy=False).tolist()

    def _encode_once(self, inputs, *, prompt_name=None, prompt=None):
        raw = self._encode_raw(inputs, prompt_name=prompt_name, prompt=prompt)
        return self._validate_and_normalize(raw, len(inputs))

    @staticmethod
    def _is_mps_runtime_error(exc: RuntimeError) -> bool:
        message = str(exc).lower()
        return "mps" in message or "metal" in message

    @staticmethod
    def _warn_mps_fallback(stage: str, exc: Exception) -> None:
        logger.warning("EmbeddingGemma 2 MPS %s failed (%s); retrying with CPU", stage, exc)
        warnings.warn(
            f"EmbeddingGemma 2 MPS {stage} failed ({exc}); falling back to CPU.",
            RuntimeWarning,
            stacklevel=3,
        )

    def _load_and_check(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            try:
                import torch
            except ImportError as exc:
                raise ImportError(
                    "EmbeddingGemma 2 requires sentence-transformers>=6.1.0, "
                    "transformers>=5.19.0, and torch. Install the EmbeddingGemma 2 extra."
                ) from exc

            selected = self._select_device(torch)
            if selected == "mps":
                try:
                    model = self._load_model_for(selected)
                except RuntimeError as exc:
                    if not self._is_mps_runtime_error(exc):
                        raise
                    self._warn_mps_fallback("model initialization", exc)
                    model = self._load_model_for("cpu")
                    selected = "cpu"
            else:
                model = self._load_model_for(selected)
            self._model = model
            self._resolved_device = selected
            if selected == "mps":
                try:
                    first = self._encode_once([_WITNESS], prompt_name="SearchQuery")[0]
                    second = self._encode_once([_WITNESS], prompt_name="SearchQuery")[0]
                    import numpy as np

                    if not np.allclose(first, second, rtol=1e-5, atol=1e-6):
                        raise EmbeddingOutputError("MPS witness vectors were not repeatable")
                except (RuntimeError, EmbeddingOutputError) as exc:
                    self._warn_mps_fallback("witness", exc)
                    cpu_model = self._load_model_for("cpu")
                    self._model = cpu_model
                    self._resolved_device = "cpu"

    def _encode(self, inputs, *, prompt_name=None, prompt=None):
        if not inputs:
            return []
        with self._inference_lock:
            # Keep first-load MPS validation and fallback atomic with inference.
            # Otherwise a second caller could use the MPS model before its
            # witness has proved the provider healthy.
            self._load_and_check()
            try:
                return self._encode_once(inputs, prompt_name=prompt_name, prompt=prompt)
            except (RuntimeError, EmbeddingOutputError) as exc:
                if self._resolved_device != "mps":
                    raise
                # Decoders also raise RuntimeError for damaged media. Those
                # inputs cannot be repaired by moving a healthy model to CPU.
                if not isinstance(exc, EmbeddingOutputError) and not self._is_mps_runtime_error(
                    exc
                ):
                    raise
                logger.warning(
                    "EmbeddingGemma 2 MPS inference failed (%s); retrying this batch on CPU",
                    exc,
                )
                warnings.warn(
                    f"EmbeddingGemma 2 MPS inference failed ({exc}); retrying this batch on CPU.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                cpu_model = self._load_model_for("cpu")
                self._model = cpu_model
                self._resolved_device = "cpu"
                # Retry exactly once. CPU failures are deliberately propagated.
                return self._encode_once(inputs, prompt_name=prompt_name, prompt=prompt)

    def embed_documents(self, input: str | list[str], metadatas: Optional[list[dict]] = None):
        texts = _as_texts(input)
        if not texts:
            return []
        if metadatas is None:
            metadata_rows = [{} for _ in texts]
        elif isinstance(metadatas, dict):
            metadata_rows = [metadatas]
        else:
            metadata_rows = list(metadatas)
        if len(metadata_rows) != len(texts):
            raise ValueError("metadatas length must match documents length")

        titled = []
        untitled = []
        for index, (text, metadata) in enumerate(zip(texts, metadata_rows)):
            title = _metadata_title(metadata)
            if title:
                titled.append((index, f"title: {title} | text: {text}"))
            else:
                untitled.append((index, text))

        vectors: list[Optional[list[float]]] = [None] * len(texts)
        if untitled:
            encoded = self._encode([text for _, text in untitled], prompt_name="Document")
            for (index, _), vector in zip(untitled, encoded):
                vectors[index] = vector
        if titled:
            encoded = self._encode([text for _, text in titled], prompt="")
            for (index, _), vector in zip(titled, encoded):
                vectors[index] = vector
        return vectors

    def __call__(self, input):  # noqa: A002 - Chroma embedding function protocol
        return self.embed_documents(input)

    def embed_query(self, input):  # noqa: A002 - Chroma embedding function protocol
        return self._encode(_as_texts(input), prompt_name="SearchQuery")

    def embed_code_query(self, input):  # noqa: A002 - Chroma embedding function protocol
        return self._encode(_as_texts(input), prompt_name="CodeRetrieval")

    def _require_media_modality(self, media_type: str) -> None:
        required = {
            "image": {"text+vision", "all"},
            "video": {"text+vision", "all"},
            "audio": {"text+audio", "all"},
        }.get(media_type)
        if required is None:
            raise ValueError("media_type must be one of: image, audio, video")
        if self.modalities not in required:
            raise ValueError(
                f"media_type={media_type!r} requires modalities={sorted(required)}; "
                f"configured modalities are {self.modalities!r}"
            )

    @staticmethod
    def _local_media_path(value, media_type: str) -> str:
        if not isinstance(value, (str, os.PathLike)):
            raise TypeError(f"{media_type} input must be a local filesystem path")
        raw = os.fspath(value)
        if raw.startswith(("http://", "https://")):
            raise ValueError("remote media URLs are not accepted; provide a local file path")
        path = Path(raw).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"{media_type} file does not exist: {path}")
        return str(path.resolve())

    @staticmethod
    def _mono_float32(audio, sampling_rate: int):
        import numpy as np
        from scipy.signal import resample_poly

        if isinstance(sampling_rate, bool) or not isinstance(sampling_rate, (int, np.integer)):
            raise ValueError("audio sampling_rate must be a positive integer")
        sampling_rate = int(sampling_rate)
        if sampling_rate <= 0:
            raise ValueError("audio sampling_rate must be a positive integer")
        samples = np.asarray(audio)
        if np.issubdtype(samples.dtype, np.integer):
            info = np.iinfo(samples.dtype)
            if np.issubdtype(samples.dtype, np.unsignedinteger):
                midpoint = (info.max + 1) / 2.0
                samples = (samples.astype(np.float32) - midpoint) / midpoint
            else:
                samples = samples.astype(np.float32) / max(abs(info.min), info.max)
        else:
            samples = samples.astype(np.float32)
        if samples.ndim == 2:
            samples = samples.mean(axis=1)
        if samples.ndim != 1 or samples.size == 0:
            raise ValueError("audio must contain a non-empty mono or multichannel sample array")
        if not np.isfinite(samples).all():
            raise ValueError("audio contains NaN or infinite samples")
        if sampling_rate != 16_000:
            divisor = math.gcd(sampling_rate, 16_000)
            samples = resample_poly(samples, 16_000 // divisor, sampling_rate // divisor).astype(
                np.float32
            )
        return np.ascontiguousarray(samples, dtype=np.float32)

    @classmethod
    def _load_audio(cls, value):
        if isinstance(value, dict):
            if "array" not in value or "sampling_rate" not in value:
                raise ValueError("audio mapping requires 'array' and 'sampling_rate'")
            audio = value["array"]
            sampling_rate = value["sampling_rate"]
        else:
            path = cls._local_media_path(value, "audio")
            soundfile_error = None
            audio = sampling_rate = None
            try:
                import soundfile as sf
            except ImportError as exc:
                soundfile_error = exc
            else:
                try:
                    audio, sampling_rate = sf.read(path, dtype="float32", always_2d=True)
                except Exception as exc:
                    soundfile_error = exc
            if audio is None and Path(path).suffix.lower() == ".wav":
                try:
                    from scipy.io import wavfile

                    sampling_rate, audio = wavfile.read(path)
                except Exception:
                    # Try TorchCodec below. It supports formats beyond the
                    # WAV-only stdlib fallback and can also recover WAVs when
                    # SciPy cannot decode them.
                    audio = sampling_rate = None
            if audio is None or sampling_rate is None:
                try:
                    from torchcodec.decoders import AudioDecoder
                except ImportError as exc:
                    detail = f" SoundFile error: {soundfile_error}." if soundfile_error else ""
                    raise ImportError(
                        f"could not decode audio file {path}.{detail} "
                        "Install mempalace[multimodal] for TorchCodec support, or provide a "
                        "decoded {'array': ..., 'sampling_rate': ...} input."
                    ) from exc
                try:
                    decoded = AudioDecoder(
                        path,
                        sample_rate=16_000,
                        num_channels=1,
                    ).get_all_samples()
                    audio = decoded.data
                    if hasattr(audio, "detach"):
                        audio = audio.detach().cpu().numpy()
                    import numpy as np

                    audio = np.asarray(audio, dtype=np.float32)
                    if audio.ndim == 2 and audio.shape[0] == 1:
                        audio = audio[0]
                    if audio.ndim != 1 or audio.size == 0:
                        raise ValueError(
                            f"TorchCodec returned audio with shape {audio.shape}, expected mono samples"
                        )
                    audio = np.ascontiguousarray(audio, dtype=np.float32)
                    sampling_rate = 16_000
                except Exception as exc:
                    prior = f"; SoundFile error: {soundfile_error}" if soundfile_error else ""
                    raise ValueError(
                        f"could not decode audio file {path} with TorchCodec: {exc}{prior}"
                    ) from exc
        mono = cls._mono_float32(audio, sampling_rate)
        return {"array": mono, "sampling_rate": 16_000}

    @staticmethod
    def _image_input(value):
        if isinstance(value, (str, os.PathLike)):
            return {"image": EmbeddingGemma2EmbeddingFunction._local_media_path(value, "image")}
        try:
            from PIL import Image
        except ImportError as exc:
            raise ImportError("PIL is required to pass an in-memory image") from exc
        if isinstance(value, Image.Image):
            return {"image": value}
        return {"image": EmbeddingGemma2EmbeddingFunction._local_media_path(value, "image")}

    def embed_media(self, input, media_type: str) -> list[float]:
        """Embed one local image, audio, or video file without a task prefix."""
        media_type = str(media_type).strip().lower()
        self._require_media_modality(media_type)
        if media_type == "image":
            media = self._image_input(input)
        elif media_type == "audio":
            media = {"audio": self._load_audio(input)}
        else:
            media = {
                "video": self._local_media_path(input, "video"),
            }
        return self._encode([media], prompt="")[0]

    def embed_image(self, input) -> list[float]:
        return self.embed_media(input, "image")

    def embed_audio(self, input) -> list[float]:
        return self.embed_media(input, "audio")

    def embed_video(self, input) -> list[float]:
        return self.embed_media(input, "video")
