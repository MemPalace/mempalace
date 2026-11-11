"""Per-person bot threads in the workspace file ``bots.sqlite3``.

Opened like ``chat.sqlite3`` (encrypted with the workspace when encryption is on). Messages are
append-only and verbatim; memory notes are removed with a tombstone. Every method that a caller
reaches takes the user id and answers :class:`Missing` for a bot that is not that user's, so a
foreign id is indistinguishable from an unknown one. Errors never carry conversation text.
"""

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commonplace.admission import GATE
from mempalace.sqlite_crypto import connect, row_factory

MAX_LIVE_BOTS = 50
NAME_MAX, JOB_MAX, INSTRUCTIONS_MAX, TEXT_MAX, NOTE_MAX = 60, 200, 4000, 8000, 2000
PAGE_DEFAULT, PAGE_MAX = 50, 100
PENDING_TTL_S = 600
KINDS = ("project", "general", "custom")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    job TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('project','general','custom')),
    board TEXT,
    read_upto INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS bots_project ON bots(user_id, board) WHERE kind='project';
CREATE UNIQUE INDEX IF NOT EXISTS bots_general ON bots(user_id) WHERE kind='general';
CREATE INDEX IF NOT EXISTS bots_by_user ON bots(user_id);
CREATE TABLE IF NOT EXISTS bot_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user','bot','event')),
    text TEXT NOT NULL,
    blocks TEXT NOT NULL DEFAULT '[]',
    request_id TEXT,
    reply_to INTEGER,
    model TEXT,
    state TEXT NOT NULL DEFAULT 'ok',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bot_messages_by_bot ON bot_messages(bot_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS bot_messages_request
    ON bot_messages(bot_id, request_id) WHERE role='user';
CREATE UNIQUE INDEX IF NOT EXISTS bot_messages_reply
    ON bot_messages(bot_id, reply_to) WHERE role='bot';
CREATE TRIGGER IF NOT EXISTS bot_messages_no_update BEFORE UPDATE ON bot_messages
BEGIN SELECT RAISE(ABORT, 'bot messages are append-only'); END;
CREATE TRIGGER IF NOT EXISTS bot_messages_no_delete BEFORE DELETE ON bot_messages
BEGIN SELECT RAISE(ABORT, 'bot messages are append-only'); END;
CREATE TABLE IF NOT EXISTS bot_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('started','ok','refused')),
    blocks TEXT NOT NULL DEFAULT '[]',
    UNIQUE (bot_id, request_id, step)
);
CREATE TABLE IF NOT EXISTS bot_pending (
    id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','done','cancelled','expired'))
);
CREATE INDEX IF NOT EXISTS bot_pending_by_bot ON bot_pending(bot_id, state);
CREATE TABLE IF NOT EXISTS bot_work (
    bot_id TEXT NOT NULL,
    board TEXT NOT NULL,
    card INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    last_word TEXT,
    last_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, board, card)
);
CREATE TABLE IF NOT EXISTS bot_forwards (
    bot_id TEXT NOT NULL,
    board TEXT NOT NULL,
    space_message_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    answered_by INTEGER,
    PRIMARY KEY (bot_id, board, space_message_id)
);
CREATE INDEX IF NOT EXISTS bot_forwards_by_post ON bot_forwards(board, space_message_id);
CREATE INDEX IF NOT EXISTS bot_work_by_card ON bot_work(board, card);
CREATE TABLE IF NOT EXISTS bot_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    removed_at TEXT
);
CREATE INDEX IF NOT EXISTS bot_memory_by_bot ON bot_memory(bot_id, id);
CREATE TRIGGER IF NOT EXISTS bot_memory_no_delete BEFORE DELETE ON bot_memory
BEGIN SELECT RAISE(ABORT, 'bot memory is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bot_memory_tombstone_only BEFORE UPDATE ON bot_memory
WHEN OLD.removed_at IS NOT NULL OR NEW.text IS NOT OLD.text OR NEW.bot_id IS NOT OLD.bot_id
    OR NEW.created_at IS NOT OLD.created_at OR NEW.removed_at IS NULL
BEGIN SELECT RAISE(ABORT, 'bot memory is append-only'); END;
"""


# Columns added after the first release; ``_upgrade`` adds them to an existing file.
_ADDED = (
    ("bot_work", "last_word", "TEXT"),
    ("bot_work", "last_seq", "INTEGER NOT NULL DEFAULT 0"),
    ("bot_forwards", "answered_by", "INTEGER"),
)
UPDATE_MAX = 100_000  # the longest space message, so an orchestrator reply is stored whole


class Missing(LookupError):
    """No such bot for this person (unknown and foreign ids look the same)."""


class Invalid(ValueError):
    """A value outside its bound. The message is fixed text, never user content."""


class Limit(Exception):
    """The person already has the maximum number of live bots."""


class Conflict(Exception):
    """A request id reused with other text, or a second reply to one message."""


def stamp(now=None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value, name, low, high):
    """``value`` unchanged when its raw length is within ``high`` (and not blank when ``low``)."""
    if not isinstance(value, str) or len(value) > high or (low and not value.strip()):
        raise Invalid(f"{name} must be {low}-{high} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise Invalid(f"{name} must be valid text") from None
    return value


def _name(value):
    return _text(value.strip() if isinstance(value, str) else value, "name", 1, NAME_MAX)


_BOT_KEYS = ("id", "name", "job", "instructions", "kind", "board", "read_upto", "created_at")


def _bot(row) -> dict:
    return {key: row[key] for key in _BOT_KEYS}


def _message(row) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "text": row["text"],
        "blocks": json.loads(row["blocks"]),
        "at": row["created_at"],
        "state": row["state"],
        "reply_to": row["reply_to"],
        "model": row["model"],
        "request_id": row["request_id"],
    }


def open_bots(gateway, workspace_id: str) -> "BotStore":
    stores = gateway.__dict__.setdefault("_bot_stores", {})
    return stores.setdefault(workspace_id, BotStore(gateway.supervisor, workspace_id))


class BotStore:
    def __init__(self, supervisor, workspace_id):
        self._supervisor = supervisor
        self._workspace_id = workspace_id
        self._lock = threading.RLock()
        self._conn = None
        self._path = None

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
            self._conn = self._path = None

    def _connection(self):
        path = Path(self._supervisor.workspace_dir(self._workspace_id)).resolve() / "bots.sqlite3"
        if path != self._path and self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._conn is None:
            with GATE.creating(path):
                path.parent.mkdir(parents=True, exist_ok=True)
                conn = connect(str(path), timeout=5, check_same_thread=False, isolation_level=None)
            try:
                conn.row_factory = row_factory
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(_SCHEMA)
                self._upgrade(conn)
            except Exception:
                conn.close()
                raise
            self._conn, self._path = conn, path
        return self._conn

    @staticmethod
    def _upgrade(conn):
        """Add columns that a file made by an earlier version lacks."""
        for table, column, kind in _ADDED:
            if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    @contextmanager
    def _tx(self, *, write=True):
        if not self._lock.acquire(timeout=5):
            raise TimeoutError("Bots busy")
        try:
            conn = self._connection()
            if not write:
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            self._lock.release()

    @staticmethod
    def _own(conn, bot_id, user_id):
        row = (
            conn.execute(
                "SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, user_id)
            ).fetchone()
            if isinstance(bot_id, str)
            else None
        )
        if row is None:
            raise Missing()
        return row

    @staticmethod
    def _live(conn, user_id):
        return conn.execute(
            "SELECT COUNT(*) FROM bots WHERE user_id=? AND archived_at IS NULL", (user_id,)
        ).fetchone()[0]

    # Bots

    def get(self, bot_id, user_id) -> dict:
        with self._tx(write=False) as conn:
            row = self._own(conn, bot_id, user_id)
            return {**_bot(row), "archived": row["archived_at"] is not None}

    def create(self, user_id, name, job="", instructions="", kind="custom", board=None) -> dict:
        name = _name(name)
        job = _text(job, "job", 0, JOB_MAX)
        instructions = _text(instructions, "instructions", 0, INSTRUCTIONS_MAX)
        if kind not in KINDS or (kind == "project") != (board is not None and kind != "custom"):
            raise Invalid("invalid kind")
        if board is not None and (not isinstance(board, str) or not board):
            raise Invalid("invalid board")
        with self._tx() as conn:
            if self._live(conn, user_id) >= MAX_LIVE_BOTS:
                raise Limit()
            bot_id = uuid.uuid4().hex
            try:
                conn.execute(
                    "INSERT INTO bots (id,user_id,name,job,instructions,kind,board,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (bot_id, user_id, name, job, instructions, kind, board, stamp()),
                )
            except sqlite3.IntegrityError:
                raise Conflict() from None
            return {**_bot(self._own(conn, bot_id, user_id)), "archived": False}

    def ensure_defaults(self, user_id, boards) -> None:
        """One general bot and one project bot per ``(slug, name)``; never recreates an archived one.

        Stops at the live-bot bound; the general bot is created first.
        """
        wanted = [("general", None, "General", "Anything across your projects")] + [
            ("project", slug, name[:NAME_MAX] or slug, "") for slug, name in boards
        ]
        with self._tx() as conn:
            existing = {
                (r["kind"], r["board"])
                for r in conn.execute(
                    "SELECT kind,board FROM bots WHERE user_id=? AND kind<>'custom'", (user_id,)
                )
            }
            live = self._live(conn, user_id)
            for kind, board, name, job in wanted:
                if (kind, board) in existing or live >= MAX_LIVE_BOTS:
                    continue
                conn.execute(
                    "INSERT INTO bots (id,user_id,name,job,kind,board,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, user_id, name, job, kind, board, stamp()),
                )
                live += 1

    def update(self, bot_id, user_id, **fields) -> dict:
        """Change name, job, instructions or ``archived`` (bool) of one bot."""
        changes = {}
        if "name" in fields:
            changes["name"] = _name(fields["name"])
        if "job" in fields:
            changes["job"] = _text(fields["job"], "job", 0, JOB_MAX)
        if "instructions" in fields:
            changes["instructions"] = _text(
                fields["instructions"], "instructions", 0, INSTRUCTIONS_MAX
            )
        if "archived" in fields and type(fields["archived"]) is not bool:
            raise Invalid("archived must be true or false")
        with self._tx() as conn:
            row = self._own(conn, bot_id, user_id)
            if "archived" in fields:
                archived = fields["archived"]
                if not archived and row["archived_at"] is not None:
                    if self._live(conn, user_id) >= MAX_LIVE_BOTS:
                        raise Limit()
                if archived != (row["archived_at"] is not None):
                    changes["archived_at"] = stamp() if archived else None
            if changes:
                conn.execute(
                    "UPDATE bots SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?",
                    (*changes.values(), bot_id),
                )
            row = self._own(conn, bot_id, user_id)
            return {**_bot(row), "archived": row["archived_at"] is not None}

    def roster(self, user_id, bot_id=None) -> list[dict]:
        """Live bots (or just ``bot_id``, archived or not) with last message and unread count.

        Most recent activity first; bots never used come after used ones.
        """
        with self._tx(write=False) as conn:
            rows = conn.execute(
                "SELECT b.*, m.id AS last_id, m.role AS last_role, m.text AS last_text, "
                "m.created_at AS last_at, "
                "(SELECT COUNT(*) FROM bot_messages u WHERE u.bot_id=b.id AND u.role<>'user' "
                "AND u.id>b.read_upto) AS unread "
                "FROM bots b LEFT JOIN bot_messages m ON m.id="
                "(SELECT MAX(id) FROM bot_messages WHERE bot_id=b.id) "
                "WHERE b.user_id=? AND (b.id=? OR (? IS NULL AND b.archived_at IS NULL)) "
                "ORDER BY (m.id IS NULL), m.id DESC, b.rowid",
                (user_id, bot_id, bot_id),
            ).fetchall()
        if bot_id is not None and not rows:
            raise Missing()
        return [
            {
                **_bot(row),
                "archived": row["archived_at"] is not None,
                "last": {"role": row["last_role"], "text": row["last_text"], "at": row["last_at"]}
                if row["last_id"] is not None
                else None,
                "unread": row["unread"],
            }
            for row in rows
        ]

    def mark_read(self, bot_id, user_id, upto) -> int:
        """Move the read mark forward to ``upto`` (never past the newest message)."""
        if type(upto) is not int or not 0 <= upto < 2**63:
            raise Invalid("upto must be a message id")
        with self._tx() as conn:
            row = self._own(conn, bot_id, user_id)
            newest = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM bot_messages WHERE bot_id=?", (bot_id,)
            ).fetchone()[0]
            mark = max(row["read_upto"], min(upto, newest))
            conn.execute("UPDATE bots SET read_upto=? WHERE id=?", (mark, bot_id))
            return mark

    # Messages

    def _add(self, conn, bot_id, role, text, **fields) -> dict:
        cursor = conn.execute(
            "INSERT INTO bot_messages (bot_id,role,text,blocks,request_id,reply_to,model,state,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                bot_id,
                role,
                text,
                json.dumps(fields.get("blocks") or [], ensure_ascii=False),
                fields.get("request_id"),
                fields.get("reply_to"),
                fields.get("model"),
                fields.get("state", "ok"),
                stamp(),
            ),
        )
        return _message(
            conn.execute("SELECT * FROM bot_messages WHERE id=?", (cursor.lastrowid,)).fetchone()
        )

    def add_user_message(self, bot_id, user_id, text, request_id) -> dict:
        """Store a message once per ``request_id``. The same text returns the stored row."""
        _text(text, "text", 1, TEXT_MAX)
        if not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None:
            raise Invalid("invalid request id")
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            row = conn.execute(
                "SELECT * FROM bot_messages WHERE bot_id=? AND request_id=? AND role='user'",
                (bot_id, request_id),
            ).fetchone()
            if row is None:
                return self._add(conn, bot_id, "user", text, request_id=request_id)
            if row["text"] != text:
                raise Conflict()
            return _message(row)

    def add_bot_message(
        self, bot_id, user_id, reply_to, text, blocks=(), model=None, state="ok"
    ) -> dict:
        """The one reply to user message ``reply_to``; a second reply is a conflict."""
        _text(text, "text", 0, TEXT_MAX * 4)
        if state != "ok" and state not in ("stopped",) and not state.startswith("failed:"):
            raise Invalid("invalid state")
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            if not conn.execute(
                "SELECT 1 FROM bot_messages WHERE id=? AND bot_id=? AND role='user'",
                (reply_to, bot_id),
            ).fetchone():
                raise Invalid("reply_to is not a message of this bot")
            try:
                return self._add(
                    conn,
                    bot_id,
                    "bot",
                    text,
                    blocks=list(blocks),
                    reply_to=reply_to,
                    model=model,
                    state=state,
                )
            except sqlite3.IntegrityError:
                raise Conflict() from None

    def add_event_message(self, bot_id, user_id, text, blocks=()) -> dict:
        _text(text, "text", 0, TEXT_MAX)
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            return self._add(conn, bot_id, "event", text, blocks=list(blocks))

    def message_for_request(self, bot_id, user_id, request_id) -> dict | None:
        with self._tx(write=False) as conn:
            self._own(conn, bot_id, user_id)
            row = conn.execute(
                "SELECT * FROM bot_messages WHERE bot_id=? AND request_id=? AND role='user'",
                (bot_id, request_id),
            ).fetchone()
            return _message(row) if row else None

    def reply_to(self, bot_id, user_id, message_id) -> dict | None:
        with self._tx(write=False) as conn:
            self._own(conn, bot_id, user_id)
            row = conn.execute(
                "SELECT * FROM bot_messages WHERE bot_id=? AND role='bot' AND reply_to=?",
                (bot_id, message_id),
            ).fetchone()
            return _message(row) if row else None

    def messages(self, bot_id, user_id, after=None, before=None, limit=PAGE_DEFAULT) -> list[dict]:
        """Oldest first. ``after`` pages forward; otherwise the newest ``limit``, older than ``before``."""
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise Invalid("limit must be 1-100")
        for value in (after, before):
            if value is not None and (type(value) is not int or not 0 <= value < 2**63):
                raise Invalid("invalid cursor")
        if after is not None and before is not None:
            raise Invalid("invalid cursor")
        with self._tx(write=False) as conn:
            self._own(conn, bot_id, user_id)
            if after is not None:
                rows = conn.execute(
                    "SELECT * FROM bot_messages WHERE bot_id=? AND id>? ORDER BY id LIMIT ?",
                    (bot_id, after, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bot_messages WHERE bot_id=? AND id<? ORDER BY id DESC LIMIT ?",
                    (bot_id, 2**63 - 1 if before is None else before, limit),
                ).fetchall()[::-1]
        return [_message(row) for row in rows]

    # Action journal

    def action_start(self, bot_id, user_id, request_id, step, tool, args_hash) -> int:
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            try:
                return conn.execute(
                    "INSERT INTO bot_actions (bot_id,request_id,step,tool,args_hash,state) "
                    "VALUES (?,?,?,?,?,'started')",
                    (bot_id, request_id, step, tool, args_hash),
                ).lastrowid
            except sqlite3.IntegrityError:
                raise Conflict() from None

    def action_finish(self, bot_id, user_id, request_id, step, state, blocks=()) -> bool:
        """``started`` -> ``ok`` or ``refused``; False when the row was not still started."""
        if state not in ("ok", "refused"):
            raise Invalid("invalid state")
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            return (
                conn.execute(
                    "UPDATE bot_actions SET state=?,blocks=? WHERE bot_id=? AND request_id=? "
                    "AND step=? AND state='started'",
                    (state, json.dumps(list(blocks), ensure_ascii=False), bot_id, request_id, step),
                ).rowcount
                == 1
            )

    def actions(self, bot_id, user_id, request_id) -> list[dict]:
        with self._tx(write=False) as conn:
            self._own(conn, bot_id, user_id)
            return [
                {
                    "step": r["step"],
                    "tool": r["tool"],
                    "args_hash": r["args_hash"],
                    "state": r["state"],
                    "blocks": json.loads(r["blocks"]),
                }
                for r in conn.execute(
                    "SELECT * FROM bot_actions WHERE bot_id=? AND request_id=? ORDER BY step",
                    (bot_id, request_id),
                )
            ]

    # Pending confirmations

    @staticmethod
    def _pending(row) -> dict:
        data = {key: row[key] for key in row.keys()}
        data["args"] = json.loads(data["args"])
        return data

    def pending_create(self, bot_id, user_id, tool, args, label, ttl_s=PENDING_TTL_S) -> dict:
        now = datetime.now(timezone.utc)
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            pending_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO bot_pending (id,bot_id,user_id,tool,args,label,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    pending_id,
                    bot_id,
                    user_id,
                    tool,
                    json.dumps(args, ensure_ascii=False),
                    label,
                    stamp(now),
                    stamp(now + timedelta(seconds=ttl_s)),
                ),
            )
            return self._pending(
                conn.execute("SELECT * FROM bot_pending WHERE id=?", (pending_id,)).fetchone()
            )

    def pending_get(self, bot_id, user_id, pending_id) -> dict | None:
        with self._tx(write=False) as conn:
            self._own(conn, bot_id, user_id)
            row = conn.execute(
                "SELECT * FROM bot_pending WHERE id=? AND bot_id=?", (pending_id, bot_id)
            ).fetchone()
            return self._pending(row) if row else None

    def pending_resolve(self, bot_id, user_id, pending_id, state, now=None) -> bool:
        """Compare-and-set ``open`` -> ``done``, ``cancelled`` or ``expired``.

        An open row past its expiry can only become ``expired``; the call then returns False.
        """
        if state not in ("done", "cancelled", "expired"):
            raise Invalid("invalid state")
        at = stamp(now)
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            if state != "expired":
                conn.execute(
                    "UPDATE bot_pending SET state='expired' WHERE id=? AND bot_id=? "
                    "AND state='open' AND expires_at<=?",
                    (pending_id, bot_id, at),
                )
            return (
                conn.execute(
                    "UPDATE bot_pending SET state=? WHERE id=? AND bot_id=? AND state='open'",
                    (state, pending_id, bot_id),
                ).rowcount
                == 1
            )

    # Work the bot filed

    def work_add(self, bot_id, user_id, board, card, message_id) -> None:
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            conn.execute(
                "INSERT OR IGNORE INTO bot_work (bot_id,board,card,message_id) VALUES (?,?,?,?)",
                (bot_id, board, card, message_id),
            )

    def work_list(self, bot_id, user_id) -> list[dict]:
        with self._tx(write=False) as conn:
            self._own(conn, bot_id, user_id)
            return [
                {"board": r["board"], "card": r["card"], "message_id": r["message_id"]}
                for r in conn.execute(
                    "SELECT * FROM bot_work WHERE bot_id=? ORDER BY message_id,card", (bot_id,)
                )
            ]

    def bots_for_card(self, board, card) -> list[dict]:
        """Every bot that filed ``card``: ``{bot_id, user_id, message_id}``, oldest first."""
        return self._lookup("bot_work", "card", board, card)

    def work_changed(
        self, bot_id, user_id, board, card, seq, word, text=None, blocks=()
    ) -> dict | None:
        """Apply hub event ``seq`` to a filed card: remember its status word, post ``text`` if it changed.

        Only an event newer than the last one applied counts, and the check, the marker and the
        message are one write. A redelivered, stale or racing handler is a no-op, and a repeated
        event for the same word adds no second message. Returns the new event message, if any.
        """
        with self._tx(write=False) as conn:
            self._own(conn, bot_id, user_id)
            row = conn.execute(
                "SELECT last_seq FROM bot_work WHERE bot_id=? AND board=? AND card=?",
                (bot_id, board, card),
            ).fetchone()
        if row is None or row["last_seq"] >= seq:
            return None
        with self._tx() as conn:
            row = conn.execute(
                "SELECT last_word FROM bot_work WHERE bot_id=? AND board=? AND card=? "
                "AND last_seq<?",
                (bot_id, board, card, seq),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE bot_work SET last_word=?, last_seq=? WHERE bot_id=? AND board=? AND card=?",
                (word, seq, bot_id, board, card),
            )
            if row["last_word"] != word and text is not None:
                return self._add(conn, bot_id, "event", text, blocks=list(blocks))
        return None

    def work_by_bot(self, user_id) -> dict[str, list[tuple[str, int]]]:
        """``{bot_id: [(board, card), ...]}`` of unfinished work for this person's live bots."""
        with self._tx(write=False) as conn:
            found: dict[str, list[tuple[str, int]]] = {}
            for r in conn.execute(
                "SELECT w.bot_id, w.board, w.card FROM bot_work w JOIN bots b ON b.id=w.bot_id "
                "WHERE b.user_id=? AND b.archived_at IS NULL AND w.last_word IS NOT 'done' "
                "ORDER BY w.bot_id, w.card",
                (user_id,),
            ):
                found.setdefault(r["bot_id"], []).append((r["board"], r["card"]))
            return found

    # Posts forwarded to the space chat

    def forward_add(self, bot_id, user_id, board, space_message_id, message_id) -> None:
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            conn.execute(
                "INSERT OR IGNORE INTO bot_forwards (bot_id,board,space_message_id,message_id) "
                "VALUES (?,?,?,?)",
                (bot_id, board, space_message_id, message_id),
            )

    def forward_list(self, bot_id, user_id) -> list[dict]:
        with self._tx(write=False) as conn:
            self._own(conn, bot_id, user_id)
            return [
                {
                    "board": r["board"],
                    "space_message_id": r["space_message_id"],
                    "message_id": r["message_id"],
                }
                for r in conn.execute(
                    "SELECT * FROM bot_forwards WHERE bot_id=? ORDER BY message_id,space_message_id",
                    (bot_id,),
                )
            ]

    def bots_for_post(self, board, space_message_id) -> list[dict]:
        """Every bot that forwarded this space post: ``{bot_id, user_id, message_id}``."""
        return self._lookup("bot_forwards", "space_message_id", board, space_message_id)

    def unanswered_forwards(self, board, before) -> list[dict]:
        """Forwarded posts on ``board`` older than message ``before`` that no reply answered yet."""
        with self._tx(write=False) as conn:
            return [
                {
                    "bot_id": r["bot_id"],
                    "user_id": r["user_id"],
                    "message_id": r["message_id"],
                    "space_message_id": r["space_message_id"],
                }
                for r in conn.execute(
                    "SELECT t.bot_id, b.user_id, t.message_id, t.space_message_id "
                    "FROM bot_forwards t "
                    "JOIN bots b ON b.id=t.bot_id WHERE t.board=? AND t.space_message_id<? "
                    "AND t.answered_by IS NULL ORDER BY t.message_id, t.bot_id",
                    (board, before),
                )
            ]

    def forward_answered(
        self, bot_id, user_id, board, reply_id, posts, text, blocks=()
    ) -> dict | None:
        """The coordinator's reply ``reply_id`` answering this bot's forwarded ``posts``, as one event.

        Only posts no reply answered yet are taken, and they are marked in the same write as the
        message, so a redelivered, racing or later reply adds nothing and never takes them over.
        """
        _text(text, "text", 0, UPDATE_MAX)
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            marked = sum(
                conn.execute(
                    "UPDATE bot_forwards SET answered_by=? WHERE bot_id=? AND board=? "
                    "AND space_message_id=? AND space_message_id<? AND answered_by IS NULL",
                    (reply_id, bot_id, board, post, reply_id),
                ).rowcount
                for post in posts
            )
            return self._add(conn, bot_id, "event", text, blocks=list(blocks)) if marked else None

    def _lookup(self, table, column, board, value) -> list[dict]:
        # Internal readers (updates, the front agent) have no user yet; they get the owner from the bot.
        with self._tx(write=False) as conn:
            return [
                {"bot_id": r["bot_id"], "user_id": r["user_id"], "message_id": r["message_id"]}
                for r in conn.execute(
                    f"SELECT t.bot_id, b.user_id, t.message_id FROM {table} t "
                    f"JOIN bots b ON b.id=t.bot_id WHERE t.board=? AND t.{column}=? "
                    "ORDER BY t.message_id, t.bot_id",
                    (board, value),
                )
            ]

    # Memory

    def memory_add(self, bot_id, user_id, text) -> dict:
        _text(text, "text", 1, NOTE_MAX)
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            at = stamp()
            memory_id = conn.execute(
                "INSERT INTO bot_memory (bot_id,text,created_at) VALUES (?,?,?)",
                (bot_id, text, at),
            ).lastrowid
            return {"id": memory_id, "text": text, "at": at}

    def memory_list(self, bot_id, user_id, limit=None) -> list[dict]:
        with self._tx(write=False) as conn:
            self._own(conn, bot_id, user_id)
            return [
                {"id": r["id"], "text": r["text"], "at": r["created_at"]}
                for r in conn.execute(
                    "SELECT * FROM bot_memory WHERE bot_id=? AND removed_at IS NULL "
                    "ORDER BY id LIMIT ?",
                    (bot_id, -1 if limit is None else limit),
                )
            ]

    def memory_remove(self, bot_id, user_id, memory_id) -> bool:
        with self._tx() as conn:
            self._own(conn, bot_id, user_id)
            return (
                type(memory_id) is int
                and 0 < memory_id < 2**63
                and conn.execute(
                    "UPDATE bot_memory SET removed_at=? WHERE id=? AND bot_id=? "
                    "AND removed_at IS NULL",
                    (stamp(), memory_id, bot_id),
                ).rowcount
                == 1
            )
