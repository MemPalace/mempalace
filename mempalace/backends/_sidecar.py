"""Shared embedder-identity sidecar (RFC 001).

A small JSON file in the palace directory, keyed by collection name, recording
the embedder identity (``model_name`` / ``dimension``). It is deliberately
*separate* from a backend's mismatch marker: a marker's presence signals
"palace initialized" (reads raise ``CollectionNotInitializedError`` when the
marker exists but the store doesn't), so recording identity at first empty open
must not create one. The sidecar is unguarded, so a brand-new palace can record
identity immediately — the same approach the chroma backend uses.
"""

import json
import logging
import os
from typing import Optional

from ..fsio import (
    atomic_write_json as _atomic_write_json,
    read_json_for_update as _read_json_for_update,
)

EMBEDDER_SIDECAR_FILENAME = "mempalace_embedder.json"

logger = logging.getLogger(__name__)


def read_embedder_sidecar(path: Optional[str], collection_name: Optional[str]):
    """Return the recorded :class:`EmbedderIdentity` for ``collection_name``, or None.

    Robust to a missing, unreadable, or malformed (non-dict) sidecar — any of
    those degrade to ``None`` (the ``unknown`` state) rather than raising.
    """
    from .base import EmbedderIdentity

    if not path or not collection_name or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(collection_name)
    if not isinstance(entry, dict) or not entry.get("model_name"):
        return None
    return EmbedderIdentity(
        model_name=str(entry["model_name"]),
        dimension=int(entry.get("dimension") or 0),
    )


def write_embedder_sidecar(path: Optional[str], collection_name: Optional[str], identity) -> None:
    """Record ``identity`` for ``collection_name`` in the sidecar, creating it if needed.

    No-ops for a missing path, missing collection name, or a nameless identity.
    Preserves other collections' entries. Creates the palace directory when it
    does not exist yet: qdrant and pgvector record a new palace's identity on
    its first open, before their first upsert creates the folder, and the
    write used to fail there silently, leaving the palace unrecorded for good.

    The file is replaced atomically, so a failed write keeps the previous
    one, and a failure raises
    :class:`~mempalace.backends.base.EmbedderIdentityRecordError` instead of
    passing silently. Only write paths record an identity, so read-only opens
    are unaffected.

    A dimension of 0 means the probe failed. It never overwrites a known
    dimension for the same model; for a new model it is recorded as unknown
    with a warning.
    """
    if not path or not collection_name or not identity or not getattr(identity, "model_name", ""):
        return
    directory = os.path.dirname(path) or "."
    if not os.path.isdir(directory):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise _record_error(path, collection_name, exc) from exc
        try:
            os.chmod(directory, 0o700)
        except (OSError, NotImplementedError):
            pass
    from ..palace import mine_lock

    try:
        with mine_lock(path):
            # A sidecar that does not parse is moved aside and the write refused.
            data = dict(_read_json_for_update(path, expect=dict) or {})
            model_name = str(identity.model_name)
            dimension = _dimension_to_record(
                data.get(collection_name), model_name, int(identity.dimension or 0)
            )
            data[collection_name] = {"model_name": model_name, "dimension": dimension}
            _atomic_write_json(path, data)
    except (OSError, NotImplementedError, TypeError, ValueError) as exc:
        raise _record_error(path, collection_name, exc) from exc


def _dimension_to_record(previous, model_name: str, dimension: int) -> int:
    """Keep a known dimension when this write only failed to probe it."""
    if dimension:
        return dimension
    if isinstance(previous, dict) and previous.get("model_name") == model_name:
        try:
            return int(previous.get("dimension") or 0)
        except (TypeError, ValueError):
            return 0
    logger.warning(
        "Recording embedder %r with an unknown dimension: its dimension probe failed.",
        model_name,
    )
    return 0


def _record_error(path: str, collection_name: str, exc: BaseException):
    from .base import EmbedderIdentityRecordError

    return EmbedderIdentityRecordError(
        f"could not record the embedder identity of collection {collection_name!r} "
        f"in {path}: {type(exc).__name__}: {exc}. Without it a later model swap "
        "would not be detected; check that the palace directory is writable and retry."
    )
