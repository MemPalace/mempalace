from __future__ import annotations

import hashlib
import re
from dataclasses import replace

from .models import MemoryRecord

DEFAULT_WING = "MemPalace"
DEFAULT_ROOM = "Knowledge Graph"
DEFAULT_CLOSET = "Imported"
DEFAULT_DRAWER = "Unfiled"


def clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return re.sub(r"\s+", " ", text) if text else None


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = clean_text(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def stable_id(source_path: str, locator: str, source_file_hash: str | None = None, record_hash: str | None = None) -> str:
    raw = "|".join([source_path, locator, source_file_hash or record_hash or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def make_snippet(content: str | None, limit: int, enabled: bool = True) -> str | None:
    if not enabled:
        return None
    text = clean_text(content)
    if not text:
        return None
    return text[:limit]


def normalize_record(record: MemoryRecord, store_content: bool = False, store_snippet: bool = True, snippet_chars: int = 240) -> MemoryRecord:
    source_hash = record.source_file_hash
    rec_id = record.id or stable_id(record.source_path, record.source_record_locator, source_hash, record.content)
    title = clean_text(record.title) or (clean_text(record.content).split(".")[0] if clean_text(record.content) else None) or f"Memory {rec_id}"
    snippet = make_snippet(record.snippet or record.content, snippet_chars, store_snippet)
    return replace(
        record,
        id=rec_id,
        title=title,
        snippet=snippet,
        content=record.content if store_content else None,
        wing=clean_text(record.wing) or DEFAULT_WING,
        room=clean_text(record.room) or DEFAULT_ROOM,
        closet=clean_text(record.closet) or DEFAULT_CLOSET,
        drawer=clean_text(record.drawer) or DEFAULT_DRAWER,
        people=dedupe(record.people),
        topics=dedupe(record.topics),
        projects=dedupe(record.projects),
        tags=dedupe(record.tags),
    )


def neo4j_memory_payload(record: MemoryRecord, store_content: bool = False) -> dict:
    payload = {
        "id": record.id,
        "title": record.title,
        "snippet": record.snippet,
        "source_path": record.source_path,
        "source_record_locator": record.source_record_locator,
        "source_file_hash": record.source_file_hash,
        "source_modified_at": record.source_modified_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_accessed_at": record.last_accessed_at,
        "importance": record.importance,
        "confidence": record.confidence,
        "retrieval_count": record.retrieval_count,
        "node_type": record.node_type,
    }
    if store_content:
        payload["content"] = record.content
    return payload
