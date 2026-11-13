"""lineage.py — In-memory walks over lineage and merge edges.

The graph tools read edges with one set-based query
(:meth:`KnowledgeGraph.reachable_edges`) and then walk them here, instead of
issuing a knowledge-graph query per node. Everything in this module is pure:
it takes adjacency maps and returns plain values.

Adjacency maps go from a node to the sorted list of nodes it points at, for
one predicate: ``merged-into`` (a node and the node it was merged into) or
``synthesized-from`` (a synthesis node and the sources it was built from).
"""

from __future__ import annotations

from collections import deque


def adjacency(edges) -> dict[str, list[str]]:
    """Group ``(subject, object)`` pairs into ``subject -> sorted objects``."""
    out: dict[str, set[str]] = {}
    for subject, obj in edges:
        out.setdefault(subject, set()).add(obj)
    return {node: sorted(targets) for node, targets in out.items()}


def canonical_chain(node: str, merged_into: dict[str, list[str]], max_hops: int):
    """Follow ``merged-into`` links from ``node`` to its canonical node.

    A node with more than one current ``merged-into`` link follows the first
    in sorted order, so the result does not depend on insertion order.

    Returns ``(chain, cycle)``. ``chain`` starts at ``node`` and ends at the
    canonical node. ``cycle`` is ``None``, or the chain extended by the node
    that closed a loop.
    """
    chain = [node]
    seen = {node}
    current = node
    for _ in range(max_hops):
        targets = merged_into.get(current)
        if not targets:
            break
        nxt = targets[0]
        if nxt in seen:
            return chain, chain + [nxt]
        chain.append(nxt)
        seen.add(nxt)
        current = nxt
    return chain, None


def ancestors(node: str, parents: dict[str, list[str]], max_depth: int) -> set[str]:
    """All ``synthesized-from`` ancestors of ``node`` within ``max_depth`` hops."""
    seen = {node}
    found: set[str] = set()
    queue = deque([(node, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for parent in parents.get(current, ()):
            if parent in seen:
                continue
            seen.add(parent)
            found.add(parent)
            queue.append((parent, depth + 1))
    return found


def heights(parents: dict[str, list[str]], nodes) -> dict[str, int]:
    """Longest ``synthesized-from`` path from each node down to a source leaf.

    A leaf (no sources) has height 0. An edge that would close a cycle counts
    as reaching a leaf, which is what a depth-first walk that stops at a node
    already on its path gives. Iterative, so a deep lineage cannot hit the
    recursion limit.
    """
    memo: dict[str, int] = {}
    for start in nodes:
        if start in memo:
            continue
        on_path = {start}
        stack = [(start, iter(parents.get(start, ())), 0)]
        while stack:
            current, it, best = stack[-1]
            advanced = False
            for parent in it:
                if parent in on_path:
                    best = max(best, 1)
                    continue
                if parent in memo:
                    best = max(best, memo[parent] + 1)
                    continue
                stack[-1] = (current, it, best)
                on_path.add(parent)
                stack.append((parent, iter(parents.get(parent, ())), 0))
                advanced = True
                break
            if advanced:
                continue
            stack.pop()
            on_path.discard(current)
            memo[current] = best
            if stack:
                below, below_it, below_best = stack[-1]
                stack[-1] = (below, below_it, max(below_best, best + 1))
    return memo
