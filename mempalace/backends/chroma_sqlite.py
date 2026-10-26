"""Raw SQLite reads of a Chroma palace's ``chroma.sqlite3``.

Status, taxonomy, ``list_drawers``, the diary, recent records, lexical and
BM25 candidates and the metadata streams read Chroma's relational tables
directly, so they never load the HNSW vector index. Connections come from
:func:`._inproc_sqlite.open_reader` (through :func:`_read_db` or
:func:`sqlite_metadata_snapshot`), ``where`` filters become SQL through
:func:`_where_pairs` and :func:`_match_sql`, and drawer rows are decoded from
their stored form in :func:`_decoded_rows`.
"""

import contextlib
import os
import sqlite3
from collections import defaultdict
from typing import Iterable, Iterator, Optional

from ..lexical import tokenize
from ..stored_text import FLAG, decode_record, decode_value, encode_filter, encode_where
from ..timeutil import recency_floor, recency_key
from ._filters import matches_where
from ._inproc_sqlite import open_reader as open_palace_reader


class MetadataRecoveryPending(sqlite3.OperationalError):
    """The persisted metadata projection needs the normal Chroma recovery path."""


# Embeddings of one collection. A VECTOR-segment row (HNSW bookkeeping, or a
# future chroma that stores one) is not a drawer; joining it returns a ghost
# with empty document and metadata.
_DRAWERS_SQL = """
    FROM embeddings e
    JOIN segments s ON e.segment_id = s.id AND s.scope = 'METADATA'
    JOIN collections c ON s.collection = c.id
"""


def _db_file(palace_path: str) -> Optional[str]:
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    return db_path if os.path.isfile(db_path) else None


def _read_db(palace_path: str, read, *, busy: bool = False):
    """``read(conn)`` on a reader of the palace database, or ``None``.

    ``None`` when the file is missing or the read raises ``sqlite3.Error``,
    so callers can fall back to Chroma. ``busy`` waits out a transient
    writer or checkpoint lock instead of failing at once.
    """
    db_path = _db_file(palace_path)
    if db_path is None:
        return None
    try:
        conn = open_palace_reader(db_path)
        try:
            if busy:
                conn.execute("PRAGMA busy_timeout = 3000")
            return read(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _has_collection(conn, collection_name: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM collections WHERE name = ?", (collection_name,)).fetchone()
        is not None
    )


def _marks(values) -> str:
    return ",".join("?" * len(values))


def _where_pairs(where: Optional[dict]) -> Optional[list[tuple[str, tuple]]]:
    """``where`` as ``[(key, (value, ...)), ...]``, each key matching any of its values.

    Accepts ``None``, ``{"k": v}``, ``{"k": {"$eq": v}}``,
    ``{"k": {"$in": [v, ...]}}`` and an ``$and`` of those. ``None`` means the
    filter has another shape, which the readers here do not evaluate.
    """
    if not where:
        return []
    if not isinstance(where, dict):
        return None
    clauses = where["$and"] if list(where) == ["$and"] else [where]
    if not isinstance(clauses, list):
        return None
    pairs: list[tuple[str, tuple]] = []
    for clause in clauses:
        if not isinstance(clause, dict) or len(clause) != 1:
            return None
        key, value = next(iter(clause.items()))
        if key.startswith("$"):
            return None
        if not isinstance(value, dict):
            value = (value,)
        elif list(value) == ["$eq"]:
            value = (value["$eq"],)
        elif list(value) == ["$in"] and isinstance(value["$in"], (list, tuple)):
            value = tuple(value["$in"])
        else:
            return None
        pairs.append((key, value))
    return pairs


def _string_equalities(where: Optional[dict]) -> Optional[list[tuple[str, tuple]]]:
    """:func:`_where_pairs` when every key matches at least one string, else ``None``."""
    pairs = _where_pairs(where)
    if pairs is None or not all(
        values and all(isinstance(v, str) for v in values) for _, values in pairs
    ):
        return None
    return pairs


def _match_sql(equalities, row_id: str, alias: str = "w") -> tuple[str, list]:
    """``AND EXISTS`` clauses: the row ``row_id`` holds one of each key's string values."""
    sql = "".join(
        f" AND EXISTS (SELECT 1 FROM embedding_metadata {alias}"
        f" WHERE {alias}.id = {row_id} AND {alias}.key = ?"
        f" AND {alias}.string_value IN ({_marks(values)}))"
        for _, values in equalities
    )
    return sql, [part for key, values in equalities for part in (key, *values)]


def _equality_sizes(conn, equalities) -> list[int]:
    """Rows matching each ``(key, values)`` string equality, from its index."""
    return [
        conn.execute(
            "SELECT COUNT(*) FROM embedding_metadata"
            f" WHERE key = ? AND string_value IN ({_marks(values)})",
            (key, *values),
        ).fetchone()[0]
        for key, values in equalities
    ]


def _cell_value(sval, ival, fval, bval):
    """The value of one ``embedding_metadata`` row; Chroma fills one column."""
    if sval is not None:
        return sval
    if ival is not None:
        return ival
    if fval is not None:
        return fval
    if bval is not None:
        return bool(bval)
    return None


def _metadata_value_columns(conn) -> list[str]:
    """Value columns actually present on ``embedding_metadata``.

    Older chromadb builds predate ``bool_value``; probing keeps one reader
    working across schema versions.
    """
    present = {row[1] for row in conn.execute("PRAGMA table_info(embedding_metadata)")}
    return [c for c in ("string_value", "int_value", "float_value", "bool_value") if c in present]


def _decoded_rows(conn, row_ids, where: Optional[dict] = None) -> list[tuple]:
    """``(drawer_id, document, metadata)`` for ``row_ids``, in that order, decoded.

    The shape Chroma's ``get`` returns: a missing document is ``None``, as is
    the metadata of a drawer that has none. A key whose value cells are all
    NULL reads as ``None`` (see :func:`_scalar_rows`). ``where`` is tested
    against the stored metadata first, the way Chroma evaluates it.
    """
    if not row_ids:
        return []
    columns = _metadata_value_columns(conn)
    cells_at = [
        columns.index(c) if c in columns else None
        for c in ("string_value", "int_value", "float_value", "bool_value")
    ]
    records: dict[int, list] = {}
    for start in range(0, len(row_ids), 500):
        chunk = row_ids[start : start + 500]
        for row_id, drawer_id in conn.execute(
            f"SELECT id, embedding_id FROM embeddings WHERE id IN ({_marks(chunk)})", chunk
        ):
            records[row_id] = [drawer_id, None, None]
        for row_id, key, *cells in conn.execute(
            f"SELECT id, key, {', '.join(columns)} FROM embedding_metadata"
            f" WHERE id IN ({_marks(chunk)})",
            chunk,
        ):
            record = records.get(row_id)
            if record is None:
                continue
            value = _cell_value(*(None if at is None else cells[at] for at in cells_at))
            if key == "chroma:document":
                record[1] = value
            elif key is not None and not key.startswith("chroma:"):
                # Collection.get keeps internal fields (notably chroma:uri)
                # out of user metadata, even when URIs were stored with rows.
                if record[2] is None:
                    record[2] = {}
                record[2][key] = value
    stored_where = encode_where(where) if where else None
    return [
        (records[row_id][0], *decode_record(records[row_id][1], records[row_id][2]))
        for row_id in row_ids
        if row_id in records
        and (stored_where is None or matches_where(records[row_id][2] or {}, stored_where))
    ]


def _scalar_rows(rows) -> list[tuple]:
    """:func:`_decoded_rows` as ``Collection.get`` returns it: no ``None`` values."""
    return [
        (i, doc, {k: v for k, v in (meta or {}).items() if v is not None}) for i, doc, meta in rows
    ]


def not_bookkeeping_sql(conn, row_id: str = "e.id") -> str:
    """SQL predicate: the row is no registry row or sentinel (``is_bookkeeping``)."""
    columns = _metadata_value_columns(conn)
    flag = ["b.string_value IN ('true', 'True', '1')"]
    flag += [f"b.{c} = 1" for c in ("int_value", "bool_value") if c in columns]
    return (
        "NOT EXISTS (SELECT 1 FROM embedding_metadata b"
        f" WHERE b.id = {row_id} AND ("
        "(b.key = 'ingest_mode' AND b.string_value = 'registry')"
        f" OR (b.key = 'is_sentinel' AND ({' OR '.join(flag)}))))"
    )


# Most full-text matches the candidate pickers below read, best-ranked first.
_FTS_SCAN_CAP = 50_000
# A metadata filter matching at most this many drawers is evaluated by reading
# those drawers' text instead of ranking the whole full-text match set.
_FTS_FILTER_DRIVEN_MAX = 20_000


def _fts_tokens(query: str, stop_words: frozenset = frozenset()) -> list[str]:
    """Query terms the trigram index can match: three or more characters.

    Stop words are dropped unless the query holds nothing else.
    """
    terms = [t for t in tokenize(query) if len(t) >= 3]
    return [t for t in terms if t not in stop_words] or terms


# When the ranked window above is full but holds fewer whole-word matches than
# asked for, this many more matches are read in storage order, newest first.
_FTS_CONTINUATION_BUDGET = 500_000


class CandidateRows(list):
    """Candidate row ids; ``truncated`` when a read budget ran out first."""

    truncated = False


def _whole_words_first(rows, query_tokens: Iterable[str], limit: Optional[int]) -> list[int]:
    """Row ids from ``(row_id, text)`` pairs, whole-word matches first.

    The whole-word BM25 re-rank scores a drawer by the query words it
    contains, so drawers holding one as a whole word go first. Drawers that
    only contain a query term inside another word keep the places left over:
    they still carry near misses such as ``vectors`` for ``vector``.
    """
    words = set(query_tokens)
    whole: list[int] = []
    partial: list[int] = []
    for row_id, text in rows:
        if words.intersection(tokenize(text)):
            whole.append(int(row_id))
            if limit is not None and len(whole) >= limit:
                break
        elif limit is None or len(partial) < limit:
            partial.append(int(row_id))
    picked = whole + partial
    return picked if limit is None else picked[:limit]


def _fts_candidate_rows(
    conn,
    collection_name: str,
    query: str,
    *,
    limit: Optional[int],
    filter_sql: str = "",
    filter_params: Iterable = (),
    stop_words: frozenset = frozenset(),
) -> list[int]:
    """Row ids of the full-text matches most worth ranking, best first.

    ``chroma.sqlite3``'s full-text index uses the trigram tokenizer, so a
    query term matches inside other words: ``aven`` hits ``haven't`` and
    ``Avenue``, and a short name can have tens of thousands of such matches.
    Taking the first ``limit`` matches in storage order handed the whole-word
    BM25 re-rank the oldest substring hits, so the drawers that actually say
    ``Aven`` never reached it. Matches are read in FTS rank order (documents
    matching more query terms first), at most ``_FTS_SCAN_CAP`` of them, and
    picked by :func:`_whole_words_first`. ``filter_sql`` is appended to the
    WHERE clause and may refer to ``embedding_fulltext_search.rowid``.
    ``stop_words`` are left out of the full-text query: they match nearly
    every drawer and would make the ranking score the whole palace.
    """
    tokens = _fts_tokens(query, stop_words)
    if not tokens:
        return CandidateRows()
    match_sql = f"""
        SELECT embedding_fulltext_search.rowid, embedding_fulltext_search.string_value
        FROM embedding_fulltext_search
        JOIN embeddings e ON e.id = embedding_fulltext_search.rowid
        JOIN segments s ON e.segment_id = s.id
        JOIN collections c ON s.collection = c.id
        WHERE embedding_fulltext_search MATCH ? AND c.name = ?
        {filter_sql}
    """
    params = (" OR ".join(tokens), collection_name, *filter_params)
    words = set(tokens)
    ranked = conn.execute(
        match_sql + " ORDER BY embedding_fulltext_search.rank LIMIT ?",
        (*params, _FTS_SCAN_CAP),
    ).fetchall()
    result = CandidateRows(_whole_words_first(ranked, tokens, limit))
    if len(ranked) < _FTS_SCAN_CAP:
        return result
    text_by_id = {int(row_id): text or "" for row_id, text in ranked}
    whole = [row_id for row_id in result if words.intersection(tokenize(text_by_id[row_id]))]
    if limit is not None and len(whole) >= limit:
        return result
    # The ranked window is full and short of whole-word matches: a name can
    # rank below tens of thousands of substring hits (``Aven`` under
    # ``Avenue``). Keep reading the rest, newest first, within a budget.
    seen = text_by_id.keys()
    extra: list[int] = []
    read = 0
    for row_id, text in conn.execute(
        match_sql + " ORDER BY embedding_fulltext_search.rowid DESC LIMIT ?",
        (*params, _FTS_CONTINUATION_BUDGET),
    ):
        read += 1
        if int(row_id) in seen or not words.intersection(tokenize(text)):
            continue
        extra.append(int(row_id))
        if limit is not None and len(whole) + len(extra) >= limit:
            break
    else:
        result.truncated = read >= _FTS_CONTINUATION_BUDGET
    whole_ids = set(whole)
    others = [row_id for row_id in result if row_id not in whole_ids]
    picked = whole + extra + others
    out = CandidateRows(picked if limit is None else picked[:limit])
    out.truncated = result.truncated
    return out


def _filtered_candidate_rows(
    conn,
    collection_name: str,
    query: str,
    *,
    limit: Optional[int],
    equalities: list[tuple[str, tuple]],
    filter_sql: str = "",
    filter_params: Iterable = (),
    stop_words: frozenset = frozenset(),
) -> Optional[list[int]]:
    """:func:`_fts_candidate_rows` for a filter that matches few drawers.

    Ranking every full-text match and testing the filter on each took seconds
    for a ten-drawer wing, because the match set is the whole palace for a
    common term. When the smallest of ``equalities`` (``(key, values)``
    string metadata matches, checked by one count on the
    ``(key, string_value)`` index)
    matches at most ``_FTS_FILTER_DRIVEN_MAX`` drawers, this reads just those
    drawers' text, keeps the ones the trigram index would match (any term of
    three or more characters as a case-insensitive substring), newest first,
    and picks by :func:`_whole_words_first`. ``filter_sql`` must hold every
    filter and may refer to ``e.id``. ``None`` when the filter is too broad.
    """
    tokens = _fts_tokens(query, stop_words)
    if not tokens or not equalities:
        return None
    sizes = _equality_sizes(conn, equalities)
    if min(sizes) > _FTS_FILTER_DRIVEN_MAX:
        return None
    key, values = equalities[sizes.index(min(sizes))]
    row_ids = [
        row[0]
        for row in conn.execute(
            f"""
            SELECT e.id FROM embedding_metadata w
            CROSS JOIN embeddings e ON e.id = w.id
            JOIN segments s ON e.segment_id = s.id
            JOIN collections c ON s.collection = c.id
            WHERE w.key = ? AND w.string_value IN ({_marks(values)}) AND c.name = ?
            {filter_sql}
            ORDER BY e.id DESC
            """,
            (key, *values, collection_name, *filter_params),
        )
    ]
    texts: dict[int, str] = {}
    for start in range(0, len(row_ids), 500):
        chunk = row_ids[start : start + 500]
        texts.update(
            conn.execute(
                "SELECT id, string_value FROM embedding_metadata"
                f" WHERE key = 'chroma:document' AND id IN ({_marks(chunk)})",
                chunk,
            ).fetchall()
        )
    matching = []
    for row_id in row_ids:
        text = texts.get(row_id) or ""
        lowered = text.lower()
        if any(token in lowered for token in tokens):
            matching.append((row_id, text))
    return _whole_words_first(matching, tokens, limit)


def _newest_row_ids(
    conn, collection_name: str, limit: int, filter_sql: str = "", filter_params: Iterable = ()
) -> list[int]:
    """Row ids of the newest ``limit`` rows, by ``created_at`` where the schema has it."""

    def newest(order):
        sql = f"""
            SELECT e.id
            FROM embeddings e
            JOIN segments s ON e.segment_id = s.id
            JOIN collections c ON s.collection = c.id
            WHERE c.name = ?
            {filter_sql}
            ORDER BY {order}
            LIMIT ?
        """
        return [r[0] for r in conn.execute(sql, (collection_name, *filter_params, limit))]

    try:
        return newest("e.created_at DESC")
    except sqlite3.Error:
        return newest("e.id DESC")


def sqlite_lexical_rows(
    palace_path: str,
    collection_name: str,
    query: str,
    *,
    where: Optional[dict],
    limit: int,
) -> list[tuple]:
    """Decoded ``(drawer_id, document, metadata)`` worth BM25-ranking for ``query``.

    The best ``limit`` full-text matches by FTS rank, or every match under a
    ``where``, which is applied to the stored metadata afterwards: a cap
    before filtering could fill with wrong-scope rows. The newest ``limit``
    rows stand in when no query term is three characters long or the
    full-text query fails; a clean miss stays empty. ``[]`` when the database
    is missing or unreadable.
    """
    tokens = [t for t in tokenize(query) if len(t) >= 3]

    def read(conn):
        row_ids: list[int] = []
        recency = not tokens
        if tokens:
            limit_sql = "" if where else "ORDER BY embedding_fulltext_search.rank LIMIT ?"
            try:
                row_ids = [
                    row[0]
                    for row in conn.execute(
                        f"""
                        SELECT e.id
                        FROM embedding_fulltext_search
                        JOIN embeddings e ON e.id = embedding_fulltext_search.rowid
                        JOIN segments s ON e.segment_id = s.id
                        JOIN collections c ON s.collection = c.id
                        WHERE embedding_fulltext_search MATCH ?
                          AND c.name = ?
                        {limit_sql}
                        """,
                        (" OR ".join(tokens), collection_name) + (() if where else (limit,)),
                    )
                ]
            except sqlite3.Error:
                recency = True
        if not row_ids and recency:
            row_ids = _newest_row_ids(conn, collection_name, limit)
        return _decoded_rows(conn, row_ids, where)

    return _read_db(palace_path, read) or []


def _sqlite_embedding_count(palace_path: str, collection_name: str) -> Optional[int]:
    """Count rows in chroma.sqlite3.embeddings for ``collection_name``.

    Mirrors :func:`mempalace.repair.sqlite_drawer_count` but kept in this
    module so the backend probe doesn't pull in the repair CLI module.
    """

    def read(conn):
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM embeddings e
            JOIN segments s ON e.segment_id = s.id
            JOIN collections c ON s.collection = c.id
            WHERE c.name = ?
            """,
            (collection_name,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    return _read_db(palace_path, read)


def _sqlite_collection_has_rows(palace_path: str, collection_name: str) -> Optional[bool]:
    """Whether ``collection_name`` holds any drawer, read from chroma.sqlite3.

    ``Collection.count()`` on a freshly built client loads the whole HNSW
    segment while holding the GIL, which on a multi-million-drawer palace
    stalls every thread in the process for seconds. This answers the same
    empty-or-not question with one indexed row. ``None`` when the database
    is missing or unreadable, so callers can fall back to ``count()``.
    """
    return _read_db(
        palace_path,
        lambda conn: (
            conn.execute(
                f"SELECT 1 {_DRAWERS_SQL} WHERE c.name = ? LIMIT 1", (collection_name,)
            ).fetchone()
            is not None
        ),
    )


# Filters matching at most this many rows are read whole and ordered in Python
# (see _sqlite_recent_records); larger ones walk the order_field index.
_RECENT_FILTER_DRIVEN_MAX = 200_000

# Timestamps carrying a UTC suffix; the rest are naive local or other forms.
_UTC_FORM_SQL = (
    "(substr(o.string_value, -6) = '+00:00' OR substr(o.string_value, -1) IN ('Z', 'z'))"
)


def _newest_first(rows: list) -> list:
    """``(id, timestamp, ...)`` rows by instant, newest first; ties and undated by id."""
    rows = sorted(rows, key=lambda r: r[0])
    return sorted(rows, key=lambda r: recency_key(r[1]), reverse=True)


def _sqlite_recent_records(
    palace_path: str,
    collection_name: str,
    *,
    limit: int,
    equalities: list[tuple[str, tuple]],
    order_field: str,
) -> Optional[list[tuple[str, str, Optional[dict]]]]:
    """Decoded ``(id, document, metadata)`` for the newest ``limit`` records.

    Newest first by the instant in ``order_field``, records without a non-empty
    string value last in storage order: the order :func:`recency_sort_key` defines.
    ``equalities`` filter on stored string metadata, each key matching any of
    its values. A missing document reads as ``""``. ``None`` when the database
    is missing or the read fails, so the caller can fall back to Chroma.
    """
    filter_sql, filter_params = _match_sql(equalities, "e.id")

    def read(conn):
        # Walking the order_field index and testing the filter on each row
        # finds `limit` matches fast when the filter is dense, but reads the
        # whole collection for a sparse one (a ten-drawer wing). Size the
        # filter from its index first and, when it is small, read just the
        # matching rows and order them here.
        sizes = _equality_sizes(conn, equalities)
        filter_driven = bool(sizes) and min(sizes) <= _RECENT_FILTER_DRIVEN_MAX
        if filter_driven:
            drive = sizes.index(min(sizes))
            drive_key, drive_values = equalities[drive]
            rest_sql, rest_params = _match_sql(
                equalities[:drive] + equalities[drive + 1 :], "w.id", "w2"
            )
            rows = conn.execute(
                f"""
                SELECT w.id, o.string_value
                FROM embedding_metadata w
                CROSS JOIN embeddings e ON e.id = w.id
                JOIN segments s ON e.segment_id = s.id AND s.scope = 'METADATA'
                JOIN collections c ON s.collection = c.id
                LEFT JOIN embedding_metadata o ON o.id = w.id AND o.key = ?
                WHERE w.key = ? AND w.string_value IN ({_marks(drive_values)}) AND c.name = ?
                {rest_sql}
                """,
                (order_field, drive_key, *drive_values, collection_name, *rest_params),
            ).fetchall()
            row_ids = [r[0] for r in _newest_first(rows)[:limit]]
        else:
            dated_sql = f"""
                SELECT e.id, o.string_value {_DRAWERS_SQL}
                JOIN embedding_metadata o ON o.id = e.id
                WHERE c.name = ? AND o.key = ? AND o.string_value > ''
                {filter_sql}
            """

            def newest(extra_sql="", extra_params=()):
                return conn.execute(
                    dated_sql + extra_sql + " ORDER BY o.string_value DESC, e.id LIMIT ?",
                    (collection_name, order_field, *filter_params, *extra_params, limit),
                ).fetchall()

            # Text order is instant order only within one form: take the
            # newest of each, from the text floor of the overall newest, and
            # merge them by instant.
            rows = newest()
            if len(rows) == limit:
                floor = recency_floor(r[1] for r in rows)
                for form in (_UTC_FORM_SQL, f"NOT ({_UTC_FORM_SQL})"):
                    rows += newest(f" AND o.string_value >= ? AND {form}", (floor,))
            row_ids = list(dict.fromkeys(r[0] for r in _newest_first(rows)))[:limit]
        # The filter-driven branch already ordered the undated rows.
        if len(row_ids) < limit and not filter_driven:
            row_ids += [
                r[0]
                for r in conn.execute(
                    f"""
                    SELECT e.id {_DRAWERS_SQL}
                    WHERE c.name = ?
                    AND NOT EXISTS (SELECT 1 FROM embedding_metadata o
                        WHERE o.id = e.id AND o.key = ? AND o.string_value > '')
                    {filter_sql}
                    ORDER BY e.id
                    LIMIT ?
                    """,
                    (collection_name, order_field, *filter_params, limit - len(row_ids)),
                )
            ]
        return [(i, doc or "", meta) for i, doc, meta in _decoded_rows(conn, row_ids)]

    return _read_db(palace_path, read)


def sqlite_wing_names(palace_path: str, limit: int = 10_000) -> Optional[list[str]]:
    """Every distinct string ``wing`` in ``chroma.sqlite3``, or ``None``.

    Steps through the ``(key, string_value)`` index one value at a time, so
    the read costs one index seek per wing, not a scan of every drawer. Spans
    every collection in the database: callers use it to recognise spellings
    of a wing, not to count drawers. ``None`` when the database is missing
    or the read fails.
    """
    sql = "SELECT MIN(string_value) FROM embedding_metadata WHERE key = 'wing'"

    def read(conn):
        names: list[str] = []
        row = conn.execute(sql).fetchone()
        while row and row[0] is not None and len(names) < limit:
            names.append(row[0])
            row = conn.execute(sql + " AND string_value > ?", (row[0],)).fetchone()
        return [decode_value(name) for name in names]

    return _read_db(palace_path, read)


def _sqlite_wing_room_counts(
    palace_path: str, collection_name: str
) -> Optional[tuple[int, dict[str, dict[str, int]]]]:
    """Tally drawers by wing/room straight from ``chroma.sqlite3``.

    Returns ``(total, {wing: {room: count}})`` or ``None`` when the read
    cannot be trusted — missing DB file, the collection has not been
    bootstrapped, or any sqlite error (including a sustained writer lock).
    ``None`` signals the caller to fall back to the ChromaDB client path
    (which also emits the right state-specific guidance for absent/empty
    palaces).

    The point of reading sqlite directly is to count drawers **without opening
    the collection**, because opening it cold-loads the HNSW vector index. On
    large palaces that load costs tens of seconds of CPU per call — a steep,
    pointless tax for an inspection command that only needs metadata the
    relational tables already hold. Wings/rooms live in plain
    ``embedding_metadata`` rows, joined to ``embeddings`` on the
    ``(id, key)`` primary key, so the tally is a bounded scan of the metadata
    segment: sub-second warm, a few seconds cold on a multi-GB DB — versus the
    ~60s the vector-index load costs.

    :func:`_sqlite_embedding_count` counts the total only; the MCP status
    fallback for a diverged index folds this tally into its wing and room
    histograms.

    Notes:
    - ``busy_timeout`` lets a transient checkpoint lock resolve instead of
      instantly demoting to the slow HNSW path; a *sustained* lock still
      raises and falls back (slow but correct).
    - ``s.scope = 'METADATA'`` makes the single-segment join explicit so a
      future ChromaDB that also stored per-vector-segment rows could not
      silently double every count.
    - ``COALESCE`` over ``string_value``/``int_value``/``float_value`` matches
      the ChromaDB path, which surfaces a numeric wing/room natively rather
      than dropping it to ``"?"``.
    """

    def read(conn):
        # Distinguish "collection never bootstrapped" (-> None, so the
        # caller can show the 'initialized but empty' message) from
        # "collection exists with zero drawers" (-> a real 0 tally).
        if not _has_collection(conn, collection_name):
            return None
        # Registry rows and sentinels track sources; they are not drawers.
        rows = conn.execute(
            f"""
            SELECT COALESCE(wm.string_value, CAST(wm.int_value AS TEXT),
                            CAST(wm.float_value AS TEXT), '?') AS wing,
                   COALESCE(rm.string_value, CAST(rm.int_value AS TEXT),
                            CAST(rm.float_value AS TEXT), '?') AS room,
                   COUNT(*) AS n
            {_DRAWERS_SQL}
            LEFT JOIN embedding_metadata wm ON wm.id = e.id AND wm.key = 'wing'
            LEFT JOIN embedding_metadata rm ON rm.id = e.id AND rm.key = 'room'
            WHERE c.name = ? AND {not_bookkeeping_sql(conn)}
            GROUP BY wing, room
            """,
            (collection_name,),
        ).fetchall()
        total = 0
        wing_rooms: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for wing, room, n in rows:
            wing_rooms[decode_value(wing)][decode_value(room)] += int(n)
            total += int(n)
        return total, wing_rooms

    return _read_db(palace_path, read, busy=True)


def sqlite_room_wing_hall_counts(palace_path: str, collection_name: str) -> Optional[list[tuple]]:
    """Grouped ``(room, wing, hall, n, last_date)`` from ``chroma.sqlite3``.

    ``last_date`` is the newest ``date`` metadata value in the group, so
    ``find_tunnels`` can still report ``recent`` without paging every drawer
    (``build_graph`` only ever uses the maximum). Returns ``None`` when sqlite
    cannot be trusted, so the caller falls back to the client path.
    """

    def read(conn):
        if not _has_collection(conn, collection_name):
            return None
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(rm.string_value, CAST(rm.int_value AS TEXT),
                         CAST(rm.float_value AS TEXT), '') AS room,
                COALESCE(wm.string_value, CAST(wm.int_value AS TEXT),
                         CAST(wm.float_value AS TEXT), '') AS wing,
                COALESCE(hm.string_value, CAST(hm.int_value AS TEXT),
                         CAST(hm.float_value AS TEXT), '') AS hall,
                COUNT(*) AS n,
                COALESCE(MAX(dm.string_value), '') AS last_date
            {_DRAWERS_SQL}
            LEFT JOIN embedding_metadata rm ON rm.id = e.id AND rm.key = 'room'
            LEFT JOIN embedding_metadata wm ON wm.id = e.id AND wm.key = 'wing'
            LEFT JOIN embedding_metadata hm ON hm.id = e.id AND hm.key = 'hall'
            LEFT JOIN embedding_metadata dm ON dm.id = e.id AND dm.key = 'date'
            WHERE c.name = ?
            GROUP BY room, wing, hall
            """,
            (collection_name,),
        ).fetchall()
        return [
            (decode_value(room), decode_value(wing), decode_value(hall), n, decode_value(last))
            for room, wing, hall, n, last in rows
        ]

    return _read_db(palace_path, read, busy=True)


def sqlite_wing_source_counts(palace_path: str, collection_name: str) -> Optional[list[tuple]]:
    """Grouped ``(wing, source_file, n)`` for transcript-mined drawers from
    ``chroma.sqlite3``, scoped to ``collection_name``; ``None`` when sqlite
    cannot serve it. Same contract as the sqlite_exact reader: only rows
    whose ``source_file`` is a Claude Code projects path or a Codex sessions
    path, which is what the audit's mixed-wing check and ``wings split`` use.
    """

    def read(conn):
        if not _has_collection(conn, collection_name):
            return None
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(wm.string_value, '') AS wing,
                sm.string_value AS source_file,
                COUNT(*) AS n
            {_DRAWERS_SQL}
            JOIN embedding_metadata sm ON sm.id = e.id AND sm.key = 'source_file'
            LEFT JOIN embedding_metadata wm ON wm.id = e.id AND wm.key = 'wing'
            WHERE c.name = ?
              AND (sm.string_value LIKE '%.claude%projects%'
                   OR sm.string_value LIKE '%.codex%sessions%')
            GROUP BY wing, source_file
            """,
            (collection_name,),
        ).fetchall()
        return [(decode_value(wing), decode_value(source), n) for wing, source, n in rows]

    return _read_db(palace_path, read, busy=True)


def _sqlite_iter_metadata(
    conn,
    collection_name: str,
    keys: Optional[Iterable[str]],
    require_key: Optional[str],
    equals: Optional[dict] = None,
) -> Iterator[Optional[dict]]:
    """Stream each drawer's stored metadata from an open connection, in row order.

    With ``keys``, only those keys are read, and a drawer that has none of
    them is skipped. With ``keys=None``, every non-internal key is read and a
    drawer without metadata yields ``None``, the way Chroma's ``get`` returns
    it. ``require_key`` limits the scan to drawers holding a string value
    under that key, found through the ``(key, string_value)`` index.
    ``equals`` limits it further to drawers whose string value under each key
    equals the given one, through the same index, so a pass over one wing
    reads that wing's rows rather than the whole collection. Chroma's own
    paging is a SQL ``OFFSET``, which re-walks every skipped row, so a full
    pass that way is quadratic in the number of drawers.
    """
    scope = f"SELECT e.id {_DRAWERS_SQL} WHERE c.name = ?"
    params: list = [collection_name]
    if require_key is not None:
        scope += """ AND e.id IN (SELECT r.id FROM embedding_metadata r
                     WHERE r.key = ? AND r.string_value IS NOT NULL)"""
        params.append(require_key)
    for key, value in (equals or {}).items():
        scope += """ AND e.id IN (SELECT r.id FROM embedding_metadata r
                     WHERE r.key = ? AND r.string_value = ?)"""
        params.extend((key, encode_filter(value)))
    # Older chromadb schemas lack bool_value; select NULL for any value
    # column the table does not have so the row shape stays fixed.
    present = set(_metadata_value_columns(conn))
    columns = "m.key, " + ", ".join(
        f"m.{col}" if col in present else f"NULL AS {col}"
        for col in ("string_value", "int_value", "float_value", "bool_value")
    )
    if keys is not None:
        # The escape flag rides along so the caller can decode these keys.
        keys = list(dict.fromkeys([*keys, FLAG]))
        sql = f"""
            SELECT m.id, {columns} FROM embedding_metadata m
            WHERE m.key IN ({_marks(keys)}) AND m.id IN ({scope})
            ORDER BY m.id
        """
        cursor = conn.execute(sql, [*keys, *params])
    else:
        sql = f"""
            SELECT ids.id, {columns} FROM ({scope}) ids
            LEFT JOIN embedding_metadata m ON m.id = ids.id AND substr(m.key, 1, 7) != 'chroma:'
            ORDER BY ids.id
        """
        cursor = conn.execute(sql, params)
    current_id = None
    current: Optional[dict] = None
    for row_id, key, sval, ival, fval, bval in cursor:
        if row_id != current_id:
            if current_id is not None:
                yield current
            current_id, current = row_id, None
        if key is not None:
            if current is None:
                current = {}
            current[key] = _cell_value(sval, ival, fval, bval)
    if current_id is not None:
        yield current


def sqlite_iter_metadata(
    palace_path: str,
    collection_name: str,
    keys: Optional[Iterable[str]] = None,
    require_key: Optional[str] = None,
    equals: Optional[dict] = None,
) -> Optional[Iterator[Optional[dict]]]:
    """Decoded :func:`_sqlite_iter_metadata` in its own read transaction.

    ``None`` when the database file is missing. The iterator raises
    :class:`MetadataRecoveryPending` when queued writes have not reached the
    metadata segment yet, and any read error.
    """
    db_path = _db_file(palace_path)
    if db_path is None:
        return None

    def rows():
        conn = open_palace_reader(db_path)
        try:
            conn.execute("BEGIN")
            if _sqlite_ready_metadata_segment(conn, collection_name) is None:
                raise MetadataRecoveryPending("metadata recovery pending; use collection reader")
            for meta in _sqlite_iter_metadata(conn, collection_name, keys, require_key, equals):
                meta = decode_record(None, meta)[1]
                if keys is not None and not meta:
                    continue
                yield meta
        finally:
            conn.close()

    return rows()


def _sqlite_ready_metadata_segment(conn, collection_name):
    """Return the unique collection/metadata segment only after queued writes landed.

    The caller must keep this check and its reads in one explicit transaction.
    Missing or ambiguous recovery state requires the normal Chroma path.
    """
    collections = conn.execute(
        "SELECT id FROM collections WHERE name = ?", (collection_name,)
    ).fetchall()
    if len(collections) != 1:
        return None
    collection_id = collections[0][0]
    segments = conn.execute(
        "SELECT id FROM segments WHERE collection = ? AND scope = 'METADATA'",
        (collection_id,),
    ).fetchall()
    if len(segments) != 1:
        return None
    segment_id = segments[0][0]
    watermark = conn.execute(
        "SELECT seq_id FROM max_seq_id WHERE segment_id = ?", (segment_id,)
    ).fetchone()
    suffix = f"/{collection_id}"
    if watermark is None:
        # A never-written collection has no watermark. Existing rows
        # or a queued write make that missing state untrustworthy.
        if (
            conn.execute(
                "SELECT 1 FROM embeddings WHERE segment_id = ? LIMIT 1", (segment_id,)
            ).fetchone()
            or conn.execute(
                "SELECT 1 FROM embeddings_queue WHERE substr(topic, -length(?)) = ? LIMIT 1",
                (suffix, suffix),
            ).fetchone()
        ):
            return None
        consumed = 0
    else:
        consumed = watermark[0]
        if isinstance(consumed, bytes):
            # Older Chroma stored big-endian 8-byte offsets. The
            # newer \x11\x11-prefixed format is not that encoding.
            if len(consumed) != 8 or consumed.startswith(b"\x11\x11"):
                return None
            consumed = int.from_bytes(consumed, "big")
        if not isinstance(consumed, int) or not 0 <= consumed <= (1 << 63) - 1:
            return None
    # Legacy Collection.get backfills queued operations first. Read
    # only when the metadata segment has already consumed them; the
    # vector segment's lag does not affect stored text or metadata.
    if conn.execute(
        "SELECT 1 FROM embeddings_queue WHERE seq_id > ? AND substr(topic, -length(?)) = ? LIMIT 1",
        (consumed, suffix, suffix),
    ).fetchone():
        return None
    return collection_id, segment_id


@contextlib.contextmanager
def _ready_snapshot(palace_path: str, collection_name: str):
    """``(conn, (collection_id, segment_id))`` in one read transaction, or ``None``.

    ``None`` when the database is missing, cannot be opened, or has queued
    writes the metadata segment has not consumed; callers must use Chroma.
    """
    conn = ready = None
    db_path = _db_file(palace_path)
    try:
        if db_path is not None:
            conn = open_palace_reader(db_path)
            conn.execute("PRAGMA busy_timeout = 3000")
            conn.execute("BEGIN")
            ready = _sqlite_ready_metadata_segment(conn, collection_name)
    except sqlite3.Error:
        ready = None
    try:
        yield None if ready is None else (conn, ready)
    finally:
        if conn is not None:
            conn.close()


@contextlib.contextmanager
def sqlite_metadata_snapshot(palace_path: str, collection_name: str):
    """A recovered metadata snapshot, or None when callers must use Chroma.

    Keep this context open through page selection and document hydration. It
    reads no documents itself and never takes a backend write lock.
    """
    with _ready_snapshot(palace_path, collection_name) as snapshot:
        yield None if snapshot is None else snapshot[0]


def sqlite_list_id_metadata(
    palace_path: str,
    collection_name: str,
    where: Optional[dict] = None,
    *,
    _connection=None,
) -> Optional[tuple[list[str], list[dict]]]:
    """All matching drawer ids + metadata from sqlite, without opening HNSW.

    Documents are deliberately excluded: ``chroma:document`` lives in the same
    ``embedding_metadata`` table, and joining it in would materialize the whole
    palace's verbatim text (hundreds of MB on a six-figure palace) just to
    render one page of previews. Callers hydrate the page they display via
    :func:`sqlite_documents_for_ids`.

    ``where`` supports equality or ``$in`` on ``wing``/``room`` and ``$and``
    of those, matching ``tool_list_drawers``; it is applied in SQL. Returns
    ``None`` when sqlite cannot be trusted so the caller can fall back to
    ``col.get`` paging.
    """
    if _db_file(palace_path) is None:
        return None
    pairs = _where_pairs(where)
    if pairs is None or any(key not in ("wing", "room") for key, _ in pairs):
        return None
    filters = dict(pairs)
    reader = (
        contextlib.nullcontext(_connection)
        if _connection is not None
        else sqlite_metadata_snapshot(palace_path, collection_name)
    )
    try:
        with reader as conn:
            if conn is None:
                return None
            value_columns = _metadata_value_columns(conn)
            if not value_columns:
                return None
            # Push the filter down as an inner join per key. Non-string operands
            # cannot be matched against ``string_value``, so those stay in the
            # Python pass below rather than silently matching nothing.
            joins = []
            params: list = []
            for idx, (key, values) in enumerate(sorted(filters.items())):
                if not values or not all(isinstance(v, str) for v in values):
                    continue
                alias = f"f{idx}"
                joins.append(
                    f"JOIN embedding_metadata {alias} ON {alias}.id = e.id "
                    f"AND {alias}.key = ? AND {alias}.string_value IN ({_marks(values)})"
                )
                params.extend([key, *(encode_filter(v) for v in values)])
            params.append(collection_name)
            rows = conn.execute(
                f"""
                SELECT e.embedding_id, m.key, {", ".join("m." + c for c in value_columns)}
                {_DRAWERS_SQL}
                {" ".join(joins)}
                LEFT JOIN embedding_metadata m
                    ON m.id = e.id AND substr(m.key, 1, 7) != 'chroma:'
                WHERE c.name = ?
                ORDER BY e.id
                """,
                params,
            ).fetchall()
    except sqlite3.Error:
        return None

    # Column positions are resolved once: this loop runs per metadata row —
    # millions of them on a large palace — so per-row dict building there is
    # the difference between one second and several.
    def _cell(name):
        return value_columns.index(name) + 2 if name in value_columns else None

    s_at, i_at, f_at, b_at = (
        _cell(c) for c in ("string_value", "int_value", "float_value", "bool_value")
    )
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        doc_id = row[0]
        meta = by_id.get(doc_id)
        if meta is None:
            meta = by_id[doc_id] = {}
            order.append(doc_id)
        key = row[1]
        if not key:
            continue
        value = _cell_value(
            row[s_at] if s_at is not None else None,
            row[i_at] if i_at is not None else None,
            row[f_at] if f_at is not None else None,
            row[b_at] if b_at is not None else None,
        )
        if value is None:
            continue
        meta[key] = value

    ids: list[str] = []
    metas: list[dict] = []
    for doc_id in order:
        meta = decode_record(None, by_id[doc_id])[1]
        if any(meta.get(key) not in values for key, values in filters.items()):
            continue
        ids.append(doc_id)
        metas.append(meta)
    return ids, metas


def sqlite_documents_for_ids(
    palace_path: str,
    collection_name: str,
    ids: list,
    *,
    _connection=None,
) -> Optional[dict]:
    """``{drawer_id: document}`` for ``ids`` only, straight from sqlite.

    Hydrates previews for the page being displayed without opening HNSW and
    without reading the rest of the palace's text.

    Resolved in two indexed steps rather than one join: ``embedding_id`` is
    only indexed as part of ``UNIQUE (segment_id, embedding_id)``, so a join
    that filters on it alone degenerates into a full scan of
    ``embedding_metadata`` — 5.7s for a 20-row page on a 165k-drawer palace.
    Seeking the segment first, then ``embedding_metadata``'s ``(id, key)``
    primary key, keeps both steps on an index.
    """
    if not ids:
        return {}
    if _db_file(palace_path) is None:
        return None
    wanted = [str(i) for i in ids]
    docs: dict[str, str] = {}
    reader = (
        contextlib.nullcontext(_connection)
        if _connection is not None
        else sqlite_metadata_snapshot(palace_path, collection_name)
    )
    try:
        with reader as conn:
            if conn is None:
                return None
            segments = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT s.id FROM segments s
                    JOIN collections c ON s.collection = c.id
                    WHERE c.name = ? AND s.scope = 'METADATA'
                    """,
                    (collection_name,),
                )
            ]
            if not segments:
                return None
            for start in range(0, len(wanted), 900):
                chunk = wanted[start : start + 900]
                rows = conn.execute(
                    f"""
                    SELECT id, embedding_id FROM embeddings
                    WHERE segment_id IN ({_marks(segments)})
                      AND embedding_id IN ({_marks(chunk)})
                    """,
                    [*segments, *chunk],
                ).fetchall()
                if not rows:
                    continue
                public_by_internal = {int(row[0]): str(row[1]) for row in rows}
                internal = list(public_by_internal)
                flagged = set()
                for internal_id, key, value, flag in conn.execute(
                    f"""
                    SELECT id, key, string_value, int_value FROM embedding_metadata
                    WHERE key IN ('chroma:document', ?) AND id IN ({_marks(internal)})
                    """,
                    [FLAG, *internal],
                ):
                    if key == FLAG:
                        if flag:
                            flagged.add(public_by_internal[int(internal_id)])
                    else:
                        docs[public_by_internal[int(internal_id)]] = str(value or "")
                for drawer_id in flagged & docs.keys():
                    docs[drawer_id] = decode_record(docs[drawer_id], {FLAG: 1})[0]
    except sqlite3.Error:
        return None
    return docs


def sqlite_diary_rows(
    palace_path: str,
    collection_name: str,
    *,
    agent_name: str,
    wing=(),
    limit: int = 10,
) -> Optional[tuple[int, list[tuple[str, Optional[str], dict]]]]:
    """Count physical diary rows and hydrate only the newest ``limit`` rows.

    All reads share one SQLite snapshot. Rows order by the instant in
    ``filed_at``, newest first; missing timestamps sort last, with storage
    order breaking ties, matching the diary's paged
    ``Collection.get`` path. Non-string timestamps, an ambiguous collection,
    or an unavailable schema return ``None`` for that path to handle instead.
    Queued writes ahead of the metadata watermark also require Chroma's
    recovery path. A missing stored document remains ``None`` as it does in
    Chroma's get. ``limit`` is bounded to the diary API's range of 1..100.
    ``wing`` is one wing or a list of wings, any of which matches.
    """
    if _db_file(palace_path) is None or not isinstance(limit, int):
        return None
    limit = max(1, min(limit, 100))
    equalities = [("room", ("diary",)), ("agent", (encode_filter(agent_name),))]
    wings = [wing] if isinstance(wing, str) else list(wing or ())
    wings = tuple(encode_filter(w) for w in wings if w)
    try:
        with _ready_snapshot(palace_path, collection_name) as snapshot:
            if snapshot is None:
                return None
            conn, (collection_id, _segment_id) = snapshot
            if "string_value" not in _metadata_value_columns(conn):
                return None

            # Drive from the smallest indexed metadata equality. Driving from
            # embeddings instead scans the entire palace for a sparse diary.
            sizes = _equality_sizes(conn, equalities)
            drive = sizes.index(min(sizes))
            drive_key, (drive_value,) = equalities[drive]
            remaining = equalities[:drive] + equalities[drive + 1 :]
            if wings:
                remaining.append(("wing", wings))
            filters, filter_params = _match_sql(remaining, "f.id")
            scope = f"""
                FROM embedding_metadata f
                CROSS JOIN embeddings e ON e.id = f.id
                JOIN segments s ON s.id = e.segment_id
                LEFT JOIN embedding_metadata stamp ON stamp.id = e.id AND stamp.key = 'filed_at'
                WHERE f.key = ? AND f.string_value = ?
                  AND s.collection = ? AND s.scope = 'METADATA'
                {filters}
            """
            params = [drive_key, drive_value, collection_id, *filter_params]
            unsupported_timestamp = "(stamp.id IS NOT NULL AND stamp.string_value IS NULL)"
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embedding_metadata_array'"
            ).fetchone():
                # The legacy diary response exposes these values unchanged.
                # Array hydration is not this scalar reader's responsibility;
                # an array timestamp must not be mistaken for a missing one.
                unsupported_timestamp += (
                    " OR EXISTS (SELECT 1 FROM embedding_metadata_array a"
                    " WHERE a.id = f.id AND a.key IN ('filed_at', 'date', 'topic'))"
                )
            total, unsupported = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN "
                + unsupported_timestamp
                + " THEN 1 ELSE 0 END), 0) "
                + scope,
                params,
            ).fetchone()
            if unsupported:
                return None
            # Ordered here, not in SQL: text order is not instant order across
            # naive local and UTC timestamps. One agent's diary is small.
            stamped = conn.execute("SELECT e.id, stamp.string_value " + scope, params).fetchall()
            selected = [row_id for row_id, _ in _newest_first(stamped)[:limit]]
            rows = _decoded_rows(conn, selected)
            return total, _scalar_rows(rows)
    except (sqlite3.Error, ValueError):
        return None


def sqlite_drawer_rows(
    palace_path: str,
    collection_name: str,
    *,
    drawer_id: str,
) -> Optional[tuple[bool, list[tuple[str, Optional[str], dict]]]]:
    """Read a direct drawer, or every scalar-parent-linked physical chunk.

    The direct physical id wins over a logical group with the same handle.
    Groups are complete and deduplicated by physical row, with no page cap.
    Missing, unsupported, or unmaterialized records return ``None`` so Chroma
    can recover them or preserve metadata types this scalar reader cannot
    decode. In particular any selected array metadata requires that fallback.
    """
    try:
        with _ready_snapshot(palace_path, collection_name) as snapshot:
            if snapshot is None:
                return None
            conn, (_collection_id, segment_id) = snapshot
            if "string_value" not in _metadata_value_columns(conn):
                return None
            selected = conn.execute(
                "SELECT id, embedding_id FROM embeddings"
                " WHERE segment_id = ? AND embedding_id = ? ORDER BY id",
                (segment_id, drawer_id),
            ).fetchall()
            direct = bool(selected)
            if not direct:
                # The composite key/string index drives the parent search;
                # each row's internal id then seeks its embedding by PK.
                selected = conn.execute(
                    "SELECT DISTINCT e.id, e.embedding_id FROM embedding_metadata p"
                    " CROSS JOIN embeddings e ON e.id = p.id"
                    " WHERE p.key IN ('parent_drawer_id', 'parent_entry_id')"
                    " AND p.string_value = ? AND e.segment_id = ? ORDER BY e.id",
                    (drawer_id, segment_id),
                ).fetchall()
            row_ids = [row_id for row_id, _ in selected]
            if not row_ids:
                return None
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embedding_metadata_array'"
            ).fetchone():
                for start in range(0, len(row_ids), 500):
                    chunk = row_ids[start : start + 500]
                    if conn.execute(
                        f"SELECT 1 FROM embedding_metadata_array WHERE id IN ({_marks(chunk)}) LIMIT 1",
                        chunk,
                    ).fetchone():
                        return None
            rows = _decoded_rows(conn, row_ids)
            return direct, _scalar_rows(rows)
    except (sqlite3.Error, ValueError):
        return None
