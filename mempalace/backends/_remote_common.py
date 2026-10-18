"""Shared helpers for the remote storage backends (pgvector, qdrant, milvus).

Metadata values are coerced one key at a time: a single non-JSON value must
not wipe ``wing``, ``room``, or ``source_file``. The backend marker is
published with :func:`mempalace.fsio.atomic_write_json` and rewritten only
when the remote target changed, so a crash mid-write cannot leave a truncated
marker that reads as a mismatch.
"""

import json
import logging
import os
from typing import Any, ClassVar, Optional

import numpy as np

from ..fsio import atomic_write_json
from ..timeutil import utc_now_iso
from .base import BackendMismatchError, DimensionMismatchError, PalaceRef

logger = logging.getLogger(__name__)

_MARKER_IDENTITY = ("backend", "palace_id", "schema_version")


def jsonable_metadata(meta: Optional[dict], *, reserved: frozenset = frozenset()) -> dict:
    """Return a JSON-serializable copy of ``meta``.

    Values ``json.dumps`` accepts are kept; anything else (NaN included) is
    stored as ``str(value)``.
    """
    cleaned = {}
    for key, value in (meta or {}).items():
        name = str(key)
        if name in reserved:
            raise ValueError(f"metadata key {name!r} clashes with a reserved field")
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            logger.warning(
                "metadata key %r is not JSON-serializable (%s); storing str()",
                name,
                type(value).__name__,
            )
            value = str(value)
        cleaned[name] = value
    return cleaned


def as_vector_array(vector: list) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float32)
    if arr.ndim != 1 or arr.size == 0:
        raise ValueError("embedding must be a non-empty 1D vector")
    return arr


def normalize_vectors(embeddings: list, backend: str) -> tuple:
    vectors = []
    dims = set()
    for embedding in embeddings:
        arr = as_vector_array(embedding)
        vectors.append(arr.astype(float).tolist())
        dims.add(int(arr.size))
    if len(dims) > 1:
        raise DimensionMismatchError(
            f"{backend} batch cannot mix embedding dimensions {sorted(dims)}"
        )
    return vectors, dims.pop() if dims else 0


class RemoteMarkerMixin:
    """The ``<backend>_backend.json`` marker that records which remote target a
    palace directory belongs to. Subclasses set ``marker_filename`` and
    ``marker_hint`` and implement ``_marker_target(palace, config)``."""

    name: ClassVar[str]
    marker_filename: ClassVar[str]
    marker_hint: ClassVar[str]

    @classmethod
    def _marker_path(cls, palace_path: str) -> str:
        return os.path.join(palace_path, cls.marker_filename)

    def _marker_target(self, palace: PalaceRef, config: Any) -> dict:
        raise NotImplementedError

    def _marker_exists(self, palace: PalaceRef) -> bool:
        return bool(palace.local_path and os.path.isfile(self._marker_path(palace.local_path)))

    def _read_marker(self, palace: PalaceRef) -> Optional[dict]:
        if not self._marker_exists(palace):
            return None
        marker_path = self._marker_path(palace.local_path)
        try:
            with open(marker_path, encoding="utf-8") as f:
                marker = json.load(f)
        except (OSError, ValueError) as exc:
            raise BackendMismatchError(f"{self.name} marker is unreadable: {marker_path}") from exc
        return marker if isinstance(marker, dict) else {}

    def _validate_marker_target(self, palace: PalaceRef, config: Any) -> None:
        marker = self._read_marker(palace)
        if marker is None:
            return
        if marker.get("backend") != self.name:
            raise BackendMismatchError(
                f"{self.name} marker does not identify the {self.name} backend"
            )
        actual = marker.get(self.name)
        if not isinstance(actual, dict):
            raise BackendMismatchError(f"{self.name} marker is missing remote target metadata")
        expected = self._marker_target(palace, config)
        mismatched = [key for key, value in expected.items() if actual.get(key) != value]
        if mismatched:
            raise BackendMismatchError(
                f"{self.name} marker remote target does not match current configuration "
                f"({', '.join(mismatched)}); {self.marker_hint} or use a fresh palace directory"
            )

    def _write_marker(self, palace: PalaceRef, config: Any) -> None:
        """Replace the marker only when the remote target changed, keeping an
        existing ``created_at``."""
        if not palace.local_path:
            return
        os.makedirs(palace.local_path, exist_ok=True)
        try:
            os.chmod(palace.local_path, 0o700)
        except (OSError, NotImplementedError):
            pass
        marker = {
            "backend": self.name,
            "schema_version": 1,
            "created_at": utc_now_iso(),
            "palace_id": palace.id,
            self.name: self._marker_target(palace, config),
        }
        path = self._marker_path(palace.local_path)
        try:
            existing = self._read_marker(palace)
        except BackendMismatchError:
            existing = None
        if existing:
            keys = _MARKER_IDENTITY + (self.name,)
            if all(existing.get(k) == marker[k] for k in keys):
                return
            marker["created_at"] = existing.get("created_at") or marker["created_at"]
        atomic_write_json(path, marker)
