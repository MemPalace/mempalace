from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import unquote

from .mempalace_schema_inspector import open_readonly_sqlite, quote_ident
from .mempalace_sqlite_reader import CONTENT_FIELDS, TITLE_FIELDS, first_existing, first_existing_value, parse_metadata
from .mempalace_chroma_reader import resolve_chroma_record
from .models import ResolvedContent


class ResolveError(RuntimeError):
    pass


def resolve_content(source_path: str, source_record_locator: str) -> ResolvedContent:
    path = Path(source_path)
    if not path.exists():
        raise ResolveError("Error: Source file is missing. The Neo4j index may be stale.")
    if source_record_locator.startswith("sqlite:"):
        return resolve_sqlite(path, source_record_locator)
    if source_record_locator.startswith("chroma:embedding:"):
        return resolve_chroma(path, source_record_locator)
    if source_record_locator.startswith("jsonl:offset:"):
        return resolve_jsonl(path, source_record_locator)
    raise ResolveError("Error: Source locator is stale or unsupported. Run sync again.")


def resolve_chroma(path: Path, locator: str) -> ResolvedContent:
    embedding_id = locator.removeprefix("chroma:embedding:")
    try:
        title, content, metadata = resolve_chroma_record(path, embedding_id)
    except LookupError as exc:
        raise ResolveError("Error: Source locator is stale or unsupported. Run sync again.") from exc
    return ResolvedContent(title, content, metadata, str(path), locator)


def resolve_sqlite(path: Path, locator: str) -> ResolvedContent:
    parts = locator.split(":", 3)
    if len(parts) != 3 and not (len(parts) == 4 and parts[2] in {"rowid", "pk"}):
        raise ResolveError("Error: Source locator is stale or unsupported. Run sync again.")
    table = parts[1]
    with open_readonly_sqlite(path) as conn:
        conn.row_factory = sqlite3.Row
        if len(parts) == 4 and parts[2] == "rowid":
            row = conn.execute(f"SELECT * FROM {quote_ident(table)} WHERE rowid=?", (parts[3],)).fetchone()
        elif len(parts) == 4 and parts[2] == "pk":
            row = resolve_composite_pk(conn, table, parts[3])
        else:
            pk = primary_key(conn, table)
            if not pk:
                raise ResolveError("Error: Source locator is stale or unsupported. Run sync again.")
            row = conn.execute(f"SELECT * FROM {quote_ident(table)} WHERE {quote_ident(pk)}=?", (parts[2],)).fetchone()
    if not row:
        raise ResolveError("Error: Source locator is stale or unsupported. Run sync again.")
    data = dict(row)
    metadata = parse_metadata(first_existing_value(data, ["metadata", "properties"]))
    content = first_existing(data, CONTENT_FIELDS) or (json.dumps(metadata, indent=2) if metadata else "")
    return ResolvedContent(first_existing(data, TITLE_FIELDS), content, metadata, str(path), locator)


def resolve_composite_pk(conn: sqlite3.Connection, table: str, encoded_values: str) -> sqlite3.Row | None:
    try:
        values = json.loads(unquote(encoded_values))
    except (TypeError, ValueError) as exc:
        raise ResolveError("Error: Source locator is stale or unsupported. Run sync again.") from exc
    if not isinstance(values, dict) or not values:
        raise ResolveError("Error: Source locator is stale or unsupported. Run sync again.")
    columns = list(values)
    where_clause = " AND ".join(f"{quote_ident(column)}=?" for column in columns)
    return conn.execute(
        f"SELECT * FROM {quote_ident(table)} WHERE {where_clause}",
        tuple(values[column] for column in columns),
    ).fetchone()


def resolve_jsonl(path: Path, locator: str) -> ResolvedContent:
    try:
        offset = int(locator.rsplit(":", 1)[1])
    except ValueError as exc:
        raise ResolveError("Error: Source locator is stale or unsupported. Run sync again.") from exc
    with path.open("rb") as fh:
        fh.seek(offset)
        line = fh.readline()
    if not line:
        raise ResolveError("Error: Source locator is stale or unsupported. Run sync again.")
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResolveError("Error: Source locator is stale or unsupported. Run sync again.") from exc
    title = payload.get("operation") or payload.get("event") or payload.get("type")
    content = json.dumps(payload, indent=2, ensure_ascii=False)
    return ResolvedContent(title, content, payload if isinstance(payload, dict) else {}, str(path), locator)


def primary_key(conn: sqlite3.Connection, table: str) -> str | None:
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    for row in rows:
        if row[5]:
            return row[1]
    return None
