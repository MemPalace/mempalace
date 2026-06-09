from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .mempalace_schema_inspector import SchemaInspection, TableInfo, open_readonly_sqlite, quote_ident
from .models import MemoryRecord, RelationshipRecord
from .normalizer import DEFAULT_CLOSET, DEFAULT_DRAWER, DEFAULT_ROOM, DEFAULT_WING

CONTENT_FIELDS = ["content", "text", "body", "summary", "description"]
TITLE_FIELDS = ["title", "name", "label"]
ID_FIELDS = ["id", "memory_id", "node_id"]


def read_sqlite_records(path: Path, inspection: SchemaInspection, source_hash: str | None = None, source_modified_at: str | None = None) -> tuple[list[MemoryRecord], list[RelationshipRecord], list[str]]:
    records: list[MemoryRecord] = []
    relationships: list[RelationshipRecord] = []
    warnings: list[str] = []
    with open_readonly_sqlite(path) as conn:
        conn.row_factory = sqlite3.Row
        memory_tables = inspection.likely_memory_tables or [table.name for table in inspection.tables if table.row_count > 0]
        relationship_tables = set(inspection.likely_relationship_tables)
        for table in inspection.tables:
            if table.name in memory_tables:
                try:
                    records.extend(_read_memory_table(conn, table, path, source_hash, source_modified_at))
                except sqlite3.Error as exc:
                    warnings.append(f"Could not read memory table {table.name}: {exc}")
            if table.name in relationship_tables:
                try:
                    relationships.extend(_read_relationship_table(conn, table.name))
                except sqlite3.Error as exc:
                    warnings.append(f"Could not read relationship table {table.name}: {exc}")
    return records, relationships, warnings


def _read_memory_table(conn: sqlite3.Connection, table: TableInfo, path: Path, source_hash: str | None, source_modified_at: str | None) -> list[MemoryRecord]:
    out: list[MemoryRecord] = []
    cursor = memory_rows(conn, table.name)
    for row in cursor:
        data = dict(row)
        metadata = parse_metadata(first_existing_value(data, ["metadata", "properties"]))
        record_id = first_existing(data, ID_FIELDS)
        locator, fallback_record_id = sqlite_locator(table, data, record_id)
        content = first_existing(data, CONTENT_FIELDS)
        title = first_existing(data, TITLE_FIELDS) or first_line(content)
        hierarchy = extract_hierarchy(data, metadata)
        out.append(
            MemoryRecord(
                id=str(record_id or fallback_record_id or ""),
                title=title,
                snippet=first_existing(data, ["snippet", "summary"]),
                content=content,
                wing=hierarchy["wing"],
                room=hierarchy["room"],
                closet=hierarchy["closet"],
                drawer=hierarchy["drawer"],
                people=list_values(data, metadata, "people"),
                topics=list_values(data, metadata, "topics"),
                projects=list_values(data, metadata, "projects"),
                tags=list_values(data, metadata, "tags"),
                source_path=str(path),
                source_record_locator=locator,
                source_file_hash=source_hash,
                source_modified_at=source_modified_at,
                created_at=first_existing(data, ["created_at", "created"]),
                updated_at=first_existing(data, ["updated_at", "modified_at", "updated"]),
                last_accessed_at=first_existing(data, ["last_accessed_at"]),
                importance=to_int(first_existing(data, ["importance", "priority"]) or metadata.get("importance")),
                confidence=to_float(first_existing(data, ["confidence"]) or metadata.get("confidence")),
                retrieval_count=to_int(first_existing(data, ["retrieval_count"]) or metadata.get("retrieval_count")),
                node_type=first_existing(data, ["node_type", "type"]) or metadata.get("type"),
            )
        )
    return out


def memory_rows(conn: sqlite3.Connection, table: str) -> sqlite3.Cursor:
    try:
        return conn.execute(f"SELECT rowid AS __rowid__, * FROM {quote_ident(table)}")
    except sqlite3.OperationalError:
        return conn.execute(f"SELECT * FROM {quote_ident(table)}")


def sqlite_locator(table: TableInfo, data: dict[str, Any], record_id: str | None) -> tuple[str, str | None]:
    if record_id:
        return f"sqlite:{table.name}:{record_id}", record_id
    rowid = data.get("__rowid__")
    if rowid not in (None, ""):
        return f"sqlite:{table.name}:rowid:{rowid}", str(rowid)

    pk_values = {name: data.get(name) for name in table.primary_keys if data.get(name) not in (None, "")}
    if len(pk_values) == 1 and len(table.primary_keys) == 1:
        pk_value = str(next(iter(pk_values.values())))
        return f"sqlite:{table.name}:{pk_value}", pk_value
    if len(pk_values) == len(table.primary_keys) and table.primary_keys:
        encoded = quote(json.dumps(pk_values, sort_keys=True, separators=(",", ":")), safe="")
        digest = sha256(json.dumps(pk_values, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        return f"sqlite:{table.name}:pk:{encoded}", digest

    encoded = json.dumps(data, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"sqlite:{table.name}:hash:{digest}", digest


def _read_relationship_table(conn: sqlite3.Connection, table: str) -> list[RelationshipRecord]:
    out: list[RelationshipRecord] = []
    cursor = conn.execute(f"SELECT * FROM {quote_ident(table)}")
    for row in cursor:
        data = dict(row)
        source = first_existing(data, ["source_memory_id", "source_id", "source", "from_id", "src", "subject"])
        target = first_existing(data, ["target_memory_id", "target_id", "target", "to_id", "dst", "object"])
        if not source or not target:
            continue
        rel_type = first_existing(data, ["relationship_type", "edge_type", "predicate", "type", "relationship"]) or "RELATED_TO"
        score = to_float(first_existing(data, ["score", "weight", "confidence"]))
        out.append(RelationshipRecord(str(source), str(target), str(rel_type), score))
    return out


def parse_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        loaded = json.loads(value if isinstance(value, str) else str(value))
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def first_existing(data: dict[str, Any], names: list[str]) -> str | None:
    value = first_existing_value(data, names)
    return str(value) if value is not None else None


def first_existing_value(data: dict[str, Any], names: list[str]) -> Any | None:
    lowered = {key.lower(): key for key in data}
    for name in names:
        key = lowered.get(name.lower())
        if key is not None and data[key] not in (None, ""):
            return data[key]
    return None


def first_line(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().splitlines()[0][:120]


def extract_hierarchy(data: dict[str, Any], metadata: dict[str, Any]) -> dict[str, str]:
    def get(name: str, default: str) -> str:
        return str(metadata.get(name) or first_existing(data, [name]) or default)

    source_closet = first_existing(data, ["source_closet"])
    if source_closet and "/" in source_closet:
        parts = [part for part in source_closet.split("/") if part]
        return {
            "wing": parts[0] if len(parts) > 0 else DEFAULT_WING,
            "room": parts[1] if len(parts) > 1 else DEFAULT_ROOM,
            "closet": parts[2] if len(parts) > 2 else DEFAULT_CLOSET,
            "drawer": parts[3] if len(parts) > 3 else DEFAULT_DRAWER,
        }
    return {
        "wing": get("wing", get("palace", DEFAULT_WING)),
        "room": get("room", get("area", DEFAULT_ROOM)),
        "closet": get("closet", get("category", DEFAULT_CLOSET)),
        "drawer": get("drawer", get("collection", DEFAULT_DRAWER)),
    }


def list_values(data: dict[str, Any], metadata: dict[str, Any], name: str) -> list[str]:
    value = metadata.get(name) or first_existing(data, [name])
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
