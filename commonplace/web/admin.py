"""Read-only, current-workspace Admin inventories for signed-in owners."""

from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

from commonplace import control_db, redact
from commonplace.console.authority import status
from commonplace.http_util import log_failure, path_parts, require_principal, send_json
from commonplace.policy import Policy

from .agents import connection_view

LOG = logging.getLogger(__name__)
PAGE_SIZE = 100
READ_SECONDS = 2.0
_ID = re.compile(r"(?:usr|ws|conn)_[a-f0-9]{16}")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class ReadContext:
    def __init__(self, clock):
        self.clock = clock
        self.now = clock.utc_now()
        self.deadline = clock.monotonic() + READ_SECONDS

    def remaining(self):
        remaining = self.deadline - self.clock.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        return remaining


@contextmanager
def read_control(db, context):
    """Open the existing control store without ControlDB locks or initialization."""
    conn = sqlite3.connect(
        Path(db.path).resolve().as_uri() + "?mode=ro",
        uri=True,
        isolation_level=None,
        timeout=min(0.250, context.remaining()),
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(lambda: int(context.clock.monotonic() >= context.deadline), 1000)
        context.remaining()
        yield conn
        context.remaining()
    finally:
        conn.close()


def select(conn, context, sql, parameters=()):
    # Recompute the connection-local busy bound before each SELECT, not just open.
    wait_ms = int(min(0.250, context.remaining()) * 1000)
    conn.execute(f"PRAGMA busy_timeout={wait_ms}")
    return conn.execute(sql, parameters)


def display(value):
    # A redacted login URL is still a forbidden link. Display strings carry no URLs.
    clean = _URL.sub("[link omitted]", str(value or ""))
    return redact.redact_value(redact.redact(clean)[0])[0]


def identifier(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def timestamp(value):
    try:
        return value if control_db._parse(value) is not None else None
    except (ValueError, TypeError):
        return None


def users(conn, workspace_id, context, after=None):
    workspace = select(
        conn, context, "SELECT id,name,created_at FROM workspaces WHERE id=?", (workspace_id,)
    ).fetchone()
    if workspace is None:
        raise LookupError()
    count = select(
        conn,
        context,
        "SELECT count(*) FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?",
        (workspace_id,),
    ).fetchone()[0]
    rows = select(
        conn,
        context,
        "SELECT u.id,u.name,u.email,m.role,u.created_at,m.created_at AS joined_at,u.disabled_at "
        "FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? "
        + ("AND (m.created_at,u.id)>(?,?) " if after else "")
        + "ORDER BY m.created_at,u.id LIMIT ?",
        (workspace_id, *(after or ()), PAGE_SIZE + 1),
    ).fetchall()
    members = []
    for row in rows[:PAGE_SIZE]:
        context.remaining()
        members.append(
            {
                "id": identifier(row["id"]),
                "name": display(row["name"]),
                "email": display(row["email"]),
                "role": row["role"],
                "created_at": timestamp(row["created_at"]),
                "joined_at": timestamp(row["joined_at"]),
                "disabled_at": timestamp(row["disabled_at"]),
            }
        )
    return {
        "workspace": {
            "id": identifier(workspace["id"]),
            "name": display(workspace["name"]),
            "created_at": timestamp(workspace["created_at"]),
        },
        "members": members,
        "count": count,
        **page(rows, "joined_at", workspace_id, "users"),
    }


def connections(conn, workspace_id, context, after=None):
    count = select(
        conn,
        context,
        "SELECT count(*) FROM connections c JOIN memberships m ON m.workspace_id=c.workspace_id "
        "AND m.user_id=c.user_id JOIN users u ON u.id=m.user_id WHERE c.workspace_id=?",
        (workspace_id,),
    ).fetchone()[0]
    rows = select(
        conn,
        context,
        "SELECT c.id,c.name,c.client,c.auth,c.user_id,c.created_at,c.expires_at,c.revoked_at,"
        "c.last_used_at,c.parent_id,c.started_by,c.policy,u.disabled_at AS user_disabled "
        "FROM connections c JOIN memberships m ON m.workspace_id=c.workspace_id "
        "AND m.user_id=c.user_id JOIN users u ON u.id=m.user_id WHERE c.workspace_id=? "
        + ("AND (c.created_at,c.id)>(?,?) " if after else "")
        + "ORDER BY c.created_at,c.id LIMIT ?",
        (workspace_id, *(after or ()), PAGE_SIZE + 1),
    ).fetchall()
    result = []
    for row in rows[:PAGE_SIZE]:
        context.remaining()
        view = connection_view(row)
        policy = Policy.from_json(row["policy"])
        for key in ("name", "client"):
            view[key] = display(view[key])
        view["read_wings"] = [display(wing) for wing in sorted(policy.read_wings)]
        for key in ("id", "user_id", "parent_id"):
            view[key] = identifier(view[key])
        for key in ("created_at", "expires_at", "revoked_at", "last_used_at"):
            view[key] = timestamp(view[key])
        expiry = control_db._parse(view["expires_at"])
        view.update(
            trust=policy.trust,
            scopes=sorted(policy.scopes),
            activity=status(view["last_used_at"], context.now),
            state=(
                "revoked"
                if row["revoked_at"]
                else "disabled"
                if row["user_disabled"]
                else "expired"
                if expiry and expiry <= context.now
                else "active"
            ),
            user_disabled=bool(row["user_disabled"]),
        )
        # Stored auth and origin are enums, never arbitrary display strings.
        view["auth"] = view["auth"] if view["auth"] in ("pat", "oauth") else None
        view["started_by"] = view["started_by"] if view["started_by"] in ("user", "agent") else None
        result.append(view)
    return {
        "connections": result,
        "count": count,
        **page(rows, "created_at", workspace_id, "connections"),
    }


def page(rows, sort_column, workspace_id, section):
    more = len(rows) > PAGE_SIZE
    cursor = None
    if more:
        last = rows[PAGE_SIZE - 1]
        cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    [workspace_id, section, last[sort_column], last["id"]], separators=(",", ":")
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return {"has_more": more, "next_cursor": cursor}


def cursor_key(raw, workspace_id, section):
    if not raw or len(raw) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        raise ValueError()
    try:
        values = json.loads(
            base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeError):
        raise ValueError() from None
    if (
        not isinstance(values, list)
        or len(values) != 4
        or values[:2] != [workspace_id, section]
        or not timestamp(values[2])
        or not identifier(values[3])
    ):
        raise ValueError()
    return values[2:]


def install(gateway):
    if not hasattr(gateway, "admin_clock"):
        gateway.admin_clock = SimpleNamespace(
            utc_now=lambda: datetime.now(timezone.utc),
            monotonic=time.monotonic,
        )

    def inventory(h):
        principal = require_principal(gateway, h)
        if principal is None:
            return
        if not principal.person or principal.role != "owner":
            send_json(h, 403, {"error": "a signed-in workspace owner is required"})
            return
        parts = path_parts(h, "/api/admin/workspaces/")
        if (
            len(parts) != 2
            or parts[0] != principal.workspace_id
            or parts[1] not in ("users", "connections")
        ):
            send_json(h, 404, {"error": "not found"})
            return
        workspace_id, section = parts
        try:
            pairs = parse_qsl(urlsplit(h.path).query, keep_blank_values=True, strict_parsing=True)
            if any(key != "cursor" for key, _ in pairs) or len(pairs) > 1:
                raise ValueError()
            after = cursor_key(pairs[0][1], workspace_id, section) if pairs else None
        except (ValueError, TypeError):
            send_json(h, 400, {"error": "Invalid admin query"})
            return
        try:
            context = ReadContext(gateway.admin_clock)
            with read_control(gateway.db, context) as conn:
                projector = users if section == "users" else connections
                result = projector(conn, workspace_id, context, after)
            send_json(
                h,
                200,
                {
                    "workspace_id": workspace_id,
                    "recorded_at": control_db._iso(context.now),
                    **result,
                },
            )
        except Exception as exc:
            log_failure(LOG, "Admin inventory unavailable", exc)
            send_json(h, 503, {"error": f"{section.title()} unavailable; try again"})

    gateway.add_prefix_route("GET", "/api/admin/workspaces/", inventory)
