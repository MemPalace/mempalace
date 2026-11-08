"""Local lexical candidates for facts; source text stays in its original store."""

import json
import logging

from . import fact_provenance

FALLBACK_SEED_MULTIPLIER = 20
SELECTIVE_SCOPE_LIMIT = 1000
COMMON_SCOPE_LIMIT = 5000


def _drop_triggers(conn):
    for name in ("insert", "update", "delete", "rename"):
        conn.execute("DROP TRIGGER IF EXISTS fact_search_" + name)


def try_install(conn):
    """An optional index must never prevent ordinary KG reads or writes.

    A failed atomic build leaves user data untouched. Disable any old index
    hooks too, so writes can proceed even if the FTS module is unavailable.
    Missing hooks ensure the next graph open retries the build.
    """
    try:
        install(conn)
        return True
    except Exception:
        _drop_triggers(conn)
        logging.getLogger(__name__).warning(
            "Fact search index unavailable; will retry on next graph open"
        )
        return False


def install(conn):
    """Atomically build or upgrade the derived index without changing facts.

    FTS document IDs come from an INTEGER PRIMARY KEY mapping to the stable
    triple ID. VACUUM can renumber triples.rowid without affecting this index.
    """
    ready = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE "
        "(type='table' AND name IN ('fact_search', 'fact_search_keys', 'fact_search_vocab')) "
        "OR (type='trigger' AND name IN "
        "('fact_search_insert', 'fact_search_update', 'fact_search_delete', 'fact_search_rename')) "
        "OR (type='index' AND name='idx_fact_search_key')"
    ).fetchone()[0]
    if ready == 8:
        return
    conn.execute("SAVEPOINT fact_search_install")
    try:
        _create(conn)
    except Exception:
        conn.execute("ROLLBACK TO fact_search_install")
        raise
    finally:
        conn.execute("RELEASE fact_search_install")


def _create(conn):
    _drop_triggers(conn)
    for name in ("fact_search_vocab", "fact_search", "fact_search_keys"):
        conn.execute("DROP TABLE IF EXISTS " + name)
    conn.execute(
        "CREATE TABLE fact_search_keys (docid INTEGER PRIMARY KEY, triple_id TEXT NOT NULL UNIQUE)"
    )
    conn.execute("INSERT INTO fact_search_keys(triple_id) SELECT id FROM triples")
    conn.execute(
        "CREATE VIRTUAL TABLE fact_search USING fts5("
        "triple_id UNINDEXED, subject, predicate, object, "
        "tokenize='unicode61 remove_diacritics 0')"
    )
    conn.execute("CREATE VIRTUAL TABLE fact_search_vocab USING fts5vocab(fact_search, 'row')")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_search_key ON triples(subject, predicate)")
    select = (
        "SELECT k.docid, t.id, s.name, replace(t.predicate, '_', ' '), o.name "
        "FROM triples t JOIN fact_search_keys k ON k.triple_id=t.id "
        "JOIN entities s ON s.id=t.subject JOIN entities o ON o.id=t.object"
    )
    insert = "INSERT OR REPLACE INTO fact_search(rowid, triple_id, subject, predicate, object) "
    conn.execute(insert + select)
    for event, before in (
        ("INSERT", ""),
        (
            "UPDATE OF id, subject, predicate, object",
            "DELETE FROM fact_search WHERE rowid=(SELECT docid FROM fact_search_keys WHERE triple_id=old.id); "
            "DELETE FROM fact_search_keys WHERE triple_id=old.id AND old.id!=new.id; ",
        ),
    ):
        name = "insert" if event == "INSERT" else "update"
        conn.execute(
            f"CREATE TRIGGER fact_search_{name} AFTER {event} ON triples BEGIN "
            + before
            + "INSERT INTO fact_search_keys(triple_id) SELECT new.id WHERE NOT EXISTS "
            "(SELECT 1 FROM fact_search_keys WHERE triple_id=new.id); "
            + insert
            + select
            + " WHERE t.id=new.id; END"
        )
    conn.execute(
        "CREATE TRIGGER fact_search_delete AFTER DELETE ON triples BEGIN "
        "DELETE FROM fact_search WHERE rowid=(SELECT docid FROM fact_search_keys WHERE triple_id=old.id); "
        "DELETE FROM fact_search_keys WHERE triple_id=old.id; END"
    )
    conn.execute(
        "CREATE TRIGGER fact_search_rename AFTER UPDATE OF name ON entities "
        "WHEN old.name IS NOT new.name BEGIN "
        "DELETE FROM fact_search WHERE rowid IN "
        "(SELECT k.docid FROM fact_search_keys k JOIN triples t ON t.id=k.triple_id "
        "WHERE t.subject=new.id OR t.object=new.id); "
        + insert
        + select
        + " WHERE t.subject=new.id OR t.object=new.id; END"
    )


def candidates(
    conn,
    tokens,
    visible_sql,
    visible_params,
    temporal_sql="",
    temporal_params=(),
    *,
    limit,
    common_term_fraction,
):
    """One SQL query: distinct BM25 seed keys, scoped history, sources and IDF.

    Selective terms start with BM25 seeds; refills use recent scoped matches.
    Small scopes use bounded FTS rowids when cheaper than following matches.
    Wide scopes check each FTS hit by primary key. History remains uncapped.
    """
    words = sorted(tokens)
    columns = (
        "source_thread",
        "source_seq_start",
        "source_seq_end",
        "source_drawer_id",
        "source_file",
        "added_at",
        *fact_provenance.COLUMNS,
    )
    source_json = ", ".join(f"'{key}', fs.{key}" for key in columns)
    scoped = bool(visible_sql or temporal_sql)
    scope_sql = visible_sql + temporal_sql
    scope_params = [*visible_params, *temporal_params]
    scope_cte = ""
    params = [*words, common_term_fraction]
    scope_filter, scope_gate = "", "1"
    scope_exists = "1"
    if scoped:
        scope_cte = (
            "scope_bound AS MATERIALIZED (SELECT CASE "
            "WHEN EXISTS (SELECT 1 FROM selective_terms) "
            "AND (SELECT documents FROM match_estimate) > ? "
            "THEN min(?, ?) ELSE ? END AS max_rows), "
            "scope_size AS MATERIALIZED (SELECT count(*) AS size FROM "
            "(SELECT 1 FROM triples t WHERE 1"
            + scope_sql
            + " LIMIT min((SELECT max_rows + 1 FROM scope_bound), "
            "COALESCE((SELECT documents FROM match_estimate), 0) + 1))), "
        )
        params.extend(
            [
                limit * FALLBACK_SEED_MULTIPLIER,
                limit,
                SELECTIVE_SCOPE_LIMIT,
                COMMON_SCOPE_LIMIT,
                *scope_params,
            ]
        )
        scope_exists = "(SELECT size FROM scope_size) > 0"
        scope_filter = scope_sql
        small_gate = (
            "(SELECT size FROM scope_size) BETWEEN 1 AND (SELECT max_rows FROM scope_bound) "
            "AND (SELECT size FROM scope_size) < (SELECT documents FROM match_estimate)"
        )
        scope_gate = "NOT (" + small_gate + ")"
    match_sql = (
        "SELECT t.subject, t.predicate, fact_search.triple_id, "
        "fact_search.rank AS relevance, fact_search.rowid AS docid FROM fact_search "
        "JOIN triples t ON t.id=fact_search.triple_id "
        "WHERE fact_search MATCH COALESCE("
        "(SELECT group_concat('\"'||term||'\"', ' OR ') FROM chosen_terms), '\"\"')"
    )
    if scoped:
        # Bound the rowid range and test membership without restarting FTS
        # for each id: those restarts recompute BM25's global term counts.
        # Only small scopes materialize ids; wide scopes take the PK path.
        scope_cte += (
            "small_docids AS MATERIALIZED (SELECT k.docid FROM triples t "
            "JOIN fact_search_keys k ON k.triple_id=t.id WHERE 1" + scope_sql + " "
            "LIMIT CASE WHEN "
            + small_gate
            + " AND "
            + scope_exists
            + " THEN (SELECT max_rows FROM scope_bound) ELSE 0 END), "
            "small_matches AS MATERIALIZED ("
            + match_sql
            + " AND fact_search.rowid >= (SELECT min(docid) FROM small_docids) "
            "AND fact_search.rowid <= (SELECT max(docid) FROM small_docids) "
            "AND +fact_search.rowid IN (SELECT docid FROM small_docids) "
            "LIMIT (SELECT count(*) FROM small_docids)), "
        )
        params.extend(scope_params)

    def seed_query(order, term_gate, seed_limit):
        queries = []
        if scoped:
            small_order = "relevance, docid" if order == "fact_search.rank" else "docid DESC"
            queries.append(
                "SELECT * FROM (SELECT * FROM small_matches ORDER BY "
                + small_order
                + " LIMIT CASE WHEN "
                + term_gate
                + " THEN ? ELSE 0 END)"
            )
            params.append(seed_limit)
        queries.append(
            "SELECT * FROM ("
            + match_sql
            + scope_filter
            + " ORDER BY "
            + order
            + " LIMIT CASE WHEN "
            + term_gate
            + " AND "
            + scope_gate
            + " AND "
            + scope_exists
            + " THEN ? ELSE 0 END)"
        )
        if scoped:
            params.extend(scope_params)
        params.append(seed_limit)
        return " UNION ALL ".join(queries)

    selective = "EXISTS (SELECT 1 FROM selective_terms)"
    ranked_sql = seed_query("fact_search.rank", selective, limit)
    params.append(limit)
    recent_sql = seed_query(
        "fact_search.rowid DESC",
        "(NOT " + selective + " OR (SELECT count(*) FROM first_keys) "
        "< (SELECT count(*) FROM first_matches))",
        limit * FALLBACK_SEED_MULTIPLIER,
    )
    params.extend([limit, *scope_params])
    rows = conn.execute(
        "WITH words(term) AS (VALUES " + ",".join("(?)" for _ in words) + "), "
        "term_stats AS MATERIALIZED ("
        "SELECT words.term, COALESCE(v.doc, 0) AS doc FROM words "
        "LEFT JOIN fact_search_vocab v ON v.term=words.term), "
        "stats AS MATERIALIZED ("
        "SELECT json_group_object(term, doc) AS frequencies, "
        "(SELECT count(*) FROM fact_search_keys) AS documents FROM term_stats), "
        "selective_terms AS MATERIALIZED ("
        "SELECT term FROM term_stats WHERE doc > 0 AND doc <= ?*(SELECT documents FROM stats)), "
        "chosen_terms AS MATERIALIZED (SELECT term FROM selective_terms UNION ALL "
        "SELECT term FROM term_stats WHERE doc > 0 AND NOT EXISTS (SELECT 1 FROM selective_terms)), "
        "match_estimate AS MATERIALIZED (SELECT sum(doc) AS documents FROM term_stats "
        "WHERE term IN (SELECT term FROM chosen_terms)), "
        + scope_cte
        + "ranked_seeds AS MATERIALIZED ("
        + ranked_sql
        + "), "
        "first_matches AS MATERIALIZED (SELECT subject, predicate, relevance "
        "FROM ranked_seeds LIMIT ?), "
        "first_keys AS MATERIALIZED (SELECT DISTINCT subject, predicate FROM first_matches), "
        "fallback_seeds AS MATERIALIZED (" + recent_sql + "), "
        "extra_matches AS MATERIALIZED (SELECT m.subject, m.predicate, m.relevance "
        "FROM fallback_seeds m "
        "WHERE NOT EXISTS (SELECT 1 FROM first_keys k "
        "WHERE k.subject=m.subject AND k.predicate=m.predicate)), "
        "extra_keys AS MATERIALIZED ("
        "SELECT subject, predicate FROM extra_matches GROUP BY subject, predicate "
        "ORDER BY min(relevance), subject, predicate "
        "LIMIT ? - (SELECT count(*) FROM first_keys)), "
        "keys AS MATERIALIZED (SELECT * FROM first_keys UNION ALL SELECT * FROM extra_keys) "
        "SELECT t.*, s.name AS subject_name, o.name AS object_name, "
        "stats.frequencies, stats.documents, "
        f"json_group_array(json_object({source_json})) "
        "FILTER (WHERE fs.id IS NOT NULL) AS sources_json "
        "FROM keys k JOIN triples t INDEXED BY idx_fact_search_key "
        "ON t.subject=k.subject AND t.predicate=k.predicate "
        "JOIN entities s ON s.id=t.subject JOIN entities o ON o.id=t.object "
        "CROSS JOIN stats LEFT JOIN fact_sources fs ON fs.triple_id=t.id "
        "WHERE 1" + visible_sql + temporal_sql + " GROUP BY t.id",
        params,
    ).fetchall()
    return [
        {
            **dict(row),
            "sources": json.loads(row["sources_json"]),
            "frequencies": json.loads(row["frequencies"]),
        }
        for row in rows
    ]


def snapshot_candidates(snapshot, *args, **kwargs):
    """Run the unchanged candidate rules over only authorized, copied rows."""
    import sqlite3
    from time import monotonic

    from commonplace.rules import checks

    checks.remaining(0)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    end = checks._deadline.get()
    if end is not None:
        conn.set_progress_handler(lambda: monotonic() >= end, 1000)
    try:
        for schema, _ in snapshot.values():
            checks.remaining(0)
            conn.execute(schema)
        # Install while empty, then insert in original document order. The same
        # triggers/tokenizer preserve seed ordering without global hidden terms.
        install(conn)
        for table, (_, rows) in snapshot.items():
            for row in rows:
                checks.remaining(0)
                columns = ", ".join('"' + key.replace('"', '""') + '"' for key in row)
                conn.execute(
                    f'INSERT INTO "{table}" ({columns}) VALUES ({", ".join("?" for _ in row)})',
                    tuple(row.values()),
                )
        checks.remaining(0)
        result = candidates(conn, *args, **kwargs)
        checks.remaining(0)
        return result
    finally:
        conn.close()
