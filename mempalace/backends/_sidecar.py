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

    ``None`` means nothing is recorded for the collection: there is no
    sidecar, or the sidecar has no entry for it. A sidecar that exists but
    cannot be read (truncated or invalid JSON, not an object, unreadable
    bytes) or whose entry for the collection is malformed raises
    :class:`~mempalace.backends.base.EmbedderIdentityUnreadableError`: a
    record was written and MemPalace cannot tell what it says, which is not
    the same as "never recorded", so writes must not treat it as a fresh
    collection.
    """
    from .base import EmbedderIdentity

    if not path or not collection_name or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None  # removed between the isfile() check and the open
    except (OSError, ValueError) as exc:  # JSONDecodeError, UnicodeDecodeError
        raise _unreadable_error(path, collection_name, f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict):
        raise _unreadable_error(
            path, collection_name, f"expected a JSON object, found {type(data).__name__}"
        )
    if collection_name not in data:
        return None
    entry = data[collection_name]
    model_name = entry.get("model_name") if isinstance(entry, dict) else None
    if not isinstance(model_name, str) or not model_name.strip():
        raise _unreadable_error(path, collection_name, "its entry has no model_name")
    try:
        dimension = int(entry.get("dimension") or 0)
    except (TypeError, ValueError) as exc:
        raise _unreadable_error(
            path, collection_name, f"its dimension is not a number: {entry.get('dimension')!r}"
        ) from exc
    return EmbedderIdentity(model_name=model_name, dimension=dimension)


def _unreadable_error(path: str, collection_name: str, detail: str):
    from .base import EmbedderIdentityUnreadableError

    return EmbedderIdentityUnreadableError(
        f"the embedder identity record of collection {collection_name!r} in {path} "
        f"is unreadable ({detail})"
    )


def _keep_unreadable_copy(path: str) -> Optional[str]:
    """Copy an unreadable sidecar aside before it is replaced; return the copy's path.

    Only an explicit record (``palace set-embedder``, a verified rebuild)
    replaces a sidecar that cannot be parsed. The copy keeps whatever it
    still says for someone who wants to look.
    """
    import shutil
    import time

    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = f"{path}.corrupt-{stamp}"
    n = 1
    while os.path.exists(target):
        n += 1
        target = f"{path}.corrupt-{stamp}-{n}"
    try:
        shutil.copy2(path, target)
    except OSError:
        return None
    return target


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
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
        else:
            # Unreadable: the other collections' records are lost either
            # way, so keep a copy of the file before replacing it.
            _keep_unreadable_copy(path)
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
