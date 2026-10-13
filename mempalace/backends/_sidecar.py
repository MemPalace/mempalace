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
import os
import tempfile
from typing import Optional

EMBEDDER_SIDECAR_FILENAME = "mempalace_embedder.json"


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
    data: dict = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    data[collection_name] = {
        "model_name": str(identity.model_name),
        "dimension": int(identity.dimension or 0),
    }
    try:
        _write_atomically(path, data)
    except (OSError, NotImplementedError, TypeError, ValueError) as exc:
        raise _record_error(path, collection_name, exc) from exc


def _record_error(path: str, collection_name: str, exc: BaseException):
    from .base import EmbedderIdentityRecordError

    return EmbedderIdentityRecordError(
        f"could not record the embedder identity of collection {collection_name!r} "
        f"in {path}: {type(exc).__name__}: {exc}. Without it a later model swap "
        "would not be detected; check that the palace directory is writable and retry."
    )


def _write_atomically(path: str, data: dict) -> None:
    """Replace ``path`` with ``data`` as JSON, or leave it untouched.

    The JSON goes to a temp file in the same directory (so ``os.replace``
    stays on one filesystem), is fsynced, then renamed over the sidecar. An
    interrupted or failed write leaves the previous sidecar intact instead
    of a truncated file, which would read back as "no identity recorded".
    A failure re-raises after the temp file is removed.
    """
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(path) or ".", prefix=".mempalace_embedder.", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # not every filesystem supports fsync
        try:
            os.chmod(tmp_path, 0o600)
        except (OSError, NotImplementedError):
            pass
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
