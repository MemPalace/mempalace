"""The control database: users, workspaces, memberships, connections, audit log.

One SQLite file next to the workspaces. It never holds memory text: palaces
live in their own directories, one per workspace, and the audit log records
ids and decisions only.

Roles are ``owner`` and ``member``. Owners manage connections (tokens) for
everyone in the workspace; members can't. A connection belongs to one member
of one workspace and carries one policy. OAuth connections (a later PR) reuse
the same row with ``auth = 'oauth'`` and no token hash.

Boards (docs/boards) name actors ``conn:<connection name>`` and
``user:<user id>`` and store the same stamp, so connection names are unique
per workspace and limited to characters an actor ref can carry.

The audit log is append-only and ordered by ``seq``. A later webhook outbox
(wake webhooks) reads it as a feed: a delivery worker keeps its own cursor per
subscription, filters by workspace and connection scope, and sends ids only,
so the log needs no change and nothing is sent from inside a request.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from . import tokens
from .admission import GATE
from .policy import Policy

SCHEMA_VERSION = 1
ROLES = ("owner", "member")
AUTH_KINDS = ("pat", "oauth")
DEFAULT_TOKEN_DAYS = 365
_LAST_USED_RESOLUTION_S = 60
_CONNECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    disabled_at TEXT
);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    personal_of TEXT REFERENCES users(id),
    initial_space_pending INTEGER NOT NULL DEFAULT 0 CHECK (initial_space_pending IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'member')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    client TEXT NOT NULL,
    auth TEXT NOT NULL CHECK (auth IN ('pat', 'oauth')),
    token_hash TEXT UNIQUE,
    token_hint TEXT NOT NULL DEFAULT '',
    policy TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    last_used_at TEXT,
    parent_id TEXT REFERENCES connections(id),
    started_by TEXT NOT NULL DEFAULT 'user' CHECK (started_by IN ('user','agent')),
    UNIQUE (workspace_id, name),
    FOREIGN KEY (workspace_id, user_id) REFERENCES memberships(workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS runner_connections (
    connection_id TEXT PRIMARY KEY REFERENCES connections(id),
    machine_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('machine','worker'))
);
CREATE TABLE IF NOT EXISTS account_plans (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    plan TEXT NOT NULL CHECK (plan IN ('free','paid')),
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcription_usage (
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    PRIMARY KEY(user_id, day)
);
CREATE TABLE IF NOT EXISTS rule_checks (
    workspace_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('off','warn','enforce')),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY (workspace_id, agent)
);
CREATE TABLE IF NOT EXISTS rule_stores (
    workspace_id TEXT PRIMARY KEY,
    rules_store_id INTEGER,
    boards_store_id INTEGER
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    workspace_id TEXT,
    user_id TEXT,
    connection_id TEXT,
    auth TEXT,
    action TEXT NOT NULL,
    tool TEXT,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    wing TEXT,
    target_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_audit_workspace ON audit_log(workspace_id, seq);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


class ControlError(ValueError):
    """A refused control operation: unknown user, wrong role, duplicate name."""


class RunnerLinkedError(ControlError):
    """A runner connection must be managed with its machine registry."""

    def __init__(self):
        super().__init__("manage this runner under /api/runners")


def link_runner(conn, connection_id, machine_id, kind):
    """Link a runner connection inside the caller's control transaction."""
    conn.execute(
        "INSERT INTO runner_connections(connection_id, machine_id, kind) VALUES (?,?,?)",
        (connection_id, machine_id, kind),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


@dataclass(frozen=True)
class Principal:
    """Who a request is, as the gateway resolved it. Never from the client."""

    user_id: str
    user_email: str
    workspace_id: str
    connection_id: str
    connection: str
    client: str
    auth: str
    role: str
    policy: Policy
    person: bool = False  # a signed-in person (web session), not an agent's token
    parent_connection_id: Optional[str] = None
    parent_agent: Optional[str] = None
    runner: Optional[str] = None

    @property
    def actor(self) -> str:
        """The board actor ref for this caller (docs/boards/schema.sql)."""
        if self.person:
            return f"user:{self.user_id}"
        return f"conn:{self.connection}"

    def identity(self) -> dict:
        """The verified identity the worker stamps on writes."""
        return {
            **({"person": True} if self.person else {}),
            "user": self.user_id,
            "connection": self.connection,
            "client": self.client,
            "auth": self.auth,
            "trust": self.policy.trust,
            "workspace": self.workspace_id,
            "parent_agent": self.parent_agent,
        }


class ControlDB:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._pre_close: list[Callable[[], None]] = []
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            with self._tx() as conn:
                workspace_columns = {r[1] for r in conn.execute("PRAGMA table_info(workspaces)")}
                if "initial_space_pending" not in workspace_columns:
                    conn.execute(
                        "ALTER TABLE workspaces ADD COLUMN initial_space_pending INTEGER "
                        "NOT NULL DEFAULT 0 CHECK (initial_space_pending IN (0,1))"
                    )
                columns = {r[1] for r in conn.execute("PRAGMA table_info(connections)")}
                if "parent_id" not in columns:
                    conn.execute(
                        "ALTER TABLE connections ADD COLUMN parent_id TEXT REFERENCES connections(id)"
                    )
                if "started_by" not in columns:
                    conn.execute(
                        "ALTER TABLE connections ADD COLUMN started_by TEXT NOT NULL DEFAULT 'user' "
                        "CHECK (started_by IN ('user','agent'))"
                    )
            for operation in ("INSERT", "UPDATE OF parent_id, workspace_id, started_by, auth"):
                suffix = "insert" if operation == "INSERT" else "update"
                self._conn.executescript(f"""
                    CREATE TRIGGER IF NOT EXISTS connections_parent_{suffix}
                    BEFORE {operation} ON connections BEGIN
                        SELECT RAISE(ABORT, 'parent must be an active connection in the same workspace')
                        WHERE NEW.parent_id IS NOT NULL AND (
                            NEW.started_by != 'agent' OR NEW.auth = 'oauth' OR NOT EXISTS (
                                SELECT 1 FROM connections WHERE id = NEW.parent_id
                                AND workspace_id = NEW.workspace_id AND revoked_at IS NULL
                            )
                        );
                        WITH RECURSIVE ancestors(id) AS (
                            SELECT NEW.parent_id UNION
                            SELECT c.parent_id FROM connections c JOIN ancestors a ON c.id = a.id
                            WHERE c.parent_id IS NOT NULL
                        )
                        SELECT RAISE(ABORT, 'connection parent cycle') FROM ancestors WHERE id = NEW.id;
                    END;
                """)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def add_pre_close_callback(self, callback: Callable[[], None]) -> None:
        """Register an owned resource to stop before SQLite closes."""
        with self._close_lock:
            if self._closed:
                raise RuntimeError("Control database is closed")
            self._pre_close.append(callback)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            error = None
            for callback in self._pre_close:
                try:
                    # A callback may join a worker that needs the SQL lock.
                    callback()
                except Exception as exc:
                    error = error or exc
            with self._lock:
                self._conn.close()
                self._closed = True
            self._pre_close.clear()
            if error is not None:
                raise error

    def _tx(self):
        conn = self._conn

        class _Tx:
            def __enter__(self_inner):
                conn.execute("BEGIN IMMEDIATE")
                return conn

            def __exit__(self_inner, exc_type, exc, tb):
                conn.execute("ROLLBACK" if exc_type else "COMMIT")
                return False

        return _Tx()

    # ── users ──
    def create_user(self, email: str, name: str = "", *, is_admin: bool = False) -> str:
        email = email.strip()
        if "@" not in email or len(email) > 254:
            raise ControlError(f"not an email address: {email!r}")
        uid = _new_id("usr")
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO users(id, email, name, is_admin, created_at) VALUES (?,?,?,?,?)",
                    (uid, email, name.strip(), int(is_admin), _iso(_now())),
                )
            except sqlite3.IntegrityError:
                raise ControlError(f"user {email} already exists") from None
        return uid

    def user(self, user_ref: str) -> sqlite3.Row:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE id = ? OR email = ?", (user_ref, user_ref)
            ).fetchone()
        if row is None:
            raise ControlError(f"no such user: {user_ref}")
        return row

    def users(self) -> list:
        with self._lock:
            return self._conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()

    def has_users(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    # ── workspaces and memberships ──
    def create_workspace(
        self,
        name: str,
        owner_ref: str,
        *,
        personal: bool = False,
        initial_space_pending: bool = False,
        conn=None,
    ) -> str:
        owner = self.user(owner_ref)
        name = name.strip()
        if not name:
            raise ControlError("workspace name is empty")
        wid = _new_id("ws")
        now = _iso(_now())
        # A new workspace adds stores to the data tree: never during a backup cut.
        with GATE.creating(), self._lock, self._tx() if conn is None else nullcontext(conn) as conn:
            conn.execute(
                "INSERT INTO workspaces(id, name, personal_of, initial_space_pending, created_at) "
                "VALUES (?,?,?,?,?)",
                (wid, name, owner["id"] if personal else None, int(initial_space_pending), now),
            )
            conn.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, created_at) VALUES (?,?,?,?)",
                (wid, owner["id"], "owner", now),
            )
        self.audit(action="workspace.create", decision="ok", workspace_id=wid, user_id=owner["id"])
        return wid

    def workspace(self, ref: str) -> sqlite3.Row:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workspaces WHERE id = ? OR name = ?", (ref, ref)
            ).fetchall()
        if len(row) != 1:
            raise ControlError(
                f"no such workspace: {ref}" if not row else f"workspace name {ref!r} is ambiguous"
            )
        return row[0]

    def workspaces(self) -> list:
        with self._lock:
            return self._conn.execute("SELECT * FROM workspaces ORDER BY created_at").fetchall()

    def role(self, workspace_id: str, user_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT role FROM memberships WHERE workspace_id = ? AND user_id = ?",
                (workspace_id, user_id),
            ).fetchone()
        return row["role"] if row else None

    def require_owner(self, workspace_id: str, actor_id: str) -> None:
        if self.role(workspace_id, actor_id) != "owner":
            raise ControlError("only a workspace owner can do that")

    def add_member(self, workspace_ref: str, user_ref: str, role: str, *, actor: str) -> None:
        if role not in ROLES:
            raise ControlError(f"role must be one of {', '.join(ROLES)}")
        ws = self.workspace(workspace_ref)
        user = self.user(user_ref)
        actor_id = self.user(actor)["id"]
        self.require_owner(ws["id"], actor_id)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO memberships(workspace_id, user_id, role, created_at) "
                    "VALUES (?,?,?,?)",
                    (ws["id"], user["id"], role, _iso(_now())),
                )
            except sqlite3.IntegrityError:
                raise ControlError(f"{user['email']} is already in {ws['name']}") from None
        self.audit(
            action="member.add",
            decision="ok",
            workspace_id=ws["id"],
            user_id=actor_id,
            reason=f"{user['id']}:{role}",
        )

    def members(self, workspace_ref: str) -> list:
        ws = self.workspace(workspace_ref)
        with self._lock:
            return self._conn.execute(
                "SELECT u.id, u.email, m.role FROM memberships m JOIN users u ON u.id = m.user_id "
                "WHERE m.workspace_id = ? ORDER BY m.created_at",
                (ws["id"],),
            ).fetchall()

    # ── connections (personal access tokens) ──
    def create_token(
        self,
        *,
        workspace_ref: str,
        user_ref: str,
        name: str,
        client: str,
        policy: Policy,
        actor: str,
        expires_days: Optional[int] = DEFAULT_TOKEN_DAYS,
        parent: Optional[str] = None,
    ) -> tuple:
        """Create a connection with a new token. Returns ``(connection_id, token)``.

        The token is returned once and only its hash is stored.
        """
        ws = self.workspace(workspace_ref)
        user = self.user(user_ref)
        actor_id = self.user(actor)["id"]
        self.require_owner(ws["id"], actor_id)
        role = self.role(ws["id"], user["id"])
        if role is None:
            raise ControlError(f"{user['email']} is not a member of {ws['name']}")
        if "admin" in policy.scopes and role != "owner":
            raise ControlError("only an owner's connection can hold the admin scope")
        if "private:read" in policy.scopes and role != "owner":
            raise ControlError("only an owner's connection can hold the private:read scope")
        name = name.strip()
        client = client.strip()
        if not _CONNECTION_NAME.fullmatch(name):
            raise ControlError(
                "connection name: up to 120 of letters, digits and . _ : @ - (e.g. grok:coordinator)"
            )
        if not client or len(client) > 60:
            raise ControlError("client must be 1-60 characters")
        token = tokens.new_token()
        cid = _new_id("conn")
        now = _now()
        expires = _iso(now + timedelta(days=expires_days)) if expires_days else None
        with self._lock:
            if parent is not None and (not isinstance(parent, str) or not parent.strip()):
                raise ControlError("parent must be a connection name or id")
            if parent == name:
                raise ControlError("a connection cannot be its own parent")
            parent_row = self.connection(parent, ws["id"]) if parent is not None else None
            if parent_row is not None and parent_row["revoked_at"]:
                raise ControlError("parent connection is revoked")
            if (
                parent_row is not None
                and self._conn.execute(
                    "SELECT 1 FROM runner_connections WHERE connection_id=?", (parent_row["id"],)
                ).fetchone()
            ):
                raise ControlError("a runner connection cannot be a parent")
            try:
                self._conn.execute(
                    "INSERT INTO connections(id, workspace_id, user_id, name, client, auth, "
                    "token_hash, token_hint, policy, created_by, created_at, expires_at, parent_id, started_by) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cid,
                        ws["id"],
                        user["id"],
                        name,
                        client,
                        "pat",
                        tokens.token_hash(token),
                        tokens.token_hint(token),
                        policy.to_json(),
                        actor_id,
                        _iso(now),
                        expires,
                        parent_row["id"] if parent_row is not None else None,
                        "agent" if parent_row is not None else "user",
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed: connections" in str(exc):
                    raise ControlError(f"a connection named {name!r} already exists") from None
                raise ControlError(str(exc)) from None
        self.audit(
            action="token.create",
            decision="ok",
            workspace_id=ws["id"],
            user_id=actor_id,
            connection_id=cid,
            auth="pat",
        )
        return cid, token

    def top_level_connection(self, connection_id: str, workspace_ref: str) -> sqlite3.Row:
        """Follow stored connection ids, refusing invalid or cyclic ancestry."""
        with self._lock:
            workspace_id = self.workspace(workspace_ref)["id"]
            seen = set()
            while True:
                if connection_id in seen:
                    raise ControlError("connection parent cycle")
                seen.add(connection_id)
                row = self._conn.execute(
                    "SELECT * FROM connections WHERE id=? AND workspace_id=?",
                    (connection_id, workspace_id),
                ).fetchone()
                if row is None:
                    raise ControlError("parent connection not found in this workspace")
                if row["parent_id"] is None:
                    return row
                connection_id = row["parent_id"]

    def connection(self, ref: str, workspace_ref: Optional[str] = None) -> sqlite3.Row:
        sql = "SELECT * FROM connections WHERE (id = ? OR name = ?)"
        params: list = [ref, ref]
        if workspace_ref:
            sql += " AND workspace_id = ?"
            params.append(self.workspace(workspace_ref)["id"])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        if len(rows) != 1:
            raise ControlError(
                f"no such connection: {ref}"
                if not rows
                else f"{ref!r} is ambiguous: add --workspace"
            )
        return rows[0]

    def connections(self, workspace_ref: Optional[str] = None) -> list:
        sql = "SELECT * FROM connections"
        params: list = []
        if workspace_ref:
            sql += " WHERE workspace_id = ?"
            params.append(self.workspace(workspace_ref)["id"])
        with self._lock:
            return self._conn.execute(sql + " ORDER BY created_at", params).fetchall()

    def revoke(
        self,
        ref: str,
        *,
        actor: str,
        workspace_ref: Optional[str] = None,
        _runner_owned: bool = False,
    ) -> None:
        row = self.connection(ref, workspace_ref)
        actor_id = self.user(actor)["id"]
        if actor_id != row["user_id"]:
            self.require_owner(row["workspace_id"], actor_id)
        with self._lock:
            if (
                not _runner_owned
                and self._conn.execute(
                    "SELECT 1 FROM runner_connections WHERE connection_id=?", (row["id"],)
                ).fetchone()
            ):
                raise RunnerLinkedError()
            self._conn.execute(
                "UPDATE connections SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
                (_iso(_now()), row["id"]),
            )
        self.audit(
            action="token.revoke",
            decision="ok",
            workspace_id=row["workspace_id"],
            user_id=actor_id,
            connection_id=row["id"],
            auth=row["auth"],
        )

    def rotate_token(
        self,
        ref: str,
        *,
        actor: str,
        workspace_ref: str,
        expires_days: int = DEFAULT_TOKEN_DAYS,
        _runner_owned: bool = False,
    ) -> str:
        """Replace a live PAT's secret, preserving its identity and permissions."""
        with self._lock:
            row = self.connection(ref, workspace_ref)
            actor_id = self.user(actor)["id"]
            self.require_owner(row["workspace_id"], actor_id)
            if (
                not _runner_owned
                and self._conn.execute(
                    "SELECT 1 FROM runner_connections WHERE connection_id=?", (row["id"],)
                ).fetchone()
            ):
                raise RunnerLinkedError()
            if row["auth"] != "pat" or row["revoked_at"]:
                raise ControlError("only active personal access tokens can be rotated")
            token = tokens.new_token()
            self._conn.execute(
                "UPDATE connections SET token_hash=?, token_hint=?, expires_at=?, "
                "last_used_at=NULL WHERE id=?",
                (
                    tokens.token_hash(token),
                    tokens.token_hint(token),
                    _iso(_now() + timedelta(days=expires_days)),
                    row["id"],
                ),
            )
            self.audit(
                action="token.rotate",
                decision="ok",
                workspace_id=row["workspace_id"],
                user_id=actor_id,
                connection_id=row["id"],
                auth="pat",
            )
        return token

    def set_connection_read_wings(self, ref, wings, *, actor, workspace_ref):
        """Change only a live agent's read policy, keeping its token and identity."""
        with self._lock, self._tx():
            row = self.connection(ref, workspace_ref)
            actor_id = self.user(actor)["id"]
            self.require_owner(row["workspace_id"], actor_id)
            principal = self.authenticate_connection(row["id"])
            if principal is not None and principal.runner is not None:
                raise RunnerLinkedError()
            if row["auth"] != "pat" or principal is None:
                raise ControlError("only active personal access tokens can be updated")
            policy = json.loads(row["policy"])
            policy["read_wings"] = list(wings)
            self._conn.execute(
                "UPDATE connections SET policy=? WHERE id=?",
                (json.dumps(policy, sort_keys=True), row["id"]),
            )
            self.audit(
                action="connection.read_wings",
                decision="ok",
                workspace_id=row["workspace_id"],
                user_id=actor_id,
                connection_id=row["id"],
                auth="pat",
            )

    def authenticate_token(self, token: str) -> Optional[Principal]:
        """The principal for a bearer token, or ``None``. Checked on every request."""
        if not tokens.looks_like_token(token):
            return None
        digest = tokens.token_hash(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT c.*, u.email AS user_email, u.disabled_at AS user_disabled, m.role AS role, "
                "r.kind AS runner_kind "
                "FROM connections c JOIN users u ON u.id = c.user_id "
                "LEFT JOIN memberships m ON m.workspace_id = c.workspace_id "
                "AND m.user_id = c.user_id "
                "LEFT JOIN runner_connections r ON r.connection_id = c.id "
                "WHERE c.token_hash = ? AND c.auth = 'pat'",
                (digest,),
            ).fetchone()
        if row is None or not tokens.hashes_match(row["token_hash"], digest):
            return None
        return self._connection_principal(row, touch=True)

    def authenticate_connection(self, connection_id: str) -> Optional[Principal]:
        """Rebuild a durable subscription owner's current connection authority."""
        with self._lock:
            row = self._conn.execute(
                "SELECT c.*, u.email AS user_email, u.disabled_at AS user_disabled, m.role, "
                "r.kind AS runner_kind "
                "FROM connections c JOIN users u ON u.id = c.user_id "
                "LEFT JOIN memberships m ON m.workspace_id = c.workspace_id "
                "AND m.user_id = c.user_id "
                "LEFT JOIN runner_connections r ON r.connection_id = c.id WHERE c.id = ?",
                (connection_id,),
            ).fetchone()
            return self._connection_principal(row)

    def _connection_principal(self, row, *, touch=False) -> Optional[Principal]:
        if row is None:
            return None
        now = _now()
        if row["revoked_at"] or row["user_disabled"] or row["role"] is None:
            return None
        expires = _parse(row["expires_at"])
        if expires is not None and expires <= now:
            return None
        if touch:
            self._touch(row, now)
        return Principal(
            user_id=row["user_id"],
            user_email=row["user_email"],
            workspace_id=row["workspace_id"],
            connection_id=row["id"],
            connection=row["name"],
            client=row["client"],
            auth=row["auth"],
            role="member" if row["runner_kind"] is not None else row["role"],
            policy=Policy.from_json(row["policy"]).for_role(row["role"]),
            parent_connection_id=row["parent_id"],
            parent_agent=(
                f"conn:{self.connection(row['parent_id'], row['workspace_id'])['name']}"
                if row["parent_id"]
                else None
            ),
            runner=row["runner_kind"],
        )

    def _touch(self, row, now: datetime) -> None:
        last = _parse(row["last_used_at"])
        if last is not None and (now - last).total_seconds() < _LAST_USED_RESOLUTION_S:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE connections SET last_used_at = ? WHERE id = ?", (_iso(now), row["id"])
            )

    def rule_checks(self, workspace_id):
        with self._lock:
            return dict(
                self._conn.execute(
                    "SELECT agent,mode FROM rule_checks WHERE workspace_id=?", (workspace_id,)
                )
            )

    def set_rule_check(self, workspace_id, agent, mode, principal, *, target_id):
        if mode not in ("default", "off", "warn", "enforce"):
            raise ControlError("invalid check mode")
        with self._lock, self._tx() as conn:
            if mode == "default":
                conn.execute(
                    "DELETE FROM rule_checks WHERE workspace_id=? AND agent=?",
                    (workspace_id, agent),
                )
            else:
                conn.execute(
                    "INSERT INTO rule_checks VALUES (?,?,?,?,?) ON CONFLICT(workspace_id,agent) "
                    "DO UPDATE SET mode=excluded.mode,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                    (workspace_id, agent, mode, _iso(_now()), principal.actor),
                )
            self.audit(
                action="rules.check",
                decision=mode,
                workspace_id=workspace_id,
                user_id=principal.user_id,
                connection_id=principal.connection_id,
                auth=principal.auth,
                target_ids=(target_id,),
            )

    def rule_stores(self, workspace_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rule_stores WHERE workspace_id=?", (workspace_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def bind_rule_stores(self, workspace_id, rules_store_id, boards_store_id, *, accept=False):
        # A replacement must never win a race against the first binding.
        with self._lock, self._tx() as conn:
            if accept:
                conn.execute(
                    "INSERT INTO rule_stores VALUES (?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET "
                    "rules_store_id=excluded.rules_store_id,boards_store_id=excluded.boards_store_id",
                    (workspace_id, rules_store_id, boards_store_id),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO rule_stores VALUES (?,NULL,NULL)", (workspace_id,)
                )
                for name, value in (
                    ("rules_store_id", rules_store_id),
                    ("boards_store_id", boards_store_id),
                ):
                    if value is not None:
                        conn.execute(
                            f"UPDATE rule_stores SET {name}=? WHERE workspace_id=? AND {name} IS NULL",
                            (value, workspace_id),
                        )
            return dict(
                conn.execute(
                    "SELECT * FROM rule_stores WHERE workspace_id=?", (workspace_id,)
                ).fetchone()
            )

    # ── audit log ──
    def audit(
        self,
        *,
        action: str,
        decision: str,
        workspace_id: Optional[str] = None,
        user_id: Optional[str] = None,
        connection_id: Optional[str] = None,
        auth: Optional[str] = None,
        tool: Optional[str] = None,
        reason: str = "",
        wing: Optional[str] = None,
        target_ids: Iterable[str] = (),
    ) -> None:
        """Append one entry. Ids and decisions only, never memory text."""
        ids = [str(t)[:200] for t in target_ids if t][:500]
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log(at, workspace_id, user_id, connection_id, auth, action, "
                "tool, decision, reason, wing, target_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _iso(_now()),
                    workspace_id,
                    user_id,
                    connection_id,
                    auth,
                    action[:60],
                    tool[:120] if tool else tool,
                    decision[:20],
                    reason[:500],
                    wing[:200] if wing else wing,
                    json.dumps(ids),
                ),
            )

    def audit_entries(self, workspace_ref: Optional[str] = None, limit: int = 100) -> list:
        sql = "SELECT * FROM audit_log"
        params: list = []
        if workspace_ref:
            sql += " WHERE workspace_id = ?"
            params.append(self.workspace(workspace_ref)["id"])
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            return self._conn.execute(sql, params).fetchall()
