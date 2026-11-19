"""Local incremental adapter runner, serialized over the complete invocation."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import uuid

from .base import BaseSourceAdapter, DrawerRecord, SourceItemMetadata, SourceRef
from .incremental import IncrementalPalaceContext
from .lifecycle import LifecycleConflictError, SourceGeneration, SourceLifecycleStore

logger = logging.getLogger(__name__)
_running = set()
_running_lock = threading.Lock()


def _validate_item(context, item, seen, capabilities):
    if (
        not isinstance(item.source_file, str)
        or not item.source_file
        or not isinstance(item.version, str)
    ):
        context.fail("source items require a nonempty source_file and string version")
    if item.source_file in seen:
        context.fail("duplicate source item in one invocation")
    seen.add(item.source_file)
    deleted = item.version == "__deleted__"
    if deleted and "supports_deletion_tombstones" not in capabilities:
        context.fail("deletion tombstone capability is required")
    return deleted


def _recover_owner(store, collection, adapter_name, source_file):
    with store.snapshot() as snapshot:
        orphan = snapshot.pending(adapter_name, source_file)
        current = snapshot.current(adapter_name, source_file)
    if orphan is not None:
        store.abandon(orphan)
    try:
        store.reclaim(collection, adapter_name, source_file)
    except LifecycleConflictError as exc:
        # Retained readers block another update, but need not block an
        # unchanged skip. Caller must resolve currentness before proceeding.
        return current, exc
    return current, None


@contextlib.contextmanager
def _invocation(palace_path):
    # The existing writer lease is intentionally process-reentrant for MCP.
    # Two incremental generators in that process must still not interleave.
    from ..palace import MineAlreadyRunning

    key = os.path.normcase(os.path.realpath(os.path.expanduser(palace_path)))
    with _running_lock:
        if key in _running:
            raise MineAlreadyRunning("an incremental source invocation already owns this palace")
        _running.add(key)
    try:
        yield
    finally:
        with _running_lock:
            _running.remove(key)


def validate_incremental_adapter(adapter):
    """Reject unsupported protocols before handles, locks or extraction."""
    from ..cli import UnsupportedSourceAdapterProtocolError

    if type(adapter).is_current is BaseSourceAdapter.is_current:
        raise UnsupportedSourceAdapterProtocolError(
            "incremental ingestion requires an adapter override of is_current"
        )
    if adapter.capabilities.intersection(
        {"supports_kg_triples", "supports_media_assets", "media_references"}
    ):
        raise UnsupportedSourceAdapterProtocolError("incremental KG/media output is unsupported")
    if not isinstance(adapter.name, str) or not adapter.name:
        raise UnsupportedSourceAdapterProtocolError("incremental adapter name must be nonempty")


def run_incremental_adapter(*, adapter, source_path, palace_path, dry_run=False):
    """Stage/extract/activate one item at a time; dry runs never open storage.

    Successful preceding items remain committed if a later item fails. Failed
    staging remains hidden and recoverable. Post-activation cleanup is best
    effort; it never rolls back the new current cursor.
    """
    from ..cli import UnsupportedSourceAdapterProtocolError, _DryRunCollectionProxy
    from ..config import MempalaceConfig
    from ..palace import get_collection, mine_palace_lock, resolve_backend_name

    validate_incremental_adapter(adapter)
    # Resolve the name without instantiating a possibly remote backend.
    backend = resolve_backend_name(palace_path)
    if backend not in {"chroma", "sqlite_exact", "rust_exact"}:
        raise UnsupportedSourceAdapterProtocolError("incremental ingestion needs a local backend")
    lock = contextlib.nullcontext() if dry_run else mine_palace_lock(palace_path)
    invocation = contextlib.nullcontext() if dry_run else _invocation(palace_path)
    with invocation, lock:
        store = None
        if dry_run:
            collection = _DryRunCollectionProxy()
        else:
            # The constructor checks tagged-only restores before initialization.
            # Open the registry before a drawer backend could create or repair it.
            store = SourceLifecycleStore(palace_path)
            collection = get_collection(palace_path)
        control = object()
        context = IncrementalPalaceContext(
            collection=collection,
            control=control,
            palace_path=palace_path,
            adapter=adapter,
            config=MempalaceConfig(palace_path=palace_path),
        )
        # Dry collection records writes for the unchanged nonincremental API.
        # Its add surface is also a no-op, so the same validation runs here.
        pending = None
        seen = set()
        iterator = None

        def finish():
            nonlocal pending
            context.check()
            context._close_item(control)
            if pending is not None:
                if store is not None:
                    store.activate(pending)
                published, pending = pending, None
                if store is not None:
                    try:
                        store.reclaim(collection, published.adapter_name, published.source_file)
                    except Exception:
                        logger.warning(
                            "source item activated; generation cleanup deferred until retry",
                            exc_info=True,
                        )

        try:
            iterator = iter(
                adapter.ingest(source=SourceRef(local_path=source_path), palace=context)
            )
            for result in iterator:
                context.check()
                if isinstance(result, SourceItemMetadata):
                    finish()
                    deleted = _validate_item(context, result, seen, adapter.capabilities)
                    current = None
                    reclamation_conflict = None
                    if store is not None:
                        current, reclamation_conflict = _recover_owner(
                            store, collection, adapter.name, result.source_file
                        )
                    cursor = current.cursor if current else None
                    decision = adapter.is_current(item=result, existing_metadata=cursor)
                    context.check()
                    if type(decision) is not bool:
                        context.fail("is_current must return a boolean")
                    exact = (
                        current is not None
                        and current.version == result.version
                        and current.deleted == deleted
                    )
                    if decision and not exact:
                        context.fail("is_current claimed a mismatched or absent generic cursor")
                    if decision:
                        context._open_item(control, current, can_skip=True)
                        context.skip_current_item()
                    else:
                        if reclamation_conflict is not None:
                            raise reclamation_conflict
                        pending = (
                            store.begin(
                                adapter.name, result.source_file, result.version, deleted=deleted
                            )
                            if store is not None
                            else SourceGeneration(
                                adapter.name,
                                result.source_file,
                                uuid.uuid4().hex,
                                result.version,
                                deleted=deleted,
                            )
                        )
                        context._open_item(control, pending)
                elif isinstance(result, DrawerRecord):
                    context.upsert_drawer(result)
                else:
                    context.fail(f"unsupported incremental result type {type(result).__name__}")
            finish()
            return context.written
        except BaseException:
            context._close_item(control)
            # Disable facade writes before generator finally blocks execute.
            if iterator is not None and hasattr(iterator, "close"):
                try:
                    iterator.close()
                except BaseException:
                    logger.warning("source iterator cleanup failed", exc_info=True)
            if pending is not None and store is not None:
                try:
                    # Activation may have committed before an I/O error reached
                    # us. Never abandon or delete an already active generation.
                    with store.snapshot() as snapshot:
                        staged = snapshot.pending(pending.adapter_name, pending.source_file)
                    if staged == pending:
                        store.abandon(pending)
                        store.reclaim(collection, pending.adapter_name, pending.source_file)
                except Exception:
                    logger.warning("failed staging retained for recovery", exc_info=True)
            raise
        finally:
            context._close_item(control)
