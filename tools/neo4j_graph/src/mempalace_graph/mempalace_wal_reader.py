from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WalEvent:
    offset_bytes: int
    event_type: str | None
    record_id: str | None
    timestamp: str | None
    metadata: dict[str, Any]
    source_path: str
    source_record_locator: str


def read_wal_events(path: Path, start_offset: int = 0) -> tuple[list[WalEvent], int, list[str]]:
    if not path.exists():
        return [], 0, [f"Warning: write log not found at {path}"]

    events: list[WalEvent] = []
    errors: list[str] = []
    size_bytes = path.stat().st_size
    if start_offset > size_bytes:
        errors.append(f"Warning: write log offset {start_offset} exceeds current size {size_bytes}; reading from start")
        start_offset = 0
    if start_offset < 0:
        errors.append(f"Warning: write log offset {start_offset} is negative; reading from start")
        start_offset = 0
    end_offset = start_offset
    with path.open("rb") as fh:
        fh.seek(start_offset)
        while True:
            offset = fh.tell()
            line = fh.readline()
            if not line:
                break
            end_offset = fh.tell()
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                errors.append(f"Skipped invalid JSONL line at offset {offset}")
                continue
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            record_id = payload.get("record_id") or params.get("drawer_id") or params.get("id")
            event_type = payload.get("operation") or payload.get("event") or payload.get("type")
            events.append(
                WalEvent(
                    offset_bytes=offset,
                    event_type=event_type,
                    record_id=record_id,
                    timestamp=payload.get("timestamp"),
                    metadata=payload,
                    source_path=str(path),
                    source_record_locator=f"jsonl:offset:{offset}",
                )
            )
    return events, end_offset, errors
