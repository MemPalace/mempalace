from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MempalacePaths:
    home: Path
    palace_dir: Path
    knowledge_graph_db: Path
    write_log: Path
    config_json: Path


@dataclass(frozen=True)
class SourcePointer:
    source_path: str
    source_record_locator: str


@dataclass
class MemoryRecord:
    id: str
    title: str | None
    snippet: str | None
    content: str | None
    wing: str
    room: str
    closet: str
    drawer: str
    people: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source_path: str = ""
    source_record_locator: str = ""
    source_file_hash: str | None = None
    source_modified_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_accessed_at: str | None = None
    importance: int | None = None
    confidence: float | None = None
    retrieval_count: int | None = None
    node_type: str | None = None


@dataclass
class RelationshipRecord:
    source_memory_id: str
    target_memory_id: str
    relationship_type: str
    score: float | None = None


@dataclass
class SyncResult:
    files_scanned: int
    files_changed: int
    records_seen: int
    records_upserted: int
    records_soft_deleted: int
    records_hard_deleted: int
    errors: list[str]


@dataclass
class ResolvedContent:
    title: str | None
    content: str
    metadata: dict
    source_path: str
    source_record_locator: str
