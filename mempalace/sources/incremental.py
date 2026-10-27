"""Guarded adapter context for generation-owned drawer writes.

This is a protocol boundary for cooperative plugins, not a Python sandbox.
Only core receives the physical collection; adapter reads remain logical.
"""

from __future__ import annotations

import hashlib
import math

from ..source_identity import source_directory_identity
from .base import DrawerRecord
from .context import PalaceContext
from .lifecycle import LifecycleStateError


class IncrementalProtocolError(LifecycleStateError):
    """An adapter violated the incremental item/write protocol."""


class _ReadCollection:
    def __init__(self, collection, fail):
        self.__collection = collection
        self.__fail = fail

    def get(self, **kwargs):
        return self.__collection.get(**kwargs)

    def query(self, **kwargs):
        return self.__collection.query(**kwargs)

    def count(self):
        return self.__collection.count()

    def lexical_search(self, *args, **kwargs):
        return self.__collection.lexical_search(*args, **kwargs)

    def get_recent(self, *args, **kwargs):
        return self.__collection.get_recent(*args, **kwargs)

    def get_all_rows(self, *args, **kwargs):
        return self.__collection.get_all_rows(*args, **kwargs)

    def get_all_metadata(self, *args, **kwargs):
        return self.__collection.get_all_metadata(*args, **kwargs)

    def facet_counts(self, *args, **kwargs):
        return self.__collection.facet_counts(*args, **kwargs)

    def __getattr__(self, name):
        self.__fail(f"direct drawer collection access {name!r} is unavailable; use upsert_drawer")


class _Unavailable:
    def __init__(self, label, fail):
        self.__label, self.__fail = label, fail

    def __getattr__(self, name):
        self.__fail(f"incremental {self.__label} access is unsupported")


class IncrementalPalaceContext(PalaceContext):
    """Per-invocation context; core alone opens and publishes item generations.

    Invalid attempts poison the invocation even if plugin code catches the
    exception. No half-valid item may subsequently be activated.
    """

    def __init__(self, *, collection, palace_path, adapter, config, control):
        super().__init__(
            drawer_collection=_ReadCollection(collection, self.fail),
            knowledge_graph=_Unavailable("KG", self.fail),
            closet_collection=_Unavailable("closet", self.fail),
            palace_path=palace_path,
            adapter_name=adapter.name,
            adapter_version=adapter.adapter_version,
            config=config,
        )
        self.__collection = collection
        self.__control = control
        self.__adapter_name = adapter.name
        self.__adapter_version = adapter.adapter_version
        self.__error = None
        self.__generation = None
        self.__item_open = False
        self.__can_skip = False
        self.__indexes = set()
        self.written = 0

    @property
    def _skip_requested(self):
        """Documented observation flag; setting it cannot bypass skip checks."""
        return self.__skip_requested

    @_skip_requested.setter
    def _skip_requested(self, value):
        if value is True:
            self.skip_current_item()
        elif value is False:
            # PalaceContext initializes this before the guarded fields exist.
            self.__skip_requested = False
        else:
            self.fail("_skip_requested must be a boolean")

    def fail(self, message):
        """Remember the first protocol error and stop the current operation."""
        if self.__error is None:
            self.__error = IncrementalProtocolError(message)
        raise self.__error

    def check(self):
        """Core checks after every resume, including generator exhaustion."""
        if self.__error is not None:
            raise self.__error

    def _open_item(self, control, generation, *, can_skip=False):
        """Set the core-owned item handle before resuming extraction."""
        if control is not self.__control:
            self.fail("item control belongs to core")
        self.check()
        self.__generation = generation
        self.__item_open = True
        self.__can_skip = can_skip
        self._skip_requested = False
        self.__indexes = set()

    def _close_item(self, control):
        """Disable writes before activation or generator cleanup."""
        if control is not self.__control:
            self.fail("item control belongs to core")
        self.__item_open = False
        self.__can_skip = False

    def skip_current_item(self):
        """Skip only an item whose exact current cursor core has verified."""
        self.check()
        if not self.__item_open or not self.__can_skip or self.__indexes:
            self.fail("skip_current_item requires a verified current item before writes")
        self.__skip_requested = True

    def upsert_drawer(self, record: DrawerRecord):
        """Append one uniquely indexed, core-stamped drawer to staging."""
        self.check()
        generation = self.__generation
        if not self.__item_open or generation is None:
            self.fail("incremental drawers require preceding SourceItemMetadata")
        if self.__can_skip or self._skip_requested or generation.deleted:
            self.fail("drawers emitted for a skipped or deleted item")
        if not isinstance(record, DrawerRecord) or record.source_file != generation.source_file:
            self.fail("drawer source_file must match the current item")
        index = record.chunk_index
        if type(index) is not int or index < 0 or index in self.__indexes:
            self.fail("chunk_index must be a unique nonnegative integer within the item")
        if not isinstance(record.content, str) or not isinstance(record.metadata, dict):
            self.fail("drawer content must be text and metadata must be a flat dict")
        meta = dict(record.metadata)
        if any(k in meta for k in ("source_generation", "source_version", "source_deleted")):
            self.fail("adapter metadata contains reserved lifecycle provenance")
        stamps = {
            **generation.metadata,
            "chunk_index": index,
            "adapter_version": self.__adapter_version,
        }
        if generation.adapter_name != self.__adapter_name:
            self.fail("generation adapter ownership mismatch")
        for key, value in meta.items():
            if key in stamps and (type(value) is not type(stamps[key]) or value != stamps[key]):
                self.fail(f"adapter metadata forges core provenance {key!r}")
            if not isinstance(key, str) or not key or type(value) not in (str, int, float, bool):
                self.fail("drawer metadata must contain flat scalar values")
            if isinstance(value, float) and not math.isfinite(value):
                self.fail("drawer metadata contains a nonfinite value")
        meta.update(stamps)
        # Read per drawer; a plugin-supplied inode never controls sync safety.
        identity = source_directory_identity(generation.source_file)
        meta.pop("source_dir_ino", None)
        if identity:
            meta["source_dir_ino"] = identity
        owner = hashlib.sha256(
            (generation.adapter_name + "\0" + generation.source_file).encode("utf-8")
        ).hexdigest()
        drawer_id = f"{owner}_{generation.generation}_{index}"
        self.__indexes.add(index)
        try:
            self.__collection.add(ids=[drawer_id], documents=[record.content], metadatas=[meta])
        except BaseException:
            # Even a caught backend failure must not permit partial publication.
            self.__error = IncrementalProtocolError("drawer write failed; item cannot be activated")
            raise
        self.written += 1
