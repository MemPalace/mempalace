"""ChromaDB-backed MemPalace storage backend (RFC 001 reference implementation)."""

import datetime as _dt
import logging
import os
import sqlite3
from typing import Any, Optional

import chromadb

from .base import (
    BaseBackend,
    BaseCollection,
    GetResult,
    HealthStatus,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
    _IncludeSpec,
)

logger = logging.getLogger(__name__)


_REQUIRED_OPERATORS = frozenset({"$eq", "$ne", "$in", "$nin", "$and", "$or", "$contains"})
_OPTIONAL_OPERATORS = frozenset({"$gt", "$gte", "$lt", "$lte"})
_SUPPORTED_OPERATORS = _REQUIRED_OPERATORS | _OPTIONAL_OPERATORS


def _validate_where(where: Optional[dict]) -> None:
    """Scan a where-clause for unknown operators and raise ``UnsupportedFilterError``.

    Spec (RFC 001 §1.4): silent dropping of unknown operators is forbidden.
    """
    if not where:
        return
    stack = [where]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for k, v in node.items():
            if k.startswith("$") and k not in _SUPPORTED_OPERATORS:
                raise UnsupportedFilterError(f"operator {k!r} not supported by chroma backend")
            if isinstance(v, dict):
                stack.append(v)
            elif isinstance(v, list):
                stack.extend(x for x in v if isinstance(x, dict))


def quarantine_stale_hnsw(palace_path: str, stale_seconds: float = 3600.0) -> list[str]:
    """Rename HNSW segment dirs whose files are stale vs. chroma.sqlite3.

    When a ChromaDB 1.5.x PersistentClient opens a palace whose on-disk
    HNSW segment is significantly older than ``chroma.sqlite3``, the Rust
    graph-walk can dereference dangling neighbor pointers for entries that
    exist in the metadata segment but not in the HNSW index, and segfault
    in a background thread on the next ``count()`` or ``query(...)`` call.

    This is the same failure mode reported at #823 (semantic search stale
    after ``add_drawer``), observed at neo-cortex-mcp#2 (SIGSEGV on
    ``count()`` with chromadb 1.5.5), and acknowledged as by-design at
    chroma-core/chroma#2594. On one fork palace (135K drawers), the drift
    caused a 65–85% crash rate on fresh-process opens; fresh-process
    crash rate dropped to 0% after the segment dir was renamed out of the
    way and ChromaDB rebuilt lazily.

    Heuristic: if ``chroma.sqlite3`` is more than ``stale_seconds`` newer
    than the segment's ``data_level0.bin``, the segment is considered
    suspect and renamed to ``<uuid>.drift-<timestamp>``. ChromaDB reopens
    cleanly without it and writes fresh index files on next use. The
    original directory is renamed, not deleted, so recovery remains
    possible if the heuristic misfires.

    The default threshold (1h) is deliberately conservative — ChromaDB's
    HNSW flush cadence means legitimate drift is normally on the order of
    seconds to minutes. A segment that is more than an hour out of date is
    almost certainly in a "crashed mid-write" state.

    Args:
        palace_path: path to the palace directory containing ``chroma.sqlite3``
        stale_seconds: minimum mtime gap to treat a segment as stale

    Returns:
        List of paths that were quarantined (empty if nothing drifted).
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return []
    try:
        sqlite_mtime = os.path.getmtime(db_path)
    except OSError:
        return []

    moved: list[str] = []
    try:
        entries = os.listdir(palace_path)
    except OSError:
        return []

    for name in entries:
        if "-" not in name or name.startswith(".") or ".drift-" in name:
            continue
        seg_dir = os.path.join(palace_path, name)
        if not os.path.isdir(seg_dir):
            continue
        hnsw_bin = os.path.join(seg_dir, "data_level0.bin")
        if not os.path.isfile(hnsw_bin):
            continue
        try:
            hnsw_mtime = os.path.getmtime(hnsw_bin)
        except OSError:
            continue
        if sqlite_mtime - hnsw_mtime < stale_seconds:
            continue
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = f"{seg_dir}.drift-{stamp}"
        try:
            os.rename(seg_dir, target)
            moved.append(target)
            logger.warning(
                "Quarantined stale HNSW segment %s "
                "(sqlite %.0fs newer than HNSW); renamed to %s",
                seg_dir,
                sqlite_mtime - hnsw_mtime,
                target,
            )
        except OSError:
            logger.exception("Failed to quarantine stale HNSW segment %s", seg_dir)
    return moved


def _fix_blob_seq_ids(palace_path: str) -> None:
    """Fix ChromaDB 0.6.x -> 1.5.x migration bug: BLOB seq_ids -> INTEGER.

    ChromaDB 0.6.x stored seq_id as big-endian 8-byte BLOBs. ChromaDB 1.5.x
    expects INTEGER. The auto-migration doesn't convert existing rows, causing
    the Rust compactor to crash with "mismatched types; Rust type u64 (as SQL
    type INTEGER) is not compatible with SQL type BLOB".

    Must run BEFORE PersistentClient is created (the compactor fires on init).
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return
    try:
        with sqlite3.connect(db_path) as conn:
            for table in ("embeddings", "max_seq_id"):
                try:
                    rows = conn.execute(
                        f"SELECT rowid, seq_id FROM {table} WHERE typeof(seq_id) = 'blob'"
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue
                if not rows:
                    continue
                updates = [(int.from_bytes(blob, byteorder="big"), rowid) for rowid, blob in rows]
                conn.executemany(f"UPDATE {table} SET seq_id = ? WHERE rowid = ?", updates)
                logger.info("Fixed %d BLOB seq_ids in %s", len(updates), table)
            conn.commit()
    except Exception:
        logger.exception("Could not fix BLOB seq_ids in %s", db_path)


# ---------------------------------------------------------------------------
# Collection adapter
# ---------------------------------------------------------------------------


def _as_list(v: Any) -> list:
    """Coerce possibly-None scalar-or-list into a list (defensive for chroma nulls)."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


class ChromaCollection(BaseCollection):
    """Thin adapter translating ChromaDB dict returns into typed results."""

    def __init__(self, collection):
        self._collection = collection

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        kwargs: dict[str, Any] = {"documents": documents, "ids": ids}
        if metadatas is not None:
            kwargs["metadatas"] = metadatas
        if embeddings is not None:
            kwargs["embeddings"] = embeddings
        self._collection.add(**kwargs)

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        kwargs: dict[str, Any] = {"documents": documents, "ids": ids}
        if metadatas is not None:
            kwargs["metadatas"] = metadatas
        if embeddings is not None:
            kwargs["embeddings"] = embeddings
        self._collection.upsert(**kwargs)

    def update(
        self,
        *,
        ids,
        documents=None,
        metadatas=None,
        embeddings=None,
    ):
        if documents is None and metadatas is None and embeddings is None:
            raise ValueError("update requires at least one of documents, metadatas, embeddings")
        kwargs: dict[str, Any] = {"ids": ids}
        if documents is not None:
            kwargs["documents"] = documents
        if metadatas is not None:
            kwargs["metadatas"] = metadatas
        if embeddings is not None:
            kwargs["embeddings"] = embeddings
        self._collection.update(**kwargs)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def query(
        self,
        *,
        query_texts=None,
        query_embeddings=None,
        n_results=10,
        where=None,
        where_document=None,
        include=None,
    ) -> QueryResult:
        _validate_where(where)
        _validate_where(where_document)

        if (query_texts is None) == (query_embeddings is None):
            raise ValueError("query requires exactly one of query_texts or query_embeddings")
        chosen = query_texts if query_texts is not None else query_embeddings
        if not chosen:
            raise ValueError("query input must be a non-empty list")

        spec = _IncludeSpec.resolve(include, default_distances=True)
        chroma_include: list[str] = []
        if spec.documents:
            chroma_include.append("documents")
        if spec.metadatas:
            chroma_include.append("metadatas")
        if spec.distances:
            chroma_include.append("distances")
        if spec.embeddings:
            chroma_include.append("embeddings")

        kwargs: dict[str, Any] = {
            "n_results": n_results,
            "include": chroma_include,
        }
        if query_texts is not None:
            kwargs["query_texts"] = query_texts
        if query_embeddings is not None:
            kwargs["query_embeddings"] = query_embeddings
        if where is not None:
            kwargs["where"] = where
        if where_document is not None:
            kwargs["where_document"] = where_document

        raw = self._collection.query(**kwargs)

        num_queries = (
            len(query_texts)
            if query_texts is not None
            else (len(query_embeddings) if query_embeddings is not None else 1)
        )

        ids = raw.get("ids") or []
        if not ids:
            return QueryResult.empty(
                num_queries=num_queries,
                embeddings_requested=spec.embeddings,
            )

        documents = raw.get("documents") or [[] for _ in ids]
        metadatas = raw.get("metadatas") or [[] for _ in ids]
        distances = raw.get("distances") or [[] for _ in ids]
        embeddings_raw = raw.get("embeddings") if spec.embeddings else None

        def _none_list_to_empty(outer):
            return [(inner or []) for inner in outer]

        return QueryResult(
            ids=_none_list_to_empty(ids),
            documents=_none_list_to_empty(documents),
            metadatas=_none_list_to_empty(metadatas),
            distances=_none_list_to_empty(distances),
            embeddings=(
                [list(inner) for inner in embeddings_raw]
                if spec.embeddings and embeddings_raw is not None
                else None
            ),
        )

    def get(
        self,
        *,
        ids=None,
        where=None,
        where_document=None,
        limit=None,
        offset=None,
        include=None,
    ) -> GetResult:
        _validate_where(where)
        _validate_where(where_document)

        spec = _IncludeSpec.resolve(include, default_distances=False)
        chroma_include: list[str] = []
        if spec.documents:
            chroma_include.append("documents")
        if spec.metadatas:
            chroma_include.append("metadatas")
        if spec.embeddings:
            chroma_include.append("embeddings")

        kwargs: dict[str, Any] = {"include": chroma_include}
        if ids is not None:
            kwargs["ids"] = ids
        if where is not None:
            kwargs["where"] = where
        if where_document is not None:
            kwargs["where_document"] = where_document
        if limit is not None:
            kwargs["limit"] = limit
        if offset is not None:
            kwargs["offset"] = offset

        raw = self._collection.get(**kwargs)
        out_ids = list(raw.get("ids") or [])
        out_docs = list(raw.get("documents") or []) if spec.documents else []
        out_metas = list(raw.get("metadatas") or []) if spec.metadatas else []
        out_embeds = raw.get("embeddings") if spec.embeddings else None

        # Pad doc/meta lists to match ids so downstream zipping is safe.
        if spec.documents and len(out_docs) < len(out_ids):
            out_docs = out_docs + [""] * (len(out_ids) - len(out_docs))
        if spec.metadatas and len(out_metas) < len(out_ids):
            out_metas = out_metas + [{}] * (len(out_ids) - len(out_metas))

        return GetResult(
            ids=out_ids,
            documents=out_docs,
            metadatas=out_metas,
            embeddings=[list(v) for v in out_embeds] if out_embeds is not None else None,
        )

    def delete(self, *, ids=None, where=None):
        _validate_where(where)
        kwargs: dict[str, Any] = {}
        if ids is not None:
            kwargs["ids"] = ids
        if where is not None:
            kwargs["where"] = where
        self._collection.delete(**kwargs)

    def count(self):
        return self._collection.count()


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class ChromaBackend(BaseBackend):
    """MemPalace's default ChromaDB backend.

    Maintains two caches:

    * ``self._clients`` — ``palace_path -> PersistentClient`` for callers
      using the ``PalaceRef`` / :meth:`get_collection` path.
    * An inode+mtime freshness check absorbed from ``mcp_server._get_client``
      (merged via #757) ensuring a palace rebuild on disk is detected on the
      next :meth:`get_collection` call.
    """

    name = "chroma"
    capabilities = frozenset(
        {
            "supports_embeddings_in",
            "supports_embeddings_passthrough",
            "supports_embeddings_out",
            "supports_metadata_filters",
            "supports_contains_fast",
            "local_mode",
        }
    )

    def __init__(self):
        # palace_path -> PersistentClient
        self._clients: dict[str, Any] = {}
        # palace_path -> (inode, mtime) of chroma.sqlite3 at cache time.
        self._freshness: dict[str, tuple[int, float]] = {}
        self._closed = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _db_stat(palace_path: str) -> tuple[int, float]:
        """Return ``(inode, mtime)`` of ``chroma.sqlite3`` or ``(0, 0.0)`` if absent."""
        db_path = os.path.join(palace_path, "chroma.sqlite3")
        try:
            st = os.stat(db_path)
            return (st.st_ino, st.st_mtime)
        except OSError:
            return (0, 0.0)

    def _client(self, palace_path: str):
        """Return a cached ``PersistentClient``, rebuilding on inode/mtime change.

        Handles the palace-rebuild case (repair/nuke/purge) by invalidating the
        cache when ``chroma.sqlite3`` changes on disk. Mirrors the semantics of
        ``mcp_server._get_client`` (merged via #757):

        * DB file missing while we hold a cached client → drop the cache so we
          do not serve stale data after a rebuild that has not yet re-created
          the DB.
        * Transition 0 → nonzero stat (DB created after cache) counts as a
          change, so the cached client is replaced with one that sees the DB.
        * FAT/exFAT filesystems return inode 0; we never fire inode comparisons
          when either side is 0 (safe fallback) but still honor mtime.
        * Mtime change uses an epsilon (0.01 s) to tolerate FS timestamp
          granularity without thrashing.
        """
        if self._closed:
            from .base import BackendClosedError  # late import avoids cycles at module load

            raise BackendClosedError("ChromaBackend has been closed")

        cached = self._clients.get(palace_path)
        cached_inode, cached_mtime = self._freshness.get(palace_path, (0, 0.0))
        current_inode, current_mtime = self._db_stat(palace_path)

        db_path = os.path.join(palace_path, "chroma.sqlite3")
        # DB was present when cache was built but is now missing → invalidate.
        if cached is not None and not os.path.isfile(db_path):
            self._clients.pop(palace_path, None)
            self._freshness.pop(palace_path, None)
            cached = None
            cached_inode, cached_mtime = 0, 0.0

        inode_changed = current_inode != 0 and cached_inode != 0 and current_inode != cached_inode
        # Transition from no-stat (0.0) to a real stat counts as a change so we
        # pick up a DB that was created after the cache was built.
        mtime_appeared = cached_mtime == 0.0 and current_mtime != 0.0
        mtime_changed = (
            current_mtime != 0.0
            and cached_mtime != 0.0
            and abs(current_mtime - cached_mtime) > 0.01
        )

        if cached is None or inode_changed or mtime_changed or mtime_appeared:
            _fix_blob_seq_ids(palace_path)
            cached = chromadb.PersistentClient(path=palace_path)
            self._clients[palace_path] = cached
            # Re-stat after the client constructor runs: chromadb creates
            # chroma.sqlite3 lazily, so the stat captured before the call
            # may still be (0, 0.0) on first open.
            self._freshness[palace_path] = self._db_stat(palace_path)
        return cached

    # ------------------------------------------------------------------
    # Public static helpers (legacy; prefer :meth:`get_collection`)
    # ------------------------------------------------------------------

    @staticmethod
    def make_client(palace_path: str):
        """Create a fresh ``PersistentClient`` (fixes BLOB seq_ids first).

        Deprecated-ish: exposed for legacy long-lived callers that manage their
        own client cache. New code should obtain a collection through
        :meth:`get_collection` which manages caching internally.
        """
        _fix_blob_seq_ids(palace_path)
        return chromadb.PersistentClient(path=palace_path)

    @staticmethod
    def backend_version() -> str:
        """Return the installed chromadb package version string."""
        return chromadb.__version__

    # ------------------------------------------------------------------
    # BaseBackend surface
    # ------------------------------------------------------------------

    def get_collection(
        self,
        *args,
        **kwargs,
    ) -> ChromaCollection:
        """Obtain a collection for a palace.

        Supports two calling conventions during the RFC 001 transition:

        * New (preferred): ``get_collection(palace=PalaceRef, collection_name=...,
          create=False, options=None)``.
        * Legacy: ``get_collection(palace_path, collection_name, create=False)``
          — still used by callers not yet migrated.
        """
        palace_ref, collection_name, create, options = _normalize_get_collection_args(args, kwargs)

        palace_path = palace_ref.local_path
        if palace_path is None:
            raise PalaceNotFoundError("ChromaBackend requires PalaceRef.local_path")

        if not create and not os.path.isdir(palace_path):
            raise PalaceNotFoundError(palace_path)

        if create:
            os.makedirs(palace_path, exist_ok=True)
            try:
                os.chmod(palace_path, 0o700)
            except (OSError, NotImplementedError):
                pass

        client = self._client(palace_path)
        hnsw_space = "cosine"
        if options and isinstance(options, dict):
            hnsw_space = options.get("hnsw_space", hnsw_space)

        if create:
            collection = client.get_or_create_collection(
                collection_name, metadata={"hnsw:space": hnsw_space}
            )
        else:
            collection = client.get_collection(collection_name)
        return ChromaCollection(collection)

    def close_palace(self, palace) -> None:
        """Drop cached handles for ``palace``. Accepts ``PalaceRef`` or legacy path str."""
        path = palace.local_path if isinstance(palace, PalaceRef) else palace
        if path is None:
            return
        self._clients.pop(path, None)
        self._freshness.pop(path, None)

    def close(self) -> None:
        self._clients.clear()
        self._freshness.clear()
        self._closed = True

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        if self._closed:
            return HealthStatus.unhealthy("backend closed")
        return HealthStatus.healthy()

    @classmethod
    def detect(cls, path: str) -> bool:
        return os.path.isfile(os.path.join(path, "chroma.sqlite3"))

    # ------------------------------------------------------------------
    # Legacy (pre-RFC 001) surface — retained while callers migrate.
    # ------------------------------------------------------------------

    def get_or_create_collection(self, palace_path: str, collection_name: str) -> ChromaCollection:
        """Legacy shim for ``get_collection(..., create=True)`` by path string."""
        return self.get_collection(palace_path, collection_name, create=True)

    def delete_collection(self, palace_path: str, collection_name: str) -> None:
        """Delete ``collection_name`` from the palace at ``palace_path``."""
        self._client(palace_path).delete_collection(collection_name)

    def create_collection(
        self, palace_path: str, collection_name: str, hnsw_space: str = "cosine"
    ) -> ChromaCollection:
        """Create (not get-or-create) ``collection_name`` with the given HNSW space."""
        collection = self._client(palace_path).create_collection(
            collection_name, metadata={"hnsw:space": hnsw_space}
        )
        return ChromaCollection(collection)


def _normalize_get_collection_args(args, kwargs):
    """Unify legacy positional ``(palace_path, collection_name, create)`` calls
    with the new kwargs-only ``(palace=PalaceRef, collection_name=..., create=...)``.

    Returns ``(PalaceRef, collection_name, create, options)``.
    """
    # New-style: palace= kwarg with a PalaceRef (spec path).
    if "palace" in kwargs:
        palace_ref = kwargs.pop("palace")
        if not isinstance(palace_ref, PalaceRef):
            raise TypeError("palace= must be a PalaceRef instance")
        collection_name = kwargs.pop("collection_name")
        create = kwargs.pop("create", False)
        options = kwargs.pop("options", None)
        if kwargs:
            raise TypeError(f"unexpected kwargs: {sorted(kwargs)}")
        if args:
            raise TypeError("positional args not allowed with palace= kwarg")
        return palace_ref, collection_name, create, options

    # Legacy: first positional is a path string.
    if args:
        palace_path = args[0]
        rest = list(args[1:])
        collection_name = kwargs.pop("collection_name", None) or (rest.pop(0) if rest else None)
        if collection_name is None:
            raise TypeError("collection_name is required")
        create = kwargs.pop("create", False)
        if rest:
            create = rest.pop(0)
        if rest:
            raise TypeError(f"unexpected positional args: {rest!r}")
        if kwargs:
            raise TypeError(f"unexpected kwargs: {sorted(kwargs)}")
        return (
            PalaceRef(id=palace_path, local_path=palace_path),
            collection_name,
            bool(create),
            None,
        )

    # Legacy kwargs-only (palace_path=..., collection_name=..., create=...)
    if "palace_path" in kwargs:
        palace_path = kwargs.pop("palace_path")
        collection_name = kwargs.pop("collection_name")
        create = kwargs.pop("create", False)
        if kwargs:
            raise TypeError(f"unexpected kwargs: {sorted(kwargs)}")
        return (
            PalaceRef(id=palace_path, local_path=palace_path),
            collection_name,
            bool(create),
            None,
        )

    raise TypeError("get_collection requires palace= or a positional palace_path")
