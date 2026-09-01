from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .mempalace_schema_inspector import open_readonly_sqlite
from .models import MemoryRecord
from .normalizer import DEFAULT_CLOSET, DEFAULT_DRAWER, DEFAULT_ROOM, DEFAULT_WING

DOCUMENT_KEY = "chroma:document"


def read_chroma_records(path: Path, source_hash: str | None = None, source_modified_at: str | None = None) -> tuple[list[MemoryRecord], list[str]]:
    warnings: list[str] = []
    records: list[MemoryRecord] = []
    with open_readonly_sqlite(path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            embeddings = conn.execute("SELECT id, embedding_id, created_at FROM embeddings ORDER BY id").fetchall()
            metadata = _load_metadata(conn)
        except sqlite3.Error as exc:
            return [], [f"Could not read Chroma memory records: {exc}"]

    for row in embeddings:
        meta = metadata.get(int(row["id"]), {})
        content = _string(meta.pop(DOCUMENT_KEY, None))
        title = _title_from_content(content) or _string(meta.get("title")) or str(row["embedding_id"])
        wing = _string(meta.get("wing")) or DEFAULT_WING
        room = _string(meta.get("room")) or _string(meta.get("topic")) or DEFAULT_ROOM
        closet = _string(meta.get("hall")) or _string(meta.get("closet")) or DEFAULT_CLOSET
        drawer = _string(meta.get("drawer")) or str(row["embedding_id"]) or DEFAULT_DRAWER
        tags = [value for value in [_string(meta.get("content_type")), _string(meta.get("type")), _string(meta.get("agent"))] if value]
        projects = [_project_from_wing(wing)] if wing.startswith("wing_project_") else []
        records.append(
            MemoryRecord(
                id=str(row["embedding_id"]),
                title=title,
                snippet=None,
                content=content,
                wing=wing,
                room=room,
                closet=closet,
                drawer=drawer,
                projects=projects,
                tags=tags,
                source_path=str(path),
                source_record_locator=f"chroma:embedding:{row['embedding_id']}",
                source_file_hash=source_hash,
                source_modified_at=source_modified_at,
                created_at=str(row["created_at"]) if row["created_at"] else None,
                updated_at=_string(meta.get("timestamp")) or _string(meta.get("filed_at")),
                node_type=_string(meta.get("content_type")) or _string(meta.get("type")),
            )
        )
    return records, warnings


def resolve_chroma_record(path: Path, embedding_id: str) -> tuple[str | None, str, dict[str, Any]]:
    with open_readonly_sqlite(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT id, embedding_id, created_at FROM embeddings WHERE embedding_id=?", (embedding_id,)).fetchone()
        if not row:
            raise LookupError(embedding_id)
        metadata = _load_metadata(conn, int(row["id"])).get(int(row["id"]), {})
    content = _string(metadata.pop(DOCUMENT_KEY, None)) or ""
    title = _title_from_content(content) or _string(metadata.get("title")) or embedding_id
    return title, content, metadata


def _load_metadata(conn: sqlite3.Connection, only_id: int | None = None) -> dict[int, dict[str, Any]]:
    sql = "SELECT id, key, string_value, int_value, float_value, bool_value FROM embedding_metadata"
    params: tuple[int, ...] = ()
    if only_id is not None:
        sql += " WHERE id=?"
        params = (only_id,)
    out: dict[int, dict[str, Any]] = {}
    for row in conn.execute(sql, params):
        out.setdefault(int(row["id"]), {})[row["key"]] = _metadata_value(row)
    return out


def _metadata_value(row: sqlite3.Row) -> Any:
    for key in ("string_value", "int_value", "float_value", "bool_value"):
        value = row[key]
        if value is not None:
            return value
    return None


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _title_from_content(content: str | None) -> str | None:
    if not content:
        return None
    for line in content.splitlines():
        cleaned = line.strip().strip("#").strip()
        if cleaned and not cleaned.startswith("---"):
            return cleaned[:160]
    return None


def _project_from_wing(wing: str) -> str:
    return wing.removeprefix("wing_project_").replace("-", " ")
