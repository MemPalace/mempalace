from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

MEMORY_HINTS = {"id", "node_id", "memory_id", "name", "title", "content", "text", "summary", "metadata", "properties", "created_at", "updated_at"}
REL_HINTS = {"source", "target", "source_id", "target_id", "from_id", "to_id", "src", "dst", "subject", "object", "predicate", "relationship", "edge_type", "type", "weight", "score", "confidence"}


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: list[str]
    row_count: int
    primary_keys: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SchemaInspection:
    database_path: Path
    tables: list[TableInfo]
    likely_memory_tables: list[str]
    likely_relationship_tables: list[str]
    warnings: list[str]


def open_readonly_sqlite(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def inspect_schema(path: Path) -> SchemaInspection:
    tables: list[TableInfo] = []
    warnings: list[str] = []
    with open_readonly_sqlite(path) as conn:
        table_names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in table_names:
            cols_raw = list(conn.execute(f"PRAGMA table_info({quote_ident(name)})"))
            columns = [row[1] for row in cols_raw]
            primary_keys = [row[1] for row in cols_raw if row[5]]
            try:
                row_count = int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(name)}").fetchone()[0])
            except sqlite3.Error as exc:
                row_count = 0
                warnings.append(f"Could not count table {name}: {exc}")
            tables.append(TableInfo(name=name, columns=columns, row_count=row_count, primary_keys=primary_keys))

    likely_memory = []
    likely_relationship = []
    for table in tables:
        cols = {c.lower() for c in table.columns}
        if table.row_count > 0 and len(cols & MEMORY_HINTS) >= 2:
            likely_memory.append(table.name)
        if table.row_count > 0 and ({"subject", "object"} <= cols or {"source_id", "target_id"} <= cols or len(cols & REL_HINTS) >= 3):
            likely_relationship.append(table.name)

    if not likely_memory:
        warnings.append("Could not detect memory table automatically.")
    if not likely_relationship:
        warnings.append("Could not detect relationship table automatically.")

    return SchemaInspection(path, tables, likely_memory, likely_relationship, warnings)


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def format_schema_report(inspection: SchemaInspection, home: Path | None = None, write_log: Path | None = None) -> str:
    lines = ["MemPalace schema inspection"]
    if home:
        lines += ["Home:", str(home)]
    lines += ["Database:", str(inspection.database_path), "Tables:"]
    for table in inspection.tables:
        lines.append(f"- {table.name}: {table.row_count} rows")
        lines.append(f"  columns: {', '.join(table.columns)}")
    lines.append("Likely memory tables:")
    lines.extend([f"- {name}" for name in inspection.likely_memory_tables] or ["- none detected"])
    lines.append("Likely relationship tables:")
    lines.extend([f"- {name}" for name in inspection.likely_relationship_tables] or ["- none detected"])
    if write_log:
        lines += ["Write log:", str(write_log)]
    for warning in inspection.warnings:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)
