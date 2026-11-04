"""Model queue and private append-only workspace replies."""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commonplace.chat.types import JOB_CODES, MAX_PENDING_PER_WORKSPACE, STALE_AFTER_S, Job, Reply
from mempalace.sqlite_crypto import connect, row_factory

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_front_jobs (            -- model answers
    workspace_id TEXT NOT NULL,
    board TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','running','done','superseded','skipped','failed')),
    code TEXT NOT NULL DEFAULT '',            -- JOB_CODES
    called INTEGER NOT NULL DEFAULT 0,        -- 1 when a model request was sent (daily caps)
    created_at TEXT NOT NULL,                 -- UTC ISO-8601
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, board, message_id)
);
CREATE INDEX IF NOT EXISTS chat_front_jobs_due ON chat_front_jobs(state, created_at);
"""


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(_SCHEMA)


_REPLIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_replies (
    id INTEGER PRIMARY KEY,
    board TEXT NOT NULL,
    message_id INTEGER NOT NULL,             -- space_messages.id answered
    for_user TEXT NOT NULL,                  -- poster's user id; only they read it (OD5)
    kind TEXT NOT NULL CHECK (kind IN ('receipt','answer','handoff')),
    code TEXT NOT NULL,                      -- RECEIPT_CODES, 'ok' for answers, HANDOFF_CODES for handoff notices
    text TEXT NOT NULL,                      -- redacted before storage
    redacted TEXT NOT NULL DEFAULT '[]',     -- JSON list of redact KINDS
    task INTEGER,
    model TEXT,                              -- 'provider/model' for answers, NULL otherwise
    created_at TEXT NOT NULL,
    UNIQUE (board, message_id, kind)
);
CREATE INDEX IF NOT EXISTS chat_replies_by_user ON chat_replies(for_user, board, id);
CREATE TRIGGER IF NOT EXISTS chat_replies_no_update BEFORE UPDATE ON chat_replies
BEGIN SELECT RAISE(ABORT, 'chat replies are append-only'); END;
CREATE TRIGGER IF NOT EXISTS chat_replies_no_delete BEFORE DELETE ON chat_replies
BEGIN SELECT RAISE(ABORT, 'chat replies are append-only'); END;
"""


def _stamp(now=None):
    return (
        datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, timezone.utc)
    ).isoformat()


def open_replies(gateway, workspace_id: str) -> "ReplyStore":
    stores = gateway.__dict__.setdefault("_chat_replies", {})
    return stores.setdefault(workspace_id, ReplyStore(gateway, workspace_id))


class ReplyStore:
    def __init__(self, gateway, workspace_id):
        self._supervisor = gateway.supervisor
        self._workspace_id = workspace_id
        self._lock = threading.Lock()
        self._conn = None
        self._path = None
        self._schema_ready = False

    def _connection(self, *, create=False):
        path = Path(self._supervisor.workspace_dir(self._workspace_id)).resolve() / "chat.sqlite3"
        if path != self._path or (not create and not path.is_file()):
            if self._conn is not None:
                self._conn.close()
            self._conn, self._path, self._schema_ready = None, path, False
        if not create and not path.is_file():
            return None
        if self._conn is None:
            if create:
                path.parent.mkdir(parents=True, exist_ok=True)
            conn = connect(
                str(path) if create else path.as_uri() + "?mode=rw",
                uri=not create,
                timeout=0.05,
                check_same_thread=False,
                isolation_level=None,
            )
            try:
                conn.row_factory = row_factory
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                conn.close()
                raise
            self._conn = conn
        if create and not self._schema_ready:
            self._conn.executescript(_REPLIES_SCHEMA)
            self._schema_ready = True
        return self._conn

    @staticmethod
    def _reply(row):
        data = dict(row)
        data["at"] = data.pop("created_at")
        data["redacted"] = tuple(json.loads(data["redacted"]))
        return Reply(**data)

    def add(
        self, *, board, message_id, for_user, kind, code, text, redacted, model, task=None
    ) -> Reply | None:
        if not self._lock.acquire(timeout=0.05):
            raise TimeoutError("Chat replies busy")
        try:
            row = (
                self._connection(create=True)
                .execute(
                    "INSERT INTO chat_replies (board,message_id,for_user,kind,code,text,redacted,model,task,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(board,message_id,kind) DO NOTHING RETURNING *",
                    (
                        board,
                        message_id,
                        for_user,
                        kind,
                        code,
                        text,
                        json.dumps(list(redacted)),
                        model,
                        task,
                        _stamp(),
                    ),
                )
                .fetchone()
            )
            return self._reply(row) if row is not None else None
        finally:
            self._lock.release()

    def page(self, *, board, for_user, message_ids=(), after_id=0, limit=100) -> list[Reply]:
        message_ids = tuple(message_ids)
        match = (
            "message_id IN (" + ",".join("?" for _ in message_ids) + ") OR " if message_ids else ""
        )
        with self._lock:
            conn = self._connection()
            if conn is None:
                return []
            return [
                self._reply(row)
                for row in conn.execute(
                    "SELECT * FROM chat_replies WHERE board=? AND for_user=? "
                    f"AND ({match}id>?) ORDER BY id LIMIT ?",
                    (board, for_user, *message_ids, after_id, limit),
                ).fetchall()
            ]

    def latest_id(self, *, board, for_user) -> int:
        with self._lock:
            conn = self._connection()
            if conn is None:
                return 0
            return conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM chat_replies WHERE board=? AND for_user=?",
                (board, for_user),
            ).fetchone()[0]


def enqueue(db, workspace_id, board, message_id, user_id) -> bool:
    now = _stamp()
    with db._lock, db._tx() as conn:
        key = (workspace_id, board, message_id)
        if conn.execute(
            "SELECT 1 FROM chat_front_jobs WHERE workspace_id=? AND board=? AND message_id=?", key
        ).fetchone():
            return False
        busy = (
            conn.execute(
                "SELECT COUNT(*) FROM chat_front_jobs WHERE workspace_id=? AND state IN ('pending','running')",
                (workspace_id,),
            ).fetchone()[0]
            >= MAX_PENDING_PER_WORKSPACE
        )
        conn.execute(
            "INSERT INTO chat_front_jobs (workspace_id,board,message_id,user_id,state,code,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (*key, user_id, "skipped" if busy else "pending", "busy" if busy else "", now, now),
        )
        return not busy


def claim(db) -> Job | None:
    with db._lock, db._tx() as conn:
        row = conn.execute(
            "UPDATE chat_front_jobs SET state='running',updated_at=? "
            "WHERE (workspace_id,board,message_id) = (SELECT workspace_id,board,message_id FROM chat_front_jobs "
            "WHERE state='pending' ORDER BY created_at,workspace_id,board,message_id LIMIT 1) AND state='pending' RETURNING *",
            (_stamp(),),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["called"] = bool(data["called"])
        return Job(**data)


def finish(db, job: Job, state: str, code: str, *, called: bool = False) -> None:
    if code not in JOB_CODES and code != "":
        raise ValueError("Invalid job code")
    with db._lock:
        db._conn.execute(
            "UPDATE chat_front_jobs SET state=?,code=?,called=MAX(called,?),updated_at=? "
            "WHERE workspace_id=? AND board=? AND message_id=? AND state='running'",
            (state, code, int(called), _stamp(), job.workspace_id, job.board, job.message_id),
        )


def recover(db, now: float) -> None:
    with db._lock, db._tx() as conn:
        for state, target, code in (
            ("running", "failed", "interrupted"),
            ("pending", "skipped", "stale"),
        ):
            conn.execute(
                "UPDATE chat_front_jobs SET state=?,code=?,updated_at=? WHERE state=? AND julianday(updated_at)<julianday(?)",
                (target, code, _stamp(now), state, _stamp(now - STALE_AFTER_S)),
            )


def counts_today(db, workspace_id, user_id: str | None = None) -> dict:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with db._lock:
        row = db._conn.execute(
            "SELECT COALESCE(SUM(called),0), COALESCE(SUM(CASE WHEN user_id=? THEN called ELSE 0 END),0) "
            "FROM chat_front_jobs WHERE workspace_id=? AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)",
            (user_id, workspace_id, start.isoformat(), (start + timedelta(days=1)).isoformat()),
        ).fetchone()
        error = db._conn.execute(
            "SELECT code FROM chat_front_jobs WHERE workspace_id=? AND state IN ('failed','skipped') "
            "AND code<>'' ORDER BY updated_at DESC,created_at DESC,message_id DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
    return {
        "used_today": row[0],
        "user_used_today": row[1],
        "last_error": error[0] if error else None,
    }
