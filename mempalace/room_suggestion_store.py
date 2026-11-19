"""Durable, private room suggestion jobs, predictions and reversible write journals.

The sidecar contains hashes and metadata, never copies of drawer documents.
Connections are short lived; job progress and cancellation use independent SQL
columns so a worker cannot overwrite a concurrent cancellation request.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path

from . import paths
from .private_state import restrict_private
from .timeutil import utc_now_iso


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class RoomSuggestionError(ValueError):
    """A user-facing failure safe to display without exposing drawer contents."""


class SuggestionStore:
    """One local sidecar per palace, with one active inference job at a time."""

    def __init__(self, palace_path: str, root: Path | None = None):
        self.palace_path = os.path.realpath(os.path.expanduser(palace_path))
        base = Path(root) if root is not None else paths.state_path("room-suggestions")
        self.root = base / paths.palace_key(self.palace_path)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        restrict_private(self.root, directory=True)
        self.path = self.root / "suggestions.sqlite3"
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise RoomSuggestionError("Suggestion state must be a regular local file")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        restrict_private(self.path)
        with self.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, config TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    enumerated INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0,
                    identity TEXT, error TEXT, selection TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs((1))
                  WHERE state IN ('queued','running','cancelling','applying','undoing');
                CREATE TABLE IF NOT EXISTS items (
                    job_id TEXT NOT NULL, id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    metadata TEXT NOT NULL, document_hash TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'pending', prediction TEXT,
                    seconds REAL NOT NULL DEFAULT 0, cache_hit INTEGER NOT NULL DEFAULT 0,
                    error TEXT, PRIMARY KEY(job_id,id)
                );
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, prediction TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS moves (
                    job_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                    before_meta TEXT NOT NULL, after_meta TEXT NOT NULL,
                    document_hash TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
                    undone INTEGER NOT NULL DEFAULT 0, source_rooms TEXT,
                    PRIMARY KEY(job_id,kind,id)
                );
            """)

    @contextlib.contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def create(self, config: dict) -> str:
        job_id = uuid.uuid4().hex
        try:
            with self.connect() as con:
                con.execute(
                    "INSERT INTO jobs(id,config,state,created_at,updated_at) VALUES(?,?,'queued',?,?)",
                    (job_id, dumps(config), utc_now_iso(), utc_now_iso()),
                )
        except sqlite3.IntegrityError as exc:
            raise RoomSuggestionError(
                "Another suggestion job is active; inspect, cancel or finish it first"
            ) from exc
        return job_id

    @contextlib.contextmanager
    def operation(self):
        """Make a competing active job an actionable, safely rolled-back error."""
        try:
            with self.connect() as con:
                yield con
        except sqlite3.IntegrityError as exc:
            raise RoomSuggestionError(
                "Another suggestion operation is active; cancel or finish it first"
            ) from exc

    def job(self, job_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise RoomSuggestionError("Invalid suggestion job ID")
        with self.connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise RoomSuggestionError("Suggestion job does not exist for this palace")
        data = dict(row)
        for key in ("config", "identity", "selection"):
            data[key] = json.loads(data[key]) if data[key] is not None else None
        return data

    def jobs(self, limit=20):
        with self.connect() as con:
            return [
                dict(row)
                for row in con.execute(
                    "SELECT id,state,created_at,updated_at,error FROM jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            ]

    def state(self, job_id, state, *, error=None):
        with self.operation() as con:
            con.execute(
                "UPDATE jobs SET state=?,error=?,updated_at=? WHERE id=?",
                (state, error, utc_now_iso(), job_id),
            )

    def cancel(self, job_id):
        job = self.job(job_id)
        if job["state"] not in {"queued", "running", "cancelling"}:
            raise RoomSuggestionError("Only pending inference can be cancelled")
        with self.connect() as con:
            con.execute(
                "UPDATE jobs SET cancelled=1,state='cancelling',updated_at=? "
                "WHERE id=? AND state IN ('queued','running','cancelling')",
                (utc_now_iso(), job_id),
            )

    def resume(self, job_id):
        if self.job(job_id)["state"] not in {"failed", "paused", "cancelling", "running", "queued"}:
            raise RoomSuggestionError("Only unfinished inference jobs can be resumed")
        with self.operation() as con:
            con.execute(
                "UPDATE jobs SET cancelled=0,state='queued',error=NULL,updated_at=? WHERE id=?",
                (utc_now_iso(), job_id),
            )

    def snapshot(self, job_id, rows):
        # Publish the entire bounded candidate list atomically before inference.
        with self.connect() as con:
            con.executemany(
                "INSERT INTO items(job_id,id,ordinal,metadata,document_hash) VALUES(?,?,?,?,?)",
                [
                    (job_id, row["id"], n, dumps(row["metadata"]), row["document_hash"])
                    for n, row in enumerate(rows)
                ],
            )
            con.execute(
                "UPDATE jobs SET enumerated=1,updated_at=? WHERE id=?", (utc_now_iso(), job_id)
            )

    def items(self, job_id):
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM items WHERE job_id=? ORDER BY ordinal", (job_id,)
            ).fetchall()
        out = []
        for raw in rows:
            row = dict(raw)
            row["metadata"] = json.loads(row["metadata"])
            row["prediction"] = json.loads(row["prediction"]) if row["prediction"] else None
            out.append(row)
        return out

    def prediction(
        self, job_id, item_id, outcome, prediction=None, *, seconds=0, cache_hit=False, error=None
    ):
        with self.connect() as con:
            con.execute(
                "UPDATE items SET outcome=?,prediction=?,seconds=?,cache_hit=?,error=? "
                "WHERE job_id=? AND id=?",
                (
                    outcome,
                    dumps(prediction) if prediction is not None else None,
                    seconds,
                    int(cache_hit),
                    error,
                    job_id,
                    item_id,
                ),
            )
            con.execute("UPDATE jobs SET updated_at=? WHERE id=?", (utc_now_iso(), job_id))

    def cache_get(self, key):
        with self.connect() as con:
            row = con.execute("SELECT prediction FROM cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, key, prediction):
        with self.connect() as con:
            con.execute("INSERT OR REPLACE INTO cache VALUES(?,?)", (key, dumps(prediction)))

    def pin_identity(self, job_id, identity):
        previous = self.job(job_id)["identity"]
        if previous is not None and previous != identity:
            raise RoomSuggestionError("The model changed; start a new suggestion job")
        with self.connect() as con:
            con.execute("UPDATE jobs SET identity=? WHERE id=?", (dumps(identity), job_id))

    def finish(self, job_id):
        with self.connect() as con:
            con.execute(
                "UPDATE jobs SET state=CASE WHEN cancelled=1 THEN 'paused' ELSE 'ready' END, "
                "updated_at=? WHERE id=?",
                (utc_now_iso(), job_id),
            )

    def journal(self, job_id, selection, moves):
        with self.operation() as con:
            con.executemany(
                "INSERT INTO moves(job_id,kind,id,before_meta,after_meta,document_hash,source_rooms) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        job_id,
                        m["kind"],
                        m["id"],
                        dumps(m["before"]),
                        dumps(m["after"]),
                        m["document_hash"],
                        dumps(m["source_rooms"]) if "source_rooms" in m else None,
                    )
                    for m in moves
                ],
            )
            con.execute(
                "UPDATE jobs SET state='applying',selection=?,updated_at=? WHERE id=?",
                (dumps(sorted(selection)), utc_now_iso(), job_id),
            )

    def moves(self, job_id):
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM moves WHERE job_id=? "
                "ORDER BY CASE kind WHEN 'drawer' THEN 0 ELSE 1 END,id",
                (job_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "before": json.loads(row["before_meta"]),
                "after": json.loads(row["after_meta"]),
                "source_rooms": json.loads(row["source_rooms"]) if row["source_rooms"] else None,
            }
            for row in rows
        ]

    def mark_move(self, job_id, kind, item_id, *, undo=False):
        column = "undone" if undo else "applied"
        with self.connect() as con:
            con.execute(
                f"UPDATE moves SET {column}=1 WHERE job_id=? AND kind=? AND id=?",
                (job_id, kind, item_id),
            )
