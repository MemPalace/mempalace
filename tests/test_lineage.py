"""Lineage walks: the in-memory algorithms and the KnowledgeGraph walks under them."""

import random
from collections import deque

import pytest

from mempalace import lineage


# ── mempalace.lineage ─────────────────────────────────────────────────────


def test_canonical_chain_follows_first_sorted_link():
    merged = lineage.adjacency([("c", "b"), ("b", "a"), ("b", "z")])
    chain, cycle = lineage.canonical_chain("c", merged, max_hops=10)
    assert chain == ["c", "b", "a"]
    assert cycle is None


def test_canonical_chain_reports_the_loop():
    merged = lineage.adjacency([("a", "b"), ("b", "c"), ("c", "a")])
    chain, cycle = lineage.canonical_chain("a", merged, max_hops=10)
    assert cycle == ["a", "b", "c", "a"]


def test_canonical_chain_stops_at_max_hops():
    merged = lineage.adjacency([(str(i), str(i + 1)) for i in range(10)])
    chain, cycle = lineage.canonical_chain("0", merged, max_hops=3)
    assert chain == ["0", "1", "2", "3"]
    assert cycle is None


def test_heights_take_the_longest_path_to_a_leaf():
    parents = lineage.adjacency([("n2", "n1"), ("n2", "s3"), ("n1", "s1"), ("n1", "s2")])
    assert lineage.heights(parents, ["n2"]) == {"n2": 2, "n1": 1, "s3": 0, "s1": 0, "s2": 0}


def test_heights_survive_a_lineage_deeper_than_the_recursion_limit():
    depth = 5000
    parents = lineage.adjacency([(f"n{i + 1}", f"n{i}") for i in range(depth)])
    assert lineage.heights(parents, [f"n{depth}"])[f"n{depth}"] == depth


def test_heights_count_a_cycle_edge_as_reaching_a_leaf():
    parents = lineage.adjacency([("a", "b"), ("b", "a")])
    assert lineage.heights(parents, ["a"])["a"] == 2


def test_ancestors_respect_max_depth():
    parents = lineage.adjacency([("d", "c"), ("c", "b"), ("b", "a")])
    assert lineage.ancestors("d", parents, max_depth=2) == {"c", "b"}
    assert lineage.ancestors("d", parents, max_depth=10) == {"c", "b", "a"}


# ── KnowledgeGraph walks ──────────────────────────────────────────────────


def _bfs_reference(kg, entity, direction, predicate, max_depth):
    """The per-node walk #1850 first shipped: one query_entity() per node."""
    queue = deque([(entity, 0)])
    seen_nodes = {entity}
    seen, facts = set(), []
    while queue:
        current, depth = queue.popleft()
        for fact in kg.query_entity(current, direction=direction):
            if predicate and fact["predicate"] != predicate:
                continue
            key = (fact["subject"], fact["predicate"], fact["object"], fact["valid_from"])
            if key not in seen:
                seen.add(key)
                facts.append((key, depth))
            if depth >= max_depth:
                continue
            for neighbor in ([fact["object"]] if direction in ("outgoing", "both") else []) + (
                [fact["subject"]] if direction in ("incoming", "both") else []
            ):
                if neighbor not in seen_nodes:
                    seen_nodes.add(neighbor)
                    queue.append((neighbor, depth + 1))
    return sorted(facts), len(seen_nodes)


@pytest.mark.parametrize("direction", ["outgoing", "incoming", "both"])
@pytest.mark.parametrize("predicate", [None, "p1"])
def test_traverse_matches_the_per_node_walk(kg, direction, predicate):
    rng = random.Random(7)
    nodes = [f"n{i}" for i in range(60)]
    for _ in range(180):
        kg.add_triple(rng.choice(nodes), rng.choice(["p1", "p2"]), rng.choice(nodes))

    walk = kg.traverse("n0", direction=direction, predicate=predicate, max_depth=3)
    got = sorted(
        ((f["subject"], f["predicate"], f["object"], f["valid_from"]), f["depth"])
        for f in walk["facts"]
    )
    expected, visited = _bfs_reference(kg, "n0", direction, predicate, max_depth=3)
    assert got == expected
    assert walk["visited_nodes"] == visited


def test_traverse_caps_the_facts_it_returns(kg):
    for i in range(50):
        kg.add_triple("hub", "links", f"leaf{i}")
    walk = kg.traverse("hub", direction="outgoing", max_facts=10)
    assert len(walk["facts"]) == 10
    assert walk["truncated"] is True


def test_reachable_edges_follows_current_links_only(kg):
    kg.add_triple("c", "synthesized-from", "b")
    kg.add_triple("b", "synthesized-from", "a")
    kg.add_triple("b", "synthesized-from", "old")
    kg.invalidate("b", "synthesized-from", "old", ended="2026-01-01")
    kg.add_triple("b", "related-to", "noise")

    edges, names = kg.reachable_edges(["c"], "synthesized-from")
    assert sorted(edges) == [("b", "a"), ("c", "b")]
    assert names["a"] == "a"


def test_reachable_edges_use_exact_ids_not_near_matches(kg):
    kg.add_triple("drawer_one_two", "merged-into", "drawer_target")
    edges, _names = kg.reachable_edges(["drawer_one"], "merged-into")
    assert edges == []


def test_entities_in_triples(kg):
    kg.add_triple("a", "p", "b")
    kg.add_entity("lonely")
    assert kg.entities_in_triples(["a", "b", "lonely", "missing"]) == {"a", "b"}


def test_apply_merge_is_one_transaction(kg):
    kg.add_triple("src", "synthesized-from", "s1", valid_from="2026-06-01")
    kg.add_triple("src", "merged-into", "elsewhere")

    # Ending the lineage link before it started is refused; nothing changes,
    # including the merged-into links that would have been handled first.
    with pytest.raises(ValueError):
        kg.apply_merge("src", "dst", ended="2026-01-01")
    current = {(f["predicate"], f["object"]) for f in kg.query_entity("src") if f["current"]}
    assert current == {("synthesized-from", "s1"), ("merged-into", "elsewhere")}

    result = kg.apply_merge("src", "dst", ended="2026-07-01")
    assert result == {
        "ended": "2026-07-01",
        "merged_edge_added": True,
        "invalidated_prior_merged_into": 1,
        "invalidated_lineage_edges": 1,
    }
    current = {(f["predicate"], f["object"]) for f in kg.query_entity("src") if f["current"]}
    assert current == {("merged-into", "dst")}
