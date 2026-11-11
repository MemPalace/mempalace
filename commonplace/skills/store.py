"""Workspace-local Markdown files with append-only, transactional version metadata."""

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from commonplace.admission import GATE
from commonplace.extensions import ToolError

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    skill_id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
    sha256 TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(skill_id, version)
);
CREATE TABLE IF NOT EXISTS installs (receipt TEXT PRIMARY KEY, skill_ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    skill_id TEXT NOT NULL, version INTEGER NOT NULL, path TEXT NOT NULL,
    sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    PRIMARY KEY(skill_id, version, path)
);
"""
# Agents are served a skill only while a share row names them. Skills stored before sharing
# existed were readable by every agent of the workspace, so they start as workspace shares.
SHARES_SCHEMA = """
CREATE TABLE shares (
    skill_id TEXT NOT NULL, target TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(skill_id, target)
);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


WORKSPACE = "workspace"
PROJECT = "project:"


def opaque(prefix):
    return prefix + secrets.token_hex(16)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SkillStore:
    def __init__(self, workspace_dir):
        self.root = Path(workspace_dir) / "skills"
        with GATE.creating(self.root):
            self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "index.sqlite3"
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='shares'").fetchone() is None:
                conn.execute(SHARES_SCHEMA)
                conn.execute(
                    "INSERT INTO shares SELECT DISTINCT skill_id,?,? FROM versions",
                    (WORKSPACE, now()),
                )
            conn.commit()

    @contextmanager
    def connection(self):
        with GATE.creating(self.path):
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
        return {
            **result,
            "source": json.loads(row["source"]),
            "markdown": text,
            "files": self._files(row["skill_id"], row["version"], content=True),
        }

    def _files(self, skill_id, version, *, content=False):
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT path,sha256,size FROM files WHERE skill_id=? AND version=? ORDER BY path",
                (skill_id, version),
            ).fetchall()
        files = []
        for row in rows:
            item = dict(row)
            if content:
                try:
                    data = (self.root / skill_id / "files" / row["sha256"]).read_bytes()
                    item["content"] = data.decode("utf-8")
                except (OSError, UnicodeError):
                    raise ToolError("no: skill file is unavailable") from None
                if digest(item["content"]) != row["sha256"]:
                    raise ToolError("no: skill file integrity check failed")
            files.append(item)
        return files

    @staticmethod
    def _visible(targets):
        """SQL guard for a reader limited to ``targets``; None means the owner sees all."""
        if targets is None:
            return "", ()
        marks = ",".join("?" * len(targets))
        return (
            f" AND EXISTS(SELECT 1 FROM shares s WHERE s.skill_id=versions.skill_id "
            f"AND s.target IN ({marks}))",
            tuple(sorted(targets)),
        )

    def get(self, skill_id, version=None, targets=None):
        # IDs are compared in SQLite before ever forming a filesystem path.
        # ``targets`` limits an agent to skills shared with it, checked on every read.
        if not isinstance(skill_id, str):
            raise ToolError("no: skill not found")
        if version is not None and (type(version) is not int or not 1 <= version < 2**63):
            raise ToolError("no: version must be a positive integer")
        guard, marks = self._visible(targets)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM versions WHERE skill_id=? "
                + ("" if version is None else "AND version=? ")
                + guard
                + (" ORDER BY version DESC LIMIT 1" if version is None else ""),
                (skill_id,) + (() if version is None else (version,)) + marks,
            ).fetchone()
        if row is None:
            raise ToolError("no: skill not found")
        return self._record(row)

    def list(self, targets=None):
        guard, marks = self._visible(targets)
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT versions.* FROM versions JOIN "
                "(SELECT skill_id,MAX(version) version FROM versions GROUP BY skill_id) latest "
                "USING(skill_id,version) WHERE 1=1" + guard + " ORDER BY name,skill_id",
                marks,
            ).fetchall()
        return [
            dict(
                row,
                source=json.loads(row["source"]),
                files=self._files(row["skill_id"], row["version"]),
            )
            for row in rows
        ]

    def history(self, skill_id, targets=None):
        self.get(skill_id, targets=targets)
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM versions WHERE skill_id=? ORDER BY version", (skill_id,)
            ).fetchall()
        return [self._record(row) for row in rows]

    def shares(self, skill_id):
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT target FROM shares WHERE skill_id=? ORDER BY target", (skill_id,)
            ).fetchall()
        return [row[0] for row in rows]

    def project_targets(self):
        """Every project target any skill is currently shared with."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT target FROM shares WHERE target LIKE ? ESCAPE '\\'",
                (PROJECT + "%",),
            ).fetchall()
        return {row[0] for row in rows}

    def share(self, skill_id, target):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "SELECT 1 FROM versions WHERE skill_id=? LIMIT 1", (skill_id,)
            ).fetchone():
                raise ToolError("no: skill not found")
            conn.execute("INSERT OR IGNORE INTO shares VALUES (?,?,?)", (skill_id, target, now()))
            conn.commit()
        return self.shares(skill_id)

    def revoke(self, skill_id, target=None):
        """Drop one share, or every share when ``target`` is None. Takes effect on the next read."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "SELECT 1 FROM versions WHERE skill_id=? LIMIT 1", (skill_id,)
            ).fetchone():
                raise ToolError("no: skill not found")
            conn.execute(
                "DELETE FROM shares WHERE skill_id=?" + ("" if target is None else " AND target=?"),
                (skill_id,) + (() if target is None else (target,)),
            )
            conn.commit()
        return self.shares(skill_id)

    def append(
        self, entries, *, skill_id=None, expected_version=None, receipt=None, shares=(WORKSPACE,)
    ):
        """Publish file references only after every new file is complete; old files stay intact."""
        paths = []
        records = []
        # The whole append (files, then rows) is one creator: a backup cut sees all or none.
        with GATE.creating():
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
                        if skill_id is None:
                            for target in shares:
                                conn.execute(
                                    "INSERT OR IGNORE INTO shares VALUES (?,?,?)",
                                    (sid, target, record["created_at"]),
                                )
                        rows = self._write_files(conn, directory, record, entry.get("files"), paths)
                        records.append(
                            {**entry, **record, "source": entry["source"], "files": rows}
                        )
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
        return [
            dict(row, files=self._files(row["skill_id"], row["version"], content=True))
            for row in records
        ]

    def _write_files(self, conn, directory, record, files, paths):
        """Store a version's helper files by content hash; ``None`` keeps the previous version's."""
        sid, version = record["skill_id"], record["version"]
        if files is None:
            rows = conn.execute(
                "SELECT path,sha256,size FROM files WHERE skill_id=? AND version=?",
                (sid, version - 1),
            ).fetchall()
            rows = [tuple(row) for row in rows]
        else:
            rows = []
            for item in files:
                data = item["content"].encode("utf-8")
                sha256 = digest(item["content"])
                folder = directory / "files"
                path = folder / sha256
                with GATE.creating():
                    folder.mkdir(exist_ok=True)
                    if path.exists():
                        if path.read_bytes() != data:
                            raise ToolError("no: skill file integrity check failed")
                    else:
                        with path.open("xb") as stream:
                            paths.append(path)
                            stream.write(data)
                rows.append((item["path"], sha256, len(data)))
        for row in rows:
            conn.execute("INSERT INTO files VALUES (?,?,?,?,?)", (sid, version, *row))
        return rows
