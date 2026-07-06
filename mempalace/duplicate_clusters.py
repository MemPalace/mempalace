"""Read-only discovery of near-duplicate logical drawers."""

from collections import defaultdict

DEFAULT_THRESHOLD = 0.15
FIND_DUPLICATES_INITIAL_K = 32
FIND_DUPLICATES_MAX_NEIGHBORS = 512


def _logical_drawer_id(row_id, metadata):
    metadata = metadata if isinstance(metadata, dict) else {}
    parent_id = metadata.get("parent_drawer_id")
    if isinstance(parent_id, str) and parent_id.strip():
        return parent_id
    return row_id


def _scope_where(wing=None, room=None):
    where = {}
    if wing:
        where["wing"] = wing
    if room:
        where["room"] = room
    return where or None


def _fetch_duplicate_rows(col, where=None, batch_size=1000):
    rows = []
    offset = 0
    while True:
        batch = col.get(
            where=where,
            include=["documents", "metadatas"],
            limit=batch_size,
            offset=offset,
        )
        ids = batch.get("ids") or []
        if not ids:
            break
        documents = batch.get("documents") or [None] * len(ids)
        metadatas = batch.get("metadatas") or [{}] * len(ids)
        for row_id, document, metadata in zip(ids, documents, metadatas):
            rows.append(
                {
                    "row_id": row_id,
                    "document": document,
                    "metadata": metadata if isinstance(metadata, dict) else {},
                    "logical_id": _logical_drawer_id(row_id, metadata),
                }
            )
        if len(ids) < batch_size:
            break
        offset += len(ids)
    return rows


def _represent_logical_drawers(rows):
    by_logical = {}
    row_to_logical = {}
    for item in rows:
        row_to_logical[item["row_id"]] = item["logical_id"]
        document = item.get("document") or ""
        if not document:
            continue
        current = by_logical.get(item["logical_id"])
        if current is None or len(document) > len(current.get("document") or ""):
            by_logical[item["logical_id"]] = item
    return list(by_logical.values()), row_to_logical


def _component_root(parent, node):
    while parent[node] != node:
        parent[node] = parent[parent[node]]
        node = parent[node]
    return node


def _component_union(parent, a, b):
    root_a = _component_root(parent, a)
    root_b = _component_root(parent, b)
    if root_a != root_b:
        parent[root_b] = root_a


def find_duplicate_clusters(
    col,
    *,
    wing=None,
    room=None,
    threshold=DEFAULT_THRESHOLD,
    max_clusters=None,
    initial_k=FIND_DUPLICATES_INITIAL_K,
    max_neighbors=FIND_DUPLICATES_MAX_NEIGHBORS,
):
    """Return read-only connected components of near-duplicate logical drawers.

    Candidate rows are fetched through the backend interface and collapsed by
    ``parent_drawer_id`` before clustering, so chunks from the same logical
    drawer never duplicate each other. Neighbor search grows K while the
    boundary result is still under ``threshold`` and stops at
    ``min(physical_rows, max_neighbors)``; dense duplicate regions beyond that
    bound are intentionally capped to keep the read-only tool predictable.
    """
    threshold = float(threshold)
    if max_clusters is not None:
        max_clusters = int(max_clusters)
    where = _scope_where(wing=wing, room=room)
    rows = _fetch_duplicate_rows(col, where=where)
    candidates, row_to_logical = _represent_logical_drawers(rows)
    neighbor_bound = min(len(rows), int(max_neighbors)) if rows else 0
    params = {
        "wing": wing,
        "room": room,
        "threshold": threshold,
        "max_clusters": max_clusters,
        "initial_k": int(initial_k),
        "neighbor_bound": neighbor_bound,
    }
    if len(candidates) < 2 or neighbor_bound < 2:
        return {"clusters": [], "params": params}

    parent = {item["logical_id"]: item["logical_id"] for item in candidates}
    edges = {}
    initial_k = max(2, int(initial_k))

    for item in candidates:
        query_id = item["logical_id"]
        document = item.get("document")
        if not document:
            continue
        k = min(initial_k, neighbor_bound)
        seen_query_size = None
        while k and k != seen_query_size:
            seen_query_size = k
            results = col.query(
                query_texts=[document],
                n_results=k,
                include=["distances"],
                where=where,
            )
            result_ids = (results.get("ids") or [[]])[0] or []
            distances = (results.get("distances") or [[]])[0] or []
            for neighbor_row_id, distance in zip(result_ids, distances):
                if neighbor_row_id not in row_to_logical:
                    continue
                neighbor_id = row_to_logical.get(neighbor_row_id, neighbor_row_id)
                if neighbor_row_id == item["row_id"] or neighbor_id == query_id:
                    continue
                distance = float(distance)
                if distance >= threshold:
                    continue
                a, b = sorted((query_id, neighbor_id))
                previous = edges.get((a, b))
                if previous is None or distance < previous:
                    edges[(a, b)] = distance
                if neighbor_id not in parent:
                    parent[neighbor_id] = neighbor_id
                _component_union(parent, query_id, neighbor_id)

            kth_distance = float(distances[-1]) if len(distances) >= k else None
            if kth_distance is not None and kth_distance < threshold and k < neighbor_bound:
                k = min(neighbor_bound, k * 2)
                continue
            break

    components = defaultdict(set)
    for a, b in edges:
        root = _component_root(parent, a)
        components[root].update((a, b))

    clusters = []
    for drawer_ids in components.values():
        if len(drawer_ids) < 2:
            continue
        cluster_pairs = [
            {"a": a, "b": b, "distance": distance}
            for (a, b), distance in sorted(edges.items())
            if a in drawer_ids and b in drawer_ids
        ]
        clusters.append(
            {
                "drawer_ids": sorted(drawer_ids),
                "pairs": cluster_pairs,
                "size": len(drawer_ids),
            }
        )

    clusters.sort(key=lambda cluster: (-cluster["size"], cluster["drawer_ids"]))
    truncated = False
    if max_clusters is not None:
        max_clusters = max(0, int(max_clusters))
        truncated = len(clusters) > max_clusters
        clusters = clusters[:max_clusters]

    result = {"clusters": clusters, "params": params}
    if truncated:
        result["truncated"] = True
    return result
