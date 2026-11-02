"""Per-workspace SQLite cards, their append-only history, and memory links."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from commonplace.extensions import ToolError
from mempalace.sqlite_crypto import connect, row_factory

from .display import need_for

STATUSES = ("backlog", "todo", "doing", "review", "done")
TASK_COLUMNS = {
    "parent_number": "INTEGER REFERENCES tasks(id)",
    "message_id": "INTEGER REFERENCES space_messages(id)",
    "repo": "TEXT",
    "branch": "TEXT",
    "pr_url": "TEXT",
    "pr_number": "INTEGER",
    "pr_repo": "TEXT",
    "checks": "TEXT",
    "status_line": "TEXT",
    "heartbeat_at": "TEXT",
    "labels": "TEXT",
    "claim_expired_at": "TEXT",
    "agent_parent": "TEXT",
    "proposal_status": "TEXT CHECK (proposal_status IN ('pending', 'approved', 'rejected'))",
    "proposed_to": "TEXT",
    "approved_by": "TEXT",
    "review_kind": "TEXT",
    "review_context": "TEXT",
}


def pr_repository(url):
    """Repository part of a PR URL, including GitLab and plural pulls URLs."""
    if not isinstance(url, str):
        return None
    from .service import normalize_repo

    try:
        parsed = urlsplit(url if "://" in url else "https://" + url)
        hostname = parsed.hostname
    except ValueError:
        return None
    parts = parsed.path.strip("/").split("/")
    if parsed.scheme not in ("http", "https") or not hostname or len(parts) < 4:
        return None
    if parts[-2] not in ("pull", "pulls", "merge_requests") or not parts[-1].isdigit():
        return None
    try:
        number = int(parts[-1])
    except ValueError:
        return None
    if number < 1:
        return None
    prefix = parts[:-2]
    if prefix[-1] == "-":
        prefix.pop()
    if len(prefix) >= 2:
        try:
            return normalize_repo(hostname + "/" + "/".join(prefix))
        except ToolError:
            return None
    return None


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def task_number(ref) -> int:
    if isinstance(ref, bool) or not isinstance(ref, (str, int)):
        raise ToolError("no: card not found")
    try:
        number = int(str(ref).removeprefix("#"))
    except ValueError:
        raise ToolError("no: card not found") from None
    if number < 1:
        raise ToolError("no: card not found")
    return number


def task_row(conn, ref):
    row = conn.execute(
        "SELECT t.*, b.slug AS board, b.wing AS board_wing, "
        "b.claim_minutes, b.done_needs_review FROM tasks t "
        "JOIN boards b ON b.id=t.board_id WHERE t.id=?",
        (task_number(ref),),
    ).fetchone()
    if row is None:
        raise ToolError("no: card not found")
    return row


def link_visible(theme, principal) -> bool:
    if principal is None:
        return True
    if not theme:
        return principal.policy.sees_all_wings
    return principal.policy.can_read_wing(theme)


def require_visible(row, principal) -> None:
    if principal is not None and not principal.policy.can_read_wing(row["board_wing"]):
        raise ToolError("no: card not found")


def owns_card(row, actor):
    return row is not None and (
        row["assignee"] == actor
        or (row["assignee"] is None and row["created_by"] == actor)
        or (row["status"] == "review" and row["return_to"] == actor)
    )


def card(conn, row, principal=None) -> dict:
    """The public card shape, deliberately independent of SQLite's schema."""
    result = {
        "number": row["id"],
        "parent": row["parent_number"],
        "board": row["board"],
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "assignee": row["assignee"],
        "claim_until": row["claim_until"],
        "ask": row["ask"],
        "ask_options": json.loads(row["ask_options"] or "[]"),
        "ask_to": row["ask_to"],
        "return_to": row["return_to"],
        "flags": json.loads(row["flags"] or "[]"),
        **{
            key: row[key]
            for key in TASK_COLUMNS
            if key not in ("labels", "parent_number", "pr_repo")
        },
        "labels": json.loads(row["labels"] or "[]"),
        "links": len(BoardStore.links(conn, row["id"], principal)),
        "version": row["version"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "position": row["position"],
    }
    result["need"] = need_for(result, principal)
    result["claim_expired"] = bool(
        row["status"] == "doing" and row["claim_until"] and row["claim_until"] <= now()
    )
    result["can_approve"] = bool(
        principal
        and row["proposal_status"] == "pending"
        and (
            (principal.person and principal.role == "owner")
            or row["proposed_to"] == principal.actor
        )
    )
    if row["parent_number"] is None:
        children = []
        for child in conn.execute(
            "SELECT * FROM tasks WHERE board_id=? AND parent_number=? ORDER BY position,id",
            (row["board_id"], row["id"]),
        ):
            fields = {**dict(child), "labels": json.loads(child["labels"] or "[]")}
            children.append(
                {
                    "number": child["id"],
                    "title": child["title"],
                    "status": child["status"],
                    "need": need_for(fields, principal),
                }
            )
        if children:
            result["children"] = {
                "done": sum(c["status"] == "done" for c in children),
                "total": len(children),
            }
            result["children_need_you"] = any(c["need"] == "you" for c in children)
            result["child_cards"] = children
    return result


def append_event(conn, number, kind, actor, *, trust="trusted", **fields):
    conn.execute(
        "INSERT INTO task_events(task_id,kind,text,reason,from_status,to_status,target,choice,options,"
        "stamp_user,stamp_connection,stamp_trust,flags,created_at,context) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            number,
            kind,
            fields.get("text"),
            fields.get("reason"),
            fields.get("from_status"),
            fields.get("to_status"),
            fields.get("target"),
            fields.get("choice"),
            None if fields.get("options") is None else json.dumps(fields["options"]),
            fields.get("user_id", actor),
            actor.removeprefix("conn:") if actor.startswith("conn:") else None,
            trust,
            json.dumps(fields.get("flags", [])),
            now(),
            fields.get("context"),
        ),
    )


def expire_claims(conn, board_id):
    expired = conn.execute(
        "SELECT id,assignee FROM tasks "
        "WHERE board_id=? AND status='doing' AND claim_until<=? AND claim_expired_at IS NULL",
        (board_id, now()),
    ).fetchall()
    for row in expired:
        conn.execute(
            "UPDATE tasks SET claim_expired_at=?,"
            "version=version+1,updated_at=?,updated_by='system' WHERE id=?",
            (now(), now(), row["id"]),
        )
        append_event(
            conn,
            row["id"],
            "expire",
            "system",
            target=row["assignee"],
            from_status="doing",
            to_status="doing",
        )
    return expired


class BoardStore:
    """No shared connection: separate processes compete through SQLite's lock.

    The optional principal on memory-link methods is for policy-aware callers.
    Calls without it are the contract's trusted, local workspace API.
    """

    def __init__(self, workspace_dir, *, legacy_origin=None, create=True):
        directory = Path(workspace_dir)
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        self.create = create
        self.path = str(directory / "boards.sqlite3")
        with self.connection() as conn:
            conn.executescript(Path(__file__).with_name("schema.sql").read_text())
            conn.execute("BEGIN IMMEDIATE")
            try:
                board_columns = {r[1] for r in conn.execute("PRAGMA table_info(boards)")}
                if "coordinator" not in board_columns:
                    conn.execute("ALTER TABLE boards ADD COLUMN coordinator TEXT")
                columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
                extra = {
                    "created_by": "TEXT",
                    "callstack": "TEXT",
                    "claim_from": "TEXT CHECK (claim_from IN ('backlog', 'todo'))",
                    "restricted_origin": "INTEGER CHECK (restricted_origin IN (0, 1))",
                }
                for name, datatype in {**TASK_COLUMNS, **extra}.items():
                    if name not in columns:
                        conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {datatype}")
                if "restricted_origin" not in columns:
                    # NULL leaves legacy agent backlog pending a policy lookup.
                    conn.execute(
                        "UPDATE tasks SET restricted_origin=CASE "
                        "WHEN stamp_trust='untrusted' THEN 1 "
                        "WHEN stamp_connection IS NULL OR status!='backlog' THEN 0 END"
                    )
                if legacy_origin is not None:
                    for row in conn.execute(
                        "SELECT * FROM tasks WHERE status='backlog' AND restricted_origin IS NULL"
                    ).fetchall():
                        conn.execute(
                            "UPDATE tasks SET restricted_origin=? WHERE id=?",
                            (int(legacy_origin(row)), row["id"]),
                        )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS tasks_by_parent ON tasks(board_id,parent_number)"
                )
                conn.execute(
                    "UPDATE tasks SET created_by=CASE WHEN stamp_connection IS NOT NULL "
                    "THEN 'conn:' || stamp_connection ELSE 'user:' || stamp_user END "
                    "WHERE created_by IS NULL"
                )
                event_columns = {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
                if "options" not in event_columns:
                    conn.execute("ALTER TABLE task_events ADD COLUMN options TEXT")
                if "reason" not in event_columns:
                    conn.execute("ALTER TABLE task_events ADD COLUMN reason TEXT")
                if "context" not in event_columns:
                    conn.execute("ALTER TABLE task_events ADD COLUMN context TEXT")
                link_columns = {r[1] for r in conn.execute("PRAGMA table_info(task_links)")}
                if "title" not in link_columns:
                    conn.execute("ALTER TABLE task_links ADD COLUMN title TEXT NOT NULL DEFAULT ''")
                if "theme" not in link_columns:
                    conn.execute("ALTER TABLE task_links ADD COLUMN theme TEXT")
                if "invalidated" not in link_columns:
                    conn.execute(
                        "ALTER TABLE task_links ADD COLUMN invalidated INTEGER NOT NULL DEFAULT 0"
                    )
                pending_pr_repo = (
                    "(pr_repo IS NULL OR pr_repo='') AND pr_url IS NOT NULL AND pr_url!=''"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS tasks_pending_pr_repo ON tasks(id) WHERE "
                    + pending_pr_repo
                )
                for row in conn.execute(
                    "SELECT id,pr_url FROM tasks WHERE " + pending_pr_repo
                ).fetchall():
                    repo = pr_repository(row["pr_url"])
                    if repo:
                        conn.execute("UPDATE tasks SET pr_repo=? WHERE id=?", (repo, row["id"]))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def connection(self):
        target = self.path if self.create else Path(self.path).resolve().as_uri() + "?mode=rw"
        conn = connect(
            target, uri=not self.create, timeout=10 if self.create else 0.2, isolation_level=None
        )
        conn.row_factory = row_factory
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

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
    def changes(self) -> int:
        with self.connection() as conn:
            return conn.execute("SELECT value FROM meta WHERE key='changes'").fetchone()[0]

    def board_for_task(self, task_ref, *, principal=None) -> dict:
        with self.connection() as conn:
            row = task_row(conn, task_ref)
            require_visible(row, principal)
            return dict(
                conn.execute("SELECT * FROM boards WHERE id=?", (row["board_id"],)).fetchone()
            )

    def link_drawer(
        self, task_ref, drawer_id: str, actor: str, title: str = "", *, principal=None, theme=None
    ):
        if not isinstance(drawer_id, str) or not drawer_id:
            raise ToolError("no: memory id is required")
        with self.transaction() as conn:
            row = task_row(conn, task_ref)
            require_visible(row, principal)
            if principal is not None and not (
                principal.policy.has_scope("memory:write")
                and (
                    principal.policy.can_write_wing(row["board_wing"])
                    or owns_card(row, principal.actor)
                )
            ):
                raise ToolError("no: this connection cannot change this board")
            expire_claims(conn, row["board_id"])
            row = task_row(conn, row["id"])
            if principal is not None:
                # Linking only adds; it never needs the card's hold.
                actor = principal.actor
            user = principal.user_id if principal is not None else actor
            connection = actor.removeprefix("conn:") if actor.startswith("conn:") else None
            added = conn.execute(
                "INSERT OR IGNORE INTO task_links(task_id,drawer_id,stamp_user,stamp_connection,"
                "created_at,title,theme) VALUES (?,?,?,?,?,?,?)",
                (row["id"], drawer_id, user, connection, now(), title, theme),
            ).rowcount
            if added:
                conn.execute(
                    "UPDATE tasks SET version=version+1,updated_at=?,updated_by=? WHERE id=?",
                    (now(), actor, row["id"]),
                )
                if row["status"] == "doing" and row["assignee"] == actor and connection:
                    until = (
                        datetime.now(timezone.utc) + timedelta(minutes=row["claim_minutes"])
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
                    conn.execute(
                        "UPDATE tasks SET claim_until=?,claim_expired_at=NULL,heartbeat_at=? WHERE id=?",
                        (until, now(), row["id"]),
                    )
                append_event(conn, row["id"], "link", actor, user_id=user, target=drawer_id)

    def task_links(self, task_ref, *, principal=None) -> list[dict]:
        with self.connection() as conn:
            row = task_row(conn, task_ref)
            require_visible(row, principal)
            return self.links(conn, row["id"])

    @staticmethod
    def links(conn, number, principal=None) -> list[dict]:
        """Links the caller may see: the memory's theme must be readable.
        Links without a theme (older rows) only show to all-theme readers."""
        return [
            {
                "drawer_id": r["drawer_id"],
                "title": r["title"],
                "invalidated": bool(r["invalidated"]),
            }
            for r in conn.execute(
                "SELECT drawer_id,title,invalidated,theme FROM task_links WHERE task_id=? "
                "ORDER BY rowid",
                (number,),
            )
            if link_visible(r["theme"], principal)
        ]

    @staticmethod
    def hidden_link_targets(conn, number, principal) -> set:
        return {
            r["drawer_id"]
            for r in conn.execute(
                "SELECT drawer_id,theme FROM task_links WHERE task_id=?", (number,)
            )
            if not link_visible(r["theme"], principal)
        }

    def cards_for_drawer(self, drawer_id, *, principal=None) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT t.id,t.title,t.status,b.slug,b.wing FROM tasks t "
                "JOIN task_links l ON l.task_id=t.id JOIN boards b ON b.id=t.board_id "
                "WHERE l.drawer_id=? ORDER BY t.id",
                (drawer_id,),
            )
            return [
                {
                    "number": r["id"],
                    "title": r["title"],
                    "status": r["status"],
                    "board": r["slug"],
                    "wing": r["wing"],
                }
                for r in rows
                if principal is None or principal.policy.can_read_wing(r["wing"])
            ]
