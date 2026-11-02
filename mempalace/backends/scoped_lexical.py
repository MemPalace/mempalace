"""One-call FTS scope: bounded term fetches and local ranking, without caches."""

import logging
import math
from contextlib import contextmanager
from heapq import nsmallest

from ..lexical import bm25_scores, index_bm25_scores, lexical_query_terms, tokenize
from ..stored_text import INDEX_SPANS, decode_record, index_text
from .base import LexicalHit, LexicalResult

logger = logging.getLogger(__name__)


class ScopedFTS:
    def __init__(self, conn, table, scope_sql, params, load_texts, hydrate):
        self.conn = conn
        self.table = table
        self.load_texts = load_texts
        self.hydrate = hydrate
        self.scope_sql, self.scope_params = scope_sql, params
        # The cutoff saturates at 40k scoped records; counting beyond that
        # cannot change the accepted terms or their ranking.
        self.count = conn.execute(
            f"SELECT count(*) FROM ({scope_sql} LIMIT 40000)", params
        ).fetchone()[0]
        if self.count <= 20000:
            conn.execute("CREATE TEMP TABLE lexical_scope(id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO lexical_scope " + scope_sql, params)

    def lengths(self, ids):
        """Read only length metadata for these already scoped candidates."""
        lengths = {}
        for start in range(0, len(ids), 900):
            chunk = ids[start : start + 900]
            marks = ",".join("?" for _ in chunk)
            if self.table == "docs_fts":
                rows = self.conn.execute(
                    f"SELECT id,sz FROM docs_fts_docsize WHERE id IN ({marks})", chunk
                )
                for row_id, encoded in rows:
                    # FTS5 stores one base-128 varint per column. Unindexed
                    # columns contribute zero; compact has just one column.
                    total = value = 0
                    for byte in encoded:
                        value = (value << 7) | (byte & 127)
                        if byte < 128:
                            total += value
                            value = 0
                    lengths[row_id] = total
            else:
                lengths.update(
                    self.conn.execute(
                        f"SELECT rowid,length(string_value) FROM {self.table} "
                        f"WHERE rowid IN ({marks})",
                        chunk,
                    )
                )
        return lengths

    def fetch(self, term, limit):
        if self.count <= 256:
            if not self.count:
                return []
            low, high = self.conn.execute("SELECT min(id),max(id) FROM lexical_scope").fetchone()
            predicate = f"rowid >= ? AND rowid <= ? AND EXISTS (SELECT 1 FROM lexical_scope WHERE lexical_scope.id={self.table}.rowid)"
            params = (low, high)
        elif self.count <= 20000:
            predicate = (
                f"EXISTS (SELECT 1 FROM lexical_scope WHERE lexical_scope.id={self.table}.rowid)"
            )
            params = ()
        else:
            predicate = f"EXISTS (SELECT 1 FROM ({self.scope_sql}) allowed WHERE allowed.id={self.table}.rowid)"
            params = self.scope_params
        return [
            row[0]
            for row in self.conn.execute(
                f"SELECT rowid FROM {self.table} WHERE {self.table} MATCH ? AND {predicate} "
                "ORDER BY rowid DESC LIMIT ?",
                (
                    '"'
                    + term.replace('"', '""')
                    + '"'
                    + ("*" if getattr(term, "prefix", False) else ""),
                    *params,
                    limit,
                ),
            )
        ]


@contextmanager
def lexical_reader(path):
    from ._inproc_sqlite import open_reader

    conn = open_reader(path)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def scope_local_search(
    collection, query, n_results, where=None, *, closet=False, stop_words=frozenset()
):
    terms, skip = lexical_query_terms(
        query,
        stop_words,
        min_length=getattr(collection, "lexical_min_token_length", 2),
        trigram=getattr(collection, "lexical_min_token_length", 2) == 3,
        closet=closet,
    )
    if skip:
        return LexicalResult(hits=[], skip_reason=skip)
    with collection.lexical_scope(where) as scope:
        cutoff = min(2000, max(1000, scope.count // 20))
        selected, ids, postings, weights = [], set(), {}, {}
        for term in terms:
            matches = scope.fetch(term, 5000 if closet else cutoff + 1)
            if closet and len(matches) >= 5000:
                # No partial closet pool: its ordering would change boosts.
                logger.debug("Closet match limit reached; boosts disabled")
                return LexicalResult(hits=[])
            if matches and (closet or len(matches) <= cutoff):
                selected.append(term)
                ids.update(matches)
                if not closet:
                    postings[term] = set(matches)
                    idf = math.log((scope.count + 1) / (len(matches) + 0.5))
                    for row_id in matches:
                        weights[row_id] = weights.get(row_id, 0.0) + idf
                if not closet and len(selected) == 8:
                    break
        # Every accepted term's complete match set is present. Drawer pools
        # contain at most 8*2000 ids; closets retain the complete capped query.
        # First stage needs only rowids. It bounds text decoding and Python
        # scoring independently of the accepted terms' full match counts.
        if not closet:
            pool_size = max(100, 10 * n_results)
            top = nsmallest(pool_size, weights, key=lambda row_id: -weights[row_id])
            boundary = min((weights[row_id] for row_id in top), default=0)
            eligible = [row_id for row_id in weights if weights[row_id] >= boundary]
            lengths = scope.lengths(eligible)
            ordered = nsmallest(
                pool_size,
                eligible,
                key=lambda row_id: (
                    -weights[row_id],
                    lengths.get(row_id, math.inf),
                    -row_id,
                ),
            )
        else:
            ordered = sorted(ids)
        evidence = (
            {
                row_id: tuple(term for term in selected if row_id in postings[term])
                for row_id in ordered
            }
            if not closet and getattr(collection, "lexical_min_token_length", 2) != 3
            else {}
        )
        hydrated = None
        if (
            not closet
            and getattr(collection, "lexical_min_token_length", 2) == 3
            and len(ordered) <= n_results
        ):
            # Every candidate already fits the requested top K. Decode once,
            # rather than loading the stored text and then decoding it again
            # during hydration (notably expensive for mirrored transcripts).
            hydrated = scope.hydrate(ordered)
            texts = {
                row_id: index_text(hydrated[row_id][1], hydrated[row_id][2], stored_form=False)
                for row_id in ordered
                if row_id in hydrated
            }
        else:
            texts = scope.load_texts(ordered)
        if closet and getattr(collection, "lexical_min_token_length", 2) == 3:
            # Preserve develop's full-query word scorer, including dropping
            # trigram substring-only matches with zero word-level signal.
            rank_query = " ".join(term[:64] for term in tokenize(query)[:256])
            scores = bm25_scores(rank_query, [texts.get(row_id, "") for row_id in ordered])
        else:
            scores = index_bm25_scores(
                selected,
                [texts.get(row_id, "") for row_id in ordered],
                [evidence[row_id] for row_id in ordered]
                if not closet and getattr(collection, "lexical_min_token_length", 2) != 3
                else None,
                trigram=getattr(collection, "lexical_min_token_length", 2) == 3,
            )
        ranked = sorted(
            ((row_id, score) for row_id, score in zip(ordered, scores) if score > 0),
            key=lambda pair: -pair[1],
        )[:n_results]
        if hydrated is None:
            hydrated = scope.hydrate([row_id for row_id, _ in ranked])
        return LexicalResult(
            hits=[
                LexicalHit(
                    id=hydrated[row_id][0],
                    document=hydrated[row_id][1],
                    metadata=hydrated[row_id][2],
                    score=score,
                    matched_terms=tuple(evidence[row_id])
                    if not closet and getattr(collection, "lexical_min_token_length", 2) != 3
                    else (),
                )
                for row_id, score in ranked
                if row_id in hydrated
            ],
            selected_terms=selected,
        )


@contextmanager
def sqlite_collection_scope(collection, where=None, *, compact=False):
    import json

    from .sqlite_exact import _equality_where_sql

    pushed = _equality_where_sql(where, locus_columns=collection._handle.has_locus_columns)
    if pushed is None:
        raise ValueError("lexical filter cannot be bounded")
    filter_sql, params = pushed
    path = collection._backend._db_path(collection._handle.palace_path)
    with lexical_reader(path) as conn:
        cid = conn.execute(
            "SELECT id FROM collections WHERE name=?", (collection._collection_name,)
        ).fetchone()[0]
        synced = conn.execute("SELECT value FROM meta WHERE key='fts5_rowid_synced'").fetchone()
        aligned = compact or (synced and synced[0] == "1")
        if aligned:
            scope_sql = (
                f"SELECT rowid AS id FROM documents WHERE collection_id=? AND ({filter_sql})"
            )
        else:
            scope_sql = (
                "SELECT docs_fts.rowid AS id FROM documents JOIN docs_fts ON documents.id=docs_fts.doc_id "
                f"AND documents.collection_id=docs_fts.collection_id WHERE documents.collection_id=? AND ({filter_sql})"
            )
        raw = {}
        decompressors = {}  # Owned by this reader and its dictionary snapshot.

        def load(ids):
            for start in range(0, len(ids), 900):
                chunk = ids[start : start + 900]
                marks = ",".join("?" for _ in chunk)
                dictionary = ",dictionary_id" if compact else ""
                if aligned:
                    rows = conn.execute(
                        f"SELECT rowid,id,document,metadata_json{dictionary} FROM documents WHERE collection_id=? AND rowid IN ({marks})",
                        (cid, *chunk),
                    )
                else:
                    rows = conn.execute(
                        "SELECT f.rowid,d.id,d.document,d.metadata_json FROM documents d JOIN docs_fts f ON d.id=f.doc_id AND d.collection_id=f.collection_id "
                        + f"WHERE d.collection_id=? AND f.rowid IN ({marks})",
                        (cid, *chunk),
                    )
                for row in rows:
                    row_id, doc_id, document, metadata = row[:4]
                    if compact:
                        document = collection._decompress(
                            conn.cursor(), document, row[4], cache=decompressors
                        )
                    raw[row_id] = (doc_id, document or "", metadata)

        def load_texts(ids):
            load(ids)
            return {
                row_id: index_text(
                    raw[row_id][1],
                    {INDEX_SPANS: json.loads(raw[row_id][2]).get(INDEX_SPANS)}
                    if INDEX_SPANS in raw[row_id][2]
                    else {},
                )
                for row_id in ids
                if row_id in raw
            }

        def hydrate(ids):
            return {
                row_id: (raw[row_id][0], *decode_record(raw[row_id][1], json.loads(raw[row_id][2])))
                for row_id in ids
                if row_id in raw
            }

        yield ScopedFTS(conn, "docs_fts", scope_sql, (cid, *params), load_texts, hydrate)
