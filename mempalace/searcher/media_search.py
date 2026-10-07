"""Opt-in retrieval across compatible text/code and local media vectors."""

from __future__ import annotations

import math
from pathlib import Path

from ..backends.base import CollectionNotInitializedError, PalaceNotFoundError
from ..config import MempalaceConfig
from ..date_window import filed_at_in_window, parse_window
from ..palace import get_collection, resolve_backend_name


def search_with_media(
    query: str,
    palace_path: str,
    *,
    include_media: bool = False,
    query_task: str = "search",
    n_results: int = 5,
    wing=None,
    room=None,
    source_file=None,
    since=None,
    before=None,
    max_distance: float = 0.0,
    collection_name=None,
) -> dict:
    """Merge candidates by cosine distance in one verified embedding space.

    This experimental path uses vector ranking throughout. Drawer-only
    default search retains its existing hybrid ranking and result schema.
    Missing asset files remain searchable references with ``available=False``.
    """
    from ..palace import ASSETS_COLLECTION_NAME
    from ..embedding import embedding_section, get_embedding_function
    from . import (
        _distance_to_similarity,
        _result_date_fields,
        _search_result_envelope,
        build_where_filter,
    )

    try:
        if MempalaceConfig().embedding_model != "embeddinggemma2":
            raise ValueError("Media and code task search require embedding_model='embeddinggemma2'")
        if query_task not in {"search", "code"}:
            raise ValueError("query_task must be 'search' or 'code'")
        if not isinstance(include_media, bool):
            raise ValueError("include_media must be a boolean")
        if (
            isinstance(n_results, bool)
            or not isinstance(n_results, int)
            or not 1 <= n_results <= 100
        ):
            raise ValueError("n_results must be an integer between 1 and 100")
        since_dt, before_dt = parse_window(since, before)
        date_active = since_dt is not None or before_dt is not None
        pool_size = min(5000 if date_active else 500, n_results * (100 if date_active else 4))
        where = build_where_filter(wing, room, source_file)
        collections = []
        names = [("text", collection_name)]
        if include_media:
            names.append(("asset", ASSETS_COLLECTION_NAME))
        for result_type, name in names:
            if resolve_backend_name(palace_path) == "chroma":
                from ..backends.chroma import hnsw_capacity_status

                probe_name = name or MempalaceConfig().collection_name
                if hnsw_capacity_status(palace_path, probe_name).get("diverged"):
                    raise ValueError(
                        f"Vector index for {probe_name} requires repair before media/code search"
                    )
            try:
                col = get_collection(
                    palace_path, collection_name=name, create=False, read_only=True
                )
            except (CollectionNotInitializedError, PalaceNotFoundError):
                continue
            if col.distance_metric != "cosine":
                raise ValueError(
                    "Unified media/code search requires cosine collections; rebuild the index"
                )
            collections.append((result_type, col))
        if not collections:
            raise ValueError("No text or media index found; mine the sandbox corpus first")
        with embedding_section():
            ef = get_embedding_function()
            query_vectors = (
                ef.embed_code_query([query]) if query_task == "code" else ef.embed_query([query])
            )
        candidates = []
        fetched = 0
        pool_full = False
        for result_type, col in collections:
            count = col.count()
            if not count:
                continue
            raw = col.query(
                query_embeddings=query_vectors,
                n_results=min(pool_size, count),
                where=where or None,
                include=["documents", "metadatas", "distances"],
            )
            ids = raw["ids"][0]
            fetched += len(ids)
            pool_full = pool_full or len(ids) >= pool_size
            for record_id, text, metadata, distance in zip(
                ids, raw["documents"][0], raw["metadatas"][0], raw["distances"][0]
            ):
                metadata = metadata or {}
                if not math.isfinite(distance):
                    continue
                if max_distance and distance > max_distance:
                    continue
                if date_active and not filed_at_in_window(
                    metadata.get("filed_at"), since_dt, before_dt
                ):
                    continue
                path = metadata.get("source_path", metadata.get("source_file", ""))
                hit = {
                    "result_type": result_type,
                    "text": text or "",
                    "wing": metadata.get("wing", "unknown"),
                    "room": metadata.get("room", "unknown"),
                    "source_path": path,
                    "source_file": Path(path).name if path else "?",
                    "similarity": round(_distance_to_similarity(distance, "cosine"), 3),
                    "distance": round(distance, 4),
                    "matched_via": "shared_vector",
                    **_result_date_fields(metadata),
                }
                if result_type == "asset":
                    hit.update(
                        asset_id=record_id,
                        media_type=metadata.get("media_type"),
                        mime_type=metadata.get("mime_type"),
                        path=path,
                        title=metadata.get("title", Path(path).name),
                        project=metadata.get("project"),
                        drawer_id=metadata.get("related_drawer_id") or None,
                        available=Path(path).is_file(),
                        embedding_identity=metadata.get("embedding_identity"),
                        embedding_dimension=metadata.get("embedding_dimension"),
                    )
                    for key in ("duration", "segment_start", "segment_end"):
                        if key in metadata:
                            hit[key] = metadata[key]
                else:
                    hit["drawer_id"] = metadata.get("drawer_id") or record_id
                candidates.append((distance, record_id, hit))
        candidates.sort(key=lambda item: (item[0], item[1]))
        result = _search_result_envelope(
            query=query,
            wing=wing,
            room=room,
            source_file=source_file,
            since=since,
            before=before,
            hits=[item[2] for item in candidates[:n_results]],
            candidates_fetched=fetched,
            pool_size=pool_size,
            date_window_active=date_active and pool_full,
        )
        result["ranking"] = "shared_cosine"
        result["query_task"] = query_task
        return result
    except Exception as exc:
        return {
            "error": str(exc),
            "hint": "Use the isolated embeddinggemma2 palace and compatible indexes.",
        }
