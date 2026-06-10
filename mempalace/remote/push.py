"""Push a palace wing to MinIO for team sharing."""

import io
import json
import logging
from datetime import datetime, timezone

from ..config import MempalaceConfig
from ..knowledge_graph import KnowledgeGraph
from ..palace import get_collection
from .minio_client import ensure_bucket, get_minio_client

logger = logging.getLogger(__name__)


def _get_wing_drawers(col, wing):
    """Yield all drawers for a wing in paginated batches."""
    batch_size = 1000
    offset = 0
    while True:
        result = col.get(
            where={"wing": wing},
            include=["documents", "metadatas"],
            limit=batch_size,
            offset=offset,
        )
        if not result["ids"]:
            break
        for i, drawer_id in enumerate(result["ids"]):
            meta = result["metadatas"][i] or {}
            yield {
                "id": drawer_id,
                "content": result["documents"][i],
                "room": meta.get("room", ""),
                "metadata": {k: v for k, v in meta.items() if k not in ("wing",)},
            }
        if len(result["ids"]) < batch_size:
            break
        offset += batch_size


def _get_wing_kg_facts(config, wing):
    """Get KG triples where subject or object matches the wing name."""
    kg = KnowledgeGraph(db_path=config.kg_path if hasattr(config, "kg_path") else None)
    try:
        facts = []
        for direction in ("outgoing", "incoming"):
            results = kg.query_entity(wing, direction=direction)
            for row in results:
                facts.append(
                    {
                        "subject": row["subject"],
                        "predicate": row["predicate"],
                        "object": row["object"],
                        "valid_from": row.get("valid_from"),
                        "valid_to": row.get("valid_to"),
                    }
                )
        seen = set()
        deduped = []
        for f in facts:
            key = (f["subject"], f["predicate"], f["object"])
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        return deduped
    finally:
        kg.close()


def push_wing(wing: str, config: MempalaceConfig = None) -> dict:
    """Push all drawers and KG facts for a wing to MinIO.

    Layout on MinIO:
        bucket/<wing>/<user_id>/manifest.json
        bucket/<wing>/<user_id>/drawers.jsonl
        bucket/<wing>/<user_id>/kg_facts.json
    """
    if config is None:
        config = MempalaceConfig()

    user_id = config.user_id
    if not user_id:
        return {
            "error": "user_id not configured. "
            "Set 'user_id' in ~/.mempalace/config.json or MEMPALACE_USER_ID env var."
        }

    try:
        client = get_minio_client(config)
    except (ImportError, ValueError) as e:
        return {"error": str(e)}

    col = get_collection(config.palace_path)
    if col is None:
        return {"error": "Cannot open palace. Check palace_path configuration."}

    all_meta = col.get(where={"wing": wing}, include=[], limit=1)
    if not all_meta["ids"]:
        return {"error": f"Wing '{wing}' not found in palace."}

    bucket = config.minio_bucket
    try:
        ensure_bucket(client, bucket)
    except Exception as e:
        return {"error": f"Cannot access MinIO bucket '{bucket}': {e}"}

    prefix = f"{wing}/{user_id}"
    timestamp = datetime.now(timezone.utc).isoformat()

    drawers_buffer = io.BytesIO()
    drawer_count = 0
    for drawer in _get_wing_drawers(col, wing):
        line = json.dumps(drawer, ensure_ascii=False) + "\n"
        drawers_buffer.write(line.encode("utf-8"))
        drawer_count += 1

    kg_facts = _get_wing_kg_facts(config, wing)

    manifest = {
        "wing": wing,
        "user_id": user_id,
        "timestamp": timestamp,
        "drawer_count": drawer_count,
        "kg_fact_count": len(kg_facts),
        "version": "1.0",
    }

    try:
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        client.put_object(
            bucket,
            f"{prefix}/manifest.json",
            io.BytesIO(manifest_bytes),
            len(manifest_bytes),
            content_type="application/json",
        )

        drawers_bytes = drawers_buffer.getvalue()
        client.put_object(
            bucket,
            f"{prefix}/drawers.jsonl",
            io.BytesIO(drawers_bytes),
            len(drawers_bytes),
            content_type="application/x-ndjson",
        )

        kg_bytes = json.dumps(kg_facts, indent=2, ensure_ascii=False).encode("utf-8")
        client.put_object(
            bucket,
            f"{prefix}/kg_facts.json",
            io.BytesIO(kg_bytes),
            len(kg_bytes),
            content_type="application/json",
        )
    except Exception as e:
        return {"error": f"Failed to upload to MinIO: {e}"}

    logger.info(
        "Pushed wing '%s' as user '%s': %d drawers, %d KG facts",
        wing,
        user_id,
        drawer_count,
        len(kg_facts),
    )

    return {
        "success": True,
        "wing": wing,
        "user_id": user_id,
        "bucket": bucket,
        "object_prefix": prefix,
        "drawer_count": drawer_count,
        "kg_fact_count": len(kg_facts),
        "timestamp": timestamp,
    }
