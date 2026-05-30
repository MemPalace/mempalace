"""Verbatim BM25 retrieval reading drawers directly from ``chroma.sqlite3``.

Extracted from :mod:`mempalace.searcher` (#1657) so the production-scale
fallback retrieval — the path that runs when the HNSW vector segment is diverged
or unloadable (#1222) — owns *all* direct ``chroma.sqlite3`` schema knowledge
behind a small interface and can be tested in isolation. The searcher now
orchestrates two retrievers: the ANN-vector path and this SQLite-exact adapter.

Interface: construct with ``(palace_path, collection_name=None)``, then call
:meth:`SqliteExactRetriever.search` → a result dict whose shape matches the
vector path so callers (and the union-merge candidate strategy) are agnostic to
which retriever produced it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


class SqliteExactRetriever:
    """BM25-only retrieval over ``chroma.sqlite3``'s FTS5 trigram index.

    Bypasses ChromaDB's Python client entirely so a corrupt vector segment
    can't segfault the MCP server. Routes through ChromaDB's own
    ``embedding_fulltext_search`` for candidate selection, then re-ranks with
    the same Okapi-BM25 the vector path uses, so the result shape matches.
    """

    def __init__(self, palace_path: str, collection_name: str | None = None):
        self.palace_path = palace_path
        self._collection_name = collection_name

    def _resolve_collection_name(self) -> str:
        if self._collection_name is not None:
            return self._collection_name
        from .config import get_configured_collection_name

        return get_configured_collection_name()

    def search(
        self,
        query: str,
        *,
        wing: str = None,
        room: str = None,
        n_results: int = 5,
        max_candidates: int = 500,
        include_internal: bool = False,
    ) -> dict:
        """Return BM25-ranked drawers for ``query`` from the SQLite layer.

        The query is split into ≥3-char trigram tokens and OR-joined for the
        FTS5 MATCH — chromadb writes the index with ``tokenize='trigram'``, so
        single-character tokens never match. When no usable token survives
        (e.g. "is a"), candidate selection falls back to the most-recent
        ``max_candidates`` rows so we still return *something* rather than
        nothing.
        """
        # Ranking primitives stay in the searcher; import lazily so this module
        # and the searcher don't form an import cycle.
        from .searcher import _bm25_scores, _tokenize

        db_path = os.path.join(self.palace_path, "chroma.sqlite3")
        if not os.path.isfile(db_path):
            return {
                "error": "No palace found",
                "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
            }
        collection_name = self._resolve_collection_name()

        def _metadata_filter_sql(row_id_expr: str) -> tuple[str, list[str]]:
            clauses = []
            params = []
            for key, value in (("wing", wing), ("room", room)):
                if not value:
                    continue
                clauses.append(
                    f"""
                    AND EXISTS (
                        SELECT 1
                        FROM embedding_metadata mf
                        WHERE mf.id = {row_id_expr}
                          AND mf.key = ?
                          AND COALESCE(
                            mf.string_value,
                            CAST(mf.int_value AS TEXT),
                            CAST(mf.float_value AS TEXT),
                            CAST(mf.bool_value AS TEXT)
                          ) = ?
                    )
                    """
                )
                params.extend([key, value])
            return "".join(clauses), params

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error as e:
            return {"error": f"sqlite open failed: {e}"}

        try:
            # FTS5 MATCH expects whitespace-separated tokens. Drop tokens
            # shorter than 3 chars (trigram tokenizer can't match them).
            tokens = [t for t in _tokenize(query) if len(t) >= 3]
            candidate_ids: list[int] = []
            use_recency_fallback = not tokens
            if tokens:
                fts_query = " OR ".join(tokens)
                filter_sql, filter_params = _metadata_filter_sql("embedding_fulltext_search.rowid")
                try:
                    rows = conn.execute(
                        f"""
                        SELECT embedding_fulltext_search.rowid
                        FROM embedding_fulltext_search
                        JOIN embeddings e ON e.id = embedding_fulltext_search.rowid
                        JOIN segments s ON e.segment_id = s.id
                        JOIN collections c ON s.collection = c.id
                        WHERE embedding_fulltext_search MATCH ?
                          AND c.name = ?
                        {filter_sql}
                        LIMIT ?
                        """,
                        (fts_query, collection_name, *filter_params, max_candidates),
                    ).fetchall()
                    candidate_ids = [r[0] for r in rows]
                except sqlite3.Error:
                    # FTS5 tokenizer mismatch or syntax error — fall through
                    # to the recency-window selector below.
                    logger.debug("FTS5 MATCH failed; using recency fallback", exc_info=True)
                    use_recency_fallback = True

            if not candidate_ids and use_recency_fallback:
                # No usable FTS tokens, or FTS itself failed — pull the most
                # recent rows for the drawers segment so we can BM25-rank
                # something rather than return empty-handed. A clean FTS miss
                # must stay empty, especially after wing/room filtering, because
                # recency fallback would return unrelated scoped drawers.
                # Wrapped in try/except because the schema may differ on legacy
                # palaces (older chromadb without ``created_at``, missing
                # ``segments`` rows after partial restore, etc.); on schema
                # mismatch we fall back to ordering by primary-key id and finally
                # to an empty result rather than letting search raise.
                try:
                    filter_sql, filter_params = _metadata_filter_sql("e.id")
                    rows = conn.execute(
                        f"""
                        SELECT e.id
                        FROM embeddings e
                        JOIN segments s ON e.segment_id = s.id
                        JOIN collections c ON s.collection = c.id
                        WHERE c.name = ?
                        {filter_sql}
                        ORDER BY e.created_at DESC
                        LIMIT ?
                        """,
                        (collection_name, *filter_params, max_candidates),
                    ).fetchall()
                    candidate_ids = [r[0] for r in rows]
                except sqlite3.Error:
                    logger.debug(
                        "recency-window query failed; trying id-ordered fallback",
                        exc_info=True,
                    )
                    try:
                        filter_sql, filter_params = _metadata_filter_sql("e.id")
                        rows = conn.execute(
                            f"""
                            SELECT e.id
                            FROM embeddings e
                            JOIN segments s ON e.segment_id = s.id
                            JOIN collections c ON s.collection = c.id
                            WHERE c.name = ?
                            {filter_sql}
                            ORDER BY e.id DESC
                            LIMIT ?
                            """,
                            (collection_name, *filter_params, max_candidates),
                        ).fetchall()
                        candidate_ids = [r[0] for r in rows]
                    except sqlite3.Error:
                        logger.debug("id-ordered fallback also failed", exc_info=True)
                        candidate_ids = []

            if not candidate_ids:
                return {
                    "query": query,
                    "filters": {"wing": wing, "room": room},
                    "total_before_filter": 0,
                    "results": [],
                    "fallback": "bm25_only_via_sqlite",
                }

            placeholders = ",".join(["?"] * len(candidate_ids))
            meta_rows = conn.execute(
                f"""
                SELECT id, key, string_value, int_value
                FROM embedding_metadata
                WHERE id IN ({placeholders})
                """,
                candidate_ids,
            ).fetchall()
        finally:
            conn.close()

        # Group metadata rows into per-drawer dicts.
        drawers: dict[int, dict] = {}
        for emb_id, key, sval, ival in meta_rows:
            d = drawers.setdefault(emb_id, {"_id": emb_id, "metadata": {}, "text": ""})
            if key == "chroma:document":
                d["text"] = sval or ""
            else:
                d["metadata"][key] = sval if sval is not None else ival

        # Apply wing/room filters in Python (FTS5 candidates may include
        # entries from other wings).
        candidates = []
        for d in drawers.values():
            meta = d["metadata"]
            if wing and meta.get("wing") != wing:
                continue
            if room and meta.get("room") != room:
                continue
            full_source = meta.get("source_file", "") or ""
            candidates.append(
                {
                    "text": d["text"],
                    "wing": meta.get("wing", "unknown"),
                    "room": meta.get("room", "unknown"),
                    "source_file": Path(full_source).name if full_source else "?",
                    "created_at": meta.get("filed_at", "unknown"),
                    # No vector distance available in BM25-only mode.
                    "similarity": None,
                    "distance": None,
                    "matched_via": "bm25_sqlite",
                    # Internal: full path + chunk_index let callers (notably
                    # candidate_strategy="union") dedupe at chunk granularity
                    # rather than basename — two files in different directories
                    # may share a basename, and one source_file is split across
                    # multiple chunks. Stripped before this helper returns.
                    "_source_file_full": full_source,
                    "_chunk_index": meta.get("chunk_index"),
                }
            )

        # Local BM25 over the candidate set.
        docs = [c["text"] for c in candidates]
        bm25_raw = _bm25_scores(query, docs)
        max_bm25 = max(bm25_raw) if bm25_raw else 0.0
        for c, raw in zip(candidates, bm25_raw):
            c["bm25_score"] = round(raw, 3)
            c["_score"] = (raw / max_bm25) if max_bm25 > 0 else 0.0
        candidates.sort(key=lambda c: c["_score"], reverse=True)
        hits = candidates[:n_results]
        for h in hits:
            h.pop("_score", None)
            # Strip internal fields by default so the public BM25-only fallback
            # response stays clean. Callers that need chunk-precise dedup
            # (notably the union-merge path) opt in via include_internal.
            if not include_internal:
                h.pop("_source_file_full", None)
                h.pop("_chunk_index", None)

        return {
            "query": query,
            "filters": {"wing": wing, "room": room},
            "total_before_filter": len(candidates),
            "results": hits,
            "fallback": "bm25_only_via_sqlite",
            "fallback_reason": "vector_search_disabled",
        }
