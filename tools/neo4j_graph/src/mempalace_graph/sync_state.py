from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

FORBIDDEN_COLUMNS = {"content", "body", "full_text", "raw_payload", "text"}


class SyncState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.create_schema()

    def close(self) -> None:
        self.conn.close()

    def create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_files (
              source_path TEXT PRIMARY KEY,
              sha256 TEXT,
              size_bytes INTEGER,
              modified_at REAL,
              last_synced_at TEXT NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_records (
              source_path TEXT NOT NULL,
              source_record_locator TEXT NOT NULL,
              memory_id TEXT NOT NULL,
              last_synced_at TEXT NOT NULL,
              PRIMARY KEY (source_path, source_record_locator)
            );
            CREATE TABLE IF NOT EXISTS sync_offsets (
              source_path TEXT PRIMARY KEY,
              offset_bytes INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              status TEXT NOT NULL,
              files_seen INTEGER DEFAULT 0,
              files_changed INTEGER DEFAULT 0,
              records_seen INTEGER DEFAULT 0,
              records_upserted INTEGER DEFAULT 0,
              records_deleted INTEGER DEFAULT 0,
              error TEXT
            );
            """
        )
        self.conn.commit()

    def start_sync_run(self) -> int:
        cur = self.conn.execute("INSERT INTO sync_runs (started_at, status) VALUES (?, ?)", (now(), "running"))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_sync_run(self, run_id: int, status: str, files_seen: int = 0, files_changed: int = 0, records_seen: int = 0, records_upserted: int = 0, records_deleted: int = 0, error: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE sync_runs
            SET finished_at=?, status=?, files_seen=?, files_changed=?, records_seen=?, records_upserted=?, records_deleted=?, error=?
            WHERE id=?
            """,
            (now(), status, files_seen, files_changed, records_seen, records_upserted, records_deleted, error, run_id),
        )
        self.conn.commit()

    def get_source_file_state(self, source_path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM source_files WHERE source_path=?", (source_path,)).fetchone()

    def upsert_source_file_state(self, source_path: str, sha256: str | None, size_bytes: int | None, modified_at: float | None, status: str = "synced") -> None:
        self.conn.execute(
            """
            INSERT INTO source_files (source_path, sha256, size_bytes, modified_at, last_synced_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_path) DO UPDATE SET
              sha256=excluded.sha256,
              size_bytes=excluded.size_bytes,
              modified_at=excluded.modified_at,
              last_synced_at=excluded.last_synced_at,
              status=excluded.status
            """,
            (source_path, sha256, size_bytes, modified_at, now(), status),
        )
        self.conn.commit()

    def get_records_for_source(self, source_path: str) -> dict[str, str]:
        rows = self.conn.execute("SELECT source_record_locator, memory_id FROM source_records WHERE source_path=?", (source_path,)).fetchall()
        return {row["source_record_locator"]: row["memory_id"] for row in rows}

    def replace_records_for_source(self, source_path: str, locator_to_memory_id: dict[str, str]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM source_records WHERE source_path=?", (source_path,))
            self.conn.executemany(
                "INSERT INTO source_records (source_path, source_record_locator, memory_id, last_synced_at) VALUES (?, ?, ?, ?)",
                [(source_path, locator, memory_id, now()) for locator, memory_id in locator_to_memory_id.items()],
            )

    def get_wal_offset(self, source_path: str) -> int:
        row = self.conn.execute("SELECT offset_bytes FROM sync_offsets WHERE source_path=?", (source_path,)).fetchone()
        return int(row["offset_bytes"]) if row else 0

    def set_wal_offset(self, source_path: str, offset: int) -> None:
        self.conn.execute(
            "INSERT INTO sync_offsets (source_path, offset_bytes, updated_at) VALUES (?, ?, ?) ON CONFLICT(source_path) DO UPDATE SET offset_bytes=excluded.offset_bytes, updated_at=excluded.updated_at",
            (source_path, offset, now()),
        )
        self.conn.commit()

    def list_known_memory_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT DISTINCT memory_id FROM source_records").fetchall()
        return {row["memory_id"] for row in rows}

    def forbidden_columns(self) -> list[str]:
        found: list[str] = []
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for row in rows:
            for col in self.conn.execute(f"PRAGMA table_info({row['name']})"):
                if col["name"].lower() in FORBIDDEN_COLUMNS:
                    found.append(f"{row['name']}.{col['name']}")
        return found


def now() -> str:
    return datetime.now(timezone.utc).isoformat()
