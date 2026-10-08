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
    Preserves other collections' entries; never raises on I/O failure. The
    file is replaced atomically, so a failed write keeps the previous one.
    """
    if not path or not collection_name or not identity or not getattr(identity, "model_name", ""):
        return
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
    _write_atomically(path, data)


def _write_atomically(path: str, data: dict) -> None:
    """Replace ``path`` with ``data`` as JSON, or leave it untouched.

    The JSON goes to a temp file in the same directory (so ``os.replace``
    stays on one filesystem), is fsynced, then renamed over the sidecar. An
    interrupted or failed write leaves the previous sidecar intact instead
    of a truncated file, which would read back as "no identity recorded".
    Never raises on I/O failure; the temp file is removed.
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
    except (OSError, NotImplementedError, TypeError, ValueError):
        pass
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
