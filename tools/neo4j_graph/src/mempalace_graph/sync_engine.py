from __future__ import annotations

import logging

from .config import Config
from .mempalace_discovery import discover_mempalace
from .mempalace_chroma_reader import read_chroma_records
from .mempalace_schema_inspector import inspect_schema
from .mempalace_sqlite_reader import read_sqlite_records
from .mempalace_wal_reader import read_wal_events
from .models import SyncResult
from .normalizer import normalize_record
from .sync_state import SyncState

logger = logging.getLogger(__name__)


def sync_once(config: Config, create_schema: bool = False, dry_run: bool = False) -> SyncResult:
    errors: list[str] = []
    sync_state = None if dry_run else SyncState(config.sync_state_path)
    run_id = sync_state.start_sync_run() if sync_state else None
    try:
        discovery = discover_mempalace(config)
        if not discovery.knowledge_graph_db.exists:
            raise RuntimeError(f"Error: knowledge_graph.sqlite3 not found at {discovery.knowledge_graph_db.path}")
        if not discovery.chroma_db.exists:
            raise RuntimeError(f"Error: chroma.sqlite3 not found at {discovery.chroma_db.path}")

        inspection = inspect_schema(discovery.knowledge_graph_db.path)
        kg_records, relationships, read_warnings = read_sqlite_records(
            discovery.knowledge_graph_db.path,
            inspection,
            source_hash=discovery.knowledge_graph_db.sha256,
            source_modified_at=discovery.knowledge_graph_db.modified_at,
        )
        chroma_records, chroma_warnings = read_chroma_records(
            discovery.chroma_db.path,
            source_hash=discovery.chroma_db.sha256,
            source_modified_at=discovery.chroma_db.modified_at,
        )
        errors.extend(read_warnings)
        errors.extend(chroma_warnings)
        records = [normalize_record(r, config.store_content, config.store_snippet, config.snippet_chars) for r in [*kg_records, *chroma_records]]

        if discovery.write_log.exists:
            offset = sync_state.get_wal_offset(str(discovery.write_log.path)) if sync_state else 0
            _events, new_offset, wal_errors = read_wal_events(discovery.write_log.path, offset)
            errors.extend(wal_errors)
            if sync_state and new_offset:
                sync_state.set_wal_offset(str(discovery.write_log.path), new_offset)

        old_ids = sync_state.list_known_memory_ids() if sync_state else set()
        new_ids = {record.id for record in records}
        deleted_ids = sorted(old_ids - new_ids)

        if dry_run:
            return SyncResult(5, 5, len(records), len(records), 0, 0, errors)

        from .neo4j_client import Neo4jClient

        client = Neo4jClient(config.neo4j_uri, config.neo4j_username, config.neo4j_password, config.neo4j_database, config.store_content)
        try:
            client.verify_connectivity()
            if create_schema:
                client.create_schema()
            client.upsert_memory_records(records)
            client.upsert_relationship_records(relationships)
            soft_deleted = hard_deleted = 0
            if config.sync_mode == "soft_delete":
                client.soft_delete_memories(deleted_ids)
                soft_deleted = len(deleted_ids)
            elif config.sync_mode == "hard_delete":
                client.hard_delete_memories(deleted_ids)
                hard_deleted = len(deleted_ids)
        finally:
            client.close()

        sync_state.upsert_source_file_state(
            str(discovery.knowledge_graph_db.path),
            discovery.knowledge_graph_db.sha256,
            discovery.knowledge_graph_db.size_bytes,
            discovery.knowledge_graph_db.path.stat().st_mtime if discovery.knowledge_graph_db.exists else None,
        )
        sync_state.upsert_source_file_state(
            str(discovery.chroma_db.path),
            discovery.chroma_db.sha256,
            discovery.chroma_db.size_bytes,
            discovery.chroma_db.path.stat().st_mtime if discovery.chroma_db.exists else None,
        )
        sync_state.replace_records_for_source(str(discovery.knowledge_graph_db.path), {record.source_record_locator: record.id for record in records if record.source_path == str(discovery.knowledge_graph_db.path)})
        sync_state.replace_records_for_source(str(discovery.chroma_db.path), {record.source_record_locator: record.id for record in records if record.source_path == str(discovery.chroma_db.path)})
        result = SyncResult(5, 5, len(records), len(records), soft_deleted, hard_deleted, errors)
        sync_state.finish_sync_run(run_id, "success", result.files_scanned, result.files_changed, result.records_seen, result.records_upserted, soft_deleted + hard_deleted, "\n".join(errors) or None)
        return result
    except Exception as exc:
        if sync_state and run_id:
            sync_state.finish_sync_run(run_id, "failed", error=str(exc))
        raise
    finally:
        if sync_state:
            sync_state.close()
