"""First-party local media asset references and vector storage."""

from __future__ import annotations

import hashlib
import logging
import math
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MEDIA_MODALITY = {"image": "vision", "audio": "audio", "video": "vision"}
_SUPPORTED_MEDIA_TYPES = frozenset(_MEDIA_MODALITY)
_SUPPORTED_EMBEDDING_DIMENSIONS = frozenset({768, 512, 256, 128})
ASSET_EMBEDDING_DIMENSION = 768  # default only; each asset records the configured width


class MediaAssetError(RuntimeError):
    """Raised when a media reference cannot safely be indexed."""


class MediaAssetSetupError(MediaAssetError):
    """A global provider or package problem that prevents all media indexing."""


@dataclass(frozen=True)
class MediaAsset:
    """A local media file reference. The media bytes stay at ``source_file``."""

    asset_id: str
    source_id: str
    source_file: str
    media_type: str
    mime_type: str
    title: str
    project: str
    inferred_wing: str
    file_size_bytes: int
    file_mtime_ns: int
    file_modified_at: str
    related_drawer_id: Optional[str] = None
    segment_start: Optional[float] = None
    segment_end: Optional[float] = None
    duration: Optional[float] = None
    file_exists: bool = True

    def __post_init__(self) -> None:
        required_strings = (
            "asset_id",
            "source_id",
            "source_file",
            "media_type",
            "mime_type",
            "title",
            "project",
            "inferred_wing",
            "file_modified_at",
        )
        for field_name in required_strings:
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise MediaAssetError(f"{field_name} must be a non-empty string")
        if self.media_type not in _SUPPORTED_MEDIA_TYPES:
            raise MediaAssetError(f"unsupported media type {self.media_type!r}")
        if not Path(self.source_file).is_absolute():
            raise MediaAssetError("source_file must be an absolute local path")
        if (
            isinstance(self.file_size_bytes, bool)
            or not isinstance(self.file_size_bytes, int)
            or self.file_size_bytes < 0
        ):
            raise MediaAssetError("file_size_bytes must be a non-negative integer")
        if (
            isinstance(self.file_mtime_ns, bool)
            or not isinstance(self.file_mtime_ns, int)
            or self.file_mtime_ns < 0
        ):
            raise MediaAssetError("file_mtime_ns must be a non-negative integer")
        if not isinstance(self.file_exists, bool):
            raise MediaAssetError("file_exists must be a boolean")
        if self.duration is not None and (
            isinstance(self.duration, bool)
            or not isinstance(self.duration, (int, float))
            or not math.isfinite(self.duration)
            or self.duration < 0
        ):
            raise MediaAssetError("duration must be a finite, non-negative number")
        if self.related_drawer_id is not None and (
            not isinstance(self.related_drawer_id, str)
            or not self.related_drawer_id.strip()
            or len(self.related_drawer_id) > 256
        ):
            raise MediaAssetError(
                "related_drawer_id must be a non-empty string of at most 256 characters"
            )
        for field_name in ("segment_start", "segment_end"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise MediaAssetError(f"{field_name} must be a finite, non-negative number")
        if (
            self.segment_start is not None
            and self.segment_end is not None
            and self.segment_end <= self.segment_start
        ):
            raise MediaAssetError("segment_end must be greater than segment_start")

    def descriptor(self) -> str:
        """Return searchable descriptor text without captioning or copying bytes."""
        return f"{self.media_type.title()} asset: {self.title}"

    def metadata(self) -> dict[str, str | int | float | bool]:
        """Return flat scalar metadata accepted by every supported backend."""
        from .config import normalize_wing_name

        metadata: dict[str, str | int | float | bool] = {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_path": self.source_file,
            "source_file": self.source_file,
            "media_type": self.media_type,
            "mime_type": self.mime_type,
            "title": self.title,
            "project": self.project,
            "wing": normalize_wing_name(self.project) or "media",
            "room": "media",
            "inferred_wing": self.inferred_wing,
            "file_size_bytes": self.file_size_bytes,
            "file_mtime_ns": self.file_mtime_ns,
            "file_modified_at": self.file_modified_at,
            "file_exists": self.file_exists,
        }
        if self.related_drawer_id:
            metadata["related_drawer_id"] = self.related_drawer_id
        if self.segment_start is not None:
            metadata["segment_start"] = float(self.segment_start)
        if self.segment_end is not None:
            metadata["segment_end"] = float(self.segment_end)
        if self.duration is not None:
            metadata["duration"] = float(self.duration)
        return metadata


def media_asset_exists(asset: MediaAsset | dict[str, Any]) -> bool:
    """Check whether a stored reference still resolves to a local file."""
    source_file = asset.source_file if isinstance(asset, MediaAsset) else asset.get("source_file")
    return isinstance(source_file, str) and Path(source_file).is_file()


def stable_asset_id(source_file: str | os.PathLike[str]) -> str:
    """Build a repeatable ID from a resolved local path."""
    resolved = str(Path(source_file).expanduser().resolve(strict=False))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return f"media_{digest}"


def _audio_duration(path: Path) -> Optional[float]:
    """Read duration from an optional audio header probe, without decoding."""
    try:
        import soundfile as sf

        duration = sf.info(str(path)).duration
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < 0
        ):
            return None
        return float(duration)
    except Exception:  # noqa: BLE001 - optional/unsupported probes must not block discovery
        return None


def _validate_provider(media_type: str):
    if media_type not in _SUPPORTED_MEDIA_TYPES:
        raise MediaAssetError(f"unsupported media type {media_type!r}")

    from .config import MempalaceConfig
    from .embedding import get_embedding_function

    cfg = MempalaceConfig()
    if cfg.embedding_model != "embeddinggemma2":
        raise MediaAssetSetupError(
            "native media indexing requires embedding_model='embeddinggemma2'; "
            f"the configured provider is {cfg.embedding_model!r}"
        )
    try:
        embedder = get_embedding_function(model="embeddinggemma2")
    except Exception as exc:
        raise MediaAssetSetupError(
            f"could not initialize the EmbeddingGemma 2 provider: {exc}"
        ) from exc
    dimension = getattr(embedder, "dimension", None)
    if dimension not in _SUPPORTED_EMBEDDING_DIMENSIONS:
        raise MediaAssetSetupError(
            "media assets require EmbeddingGemma 2 dimension 768, 512, 256, or 128; "
            f"configured dimension is {dimension!r}"
        )
    modalities = str(getattr(embedder, "modalities", "")).split("+")
    needed = _MEDIA_MODALITY[media_type]
    if needed not in modalities and "all" not in modalities:
        raise MediaAssetSetupError(
            f"EmbeddingGemma 2 is configured for {getattr(embedder, 'modalities', None)!r}; "
            f"enable the {needed} encoder to index {media_type} assets"
        )
    if not callable(getattr(embedder, "embed_media", None)):
        raise MediaAssetSetupError(
            "the configured EmbeddingGemma 2 provider does not expose native media embedding"
        )
    return embedder


def _validate_vector(vector: Any, dimension: int) -> list[float]:
    try:
        import numpy as np

        values = np.asarray(vector, dtype=np.float32)
    except ImportError as exc:
        raise MediaAssetSetupError(f"NumPy is required to validate media vectors: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise MediaAssetError(f"media embedder returned an invalid vector: {exc}") from exc
    if values.ndim == 2 and values.shape[0] == 1:
        values = values[0]
    if values.ndim != 1 or values.size != dimension:
        raise MediaAssetError(
            f"media embedder returned shape {values.shape}; expected ({dimension},)"
        )
    if not bool(np.isfinite(values).all()):
        raise MediaAssetError("media embedder returned NaN or infinite values")
    norm = float(np.linalg.norm(values))
    if not math.isfinite(norm) or norm <= 0:
        raise MediaAssetError("media embedder returned a zero or invalid vector")
    return (values / norm).astype(np.float32, copy=False).tolist()


def _existing_asset_metadata(collection, asset_id: str) -> dict[str, Any]:
    try:
        result = collection.get(ids=[asset_id], include=["metadatas"])
        return result.metadatas[0] if result.ids and result.metadatas else {}
    except Exception as exc:
        raise MediaAssetError(f"could not inspect existing media asset {asset_id}: {exc}") from exc


def _validate_related_drawer(asset: MediaAsset, drawers) -> None:
    if asset.related_drawer_id is None:
        return
    try:
        result = drawers.get(ids=[asset.related_drawer_id], include=[])
    except Exception as exc:
        raise MediaAssetError(
            f"could not verify related drawer {asset.related_drawer_id!r}: {exc}"
        ) from exc
    if asset.related_drawer_id not in result.ids:
        raise MediaAssetError(
            f"related drawer {asset.related_drawer_id!r} does not exist in this palace; "
            "index the drawer first or omit --related-drawer-id"
        )


def store_media_asset(asset: MediaAsset, palace_path: str, *, drawer_collection=None) -> int:
    """Embed and idempotently upsert one media reference into the fixed assets collection."""
    if not isinstance(asset, MediaAsset):
        raise TypeError("asset must be a MediaAsset")
    path = Path(asset.source_file)
    if not path.is_file():
        raise MediaAssetError(f"media file is no longer available: {asset.source_file}")

    # Provider configuration is lazy and cheap to inspect. Do this before
    # opening the collection so global setup failures are reported as fatal,
    # while actual model inference remains after identity validation below.
    embedder = _validate_provider(asset.media_type)

    from .palace import ASSETS_COLLECTION_NAME, get_collection

    # Opening the target enforces collection identity before costly native
    # model work. Preserve the asset's original filed_at on re-ingest.
    collection = get_collection(palace_path, collection_name=ASSETS_COLLECTION_NAME)
    prior = _existing_asset_metadata(collection, asset.asset_id)
    if asset.related_drawer_id is not None:
        if drawer_collection is None:
            drawer_collection = get_collection(palace_path)
        _validate_related_drawer(asset, drawer_collection)

    try:
        dimension = int(embedder.dimension)
        vector = _validate_vector(
            embedder.embed_media(str(path), media_type=asset.media_type), dimension
        )
    except ImportError as exc:
        raise MediaAssetSetupError(
            f"missing media embedding dependency for {asset.media_type}: {exc}"
        ) from exc
    except MediaAssetSetupError:
        raise
    except MediaAssetError:
        raise
    except Exception as exc:
        raise MediaAssetError(
            f"failed to embed {asset.media_type} asset {path.name}: {exc}"
        ) from exc

    indexed_at = datetime.now(timezone.utc).isoformat()
    metadata = asset.metadata()
    metadata.update(
        {
            "embedding_model": embedder.identity,
            "embedding_identity": embedder.identity,
            "embedding_dimension": dimension,
            "embedding_modalities": str(embedder.modalities),
            "indexed_at": indexed_at,
            "last_modified": indexed_at,
            "filed_at": prior.get("filed_at") or indexed_at,
        }
    )
    collection.upsert(
        ids=[asset.asset_id],
        documents=[asset.descriptor()],
        metadatas=[metadata],
        embeddings=[vector],
    )
    return 1


def asset_from_path(
    path: str | os.PathLike[str],
    *,
    source_root: str | os.PathLike[str],
    media_type: str,
    project: str,
    related_drawer_id: Optional[str] = None,
) -> MediaAsset:
    """Construct flat asset metadata from a path already checked by the adapter."""
    resolved = Path(path).resolve(strict=True)
    root = Path(source_root).resolve(strict=True)
    if media_type not in _SUPPORTED_MEDIA_TYPES:
        raise MediaAssetError(f"unsupported media type {media_type!r}")
    stat = resolved.stat()
    mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    source_id = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return MediaAsset(
        asset_id=stable_asset_id(resolved),
        source_id=f"source_{source_id}",
        source_file=str(resolved),
        media_type=media_type,
        mime_type=mime_type,
        title=resolved.name,
        project=project,
        inferred_wing="audio" if media_type == "audio" else "visual",
        file_size_bytes=stat.st_size,
        file_mtime_ns=stat.st_mtime_ns,
        file_modified_at=modified,
        related_drawer_id=related_drawer_id,
        duration=_audio_duration(resolved) if media_type == "audio" else None,
    )
