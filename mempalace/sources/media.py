"""Local image, audio, and video reference adapter."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterator

from .base import (
    AdapterSchema,
    BaseSourceAdapter,
    FieldSpec,
    SourceNotFoundError,
    SourceRef,
    SourceSummary,
)

logger = logging.getLogger(__name__)

_EXTENSIONS = {
    "image": frozenset({".png", ".jpg", ".jpeg", ".webp"}),
    "audio": frozenset({".wav", ".mp3", ".m4a", ".flac"}),
    "video": frozenset({".mp4", ".mov", ".m4v", ".webm"}),
}
_MEDIA_BY_EXTENSION = {
    extension: media_type
    for media_type, extensions in _EXTENSIONS.items()
    for extension in extensions
}
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".cache",
        ".mempalace",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        "target",
        ".next",
        ".turbo",
        "coverage",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".nox",
        ".idea",
        ".vscode",
        ".ipynb_checkpoints",
        "htmlcov",
    }
)


def _path_has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


class MediaAdapter(BaseSourceAdapter):
    """Enumerate local media references without decoding their contents."""

    name = "media"
    adapter_version = "1.0.0"
    capabilities = frozenset({"local_filesystem", "media_references"})
    supported_modes = frozenset({"metadata_only"})
    declared_transformations = frozenset()
    default_privacy_class = "private_local"

    def describe_schema(self) -> AdapterSchema:
        fields = {
            "asset_id": FieldSpec(
                "string", True, "Stable path-derived media asset ID", indexed=True
            ),
            "source_id": FieldSpec("string", True, "Stable source-root ID", indexed=True),
            "source_path": FieldSpec("string", True, "Resolved local media path", indexed=True),
            "source_file": FieldSpec("string", True, "Resolved local media path", indexed=True),
            "media_type": FieldSpec("string", True, "image, audio, or video", indexed=True),
            "mime_type": FieldSpec("string", True, "MIME type inferred from extension"),
            "title": FieldSpec("string", True, "Original media filename", indexed=True),
            "project": FieldSpec("string", True, "Project label", indexed=True),
            "wing": FieldSpec("string", True, "Normalized project wing", indexed=True),
            "room": FieldSpec("string", True, "Fixed media room", indexed=True),
            "inferred_wing": FieldSpec("string", True, "Media-type routing hint", indexed=True),
            "file_size_bytes": FieldSpec("int", True, "Source file size in bytes"),
            "file_mtime_ns": FieldSpec("int", True, "Source file modification time"),
            "file_modified_at": FieldSpec("string", True, "UTC source modification time"),
            "file_exists": FieldSpec("bool", True, "Whether the source existed at indexing time"),
            "related_drawer_id": FieldSpec(
                "string", False, "Optional linked text drawer ID", indexed=True
            ),
            "segment_start": FieldSpec("float", False, "Optional segment start in seconds"),
            "segment_end": FieldSpec("float", False, "Optional segment end in seconds"),
            "duration": FieldSpec("float", False, "Audio duration in seconds when available"),
            "embedding_model": FieldSpec("string", True, "Vector provider identity"),
            "embedding_identity": FieldSpec("string", True, "Vector provider identity"),
            "embedding_dimension": FieldSpec("int", True, "Stored vector width"),
            "embedding_modalities": FieldSpec("string", True, "Loaded provider modalities"),
            "indexed_at": FieldSpec("string", True, "UTC indexing time"),
            "filed_at": FieldSpec("string", True, "UTC indexing time"),
            "last_modified": FieldSpec("string", True, "UTC last index update time"),
        }
        return AdapterSchema(fields=fields, version="1.0")

    def source_summary(self, *, source: SourceRef) -> SourceSummary:
        return SourceSummary(
            description=f"Local media references under {source.local_path or source.uri or '(missing path)'}"
        )

    def ingest(self, *, source: SourceRef, palace) -> Iterator[object]:
        del palace  # Enumeration is model-free and does not touch palace storage.
        if not source.local_path:
            raise SourceNotFoundError("media source requires SourceRef.local_path")

        supplied_root = Path(source.local_path).expanduser().absolute()
        if _path_has_symlink_component(supplied_root):
            raise SourceNotFoundError(
                f"media source path traverses a symbolic link: {supplied_root}"
            )
        try:
            root = supplied_root.resolve(strict=True)
        except OSError as exc:
            raise SourceNotFoundError(f"media source does not exist: {supplied_root}") from exc
        if not root.is_dir() and not root.is_file():
            raise SourceNotFoundError(f"media source is not a regular file or directory: {root}")

        options = source.options or {}
        project = options.get("project") or root.name or "media"
        related_drawer_id = options.get("related_drawer_id")
        if not isinstance(project, str) or not project.strip():
            raise ValueError("media source project must be a non-empty string")
        if related_drawer_id is not None and (
            not isinstance(related_drawer_id, str)
            or not related_drawer_id.strip()
            or len(related_drawer_id) > 256
        ):
            raise ValueError(
                "related_drawer_id must be a non-empty string of at most 256 characters"
            )

        from ..media import asset_from_path

        for path in self._iter_media_paths(root):
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root if root.is_dir() else root.parent)
                if not resolved.is_file() or path.is_symlink():
                    continue
                media_type = _MEDIA_BY_EXTENSION.get(resolved.suffix.lower())
                if media_type is None:
                    continue
                yield asset_from_path(
                    resolved,
                    source_root=root if root.is_dir() else root.parent,
                    media_type=media_type,
                    project=project.strip(),
                    related_drawer_id=related_drawer_id,
                )
            except (OSError, ValueError) as exc:
                logger.warning("skipping unreadable media reference %s: %s", path, exc)

    @staticmethod
    def _iter_media_paths(root: Path) -> Iterator[Path]:
        if root.is_file():
            if not root.name.startswith(".") and root.suffix.lower() in _MEDIA_BY_EXTENSION:
                yield root
            return

        for current, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not name.startswith(".")
                and name.lower() not in _SKIP_DIRS
                and not (current_path / name).is_symlink()
            )
            for filename in sorted(filenames):
                if filename.startswith("."):
                    continue
                path = current_path / filename
                if path.is_symlink() or path.suffix.lower() not in _MEDIA_BY_EXTENSION:
                    continue
                yield path
