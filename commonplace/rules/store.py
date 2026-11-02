"""Per-workspace rules and transactional, append-only version snapshots."""

import hashlib
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from mempalace.sqlite_crypto import connect, row_factory


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sequence(conn):
    return conn.execute("SELECT value FROM meta WHERE key='seq'").fetchone()[0]


def bump(conn, agent):
    conn.execute(
        "INSERT INTO agent_seq(agent,token) VALUES (?,?) "
        "ON CONFLICT(agent) DO UPDATE SET token=excluded.token",
        (agent, secrets.randbits(63) | 1),
    )


def rules_sequence(conn, agent):
    tokens = dict(
        conn.execute("SELECT agent,token FROM agent_seq WHERE agent IN (?, '*')", (agent,))
    )
    digest = hashlib.sha256(f"{tokens.get(agent, 0)}:{tokens['*']}".encode()).digest()
    return 100_000_000 + int.from_bytes(digest[:8], "big") % 900_000_000


def migrate(conn, boards_visibility=0):
    """Add display and target columns without changing existing rule text or history."""
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO meta VALUES ('store_id', (random() & 0x7fffffffffffffff) | 1)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_seq (agent TEXT PRIMARY KEY, token INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO agent_seq VALUES ('*', (random() & 0x7fffffffffffffff) | 1)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_served (agent TEXT PRIMARY KEY, "
            "state TEXT NOT NULL CHECK (state IN ('served','too_large')), rules_seq INTEGER, at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta VALUES ('boards_visibility_seen', ?)", (boards_visibility,)
        )
        if "origin_agent" not in {r[1] for r in conn.execute("PRAGMA table_info(rules)")}:
            # The agent a rule first applied to keeps matching its republishes after a move.
            conn.execute("ALTER TABLE rules ADD COLUMN origin_agent TEXT")
            conn.execute("UPDATE rules SET origin_agent=agent WHERE agent!='*'")
        if "agent_text_by" not in {r[1] for r in conn.execute("PRAGMA table_info(rules)")}:
            # Until rules could move, the rule's own agent wrote its agent_text.
            conn.execute("ALTER TABLE rules ADD COLUMN agent_text_by TEXT")
            conn.execute("UPDATE rules SET agent_text_by=origin_agent WHERE agent_text IS NOT NULL")
        if "agent" not in {r[1] for r in conn.execute("PRAGMA table_info(rule_versions)")}:
            conn.execute("ALTER TABLE rule_versions ADD COLUMN agent TEXT")
        for table in ("rules", "rule_versions"):
            if "summary" not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN summary TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS rules_by_origin ON rules(origin_agent, key)")
        if own_transaction:
            conn.commit()
    except BaseException:
        if own_transaction:
            conn.rollback()
        raise


class RuleStore:
    _initialized = set()
    _init_lock = threading.Lock()

    def __init__(self, workspace_dir):
        directory = Path(workspace_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = str((directory / "rules.sqlite3").resolve())
        with self._init_lock:
            if self.path not in self._initialized or not Path(self.path).exists():
                with self.connection() as conn:
                    conn.executescript(Path(__file__).with_name("schema.sql").read_text())
                    from .checks import visibility

                    migrate(conn, visibility(directory))
                self._initialized.add(self.path)

    @contextmanager
    def connection(self):
        conn = connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = row_factory
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def read(self):
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.rollback()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @property
    def seq(self):
        with self.connection() as conn:
            return sequence(conn)
