"""Operation-scoped drawer visibility for SQL fast paths and collection reads.

Temporary views affect only a reader connection, never storage or physical
backend maintenance. Registry-first/backend-second snapshots are checked before
any rows escape. The lease prevents reclamation until the operation closes.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

from ..stored_text import FLAG, decode_record, encode_filter
from .lifecycle import (
    LifecycleStateError,
    SourceLifecycleSnapshot,
    StaleLifecycleSnapshot,
    visibility_epoch,
)


class _Cursor:
    def __init__(self, owner, cursor):
        self.owner, self.cursor = owner, cursor

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def _call(self, name, *args):
        try:
            return getattr(self.cursor, name)(*args)
        except sqlite3.Error:
            if self.owner.error is not None:
                raise self.owner.error from None
            raise

    def execute(self, sql, parameters=()):
        try:
            self.cursor.execute(sql, parameters)
            return self
        except sqlite3.Error:
            if self.owner.error is not None:
                raise self.owner.error from None
            raise

    def fetchone(self):
        return self._call("fetchone")

    def fetchall(self):
        return self._call("fetchall")

    def fetchmany(self, size=None):
        return self._call("fetchmany", *([] if size is None else [size]))

    def __iter__(self):
        return self

    def __next__(self):
        return self._call("__next__")


class LogicalSqliteReader:
    """A close-owning SQL reader with the same active visibility as collections."""

    def __init__(self, conn, palace_path, backend, snapshot=None):
        self.conn = conn
        self.error = None
        self.snapshot = snapshot or SourceLifecycleSnapshot(palace_path)
        self.owns_snapshot = snapshot is None
        self.closed = False
        try:
            if self.owns_snapshot:
                self.snapshot.__enter__()
            before = self._epoch()
            if not conn.in_transaction:
                conn.execute("BEGIN")
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            # Compare after pinning the backend snapshot, before streaming.
            after = visibility_epoch(palace_path)
            if before != (None if after is None else after[2]) or (
                after is not None and self.snapshot._identity != after[1]
            ):
                raise StaleLifecycleSnapshot("registry changed while opening drawer snapshot")
            conn.create_function("mp_source_visible", 1, self._visible)
            from ..config import get_configured_collection_name

            collection_name = get_configured_collection_name()
            name = collection_name.replace("'", "''")
            if backend == "chroma":
                if not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='embedding_metadata'"
                ).fetchone():
                    return  # Legacy unavailable-schema callers retain their fallback contract.
                if (
                    "key"
                    in {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(embedding_metadata_array)")
                    }
                    and conn.execute(
                        "SELECT 1 FROM embedding_metadata_array a JOIN main.embeddings e ON e.id=a.id "
                        "JOIN segments s ON s.id=e.segment_id JOIN collections c ON c.id=s.collection "
                        "WHERE c.name=? AND a.key IN ('source_generation','source_version') LIMIT 1",
                        (collection_name,),
                    ).fetchone()
                ):
                    raise LifecycleStateError(
                        "generation drawer has invalid array lifecycle metadata"
                    )
                # No registry and no lifecycle keys: preserve the ordinary
                # indexed reader plan exactly. The indexed key probe also
                # catches a vector-only restore before returning any rows.
                if (
                    before is None
                    and not conn.execute(
                        "SELECT 1 FROM main.embedding_metadata WHERE key IN "
                        "('source_generation','source_version') LIMIT 1"
                    ).fetchone()
                ):
                    return
                keys = ["adapter_name", "source_file", "source_generation", "source_version", FLAG]
                quoted = ",".join("'" + k.replace("'", "''") + "'" for k in keys)
                columns = {r[1] for r in conn.execute("PRAGMA table_info(embedding_metadata)")}
                value = (
                    "COALESCE("
                    + ",".join(
                        c
                        for c in ("string_value", "int_value", "float_value", "bool_value")
                        if c in columns
                    )
                    + ")"
                )
                meta = (
                    f"(SELECT json_group_object(key,{value}) FROM main.embedding_metadata "
                    f"WHERE id=e.id AND key IN ({quoted}))"
                )
                conn.execute(
                    "CREATE TEMP VIEW embeddings AS SELECT e.* FROM main.embeddings e WHERE "
                    "NOT EXISTS (SELECT 1 FROM main.segments s JOIN main.collections c "
                    f"ON c.id=s.collection WHERE s.id=e.segment_id AND c.name='{name}') "
                    f"OR mp_source_visible({meta})"
                )
                conn.execute(
                    "CREATE TEMP VIEW embedding_metadata AS SELECT m.* FROM main.embedding_metadata m "
                    "WHERE EXISTS(SELECT 1 FROM temp.embeddings e WHERE e.id=m.id)"
                )
            else:
                conn.execute(
                    "CREATE TEMP VIEW documents AS SELECT d.rowid AS rowid, d.* FROM main.documents d "
                    "WHERE NOT EXISTS(SELECT 1 FROM main.collections c "
                    f"WHERE c.id=d.collection_id AND c.name='{name}') "
                    "OR mp_source_visible(d.metadata_json)"
                )
        except BaseException:
            self.close()
            raise

    def _epoch(self):
        if self.snapshot._conn is None:
            return None
        return self.snapshot._conn.execute(
            "SELECT epoch FROM source_lifecycle_state WHERE singleton=1"
        ).fetchone()[0]

    def _visible(self, metadata):
        try:
            meta = decode_record(None, json.loads(metadata or "{}"))[1]
            return int(self.snapshot.visible(meta))
        except (LifecycleStateError, ValueError, TypeError, AttributeError) as exc:
            self.error = (
                exc
                if isinstance(exc, LifecycleStateError)
                else LifecycleStateError("invalid managed drawer metadata")
            )
            raise self.error from exc

    def execute(self, sql, parameters=()):
        # Existing snapshot callers can BEGIN without nesting a transaction.
        if sql.strip().upper() == "BEGIN" and self.conn.in_transaction:
            return _Cursor(self, self.conn.execute("SELECT 1 WHERE 0"))
        try:
            return _Cursor(self, self.conn.execute(sql, parameters))
        except sqlite3.Error:
            if self.error is not None:
                raise self.error from None
            raise

    def cursor(self):
        return _Cursor(self, self.conn.cursor())

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.conn.close()
            finally:
                if self.owns_snapshot:
                    self.snapshot.close()


def open_logical_reader(db_path, *, backend="chroma", snapshot=None):
    """Open a fresh logical SQL snapshot, retrying the opening race twice."""
    from ..backends._inproc_sqlite import open_reader

    palace_path = str(Path(db_path).parent)
    for attempt in range(3):
        state = snapshot or SourceLifecycleSnapshot(palace_path)
        own = snapshot is None
        try:
            if own:
                state.__enter__()
            conn = (
                open_reader(db_path)
                if backend == "chroma"
                else sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
            )
            reader = LogicalSqliteReader(conn, palace_path, backend, snapshot=state)
            reader.owns_snapshot = own
            return reader
        except BaseException as exc:
            if own:
                state.close()
            if not isinstance(exc, StaleLifecycleSnapshot) or snapshot is not None or attempt == 2:
                raise
    raise AssertionError("unreachable")


@contextlib.contextmanager
def open_ready_chroma_reader(collection):
    """Recover a pending metadata queue once, then reopen both snapshots.

    The bounded SDK read starts normal supported recovery. Its physical result
    is discarded; only the reopened, lifecycle-validated reader may serve data.
    Never hold an old registry/backend snapshot across that recovery.
    """
    from ..backends.chroma_sqlite import MetadataRecoveryPending, _sqlite_ready_metadata_segment

    for attempt in range(2):
        with open_logical_reader(Path(collection._palace_path) / "chroma.sqlite3") as reader:
            if _sqlite_ready_metadata_segment(reader, collection._collection.name) is not None:
                yield reader
                return
        if attempt == 0:
            collection._collection.get(limit=1, include=[])
    raise MetadataRecoveryPending("metadata recovery still pending after SDK recovery")


def chroma_rows(
    palace_path,
    collection_name,
    *,
    ids=None,
    where=None,
    where_document=None,
    _reader=None,
    documents=True,
    batch_size=256,
):
    """Stream decoded records through one native row-id cursor and snapshot."""
    import contextlib

    from ..backends._filters import matches_where_document, validate_where
    from ..backends.chroma_sqlite import (
        _DRAWERS_SQL,
        MetadataRecoveryPending,
        _decoded_rows,
        _marks,
        _match_sql,
        _sqlite_ready_metadata_segment,
        _string_equalities,
    )

    validate_where(where)
    validate_where(where_document)
    context = (
        contextlib.nullcontext(_reader)
        if _reader is not None
        else open_logical_reader(Path(palace_path) / "chroma.sqlite3")
    )
    with context as conn:
        if _sqlite_ready_metadata_segment(conn, collection_name) is None:
            raise MetadataRecoveryPending("metadata recovery pending; retry after recovery")
        sql = f"SELECT e.id {_DRAWERS_SQL} WHERE c.name=?"
        params = [collection_name]
        if ids is not None:
            # Bounded parameter groups, all sharing this operation snapshot.
            groups = [ids[i : i + 500] for i in range(0, len(ids), 500)]
        else:
            groups = [None]
        pairs = _string_equalities(where)
        if pairs is not None:
            extra, values = _match_sql(
                [(k, tuple(encode_filter(v) for v in vs)) for k, vs in pairs], "e.id"
            )
            sql += extra
            params.extend(values)
        for group in groups:
            query, values = sql, list(params)
            if group is not None:
                query += f" AND e.embedding_id IN ({_marks(group)})"
                values.extend(group)
            cursor = conn.execute(query + " ORDER BY e.id", values)
            while True:
                batch = cursor.fetchmany(batch_size)
                if not batch:
                    break
                records = _decoded_rows(
                    conn, [r[0] for r in batch], documents=documents or bool(where_document)
                )
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='embedding_metadata_array'"
                ).fetchone():
                    from ..backends._filters import matches_where
                    from ..backends.chroma_sqlite import _cell_value
                    from ..stored_text import decode_value

                    row_ids = [r[0] for r in batch]
                    arrays = {}
                    for rid, key, sval, ival, fval, bval in conn.execute(
                        f"SELECT e.embedding_id, a.key, a.string_value, a.int_value, a.float_value, a.bool_value "
                        f"FROM embedding_metadata_array a JOIN embeddings e ON e.id=a.id "
                        f"WHERE a.id IN ({_marks(row_ids)}) ORDER BY a.rowid",
                        row_ids,
                    ):
                        arrays.setdefault(rid, {}).setdefault(key, []).append(
                            decode_value(_cell_value(sval, ival, fval, bval))
                        )
                    records = [
                        (rid, doc, {**(meta or {}), **arrays[rid]} if rid in arrays else meta)
                        for rid, doc, meta in records
                    ]
                from ..backends._filters import matches_where

                for row in records:
                    if matches_where(row[2] or {}, where) and matches_where_document(
                        row[1] or "", where_document
                    ):
                        yield row


def chroma_get(
    collection, *, ids=None, where=None, where_document=None, limit=None, offset=None, include=None
):
    """Projection-safe logical pagination; hidden rows never consume offset."""
    from ..backends.base import GetResult, _IncludeSpec
    from ..sources.lifecycle import LifecycleStateError

    spec = _IncludeSpec.resolve(include, default_distances=False)
    if (offset is not None and offset < 0) or (limit is not None and limit < 0):
        raise ValueError("logical drawer bounds must be nonnegative")
    with open_ready_chroma_reader(collection) as reader:
        rows = chroma_rows(
            collection._palace_path,
            collection._collection.name,
            ids=ids,
            where=where,
            where_document=where_document,
            _reader=reader,
            documents=spec.documents,
            batch_size=min(256, max(1, (offset or 0) + limit)) if limit is not None else 256,
        )
        selected = []
        try:
            if limit != 0:
                for index, row in enumerate(rows):
                    if index < (offset or 0):
                        continue
                    selected.append(row)
                    if limit is not None and len(selected) >= limit:
                        break
        finally:
            rows.close()
        embeddings = None
        if spec.embeddings:
            embeddings = []
            if selected:
                raw = collection._get_physical(ids=[r[0] for r in selected], include=["embeddings"])
                mapping = dict(zip(raw.ids, raw.embeddings))
                if any(r[0] not in mapping for r in selected):
                    raise LifecycleStateError("drawer disappeared during embedding hydration")
                embeddings = [mapping[r[0]] for r in selected]
        return GetResult(
            ids=[r[0] for r in selected],
            documents=[r[1] for r in selected] if spec.documents else [],
            metadatas=[r[2] for r in selected] if spec.metadatas else [],
            embeddings=embeddings,
        )


def chroma_query(collection, **kwargs):
    """Bounded ANN candidate refill, registry-first and pinned metadata hydration."""
    import warnings

    from ..backends.base import QueryResult

    include = kwargs.get("include")
    wanted = set(["documents", "metadatas", "distances"] if include is None else include)
    n = max(0, kwargs.get("n_results", 10))
    cap = max(n, min(4096, n * 16))
    for attempt in range(3):
        try:
            with open_logical_reader(Path(collection._palace_path) / "chroma.sqlite3") as reader:
                size = min(cap, max(n, 32))
                while True:
                    result = collection._query_physical(
                        **{**kwargs, "n_results": size, "include": list(wanted | {"metadatas"})}
                    )
                    candidates = list(dict.fromkeys(rid for group in result.ids for rid in group))
                    by_id = {
                        r[0]: r
                        for r in chroma_rows(
                            collection._palace_path,
                            collection._collection.name,
                            ids=candidates,
                            _reader=reader,
                            documents="documents" in wanted,
                        )
                    }
                    # Check every candidate, including candidates absent from
                    # the old backend snapshot: an unknown generation retries.
                    for metas in result.metadatas:
                        for meta in metas:
                            reader.snapshot.visible(meta)
                    keep = [[i for i, rid in enumerate(ids) if rid in by_id] for ids in result.ids]
                    if all(len(k) >= n for k in keep) or all(len(r) < size for r in result.ids):
                        break
                    if size >= cap:
                        warnings.warn(
                            "source visibility ANN candidate budget exhausted; results truncated",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                        break
                    size = min(cap, size * 2)
                keep = [k[:n] for k in keep]

                def pick(field, result=result, keep=keep, by_id=by_id):
                    if field in ("documents", "metadatas"):
                        at = 1 if field == "documents" else 2
                        return [
                            [by_id[result.ids[q][i]][at] for i in indices]
                            for q, indices in enumerate(keep)
                        ]
                    values = getattr(result, field)
                    return [[values[q][i] for i in indices] for q, indices in enumerate(keep)]

                return QueryResult(
                    ids=pick("ids"),
                    documents=pick("documents") if "documents" in wanted else [[] for _ in keep],
                    metadatas=pick("metadatas") if "metadatas" in wanted else [[] for _ in keep],
                    distances=pick("distances") if "distances" in wanted else [[] for _ in keep],
                    embeddings=pick("embeddings") if "embeddings" in wanted else None,
                )
        except StaleLifecycleSnapshot:
            if attempt == 2:
                raise


def iter_drawer_batches(collection, palace_path, *, batch_size=1000):
    """Single leased native cursor for local exports; legacy backend fallback."""
    from ..backends.base import GetResult
    from ..backends.chroma import ChromaCollection
    from ..backends.embedding_wrapper import EmbeddingCollection
    from ..backends.sqlite_exact import _DB_FILENAME, SQLiteExactCollection, _json_loads

    inner = collection._inner if isinstance(collection, EmbeddingCollection) else collection
    if isinstance(inner, ChromaCollection):
        with open_ready_chroma_reader(inner) as reader:
            rows = chroma_rows(palace_path, inner._collection.name, _reader=reader)
            try:
                batch = []
                for row in rows:
                    batch.append(row)
                    if len(batch) == batch_size:
                        yield GetResult(
                            [r[0] for r in batch],
                            [r[1] for r in batch],
                            [r[2] or {} for r in batch],
                        )
                        batch = []
                if batch:
                    yield GetResult(
                        [r[0] for r in batch], [r[1] for r in batch], [r[2] or {} for r in batch]
                    )
            finally:
                rows.close()
    elif isinstance(inner, SQLiteExactCollection):
        with open_logical_reader(Path(palace_path) / _DB_FILENAME, backend="exact") as conn:
            cursor = conn.execute(
                "SELECT d.id, d.document, d.metadata_json FROM documents d "
                "JOIN collections c ON c.id=d.collection_id WHERE c.name=? ORDER BY d.rowid",
                (inner._collection_name,),
            )
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                decoded = [(rid, *decode_record(doc, _json_loads(meta))) for rid, doc, meta in rows]
                yield GetResult(
                    [r[0] for r in decoded], [r[1] for r in decoded], [r[2] for r in decoded]
                )
    else:
        offset = 0
        while True:
            rows = collection.get(
                limit=batch_size, offset=offset, include=["documents", "metadatas"]
            )
            if not rows.ids:
                break
            yield rows
            offset += len(rows.ids)
