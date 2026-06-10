"""Pull a palace wing from MinIO (all users) and merge into local palace."""

import json
import logging
from datetime import datetime, timezone

from ..config import MempalaceConfig
from ..ids import make_drawer_id_from_content
from ..knowledge_graph import KnowledgeGraph
from ..palace import get_collection
from .minio_client import get_minio_client

logger = logging.getLogger(__name__)


def _discover_users(client, bucket, wing):
    """List all user prefixes under bucket/<wing>/."""
    prefix = f"{wing}/"
    users = set()
    for obj in client.list_objects(bucket, prefix=prefix, recursive=False):
        name = obj.object_name
        if name.endswith("/"):
            user_id = name[len(prefix) :].rstrip("/")
            if user_id:
                users.add(user_id)
    return sorted(users)


def _download_json(client, bucket, object_name):
    """Download and parse a JSON object. Returns None on failure."""
    try:
        response = client.get_object(bucket, object_name)
        data = json.loads(response.read().decode("utf-8"))
        response.close()
        response.release_conn()
        return data
    except Exception as e:
        logger.warning("Failed to download %s/%s: %s", bucket, object_name, e)
        return None


def _download_jsonl(client, bucket, object_name):
    """Download and yield lines from a JSONL object."""
    try:
        response = client.get_object(bucket, object_name)
        content = response.read().decode("utf-8")
        response.close()
        response.release_conn()
        for line in content.splitlines():
            line = line.strip()
            if line:
                yield json.loads(line)
    except Exception as e:
        logger.warning("Failed to download %s/%s: %s", bucket, object_name, e)


def _merge_drawer(col, wing, drawer_data, source_user, chunk_size):
    """Upsert a single drawer into the local palace.

    Returns 'imported' or 'skipped'.
    """
    content = drawer_data["content"]
    room = drawer_data.get("room", "unknown")

    drawer_id = make_drawer_id_from_content(wing, room, content)

    existing = col.get(ids=[drawer_id], include=[])
    if existing["ids"]:
        return "skipped"

    meta = {
        "wing": wing,
        "room": room,
        "added_by": f"pull:{source_user}",
        "filed_at": datetime.now(timezone.utc).isoformat(),
        "source_user": source_user,
    }
    original_meta = drawer_data.get("metadata", {})
    if original_meta.get("source_file"):
        meta["source_file"] = original_meta["source_file"]

    if len(content) <= chunk_size:
        col.upsert(
            ids=[drawer_id],
            documents=[content],
            metadatas=[{**meta, "chunk_index": 0}],
        )
    else:
        chunk_ids = []
        chunk_docs = []
        chunk_metas = []
        for i in range(0, len(content), chunk_size):
            chunk_idx = i // chunk_size
            chunk_ids.append(f"{drawer_id}_chunk_{chunk_idx:06d}")
            chunk_docs.append(content[i : i + chunk_size])
            chunk_metas.append({**meta, "chunk_index": chunk_idx, "parent_drawer_id": drawer_id})
        col.upsert(ids=chunk_ids, documents=chunk_docs, metadatas=chunk_metas)

    return "imported"


def _merge_kg_fact(kg, fact, source_user):
    """Add a KG triple if it doesn't already exist.

    Returns 'imported' or 'skipped'.
    """
    existing = kg.query_entity(fact["subject"], direction="outgoing")
    for e in existing:
        if e["predicate"] == fact["predicate"] and e["object"] == fact["object"]:
            return "skipped"

    kg.add_triple(
        subject=fact["subject"],
        predicate=fact["predicate"],
        obj=fact["object"],
        valid_from=fact.get("valid_from"),
        valid_to=fact.get("valid_to"),
        source_file=f"pull:{source_user}",
    )
    return "imported"


def pull_wing(wing: str, config: MempalaceConfig = None) -> dict:
    """Pull a wing from MinIO (all users) and merge into local palace.

    Downloads drawers and KG facts from every user who pushed to this wing,
    then upserts into the local palace. Content-addressable drawer IDs
    ensure natural deduplication.
    """
    if config is None:
        config = MempalaceConfig()

    try:
        client = get_minio_client(config)
    except (ImportError, ValueError) as e:
        return {"error": str(e)}

    bucket = config.minio_bucket

    try:
        if not client.bucket_exists(bucket):
            return {"error": f"Bucket '{bucket}' does not exist on MinIO."}
    except Exception as e:
        return {"error": f"Cannot connect to MinIO: {e}"}

    users = _discover_users(client, bucket, wing)
    if not users:
        return {"error": f"Wing '{wing}' not found on remote. No users have pushed this wing."}

    col = get_collection(config.palace_path, create=True)
    if col is None:
        return {"error": "Cannot open palace. Check palace_path configuration."}

    chunk_size = config.chunk_size

    total_imported = 0
    total_skipped = 0
    kg_imported = 0
    kg_skipped = 0
    warnings = []

    for user_id in users:
        prefix = f"{wing}/{user_id}"
        logger.info("Pulling wing '%s' from user '%s'", wing, user_id)

        user_imported = 0
        user_skipped = 0

        for drawer_data in _download_jsonl(client, bucket, f"{prefix}/drawers.jsonl"):
            result = _merge_drawer(col, wing, drawer_data, user_id, chunk_size)
            if result == "imported":
                user_imported += 1
            else:
                user_skipped += 1

        total_imported += user_imported
        total_skipped += user_skipped

        kg_facts = _download_json(client, bucket, f"{prefix}/kg_facts.json")
        if kg_facts:
            kg = KnowledgeGraph(db_path=config.kg_path if hasattr(config, "kg_path") else None)
            try:
                for fact in kg_facts:
                    result = _merge_kg_fact(kg, fact, user_id)
                    if result == "imported":
                        kg_imported += 1
                    else:
                        kg_skipped += 1
            finally:
                kg.close()

        logger.info(
            "User '%s': %d imported, %d skipped",
            user_id,
            user_imported,
            user_skipped,
        )

    result = {
        "success": True,
        "wing": wing,
        "sources": users,
        "drawers_imported": total_imported,
        "drawers_skipped": total_skipped,
        "kg_facts_imported": kg_imported,
        "kg_facts_skipped": kg_skipped,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if warnings:
        result["warnings"] = warnings

    logger.info(
        "Pull complete for wing '%s': %d drawers imported, %d skipped, %d KG facts imported",
        wing,
        total_imported,
        total_skipped,
        kg_imported,
    )

    return result
