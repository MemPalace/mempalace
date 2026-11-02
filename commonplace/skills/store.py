"""Workspace-local Markdown files with append-only, transactional version metadata."""

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from commonplace.extensions import ToolError

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    skill_id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
    sha256 TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(skill_id, version)
);
CREATE TABLE IF NOT EXISTS installs (receipt TEXT PRIMARY KEY, skill_ids TEXT NOT NULL);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def opaque(prefix):
    return prefix + secrets.token_hex(16)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SkillStore:
    def __init__(self, workspace_dir):
        self.root = Path(workspace_dir) / "skills"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "index.sqlite3"
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _record(self, row):
        result = dict(row)
        path = self.root / row["skill_id"] / f"{row['version']}-{row['sha256']}.md"
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            raise ToolError("no: skill Markdown is unavailable") from None
        if digest(text) != row["sha256"]:
            raise ToolError("no: skill Markdown integrity check failed")
        return {**result, "source": json.loads(row["source"]), "markdown": text}

    def get(self, skill_id, version=None):
        # IDs are compared in SQLite before ever forming a filesystem path.
        if not isinstance(skill_id, str):
            raise ToolError("no: skill not found")
        if version is not None and (type(version) is not int or not 1 <= version < 2**63):
            raise ToolError("no: version must be a positive integer")
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM versions WHERE skill_id=? "
                + ("ORDER BY version DESC LIMIT 1" if version is None else "AND version=?"),
                (skill_id,) if version is None else (skill_id, version),
            ).fetchone()
        if row is None:
            raise ToolError("no: skill not found")
        return self._record(row)

    def list(self):
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT v.* FROM versions v JOIN "
                "(SELECT skill_id,MAX(version) version FROM versions GROUP BY skill_id) latest "
                "USING(skill_id,version) ORDER BY name,skill_id"
            ).fetchall()
        return [dict(row, source=json.loads(row["source"])) for row in rows]

    def history(self, skill_id):
        self.get(skill_id)
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM versions WHERE skill_id=? ORDER BY version", (skill_id,)
            ).fetchall()
        return [self._record(row) for row in rows]

    def append(self, entries, *, skill_id=None, expected_version=None, receipt=None):
        """Publish file references only after every new file is complete; old files stay intact."""
        paths = []
        records = []
        try:
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if receipt:
                    existing = conn.execute(
                        "SELECT skill_ids FROM installs WHERE receipt=?", (receipt,)
                    ).fetchone()
                    if existing:
                        return [self.get(sid, 1) for sid in json.loads(existing[0])]
                if skill_id is not None:
                    current = conn.execute(
                        "SELECT MAX(version) FROM versions WHERE skill_id=?", (skill_id,)
                    ).fetchone()[0]
                    if current is None:
                        raise ToolError("no: skill not found")
                    if type(expected_version) is not int or expected_version != current:
                        raise ToolError("no: skill version changed")
                for entry in entries:
                    sid = skill_id or opaque("sk_")
                    version = expected_version + 1 if skill_id else 1
                    directory = self.root / sid
                    directory.mkdir(exist_ok=True)
                    sha256 = digest(entry["markdown"])
                    path = directory / f"{version}-{sha256}.md"
                    # A crash before the metadata commit may leave a complete orphan.
                    # Reuse only an identical orphan; never overwrite a prior version.
                    if path.exists():
                        if path.read_bytes() != entry["markdown"].encode("utf-8"):
                            raise ToolError("no: skill Markdown integrity check failed")
                    else:
                        with path.open("xb") as stream:
                            paths.append(path)
                            stream.write(entry["markdown"].encode("utf-8"))
                    record = dict(
                        skill_id=sid,
                        version=version,
                        name=entry["name"],
                        sha256=sha256,
                        source=json.dumps(entry["source"], ensure_ascii=False),
                        created_at=now(),
                    )
                    conn.execute(
                        "INSERT INTO versions VALUES "
                        "(:skill_id,:version,:name,:sha256,:source,:created_at)",
                        record,
                    )
                    records.append({**entry, **record, "source": entry["source"]})
                if receipt:
                    conn.execute(
                        "INSERT INTO installs VALUES (?,?)",
                        (receipt, json.dumps([row["skill_id"] for row in records])),
                    )
                conn.commit()
        except BaseException:
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        return records
