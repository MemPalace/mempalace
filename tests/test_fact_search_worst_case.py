"""Scope, history and sanitization regressions for bounded fact lookup."""

import logging

import pytest

from mempalace import fact_index, mcp_server
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.searcher import facts as fact_search
from tests._mcp_server_helpers import _patch_mcp_server


def _search_steps(kg, tokens, **scope):
    steps = 0

    def progress():
        nonlocal steps
        steps += 100
        return 0

    conn = kg._conn()
    conn.set_progress_handler(progress, 100)
    try:
        rows = kg.search_fact_rows(
            tokens,
            candidate_limit=fact_search.FACT_CANDIDATE_LIMIT,
            common_term_fraction=fact_search.FACT_COMMON_TERM_FRACTION,
            **scope,
        )
    finally:
        conn.set_progress_handler(None, 0)
    return {row["id"] for row in rows}, steps


@pytest.mark.parametrize("entry", ["facts", "kg_search", "kg_query", "search"])
def test_long_query_is_sanitized_once(monkeypatch, config, kg, caplog, entry):
    _patch_mcp_server(monkeypatch, config, kg)
    kg.add_triple("Kestrel", "deploy_port", "8443", wing="shared")
    monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp_server, "search_memories", lambda *a, **kw: {"results": []})
    query = "Kestrel " + "deployment " * 20 + "port?"
    assert 200 < len(query) <= 250
    calls = []
    sanitize = fact_search.sanitize_query

    def counted(raw):
        calls.append(raw)
        return sanitize(raw)

    monkeypatch.setattr(fact_search, "sanitize_query", counted)
    monkeypatch.setattr(mcp_server, "sanitize_query", counted)
    caplog.set_level(logging.WARNING, logger="mempalace.query_sanitizer")
    if entry == "facts":
        found = fact_search.search_facts(kg, query)
    elif entry == "kg_search":
        found = mcp_server.tool_kg_search(query)["facts"]
    elif entry == "kg_query":
        found = mcp_server.tool_kg_query(query, search=True)["facts"]
    else:
        found = mcp_server.tool_search(query)["facts"]
    assert [fact["object"] for fact in found] == ["8443"]
    assert calls == [query]
    warnings = [record for record in caplog.records if "Query sanitized:" in record.message]
    assert len(warnings) == 1


def test_common_seed_prefix_is_scoped_and_selected_history_is_not_capped():
    seed_cap = fact_search.FACT_CANDIDATE_LIMIT * fact_index.FALLBACK_SEED_MULTIPLIER
    with KnowledgeGraph.empty() as kg:
        for i in range(seed_cap + 1):
            kg.add_triple("Hidden entity project", "status", f"phase {i}", wing="vault")
        history = {
            kg.add_triple(
                "Visible entity project",
                "status",
                f"phase {i}",
                wing="shared",
                valid_to="2025-01-01",
            )
            for i in range(seed_cap + 1)
        }
        current = kg.add_triple("Visible entity project", "status", "current", wing="shared")
        hidden = kg.add_triple(
            "Visible entity project", "status", "private", wing="shared", sensitivity="private"
        )
        found = fact_search.search_facts(
            kg, "entity project", wings=["shared"], sensitivities=["internal"], limit=None
        )
        assert found[0]["id"] == current
        assert {fact["id"] for fact in found} == history | {current}
        assert hidden not in {fact["id"] for fact in found}
        assert all(not fact["current"] for fact in found[1:])


def test_common_seed_sql_work_does_not_grow_with_unselected_matches():
    with KnowledgeGraph.empty() as kg:
        conn = kg._conn()

        def append(start, end):
            conn.executemany(
                "INSERT INTO entities(id,name) VALUES(?,?)",
                ((f"e{i}", f"Entity project {i}") for i in range(start, end)),
            )
            conn.executemany(
                "INSERT INTO triples(id,subject,predicate,object,wing) VALUES(?,?,?,?,?)",
                ((f"t{i}", f"e{i}", "status", "e0", "shared") for i in range(start, end)),
            )

        append(0, 2000)
        initial, initial_steps = _search_steps(kg, {"entity", "project"})
        append(2000, 10000)
        expanded, expanded_steps = _search_steps(kg, {"entity", "project"})
        assert len(initial) == fact_search.FACT_CANDIDATE_LIMIT
        assert len(expanded) == len(initial)
        assert expanded.isdisjoint(initial)  # The capped prefix keeps recent matches.
        # Count VM work, not wall time: unrelated matches must not trigger
        # another complete SQL scan.
        assert expanded_steps <= initial_steps * 1.25


@pytest.mark.parametrize("sparse", [False, True])
def test_scoped_seed_work_is_bounded_as_nonmatching_facts_grow(sparse):
    with KnowledgeGraph.empty() as kg:
        conn = kg._conn()
        conn.execute(
            "INSERT INTO entities(id,name) VALUES"
            "('target','Kestrel entity project'),('other','Unrelated entity project')"
        )
        conn.execute(
            "INSERT INTO triples(id,subject,predicate,object,wing) "
            "VALUES('target','target','deploy_port','target','shared')"
        )
        tokens = {"entity", "project"} if sparse else {"kestrel"}
        growing_wing = "vault" if sparse else "shared"

        def append(start, end):
            conn.executemany(
                "INSERT INTO triples(id,subject,predicate,object,wing) VALUES(?,?,?,?,?)",
                ((f"t{i}", "other", "is_a", "other", growing_wing) for i in range(start, end)),
            )

        append(0, 2000)
        initial, initial_steps = _search_steps(
            kg, tokens, wings=["shared"], sensitivities=["internal"]
        )
        append(2000, 10000)
        expanded, expanded_steps = _search_steps(
            kg, tokens, wings=["shared"], sensitivities=["internal"]
        )
        assert initial == expanded == {"target"}
        # Vocabulary accounting can cross FTS page boundaries; scope work
        # must stay within a fixed allowance rather than grow per fact.
        assert expanded_steps <= initial_steps + 1000


def test_common_scope_above_1000_keeps_work_bounded_as_newer_hidden_matches_grow():
    with KnowledgeGraph.empty() as kg:
        conn = kg._conn()
        conn.executemany(
            "INSERT INTO entities(id,name) VALUES(?,?)",
            ((f"e{i}", f"Entity project {i}") for i in range(1002)),
        )
        conn.executemany(
            "INSERT INTO triples(id,subject,predicate,object,wing) VALUES(?,?,?,?,?)",
            ((f"v{i}", f"e{i}", "status", "e0", "shared") for i in range(1001)),
        )

        def append(start, end):
            conn.executemany(
                "INSERT INTO triples(id,subject,predicate,object,wing) VALUES(?,?,?,?,?)",
                ((f"h{i}", "e1001", "status", "e0", "vault") for i in range(start, end)),
            )

        append(0, 2000)
        initial, initial_steps = _search_steps(
            kg, {"entity", "project"}, wings=["shared"], sensitivities=["internal"]
        )
        append(2000, 10000)
        expanded, expanded_steps = _search_steps(
            kg, {"entity", "project"}, wings=["shared"], sensitivities=["internal"]
        )
        assert len(initial) == fact_search.FACT_CANDIDATE_LIMIT
        assert initial == expanded
        # The visible docid range stays fixed as newer hidden matches grow.
        assert expanded_steps <= initial_steps + 1000


@pytest.mark.parametrize("tokens", [{"entity", "project"}, {"needle"}])
@pytest.mark.parametrize("as_of", [None, "2025-01-15"])
@pytest.mark.parametrize("wings", [["shared"], ["shared", "vault"], []])
@pytest.mark.parametrize("common_term_fraction", [0.0, 0.5])
def test_small_and_wide_scope_strategies_return_identical_rows(
    monkeypatch, tokens, as_of, wings, common_term_fraction
):
    with KnowledgeGraph.empty() as kg:
        for wing in ("shared", "vault"):
            for sensitivity in ("public", "internal", "private"):
                for i in range(3):
                    kg.add_triple(
                        f"Entity project {i}",
                        "status",
                        "needle" if i == 0 else "other",
                        wing=wing,
                        sensitivity=sensitivity,
                        valid_from="2025-01-01",
                        valid_to="2025-02-01" if i == 0 else None,
                        source_thread="source",
                    )
        # Keep enough global matches to exercise the rowid path, including
        # selective queries, while the caller's visible scope stays small.
        for i in range(12):
            kg.add_triple(f"Outside {i}", "status", "needle", wing="outside")
        for i in range(20):
            kg.add_triple(f"Filler {i}", "status", "other", wing="outside")
        results = []
        for threshold in (1000, 0):
            monkeypatch.setattr(fact_index, "SELECTIVE_SCOPE_LIMIT", threshold)
            monkeypatch.setattr(fact_index, "COMMON_SCOPE_LIMIT", threshold)
            results.append(
                kg.search_fact_rows(
                    tokens,
                    candidate_limit=fact_search.FACT_CANDIDATE_LIMIT,
                    common_term_fraction=common_term_fraction,
                    wings=wings,
                    sensitivities=["public", "internal"],
                    as_of=as_of,
                )
            )
        assert results[0] == results[1]
        assert bool(results[0]) == bool(wings)
