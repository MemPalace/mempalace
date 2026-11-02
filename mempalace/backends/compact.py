"""Compressed local palace storage with nullable, quantised exact vectors.

The SQLite lifecycle, writer lease and identity store are shared with
sqlite_exact. This is a separate disk format, never an exact-vector alias.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
import warnings
import weakref
from functools import partial
from pathlib import Path

import numpy as np
import zstandard as zstd

from ..stored_text import (
    FLAG,
    KEYWORD_ONLY,
    decode_record,
    decode_value,
    encode_filter,
    index_text,
    prepare_stored_write,
)
from ..timeutil import utc_now_iso
from ._filters import _compare, matches_where, validate_where
from ._magic import read_header_fields
from .base import (
    BackendClosedError,
    BackendError,
    DimensionMismatchError,
    GetResult,
    LexicalHit,
    LexicalResult,
    PalaceNotFoundError,
    QueryResult,
    _IncludeSpec,
    validate_write_batch,
)
from .sqlite_exact import (
    SQLiteExactBackend,
    SQLiteExactCollection,
    _as_vector_array,
    _bump_write_generation,
    _json_dumps,
    _json_loads,
    _SnapshotChanged,
)

_DB_FILENAME = "compact.sqlite3"
_APPLICATION_ID = 0x4D504343  # MPCC
_SCHEMA_VERSION = 2
_DICTIONARY_SIZE = 32768
_SAMPLE_CAP = 1024 * 1024
_MAX_LOCUS_VALUES = 1000
_LOG = logging.getLogger(__name__)
_FILE_CACHES = weakref.WeakValueDictionary()
_FILE_CACHES_LOCK = threading.RLock()
_REINDEX_ACTIVE = set()
_REINDEX_LOCK = threading.RLock()
_DATA_OBJECTS = {"meta", "collections", "dictionaries", "documents", "vectors", "docs_fts"}
_REQUIRED_OBJECTS = {
    "meta",
    "collections",
    "dictionaries",
    "documents",
    "vectors",
    "docs_fts",
    "immutable_dictionary",
    "delete_vector",
    "dictionary_in_use",
    "compact_vectors_collection",
    "compact_parent_drawer",
    "compact_parent_entry",
    "compact_room",
    "idx_documents_coll_wing_room_hall",
    "idx_documents_coll_source_file",
    "idx_documents_coll_filed_at",
    "idx_documents_coll_wing_filed_at",
}


def _remove_stage(directory):
    try:
        shutil.rmtree(directory)
    except OSError:
        _LOG.warning("Could not remove an abandoned compact reindex stage", exc_info=True)


class _FileCache:
    def __init__(self):
        self.lock = threading.RLock()
        self.collections = {}


def _file_cache(path, file_id):
    stat = os.stat(path)
    key = (os.path.realpath(path), stat.st_dev, stat.st_ino, file_id)
    with _FILE_CACHES_LOCK:
        shared = _FILE_CACHES.get(key)
        if shared is None:
            shared = _FileCache()
            _FILE_CACHES[key] = shared
        return shared


def _cleanup_reindex(palace_path):
    """Caller owns the palace lease; do not remove a live in-process stage."""
    with _REINDEX_LOCK:
        try:
            entries = list(os.scandir(palace_path))
        except OSError:
            _LOG.warning("Could not inspect compact reindex stages", exc_info=True)
            return
        for entry in entries:
            if (
                entry.name.startswith("compact-reindex-")
                and entry.is_dir(follow_symlinks=False)
                and os.path.realpath(entry.path) not in _REINDEX_ACTIVE
            ):
                _remove_stage(entry.path)


@contextlib.contextmanager
def _reindex_directory(palace_path):
    with _REINDEX_LOCK:
        _cleanup_reindex(palace_path)
        directory = tempfile.mkdtemp(prefix="compact-reindex-", dir=palace_path)
        directory = os.path.realpath(directory)
        _REINDEX_ACTIVE.add(directory)
    try:
        yield directory
    finally:
        with _REINDEX_LOCK:
            _REINDEX_ACTIVE.remove(directory)
            _remove_stage(directory)


def _indexed_operand(expected):
    operations = expected if isinstance(expected, dict) else {"$eq": expected}
    return bool(operations) and all(
        (op == "$eq" and not isinstance(value, (list, dict)))
        or (op == "$in" and all(not isinstance(v, (list, dict)) for v in value or []))
        for op, value in operations.items()
    )


def _field(key):
    if key in {"wing", "room", "hall", "parent_drawer_id", "parent_entry_id"}:
        return "d." + key
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"json_extract(d.metadata_json, '$.{key}')"
    literal = key.replace("'", "''")
    return f"(SELECT value FROM json_each(d.metadata_json) WHERE key='{literal}' LIMIT 1)"


def _where_sql(where):
    """Compile filters before selection, including parent-id OR hydration.

    Equality and membership use indexed stored values. Unusual comparisons
    use a SQL scalar function on metadata values only, never documents.
    """
    validate_where(where)
    if not where:
        return "1", []
    clauses, params = [], []
    for key, expected in where.items():
        if key in {"$and", "$or"}:
            parts = [_where_sql(child) for child in expected or []]
            parent_indexes = {
                "parent_drawer_id": "compact_parent_drawer",
                "parent_entry_id": "compact_parent_entry",
            }
            # Pin each parent branch to its own index. A plain OR lets the
            # planner choose a collection-wide covering index when other
            # metadata indexes exist, turning every split-drawer read into O(n).
            if (
                key == "$or"
                and expected
                and all(
                    len(child) == 1
                    and next(iter(child)) in parent_indexes
                    and _indexed_operand(next(iter(child.values())))
                    for child in expected
                )
            ):
                branches = []
                for child, (sql, values) in zip(expected, parts):
                    index = parent_indexes[next(iter(child))]
                    branches.append(
                        f"SELECT p.rowid FROM documents p INDEXED BY {index} WHERE "
                        + sql.replace("d.", "p.")
                    )
                    params.extend(values)
                clauses.append("d.rowid IN (" + " UNION ".join(branches) + ")")
                continue
            joiner = " AND " if key == "$and" else " OR "
            clauses.append(
                "(" + joiner.join(p[0] for p in parts) + ")"
                if parts
                else ("1" if key == "$and" else "0")
            )
            for _, values in parts:
                params.extend(values)
            continue
        expr = _field(key)
        operations = expected if isinstance(expected, dict) else {"$eq": expected}
        for op, operand in operations.items():
            # JSON arrays/objects have a textual SQLite representation, which
            # must not equal a caller's string containing that same JSON.
            textual = (
                [operand]
                if op in {"$eq", "$ne"}
                else (operand or [] if op in {"$in", "$nin"} else [])
            )
            if any(isinstance(v, str) and v.lstrip().startswith(("[", "{")) for v in textual):
                # Keep the equality/index probe, then distinguish a JSON string
                # from an array/object with the same SQLite text representation.
                values = textual
                parts = []
                for item in values:
                    equality, arguments = (
                        _where_sql({key: {"$eq": item}})
                        if not (isinstance(item, str) and item.lstrip().startswith(("[", "{")))
                        else (
                            f"{expr} IS ? AND compact_compare(d.metadata_json, ?, '$eq', ?)",
                            [encode_filter(item), key, json.dumps(item, ensure_ascii=True)],
                        )
                    )
                    parts.append(f"({equality})")
                    params.extend(arguments)
                membership = " OR ".join(parts) or "0"
                clauses.append(f"NOT ({membership})" if op in {"$ne", "$nin"} else membership)
                continue
            value = encode_filter({op: operand})[op]
            if op in {"$eq", "$ne"} and not isinstance(value, (list, dict)):
                clauses.append(f"{expr} IS {'NOT ' if op == '$ne' else ''}?")
                params.append(value)
            elif op in {"$in", "$nin"} and all(
                not isinstance(v, (list, dict)) for v in value or []
            ):
                values = list(value or [])
                if not values:
                    clauses.append("1" if op == "$nin" else "0")
                else:
                    # IS semantics include missing/NULL keys exactly as the core does.
                    membership = f"{expr} IN ({','.join('?' for _ in values)})"
                    if None in values:
                        membership = f"({membership} OR {expr} IS NULL)"
                    if op == "$nin":
                        membership = f"COALESCE({membership}, 0)"
                    clauses.append(f"NOT ({membership})" if op == "$nin" else membership)
                    params.extend(values)
            else:
                clauses.append("compact_compare(d.metadata_json, ?, ?, ?)")
                params.extend([key, op, json.dumps(operand, ensure_ascii=True)])
    return " AND ".join(f"({part})" for part in clauses) or "1", params


def _quantize(vector):
    arr = _as_vector_array(vector)
    if not np.isfinite(arr).all():
        raise ValueError("embedding must contain finite values")
    maximum = float(np.max(np.abs(arr)))
    scale = np.float32(maximum / 127) if maximum else np.float32(1)
    # Tiny float32 vectors can underflow their scale. Preserve a positive scale.
    scale = max(scale, np.nextafter(np.float32(0), np.float32(1)))
    quantized = np.rint(arr / scale).clip(-127, 127).astype(np.int8)
    return quantized.tobytes(), np.asarray(scale, dtype=np.float32).tobytes(), arr.size


def _vector(blob, scale):
    if blob is None:
        return None
    return (
        np.frombuffer(blob, np.int8).astype(np.float32) * np.frombuffer(scale, np.float32)[0]
    ).tolist()


class _Codes:
    """Memoize common stored loci, preserving Python's bool/int equality."""

    def __init__(self):
        self.values = {}
        self.raw = {}

    @staticmethod
    def key(value):
        if isinstance(value, (dict, list)):
            return (type(value).__name__, json.dumps(value, sort_keys=True))
        return value

    def code(self, raw, kind=None):
        key = (raw, kind) if kind is not None else raw
        found = self.raw.get(key)
        if found is None:
            value = json.loads(raw) if kind in {"array", "object"} else raw
            value = self.key(decode_value(value))
            found = self.values.setdefault(value, len(self.values))
            self.raw[key] = found
        return found


class _VectorCache:
    """Capacity-backed int8 rows and a dense int32 rowid-to-position index."""

    def __init__(self, version, dimension, capacity, first_rowid=0, last_rowid=0):
        self.version = version
        self.dimension = dimension
        self.size = 0
        self.books = {"wing": _Codes(), "room": _Codes()}
        self.loci_ready = set()
        self.loci_attempted = set()
        self.offset = first_rowid
        self.rowid_index = np.full(max(16, last_rowid - first_rowid + 17), -1, np.int32)
        self._allocate(max(16, capacity + capacity // 64))

    def _allocate(self, capacity):
        arrays = {
            "rowids": np.empty(capacity, np.int64),
            "matrix": np.empty((capacity, self.dimension), np.int8),
            "norms": np.empty(capacity, np.float32),
            "wing": np.empty(capacity, np.int32),
            "room": np.empty(capacity, np.int32),
        }
        for name, array in arrays.items():
            if hasattr(self, name):
                array[: self.size] = getattr(self, name)[: self.size]
            setattr(self, name, array)

    def positions(self, rowids):
        indexes = np.asarray(rowids, np.int64) - self.offset
        valid = (indexes >= 0) & (indexes < len(self.rowid_index))
        result = np.full(len(indexes), -1, np.int32)
        result[valid] = self.rowid_index[indexes[valid]]
        return result

    def _index_slot(self, rowid):
        slot = rowid - self.offset
        if slot < 0 or slot >= len(self.rowid_index):
            offset = min(rowid, self.offset)
            capacity = max(rowid - offset + 17, len(self.rowid_index) * 2 + self.offset - offset)
            array = np.full(capacity, -1, np.int32)
            start = self.offset - offset
            array[start : start + len(self.rowid_index)] = self.rowid_index
            self.offset, self.rowid_index = offset, array
            slot = rowid - offset
        return slot

    def put(self, rowid, blob, wing=0, room=0):
        if blob is None and not self.offset <= rowid < self.offset + len(self.rowid_index):
            return
        slot = self._index_slot(rowid)
        position = int(self.rowid_index[slot])
        if blob is None:
            if position >= 0:
                self.size -= 1
                self.rowid_index[slot] = -1
                if position != self.size:
                    for name in ("rowids", "matrix", "norms", "wing", "room"):
                        getattr(self, name)[position] = getattr(self, name)[self.size]
                    self.rowid_index[self.rowids[position] - self.offset] = position
            return
        if position < 0:
            if self.size == len(self.rowids):
                self._allocate(self.size + max(16, self.size // 4))
            position = self.size
            self.size += 1
            self.rowid_index[slot] = position
        self.rowids[position] = rowid
        vector = np.frombuffer(blob, np.int8)
        self.matrix[position] = vector
        self.norms[position] = np.sqrt(np.dot(vector.astype(np.int64), vector.astype(np.int64)))
        self.wing[position], self.room[position] = wing, room

    def filter(self, where):
        """Return a vectorized mask, or None when a predicate needs SQL."""
        parts = []
        for key, value in (where or {}).items():
            if key in {"$and", "$or"}:
                children = [self.filter(child) for child in value or []]
                if any(child is None for child in children):
                    return None
                mask = np.full(self.size, key == "$and", dtype=bool)
                combine = np.logical_and if key == "$and" else np.logical_or
                for child in children:
                    combine(mask, child, out=mask)
                parts.append(mask)
            elif key in self.loci_ready and _indexed_operand(value):
                operations = value if isinstance(value, dict) else {"$eq": value}
                book = self.books[key]
                array = getattr(self, key)[: self.size]
                for op, operand in operations.items():
                    values = [operand] if op == "$eq" else operand or []
                    codes = tuple(
                        {book.values[book.key(v)] for v in values if book.key(v) in book.values}
                    )
                    parts.append(array == codes[0] if len(codes) == 1 else np.isin(array, codes))
            else:
                return None
        mask = np.ones(self.size, dtype=bool)
        for part in parts:
            np.logical_and(mask, part, out=mask)
        return mask


class CompactCollection(SQLiteExactCollection):
    """RFC 001 collection; the keyword-only marker is an internal write API."""

    supports_keyword_only = True

    @staticmethod
    def _generation(cur):
        values = dict(
            cur.execute("SELECT key,value FROM meta WHERE key IN ('file_id','write_generation')")
        )
        return values.get("file_id"), int(values.get("write_generation", 0))

    @contextlib.contextmanager
    def _write_lock(self):
        from ..palace import mine_palace_lock

        with self._handle.lock:
            self._ensure_open()
            # A reader may own the shared matrix for an entire query. Wait
            # before acquiring the cross-process writer lease.
            with self._handle.compact_shared.lock:
                with mine_palace_lock(self._handle.palace_path):
                    yield

    @staticmethod
    def _cache_version(cur):
        return (
            cur.connection,
            cur.execute("PRAGMA data_version").fetchone()[0],
            cur.connection.total_changes,
        )

    @contextlib.contextmanager
    def _cursor(self, *, write=False):
        if write:
            self._refresh_read_handle()
        with self._write_lock() if write else self._read_lock():
            handle = self._handle
            self._ensure_open()
            shared = handle.compact_shared
            with shared.lock:
                cur = handle.conn.cursor()
                changes = {}
                handle.__dict__.setdefault("compact_changes", {})[cur] = changes
                committed = False
                try:
                    cur.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    generation = self._generation(cur)  # read inside the pinned snapshot
                    for cid, cache in list(shared.collections.items()):
                        if cache.version != generation:
                            del shared.collections[cid]
                    before = handle.conn.total_changes
                    yield cur
                    patches = {}
                    invalidated = set()
                    mutated = write and handle.conn.total_changes != before
                    if mutated:
                        _bump_write_generation(cur)
                        for cid, rowids in changes.items():
                            cur.execute(
                                "INSERT INTO meta(key,value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)",
                                (f"compact_generation:{cid}",),
                            )
                            if cid not in shared.collections:
                                continue
                            if rowids is None or shared.collections[cid].dimension != (
                                self._collection_dimension(cur, cid) or 0
                            ):
                                invalidated.add(cid)
                            else:
                                patches[cid] = self._cache_rows(
                                    cur, rowids, shared.collections[cid].loci_ready
                                )
                        generation = self._generation(cur)
                    handle.conn.commit()
                    committed = True
                    if mutated:
                        try:
                            for cid in invalidated:
                                shared.collections.pop(cid, None)
                            for cid, cache in shared.collections.items():
                                for row in patches.get(cid, []):
                                    self._patch_cache(cur, cache, row)
                                cache.version = generation
                        except Exception:
                            shared.collections.clear()
                            _LOG.warning(
                                "Discarding compact cache after a committed write", exc_info=True
                            )
                        handle._native_cache.clear()
                except BaseException:
                    if not committed:
                        handle.conn.rollback()
                    raise
                finally:
                    handle.compact_changes.pop(cur, None)
                    cur.close()

    def _changed(self, cur, cid, rowids=None):
        changes = self._handle.compact_changes[cur]
        if rowids is None:
            changes[cid] = None
        elif cid not in changes or changes[cid] is not None:
            changes.setdefault(cid, set()).update(rowids)

    @staticmethod
    def _cache_rows(cur, rowids, loci_ready):
        if not loci_ready:
            return cur.execute(
                "SELECT wanted.value AS rowid,v.embedding FROM json_each(?) wanted "
                "LEFT JOIN vectors v ON v.rowid=wanted.value",
                (json.dumps(list(rowids)),),
            ).fetchall()
        return cur.execute(
            "SELECT wanted.value AS rowid,v.embedding,d.wing,d.room,"
            "CASE WHEN substr(d.wing,1,1) IN ('[','{') THEN json_type(d.metadata_json,'$.wing') END AS wing_type,"
            "CASE WHEN substr(d.room,1,1) IN ('[','{') THEN json_type(d.metadata_json,'$.room') END AS room_type "
            "FROM json_each(?) wanted LEFT JOIN documents d ON d.rowid=wanted.value "
            "LEFT JOIN vectors v ON v.rowid=d.rowid",
            (json.dumps(list(rowids)),),
        ).fetchall()

    @staticmethod
    def _patch_cache(cur, cache, row):
        if row["embedding"] is None:
            cache.put(row["rowid"], None)
        elif cache.loci_ready:
            codes = [
                cache.books[key].code(row[key], row[key + "_type"])
                if key in cache.loci_ready
                else 0
                for key in ("wing", "room")
            ]
            cache.put(row["rowid"], row["embedding"], *codes)
        else:
            cache.put(row["rowid"], row["embedding"])

    def _retry_snapshot(self, operation):
        for _ in range(3):
            try:
                return operation()
            except _SnapshotChanged:
                with self._read_lock() as handle:
                    with handle.compact_shared.lock:
                        handle._vector_cache.clear()
        raise BackendError("Palace changed repeatedly during compact search; retry the query")

    def query(self, **kwargs):
        return self._retry_snapshot(lambda: self._query_once(**kwargs))

    def _dictionary(self, cur, dictionary_id):
        if dictionary_id is None:
            return None
        row = cur.execute("SELECT data FROM dictionaries WHERE id=?", (dictionary_id,)).fetchone()
        if row is None:
            raise BackendError("compact document dictionary is missing")
        return zstd.ZstdCompressionDict(row[0])

    def _decompress(self, cur, blob, dictionary_id, *, cache=None):
        # Handle reads are serialized. Independent SQLite snapshots must supply
        # their own cache: Zstandard contexts cannot serve overlapping reads.
        if cache is None:
            cache = self._handle.__dict__.setdefault("compact_decompressors", {})
        if dictionary_id not in cache:
            cache[dictionary_id] = zstd.ZstdDecompressor(
                dict_data=self._dictionary(cur, dictionary_id)
            )
        return cache[dictionary_id].decompress(blob).decode("utf-8", "surrogatepass")

    def _ensure_collection_dimension(self, cur, collection_id, dims):
        distinct = set(dims)
        stored = self._collection_dimension(cur, collection_id)
        if len(distinct) > 1 or (stored is not None and distinct and distinct != {stored}):
            raise DimensionMismatchError(
                f"compact expects embedding dimension {stored}, got {sorted(distinct)}"
            )
        if stored is None and distinct:
            cur.execute(
                "UPDATE collections SET dimension=? WHERE id=?", (distinct.pop(), collection_id)
            )

    def _record_pending_identity(self, cur):
        identity = getattr(self, "_pending_embedder_identity", None)
        if identity is not None:
            from .base import EmbedderIdentity, check_embedder_identity

            row = cur.execute(
                "SELECT value FROM meta WHERE key=?", (self._embedder_meta_key(),)
            ).fetchone()
            stored = (
                EmbedderIdentity(
                    row[0], self._collection_dimension(cur, self._collection_id(cur)) or 0
                )
                if row
                else None
            )
            check_embedder_identity(stored, identity)
            cur.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
                (self._embedder_meta_key(), identity.model_name),
            )

    @staticmethod
    def _put_vector(cur, rowid, cid, blob, scale, dim):
        if blob is None:
            cur.execute("DELETE FROM vectors WHERE rowid=?", (rowid,))
        else:
            cur.execute(
                "INSERT INTO vectors(rowid,collection_id,embedding,scale,dim) VALUES (?,?,?,?,?) "
                "ON CONFLICT(rowid) DO UPDATE SET embedding=excluded.embedding,scale=excluded.scale,dim=excluded.dim",
                (rowid, cid, blob, scale, dim),
            )

    def _train_dictionary(self, cur, cid):
        if cur.execute("SELECT 1 FROM dictionaries WHERE collection_id=?", (cid,)).fetchone():
            return
        count, total = cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(raw_size),0) FROM documents WHERE collection_id=?",
            (cid,),
        ).fetchone()
        if count < 128 or total < 128 * 1024:
            return
        samples, size = [], 0
        for row in cur.execute(
            "SELECT document FROM documents WHERE collection_id=? ORDER BY rowid", (cid,)
        ):
            raw = zstd.ZstdDecompressor().decompress(row[0])[: min(8192, _SAMPLE_CAP - size)]
            if raw:
                samples.append(raw)
                size += len(raw)
            if size >= _SAMPLE_CAP:
                break
        try:
            dictionary = zstd.train_dictionary(_DICTIONARY_SIZE, samples)
        except zstd.ZstdError:
            return  # Insufficient diversity: bootstrap until training succeeds.
        cur.execute(
            "INSERT INTO dictionaries(collection_id,data) VALUES (?,?)",
            (cid, dictionary.as_bytes()),
        )

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        self._write(documents, ids, metadatas, embeddings, upsert=False)

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        self._write(documents, ids, metadatas, embeddings, upsert=True)

    def _write(self, documents, ids, metadatas, embeddings, *, upsert):
        validate_write_batch(ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
        documents, ids, metadatas = prepare_stored_write(
            documents, ids, metadatas or [{} for _ in ids]
        )
        prepared = []
        for i, (doc_id, doc, meta) in enumerate(zip(ids, documents, metadatas)):
            if meta.get(KEYWORD_ONLY) is True:
                blob, scale, dim = None, None, None
            elif embeddings is None or embeddings[i] is None:
                raise ValueError("compact requires embeddings for vector records")
            else:
                blob, scale, dim = _quantize(embeddings[i])
            prepared.append((doc_id, doc, meta, blob, scale, dim))
        with self._cursor(write=True) as cur:
            cid = self._collection_id(cur)
            self._ensure_collection_dimension(
                cur, cid, [p[5] for p in prepared if p[5] is not None]
            )
            dictionary_row = cur.execute(
                "SELECT id FROM dictionaries WHERE collection_id=?", (cid,)
            ).fetchone()
            dictionary_id = dictionary_row[0] if dictionary_row else None
            compressor = zstd.ZstdCompressor(
                level=3, dict_data=self._dictionary(cur, dictionary_id)
            )
            now = utc_now_iso()
            for doc_id, doc, meta, blob, scale, dim in prepared:
                raw = doc.encode("utf-8", "surrogatepass")
                conflict = (
                    (
                        " ON CONFLICT(collection_id,id) DO UPDATE SET "
                        "document=excluded.document, dictionary_id=excluded.dictionary_id, "
                        "raw_size=excluded.raw_size, metadata_json=excluded.metadata_json, "
                        "updated_at=excluded.updated_at"
                    )
                    if upsert
                    else ""
                )
                rowid = cur.execute(
                    "INSERT INTO documents(collection_id,id,document,dictionary_id,raw_size,"
                    "metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)"
                    + conflict
                    + " RETURNING rowid",
                    (
                        cid,
                        doc_id,
                        compressor.compress(raw),
                        dictionary_id,
                        len(raw),
                        _json_dumps(meta),
                        now,
                        now,
                    ),
                ).fetchone()[0]
                self._put_vector(cur, rowid, cid, blob, scale, dim)
                self._changed(cur, cid, [rowid])
                cur.execute("DELETE FROM docs_fts WHERE rowid=?", (rowid,))
                cur.execute(
                    "INSERT INTO docs_fts(rowid,document) VALUES (?,?)",
                    (rowid, index_text(doc, meta)),
                )
            self._train_dictionary(cur, cid)
            if prepared:
                self._record_pending_identity(cur)

    def update(self, *, ids, documents=None, metadatas=None, embeddings=None):
        if documents is None and metadatas is None and embeddings is None:
            raise ValueError("update requires documents, metadatas or embeddings")
        validate_write_batch(ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
        # One transaction, with no decompression for metadata/vector-only edits.
        with self._cursor(write=True) as cur:
            cid = self._collection_id(cur)
            updates = []
            for i, doc_id in enumerate(ids):
                row = cur.execute(
                    "SELECT d.rowid,d.metadata_json,v.embedding FROM documents d LEFT JOIN vectors v ON v.rowid=d.rowid WHERE d.collection_id=? AND d.id=?",
                    (cid, doc_id),
                ).fetchone()
                if row is None:
                    continue
                _, meta = decode_record(None, _json_loads(row[1]))
                meta.update((metadatas[i] or {}) if metadatas is not None else {})
                if meta.get(KEYWORD_ONLY) is not True and embeddings is None and row[2] is None:
                    raise ValueError("promoting a keyword-only record requires an embedding")
                doc = documents[i] if documents is not None else None
                docs, _, metas = prepare_stored_write(
                    [doc] if doc is not None else None, [doc_id], [meta]
                )
                if documents is None and _json_loads(row[1]).get(FLAG):
                    metas[0][FLAG] = 1
                values = [_json_dumps(metas[0]), utc_now_iso()]
                sets = ["metadata_json=?", "updated_at=?"]
                vector_update = None
                dim = None
                if meta.get(KEYWORD_ONLY) is True or embeddings is not None:
                    vector = embeddings[i] if embeddings is not None else None
                    blob, scale, dim = (
                        (None, None, None) if meta.get(KEYWORD_ONLY) is True else _quantize(vector)
                    )
                    vector_update = (blob, scale, dim)
                if docs is not None:
                    doc = docs[0]
                    raw = doc.encode("utf-8", "surrogatepass")
                    dictionary = cur.execute(
                        "SELECT id FROM dictionaries WHERE collection_id=?", (cid,)
                    ).fetchone()
                    did = dictionary[0] if dictionary else None
                    compressed = zstd.ZstdCompressor(
                        level=3, dict_data=self._dictionary(cur, did)
                    ).compress(raw)
                    sets.extend(["document=?", "dictionary_id=?", "raw_size=?"])
                    values.extend([compressed, did, len(raw)])
                updates.append((row[0], sets, values, doc, dim, vector_update, metas[0]))
            self._ensure_collection_dimension(cur, cid, [u[4] for u in updates if u[4] is not None])
            if updates:
                self._record_pending_identity(cur)
            self._changed(cur, cid, [u[0] for u in updates])
            for rowid, sets, values, doc, _, vector_update, meta in updates:
                if vector_update is not None:
                    self._put_vector(cur, rowid, cid, *vector_update)
                cur.execute(
                    "UPDATE documents SET " + ",".join(sets) + " WHERE rowid=?", (*values, rowid)
                )
                if doc is not None:
                    cur.execute("DELETE FROM docs_fts WHERE rowid=?", (rowid,))
                    cur.execute(
                        "INSERT INTO docs_fts(rowid,document) VALUES (?,?)",
                        (rowid, index_text(doc, meta)),
                    )

    def _select(
        self,
        cur,
        *,
        ids=None,
        where=None,
        where_document=None,
        limit=None,
        offset=None,
        spec=None,
        order="d.rowid",
        rowids_only=False,
    ):
        spec = spec or _IncludeSpec.resolve(None, default_distances=False)
        sql, params = _where_sql(where)
        if ids is not None:
            # json_each avoids SQL parameter limits for large batches.
            sql += " AND d.id IN (SELECT value FROM json_each(?))"
            params.append(json.dumps(list(ids)))
        if where_document:
            validate_where(where_document)
            sql += " AND compact_document_match(d.document,d.dictionary_id,d.metadata_json,?)"
            params.append(json.dumps(where_document, ensure_ascii=True))
        cols = ["d.rowid"] if rowids_only else ["d.rowid", "d.id"]
        if spec.documents:
            cols.extend(["d.document", "d.dictionary_id"])
        if spec.documents or spec.metadatas:
            cols.append("d.metadata_json")
        if spec.embeddings:
            cols.extend(["v.embedding", "v.scale"])
        cid = self._collection_id(cur)
        # Parent UNION masks must drive physical rowid lookups. Otherwise
        # SQLite may prefer a collection-wide covering index and sort it.
        hint = " NOT INDEXED" if "d.rowid IN (" in sql else ""
        vector_join = " LEFT JOIN vectors v ON v.rowid=d.rowid" if spec.embeddings else ""
        sql = f"SELECT {','.join(cols)} FROM documents d{hint}{vector_join} WHERE d.collection_id=? AND ({sql})"
        if not rowids_only:
            sql += f" ORDER BY {order}"
        params = [cid, *params]
        if limit is not None or offset:
            sql += " LIMIT ? OFFSET ?"
            params.extend([int(limit) if limit is not None else -1, int(offset or 0)])
        return cur.execute(sql, params).fetchall()

    def _result(self, cur, rows, spec):
        docs, metas, vectors = [], [], []
        for row in rows:
            doc = (
                self._decompress(cur, row["document"], row["dictionary_id"])
                if spec.documents
                else None
            )
            if spec.documents or spec.metadatas:
                doc, meta = decode_record(doc, _json_loads(row["metadata_json"]))
                if spec.documents:
                    docs.append(doc)
                if spec.metadatas:
                    metas.append(meta)
            if spec.embeddings:
                vectors.append(_vector(row["embedding"], row["scale"]))
        return GetResult(
            ids=[r["id"] for r in rows],
            documents=docs,
            metadatas=metas,
            embeddings=vectors if spec.embeddings else None,
        )

    def get(
        self, *, ids=None, where=None, where_document=None, limit=None, offset=None, include=None
    ):
        if (limit is not None and limit < 0) or (offset is not None and offset < 0):
            raise ValueError("limit and offset must be nonnegative")
        spec = _IncludeSpec.resolve(include, default_distances=False)
        with self._cursor() as cur:
            rows = self._select(
                cur,
                ids=ids,
                where=where,
                where_document=where_document,
                limit=None if ids is not None else limit,
                offset=None if ids is not None else offset,
                spec=spec,
            )
            if ids is not None:
                by_id = {r["id"]: r for r in rows}
                rows = [by_id[i] for i in ids if i in by_id]
                rows = rows[offset or 0 :]
                if limit is not None:
                    rows = rows[:limit]
            return self._result(cur, rows, spec)

    def _index_version(self, cur):
        if self._handle.immutable:
            db_path = self._backend._db_path(self._handle.palace_path)
            try:
                wal_has_frames = os.path.getsize(db_path + "-wal") > 0
            except FileNotFoundError:
                wal_has_frames = False
            if (
                self._backend._database_signature(db_path) != self._handle.immutable_signature
                or wal_has_frames
            ):
                raise _SnapshotChanged()
        return self._cache_version(cur)

    def _vectors(self, cur, cid, version):
        generation = self._generation(cur)
        cached = self._handle._vector_cache.get(cid)
        if cached is not None and cached.version == generation:
            return cached
        count, first, last = cur.execute(
            "SELECT COUNT(*),MIN(rowid),MAX(rowid) FROM vectors WHERE collection_id=?", (cid,)
        ).fetchone()
        cache = _VectorCache(
            generation, self._collection_dimension(cur, cid) or 0, count, first or 0, last or 0
        )
        loader = self._handle.conn.cursor()
        loader.row_factory = None
        try:
            loader.execute(
                "SELECT rowid,embedding FROM vectors WHERE collection_id=? ORDER BY rowid",
                (cid,),
            )
            while rows := loader.fetchmany(4096):
                start, end = cache.size, cache.size + len(rows)
                ids = np.fromiter((row[0] for row in rows), np.int64, count=len(rows))
                cache.rowids[start:end] = ids
                cache.rowid_index[ids - cache.offset] = np.arange(start, end, dtype=np.int32)
                cache.matrix[start:end] = np.frombuffer(
                    b"".join(row[1] for row in rows), np.int8
                ).reshape(len(rows), cache.dimension)
                cache.norms[start:end] = np.sqrt(
                    np.einsum(
                        "ij,ij->i", cache.matrix[start:end], cache.matrix[start:end], dtype=np.int64
                    )
                )
                cache.size = end
        finally:
            loader.close()
        self._handle._vector_cache[cid] = cache
        return cache

    @staticmethod
    def _supports_loci(where):
        return all(
            all(CompactCollection._supports_loci(child) for child in value or [])
            if key in {"$and", "$or"}
            else key in {"wing", "room"} and _indexed_operand(value)
            for key, value in where.items()
        )

    def _load_loci(self, cur, cid, cache):
        """Enumerate distinct indexed values, then collect their indexed rowids.

        Reading virtual columns across the entire collection evaluates JSON on
        some SQLite builds. Seek to each distinct value instead; the rowid
        aggregation needs only index entries. High-cardinality loci retain SQL
        masks until the next vector reload rather than repeating this probe.
        """
        fields = (("wing", "idx_documents_coll_wing_room_hall"), ("room", "compact_room"))
        for key, index in fields:
            if key in cache.loci_attempted:
                continue
            prefix = f"FROM documents INDEXED BY {index} WHERE collection_id=? AND {key}"
            values = []
            if cur.execute(f"SELECT 1 {prefix} IS NULL LIMIT 1", (cid,)).fetchone():
                values.append(None)
            row = cur.execute(
                f"SELECT {key} {prefix} IS NOT NULL ORDER BY {key} LIMIT 1", (cid,)
            ).fetchone()
            while row is not None:
                values.append(row[0])
                if len(values) > _MAX_LOCUS_VALUES:
                    break
                row = cur.execute(
                    f"SELECT {key} {prefix}>? ORDER BY {key} LIMIT 1", (cid, row[0])
                ).fetchone()
            cache.loci_attempted.add(key)
            if len(values) > _MAX_LOCUS_VALUES:
                continue
            book, array = cache.books[key], getattr(cache, key)
            for raw in values:
                text = cur.execute(
                    f"SELECT group_concat(rowid) FROM documents INDEXED BY {index} WHERE collection_id=? AND {key} IS ?",
                    (cid, raw),
                ).fetchone()[0]
                ids = (
                    np.fromstring(text, sep=",", dtype=np.int64) if text else np.empty(0, np.int64)
                )
                positions = cache.positions(ids)
                valid = positions >= 0
                if isinstance(raw, str) and raw.startswith(("[", "{")):
                    # SQLite stores arrays/objects as text in the same index
                    # range as JSON-looking strings. Split only these rows.
                    ids, positions = ids[valid], positions[valid]
                    for start in range(0, len(ids), 4096):
                        kinds = dict(
                            cur.execute(
                                f"SELECT rowid,json_type(metadata_json,'$.{key}') FROM documents WHERE rowid IN (SELECT value FROM json_each(?))",
                                (json.dumps(ids[start : start + 4096].tolist()),),
                            )
                        )
                        array[positions[start : start + 4096]] = [
                            book.code(raw, kinds[int(rowid)]) for rowid in ids[start : start + 4096]
                        ]
                else:
                    array[positions[valid]] = book.code(raw)
            cache.loci_ready.add(key)

    def _query_once(
        self,
        *,
        query_texts=None,
        query_embeddings=None,
        n_results=10,
        where=None,
        where_document=None,
        include=None,
    ):
        if query_embeddings is None:
            raise ValueError("compact requires query_embeddings; use the core embedding wrapper")
        validate_where(where)
        validate_where(where_document)
        spec = _IncludeSpec.resolve(include, default_distances=True)
        out = QueryResult(
            ids=[],
            documents=[],
            metadatas=[],
            distances=[],
            embeddings=[] if spec.embeddings else None,
        )
        with self._cursor() as cur:
            cid = self._collection_id(cur)
            version = self._index_version(cur)
            cache = self._vectors(cur, cid, version)
            rowids = cache.rowids[: cache.size]
            matrix = cache.matrix[: cache.size]
            norms = cache.norms[: cache.size]
            candidates = None
            cached_filter = cache.filter(where) if where and not where_document else None
            if cached_filter is not None:
                candidates = np.flatnonzero(cached_filter)
            elif where or where_document:
                found = self._select(
                    cur,
                    where=where,
                    where_document=where_document,
                    spec=_IncludeSpec(False, False, False, False),
                    rowids_only=True,
                )
                selected = np.fromiter((row["rowid"] for row in found), np.int64, count=len(found))
                candidates = cache.positions(selected)
                candidates = candidates[candidates >= 0]
                candidates.sort()
                if (
                    cache.loci_attempted != cache.books.keys()
                    and where
                    and not where_document
                    and self._supports_loci(where)
                ):
                    self._load_loci(cur, cid, cache)
            count = len(rowids) if candidates is None else len(candidates)
            for vector in query_embeddings:
                q = _as_vector_array(vector)
                dim = self._collection_dimension(cur, cid)
                if dim is not None and q.size != dim:
                    raise DimensionMismatchError(
                        f"compact expects embedding dimension {dim}, got {q.size}"
                    )
                if not np.isfinite(q).all():
                    raise ValueError("query embedding must contain finite values")
                # Normalise in float64 before casting: extreme finite float32
                # queries must not overflow/underflow their cosine denominator.
                q64 = q.astype(np.float64)
                qnorm = np.linalg.norm(q64)
                q = (q64 / qnorm if qnorm else q64).astype(np.float32)
                distances = np.ones(count, dtype=np.float32)
                for start in range(0, count, 8192):
                    part = (
                        slice(start, min(start + 8192, count))
                        if candidates is None
                        else candidates[start : start + 8192]
                    )
                    dots = matrix[part].astype(np.float32) @ q
                    cos = np.zeros(len(dots), dtype=np.float32)
                    np.divide(dots, norms[part], out=cos, where=norms[part] > 0)
                    distances[start : start + len(dots)] -= np.clip(cos, -1, 1)
                k = min(max(0, int(n_results)), count)
                selected_rowids = rowids if candidates is None else rowids[candidates]
                # Partition first, then order only the cut and its boundary ties.
                cut = np.partition(distances, k - 1)[k - 1] if k else -np.inf
                eligible = np.flatnonzero(distances <= cut)
                ranked = eligible[np.lexsort((selected_rowids[eligible], distances[eligible]))[:k]]
                top = selected_rowids[ranked].tolist()
                result = self._hydrate_rowids(cur, top, spec)
                out.ids.append(result.ids)
                out.documents.append(result.documents)
                out.metadatas.append(result.metadatas)
                out.distances.append(
                    distances[ranked].astype(float).tolist() if spec.distances else []
                )
                if spec.embeddings:
                    out.embeddings.append(result.embeddings)
            if version != self._index_version(cur):
                raise _SnapshotChanged()
        return out

    def _hydrate_rowids(self, cur, rowids, spec, *, keyed=False):
        cols = ["d.rowid", "d.id"]
        if spec.documents:
            cols.extend(["d.document", "d.dictionary_id"])
        if spec.documents or spec.metadatas:
            cols.append("d.metadata_json")
        if spec.embeddings:
            cols.extend(["v.embedding", "v.scale"])
        vector_join = " LEFT JOIN vectors v ON v.rowid=d.rowid" if spec.embeddings else ""
        rows = cur.execute(
            f"SELECT {','.join(cols)} FROM documents d{vector_join} WHERE d.rowid IN (SELECT value FROM json_each(?))",
            (json.dumps(rowids),),
        ).fetchall()
        by_id = {r["rowid"]: r for r in rows}
        if keyed:
            return {key: self._result(cur, [row], spec) for key, row in by_id.items()}
        return self._result(cur, [by_id[r] for r in rowids if r in by_id], spec)

    def lexical_scope(self, where=None):
        from .scoped_lexical import sqlite_collection_scope

        return sqlite_collection_scope(self, where, compact=True)

    def lexical_search(self, **kwargs):
        return self._retry_snapshot(lambda: self._lexical_once(**kwargs))

    def _lexical_once(self, *, query, n_results=10, where=None):
        from ..lexical import tokenize

        sql, params = _where_sql(where)
        tokens = tokenize(query)
        if not tokens or n_results <= 0:
            return LexicalResult(hits=[])
        match = " OR ".join('"' + word.replace('"', '""') + '"' for word in dict.fromkeys(tokens))
        with self._cursor() as cur:
            cid = self._collection_id(cur)
            # Contentless UNINDEXED columns are NULL: all identity and filter
            # data comes from documents, joined on its physical rowid.
            if where:
                # Keep one FTS cursor for the whole search. A metadata-first
                # join restarts FTS/BM25 for each row and scales quadratically.
                # The materialized mask gets an automatic covering rowid index;
                # EXISTS probes it without reading/decompressing document rows.
                mask_hint = " NOT INDEXED" if "d.rowid IN (" in sql else ""
                query_sql = (
                    "WITH allowed AS MATERIALIZED ("
                    f"SELECT d.rowid FROM documents d{mask_hint} WHERE d.collection_id=? AND ({sql})) "
                    "SELECT docs_fts.rowid, bm25(docs_fts) AS score FROM docs_fts "
                    "WHERE docs_fts MATCH ? AND EXISTS ("
                    "SELECT 1 FROM allowed WHERE allowed.rowid=docs_fts.rowid) "
                    "ORDER BY score,docs_fts.rowid LIMIT ?"
                )
                query_params = (cid, *params, match, int(n_results))
            else:
                query_sql = (
                    "SELECT d.rowid, bm25(docs_fts) AS score FROM docs_fts "
                    "JOIN documents d ON d.rowid=docs_fts.rowid "
                    "WHERE docs_fts MATCH ? AND d.collection_id=? "
                    "ORDER BY score,d.rowid LIMIT ?"
                )
                query_params = (match, cid, int(n_results))
            version = self._index_version(cur)
            rows = cur.execute(query_sql, query_params).fetchall()
            result = self._hydrate_rowids(
                cur, [r[0] for r in rows], _IncludeSpec(True, True, False, False), keyed=True
            )
            if version != self._index_version(cur):
                raise _SnapshotChanged()
            return LexicalResult(
                hits=[
                    LexicalHit(
                        id=result[row[0]].ids[0],
                        document=result[row[0]].documents[0],
                        metadata=result[row[0]].metadatas[0],
                        score=max(0.0, -float(row[1])),
                    )
                    for row in rows
                    if row[0] in result and matches_where(result[row[0]].metadatas[0], where)
                ]
            )

    def delete(self, *, ids=None, where=None):
        with self._cursor(write=True) as cur:
            rows = self._select(
                cur, ids=ids, where=where, spec=_IncludeSpec(False, False, False, False)
            )
            self._changed(cur, self._collection_id(cur), [r["rowid"] for r in rows])
            rowids = json.dumps([r["rowid"] for r in rows])
            cur.execute(
                "DELETE FROM docs_fts WHERE rowid IN (SELECT value FROM json_each(?))", (rowids,)
            )
            cur.execute(
                "DELETE FROM documents WHERE rowid IN (SELECT value FROM json_each(?))", (rowids,)
            )

    def facet_counts(self, field, where=None, limit=1000):
        from ..stored_text import decode_value

        sql, params = _where_sql(where)
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT {_field(field)},COUNT(*) FROM documents d WHERE d.collection_id=? AND ({sql}) GROUP BY 1 ORDER BY 2 DESC,1 LIMIT ?",
                (self._collection_id(cur), *params, limit),
            ).fetchall()
        return {str(decode_value(key)): count for key, count in rows if key is not None}

    def _rows_by_ids(self, cur, collection_id, ids, spec):
        rows = self._select(cur, ids=ids, spec=spec)
        result = self._result(cur, rows, spec)
        return {
            row["id"]: {
                "id": row["id"],
                "document": result.documents[i] if spec.documents else "",
                "metadata": result.metadatas[i] if spec.metadatas else {},
                "embedding": result.embeddings[i] if spec.embeddings else None,
            }
            for i, row in enumerate(rows)
        }

    @staticmethod
    def _get_result(rows, spec):
        return GetResult(
            ids=[row["id"] for row in rows],
            documents=[row["document"] for row in rows] if spec.documents else [],
            metadatas=[row["metadata"] for row in rows] if spec.metadatas else [],
            embeddings=[row["embedding"] for row in rows] if spec.embeddings else None,
        )

    def _collection_snapshot(self, cur):
        cid = self._collection_id(cur)
        row = cur.execute(
            "SELECT value FROM meta WHERE key=?", (f"compact_generation:{cid}",)
        ).fetchone()
        identity = cur.execute(
            "SELECT value FROM meta WHERE key=?", (self._embedder_meta_key(),)
        ).fetchone()
        return (
            cid,
            int(row[0]) if row else 0,
            self._collection_dimension(cur, cid),
            identity[0] if identity else None,
        )

    def reindex(self, embed, batch_size=256):
        """Stage quantized batches, then replace only vectors in one transaction.

        The caller holds the palace writer lease over the snapshot and swap.
        Embedding runs outside the backend mutex/transaction, so request-lock
        handoffs cannot deadlock a second request using this connection.
        """
        from ..palace import mine_palace_lock

        with (
            mine_palace_lock(self._handle.palace_path),
            _reindex_directory(self._handle.palace_path) as directory,
        ):
            with self._cursor() as cur:
                snapshot = self._collection_snapshot(cur)
            stage = sqlite3.connect(os.path.join(directory, "vectors.sqlite3"))
            try:
                stage.execute(
                    "CREATE TABLE staged(rowid INTEGER PRIMARY KEY,embedding BLOB,scale BLOB,dim INTEGER)"
                )
                after = 0
                dimensions = set()
                while True:
                    with self._cursor() as cur:
                        if snapshot != self._collection_snapshot(cur):
                            raise BackendError(
                                "compact reindex aborted: collection changed during staging; retry reindex"
                            )
                        cid = snapshot[0]
                        rows = cur.execute(
                            "SELECT rowid,id,document,dictionary_id,metadata_json FROM documents WHERE collection_id=? AND rowid>? ORDER BY rowid LIMIT ?",
                            (cid, after, batch_size),
                        ).fetchall()
                        if not rows:
                            break
                        records = self._result(cur, rows, _IncludeSpec(True, True, False, False))
                    vectors = embed(records.documents, records.metadatas)
                    if len(vectors) != len(rows):
                        raise ValueError("compact reindex embedding batch length mismatch")
                    prepared = [
                        (None, None, None) if meta.get(KEYWORD_ONLY) is True else _quantize(vector)
                        for meta, vector in zip(records.metadatas, vectors)
                    ]
                    dimensions.update(v[2] for v in prepared if v[2] is not None)
                    if len(dimensions) > 1:
                        raise DimensionMismatchError(
                            "compact reindex cannot mix embedding dimensions"
                        )
                    stage.executemany(
                        "INSERT INTO staged VALUES (?,?,?,?)",
                        [(row["rowid"], *vector) for row, vector in zip(rows, prepared)],
                    )
                    stage.commit()
                    after = rows[-1]["rowid"]
                with self._cursor(write=True) as cur:
                    if snapshot != self._collection_snapshot(cur):
                        raise BackendError(
                            "compact reindex aborted: collection changed during staging; retry reindex"
                        )
                    cid = self._collection_id(cur)
                    self._changed(cur, cid)
                    cur.execute("DELETE FROM vectors WHERE collection_id=?", (cid,))
                    cur.execute("UPDATE collections SET dimension=NULL WHERE id=?", (cid,))
                    self._ensure_collection_dimension(cur, cid, list(dimensions))
                    staged = stage.execute("SELECT rowid,embedding,scale,dim FROM staged")
                    while batch := staged.fetchmany(batch_size):
                        for rowid, blob, scale, dim in batch:
                            self._put_vector(cur, rowid, cid, blob, scale, dim)
                    invalid = cur.execute(
                        "SELECT 1 FROM vectors v LEFT JOIN documents d ON d.rowid=v.rowid "
                        "JOIN collections c ON c.id=v.collection_id "
                        "WHERE v.collection_id=? AND (v.dim IS NOT c.dimension OR d.collection_id IS NOT v.collection_id) LIMIT 1",
                        (cid,),
                    ).fetchone()
                    expected = stage.execute(
                        "SELECT COUNT(*) FROM staged WHERE embedding IS NOT NULL"
                    ).fetchone()[0]
                    actual = cur.execute(
                        "SELECT COUNT(*) FROM vectors WHERE collection_id=?", (cid,)
                    ).fetchone()[0]
                    if invalid or expected != actual:
                        raise BackendError(
                            "compact reindex vector verification failed; original vectors retained"
                        )
            finally:
                stage.close()

    def maintenance_state(self):
        state = super().maintenance_state()
        with self._cursor() as cur:
            state["consistency_token"] = list(self._cache_version(cur)[1:])
        return state


class CompactBackend(SQLiteExactBackend):
    name = "compact"
    capabilities = (
        SQLiteExactBackend.capabilities
        - {"supports_embeddings_passthrough", "supports_migration_export"}
    ) | {
        "supports_range_filters",
    }

    @staticmethod
    def _db_path(palace_path):
        return os.path.join(palace_path, _DB_FILENAME)

    def _connect(self, palace_path, create, *, read_only=False):
        if sqlite3.sqlite_version_info < (3, 43, 0):
            raise BackendError("compact requires SQLite >= 3.43 (contentless_delete FTS5)")
        for _ in range(3):
            handle = super()._connect(palace_path, create, read_only=read_only)
            with handle.lock:
                if handle.retired:
                    continue
                if handle.closed:
                    raise BackendClosedError("CompactCollection has been closed")
                if getattr(handle, "compact_functions_registered", False):
                    return handle
                if handle.conn.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
                    raise BackendError("not a compact palace database (application_id mismatch)")
                self._validate_schema(handle.conn)
                if not read_only:
                    from ..palace import mine_palace_lock

                    with mine_palace_lock(palace_path):
                        _cleanup_reindex(palace_path)
                from ..stored_text import decode_value
                from ._filters import matches_where_document

                handle.conn.create_function(
                    "compact_compare",
                    4,
                    lambda metadata, key, op, operand: _compare(
                        decode_value(json.loads(metadata).get(key)), op, json.loads(operand)
                    ),
                    deterministic=True,
                )

                def document_match(blob, did, metadata, predicate, *, handle=handle):
                    col = CompactCollection(handle, "", backend=self)
                    cursor = handle.conn.cursor()
                    try:
                        doc = col._decompress(cursor, blob, did)
                    finally:
                        cursor.close()
                    doc, _ = decode_record(doc, _json_loads(metadata))
                    return matches_where_document(doc, json.loads(predicate))

                handle.conn.create_function("compact_document_match", 4, document_match)
                # Old read-only files can lack a token until their next writable
                # open. Keep those caches private to this connection lifetime.
                file_id = handle.conn.execute(
                    "SELECT value FROM meta WHERE key='file_id'"
                ).fetchone()
                handle.compact_shared = _file_cache(
                    self._db_path(palace_path), file_id[0] if file_id else uuid.uuid4().hex
                )
                handle._vector_cache = handle.compact_shared.collections
                handle.compact_functions_registered = True
                return handle
        raise BackendError(
            "Palace changed repeatedly while opening a compact handle; retry the open"
        )

    @staticmethod
    def _release_cache(handle):
        with handle.lock:
            handle._vector_cache = {}
            handle.compact_shared = None

    def _retire_read_only_handle(self, palace_path, handle):
        try:
            super()._retire_read_only_handle(palace_path, handle)
        finally:
            self._release_cache(handle)

    def _close_handle(self, handle):
        with handle.lock:
            try:
                super()._close_handle(handle)
            finally:
                self._release_cache(handle)

    @staticmethod
    def _missing_schema(conn):
        objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        missing = _REQUIRED_OBJECTS - objects
        for table, required in {
            "documents": {
                "rowid",
                "collection_id",
                "id",
                "document",
                "dictionary_id",
                "raw_size",
                "metadata_json",
                "wing",
                "room",
                "parent_drawer_id",
                "parent_entry_id",
            },
            "vectors": {"rowid", "collection_id", "embedding", "scale", "dim"},
            "collections": {"id", "name", "dimension", "created_at"},
        }.items():
            if table in objects:
                columns = {row[1] for row in conn.execute(f"PRAGMA table_xinfo({table})")}
                missing.update(f"{table}.{column}" for column in required - columns)
        return missing

    @staticmethod
    def _check_version(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != _SCHEMA_VERSION:
            guidance = (
                "upgrade MemPalace"
                if version > _SCHEMA_VERSION
                else (
                    "read-only searches require an in-place upgrade first; open writable once "
                    "to upgrade without rebuilding, or run mempalace --palace PATH repair "
                    "rebuild-index (re-embeds every drawer)"
                    if version == 1
                    else "recover from a backup"
                )
            )
            raise BackendError(f"unsupported compact format version {version}; {guidance}")

    @classmethod
    def _missing_data_schema(cls, conn):
        return {item for item in cls._missing_schema(conn) if item in _DATA_OBJECTS or "." in item}

    @classmethod
    def _validate_schema(cls, conn):
        cls._check_version(conn)
        missing = cls._missing_schema(conn)
        if missing:
            raise BackendError(
                "incomplete compact schema (missing " + ", ".join(sorted(missing)) + "); "
                "open writable to restore derived indexes, or run mempalace repair rebuild-index "
                "to complete an empty palace; missing data tables in populated palaces require a backup"
            )

    def repair_empty_schema(self, palace_path):
        """Complete missing objects only; never drop tables or overwrite data."""
        from ..palace import mine_palace_lock

        with mine_palace_lock(palace_path):
            db_path = Path(self._db_path(palace_path)).resolve()
            if not db_path.is_file():
                raise PalaceNotFoundError("compact palace database does not exist")
            try:
                conn = sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True)
            except sqlite3.OperationalError as exc:
                raise BackendError("could not open compact palace for repair") from exc
            try:
                if (
                    not self._missing_schema(conn)
                    and conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
                ):
                    return False
                self._init_schema(conn, repair_empty=True)
                return True
            finally:
                conn.close()

    def _init_schema(self, conn, *, repair_empty=False):
        application_id = conn.execute("PRAGMA application_id").fetchone()[0]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if application_id != _APPLICATION_ID and (application_id != 0 or tables):
            raise BackendError("not a compact palace database (application_id mismatch)")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        fresh = not tables
        if tables:
            if version not in (1, _SCHEMA_VERSION):
                self._check_version(conn)
            missing_data = self._missing_data_schema(conn)
            if missing_data:
                if not repair_empty:
                    self._validate_schema(conn)
                if (
                    "documents" in tables
                    and conn.execute("SELECT 1 FROM documents LIMIT 1").fetchone()
                ):
                    raise BackendError(
                        "cannot complete compact schema containing documents; recover from a backup"
                    )
            if not self._missing_schema(conn) and version == _SCHEMA_VERSION:
                if not conn.execute("SELECT 1 FROM meta WHERE key='file_id'").fetchone():
                    conn.execute("INSERT INTO meta VALUES ('file_id',?)", (uuid.uuid4().hex,))
                    conn.commit()
                if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    conn.execute("PRAGMA journal_mode=WAL")
                return
        elif version not in (0, _SCHEMA_VERSION):
            self._check_version(conn)
        # The format stamp and schema reach the main file in the same commit.
        # Switching to WAL afterwards leaves detection correct even on a crash.
        if fresh:
            conn.execute("PRAGMA journal_mode=DELETE")
        try:
            conn.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS collections(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,dimension INTEGER,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dictionaries(id INTEGER PRIMARY KEY AUTOINCREMENT,collection_id INTEGER UNIQUE NOT NULL,data BLOB NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_dictionary BEFORE UPDATE ON dictionaries
                    BEGIN SELECT RAISE(ABORT,'compact dictionaries are immutable'); END;
                CREATE TABLE IF NOT EXISTS documents(
                    rowid INTEGER PRIMARY KEY, collection_id INTEGER NOT NULL,id TEXT NOT NULL,document BLOB NOT NULL,
                    dictionary_id INTEGER,raw_size INTEGER NOT NULL,metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    wing AS (json_extract(metadata_json,'$.wing')) VIRTUAL,
                    room AS (json_extract(metadata_json,'$.room')) VIRTUAL,
                    hall AS (json_extract(metadata_json,'$.hall')) VIRTUAL,
                    parent_drawer_id AS (json_extract(metadata_json,'$.parent_drawer_id')) VIRTUAL,
                    parent_entry_id AS (json_extract(metadata_json,'$.parent_entry_id')) VIRTUAL,
                    UNIQUE(collection_id,id));
                CREATE TABLE IF NOT EXISTS vectors(rowid INTEGER PRIMARY KEY,collection_id INTEGER NOT NULL,
                    embedding BLOB NOT NULL,scale BLOB NOT NULL,dim INTEGER NOT NULL,
                    CHECK(length(embedding)=dim AND length(scale)=4 AND dim>0));
                CREATE INDEX IF NOT EXISTS compact_vectors_collection ON vectors(collection_id);
                CREATE TRIGGER IF NOT EXISTS delete_vector AFTER DELETE ON documents BEGIN
                    DELETE FROM vectors WHERE rowid=old.rowid; END;
                CREATE TRIGGER IF NOT EXISTS dictionary_in_use BEFORE DELETE ON dictionaries
                    WHEN EXISTS(SELECT 1 FROM documents WHERE dictionary_id=old.id)
                    BEGIN SELECT RAISE(ABORT,'compact dictionary is in use'); END;
                CREATE INDEX IF NOT EXISTS compact_parent_drawer ON documents(parent_drawer_id,collection_id);
                CREATE INDEX IF NOT EXISTS compact_parent_entry ON documents(parent_entry_id,collection_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(document,content='',contentless_delete=1);
                INSERT OR IGNORE INTO meta VALUES ('fts5_available','1');
            """)
            self._ensure_locus_columns(conn)
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('file_id',?)", (uuid.uuid4().hex,))
            conn.execute("CREATE INDEX IF NOT EXISTS compact_room ON documents(collection_id,room)")
            conn.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._validate_schema(conn)
            conn.commit()
            if fresh:
                conn.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            conn.rollback()
            raise

    def get_collection(self, *args, **kwargs):
        # sqlite_exact supplies lifecycle/creation; its returned wrapper has
        # done no document or vector I/O and is immediately replaced.
        embedder = kwargs.pop("embedder", None)
        base = super().get_collection(*args, **kwargs)
        collection = CompactCollection(base._handle, base._collection_name, backend=self)
        if embedder is not None:
            from .base import (
                EmbedderIdentity,
                EmbedderIdentityUnknownWarning,
                check_embedder_identity,
            )

            identity = EmbedderIdentity(embedder.model_name, embedder.dimension)
            collection._pending_embedder_identity = identity
            with collection._cursor(write=not collection._handle.read_only) as cur:
                cid = collection._collection_id(cur)
                dim = collection._collection_dimension(cur, cid)
                if dim is not None and dim != identity.dimension:
                    raise DimensionMismatchError(
                        f"compact expects embedding dimension {dim}, got {identity.dimension}"
                    )
                identity_row = cur.execute(
                    "SELECT value FROM meta WHERE key=?", (collection._embedder_meta_key(),)
                ).fetchone()
                stored = EmbedderIdentity(identity_row[0], dim or 0) if identity_row else None
                check_embedder_identity(stored, identity)
                exists = cur.execute(
                    "SELECT 1 FROM documents WHERE collection_id=? LIMIT 1", (cid,)
                ).fetchone()
                if stored is None and exists:
                    warnings.warn(
                        "compact collection has unknown embedder identity",
                        EmbedderIdentityUnknownWarning,
                        stacklevel=2,
                    )
                if not collection._handle.read_only:
                    collection._ensure_collection_dimension(cur, cid, [identity.dimension])
                    if stored is None and not exists:
                        cur.execute(
                            "INSERT INTO meta(key,value) VALUES (?,?)",
                            (collection._embedder_meta_key(), identity.model_name),
                        )
        return collection

    @classmethod
    def detect(cls, path):
        db_path = cls._db_path(path)
        if not os.path.isfile(db_path):
            return False
        try:
            fields = read_header_fields(db_path)
            return fields is not None and fields[4] == _APPLICATION_ID
        except OSError:
            return False

    def delete_collection(self, palace_path, collection_name):
        collection = self.get_collection(palace_path, collection_name)
        with collection._cursor(write=True) as cur:
            cid = collection._collection_id(cur)
            cur.execute(
                "DELETE FROM docs_fts WHERE rowid IN (SELECT rowid FROM documents WHERE collection_id=?)",
                (cid,),
            )
            cur.execute("DELETE FROM documents WHERE collection_id=?", (cid,))
            cur.execute("DELETE FROM dictionaries WHERE collection_id=?", (cid,))
            cur.execute("DELETE FROM collections WHERE id=?", (cid,))
            collection._changed(cur, cid)


# These readers touch only unchanged metadata columns, never compressed text.
from .sqlite_exact import (  # noqa: E402
    sqlite_room_wing_hall_counts as _room_counts,
    sqlite_wing_room_counts as _wing_counts,
    sqlite_wing_source_counts as _source_counts,
)

sqlite_room_wing_hall_counts = partial(_room_counts, db_filename=_DB_FILENAME)
sqlite_wing_room_counts = partial(_wing_counts, db_filename=_DB_FILENAME)
sqlite_wing_source_counts = partial(_source_counts, db_filename=_DB_FILENAME)
