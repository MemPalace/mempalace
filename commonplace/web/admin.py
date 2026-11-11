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
from importlib import resources
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
        select(conn, context, "SELECT name FROM sqlite_master LIMIT 1").fetchall()
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


def identifier(value, pattern=_ID):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def slug(value):
    return identifier(value, r"[a-z0-9][a-z0-9_-]{0,99}")


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


@contextmanager
def read_store(gateway, workspace_id, name, context):
    """Resolve the current generation and open an existing encrypted store only."""
    from mempalace.sqlite_crypto import connect, row_factory

    context.remaining()
    path = Path(gateway.supervisor.workspace_dir(workspace_id)) / name
    if not path.exists():
        yield None
        context.remaining()
        return
    conn = connect(
        path.resolve().as_uri() + "?mode=ro",
        uri=True,
        isolation_level=None,
        timeout=min(0.250, context.remaining()),
    )
    try:
        conn.row_factory = row_factory
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(lambda: int(context.clock.monotonic() >= context.deadline), 1000)
        select(conn, context, "SELECT name FROM sqlite_master LIMIT 1").fetchall()
        context.remaining()
        yield conn
        context.remaining()
    finally:
        conn.close()


def has_table(conn, context, name):
    return conn is not None and bool(
        select(
            conn, context, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def orchestration_setting(conn, workspace_id, context):
    if not has_table(conn, context, "orchestration_settings"):
        return False
    row = select(
        conn,
        context,
        "SELECT enabled FROM orchestration_settings WHERE workspace_id=?",
        (workspace_id,),
    ).fetchone()
    return bool(row and row[0])


def board_rows(conn, context):
    if conn is None:
        return []
    coordinator = (
        "coordinator"
        if "coordinator" in columns(conn, context, "boards")
        else "NULL AS coordinator"
    )
    return select(
        conn,
        context,
        f"SELECT id,slug,name,wing,{coordinator} FROM boards WHERE archived=0 ORDER BY slug,id",
    ).fetchall()


def columns(conn, context, table):
    # Names are internal constants; no request input is interpolated.
    return {row["name"] for row in select(conn, context, f'PRAGMA table_info("{table}")')}


def current_connection(conn, workspace_id, context, *, cid=None, name=None):
    sql = (
        "SELECT c.id,c.name,c.policy,c.last_used_at,c.expires_at,c.revoked_at,u.disabled_at "
        "FROM connections c JOIN memberships m ON m.workspace_id=c.workspace_id AND m.user_id=c.user_id "
        "JOIN users u ON u.id=c.user_id WHERE c.workspace_id=? AND "
    )
    rows = select(
        conn,
        context,
        sql + ("c.id=?" if cid is not None else "c.name=?"),
        (workspace_id, cid if cid is not None else name),
    ).fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    expires = control_db._parse(row["expires_at"])
    if row["disabled_at"] or row["revoked_at"] or (expires is not None and expires <= context.now):
        return None
    if has_table(conn, context, "runner_connections"):
        kind = select(
            conn, context, "SELECT kind FROM runner_connections WHERE connection_id=?", (row["id"],)
        ).fetchone()
        if kind and kind[0] != "worker":
            return None
    return row


def wakeable(conn, workspace_id, context, board, actor):
    if not has_table(conn, context, "webhooks"):
        return False
    rows = select(
        conn,
        context,
        "SELECT connection_id,owner_actor,kinds,board,waiting_on,active FROM webhooks "
        "WHERE workspace_id=? AND owner_actor=? AND active=1",
        (workspace_id, actor),
    ).fetchall()
    eligible = False
    for row in rows:
        context.remaining()
        kinds = json.loads(row["kinds"])
        if not isinstance(kinds, list) or any(not isinstance(k, str) for k in kinds):
            raise ValueError()
        if not ({"space_message", "*"} & set(kinds)) or row["board"] not in ("", board["slug"]):
            continue
        if row["connection_id"]:
            connection = current_connection(conn, workspace_id, context, cid=row["connection_id"])
            if connection is None or actor not in (
                "conn:" + connection["name"],
                "conn:" + connection["id"],
            ):
                continue
            policy = Policy.from_json(connection["policy"])
        elif actor.startswith("user:"):
            user = select(
                conn,
                context,
                "SELECT u.id, m.role FROM memberships m JOIN users u ON u.id=m.user_id "
                "WHERE m.workspace_id=? AND u.id=? AND u.disabled_at IS NULL",
                (workspace_id, actor[5:]),
            ).fetchone()
            if user is None:
                continue
            from commonplace.sessions import person_policy

            policy = person_policy(user["role"])
        else:
            continue
        if policy.has_scope("memory:read") and policy.can_read_wing(board["wing"]):
            eligible = True
    return eligible


def orchestrators(conn, board_conn, workspace_id, context):
    result = []
    seats_present = has_table(conn, context, "project_orchestrators")
    for board in board_rows(board_conn, context):
        context.remaining()
        seat = (
            select(
                conn,
                context,
                "SELECT name,connection_id FROM project_orchestrators "
                "WHERE workspace_id=? AND board=? AND role='main'",
                (workspace_id, board["slug"]),
            ).fetchone()
            if seats_present
            else None
        )
        actor = board["coordinator"]
        connection = None
        if actor and actor.startswith("conn:"):
            ref = actor[5:]
            if identifier(ref):
                connection = current_connection(conn, workspace_id, context, cid=ref)
            else:
                connection = current_connection(conn, workspace_id, context, name=ref)
        result.append(
            {
                "id": board["id"],
                "slug": slug(board["slug"]),
                "name": display(board["name"]),
                "coordinator": display(actor) if actor else None,
                "seat_name": display(seat["name"]) if seat else None,
                "source": "coordinator" if actor else "seat" if seat else "not_configured",
                "connected": connection is not None,
                "activity": status(connection["last_used_at"], context.now) if connection else None,
                "last_used_at": timestamp(connection["last_used_at"]) if connection else None,
                "wakeable": bool(
                    connection and wakeable(conn, workspace_id, context, board, actor)
                ),
            }
        )
    return result


REQUESTER_LIMIT = 32
CANDIDATE_LIMIT = 100


def requester_views(conn, workspace_id, context, raw):
    """Name and state of a machine's stored requester ids, as the owner may write them back.

    The state follows the start check: only an active, trusted, personal-token agent counts.
    Ids of other workspaces stay unnamed."""
    try:
        refs = json.loads(raw or "[]")
    except ValueError:
        return [], 0
    refs = [ref for ref in refs if isinstance(ref, str)]
    result = []
    for ref in refs:
        if len(result) == REQUESTER_LIMIT:
            break
        if identifier(ref) is None:
            continue
        context.remaining()
        row = select(
            conn,
            context,
            "SELECT name,auth,policy FROM connections WHERE id=? AND workspace_id=?",
            (ref, workspace_id),
        ).fetchone()
        live = (
            row is not None
            and row["auth"] == "pat"
            and Policy.from_json(row["policy"]).trust == "trusted"
            and current_connection(conn, workspace_id, context, cid=ref) is not None
        )
        result.append(
            {
                "id": ref,
                "name": display(row["name"]) if row else None,
                "state": "active" if live else "inactive",
            }
        )
    return result, len(refs)


def attach_requesters(conn, workspace_id, context, items):
    """Add each listed machine's requesters; only the machines on the page are resolved."""
    if not items or "requesters" not in columns(conn, context, "runner_machines"):
        for item in items:
            item.update(requesters=[], requesters_total=0)
        return
    for item in items:
        context.remaining()
        row = select(
            conn,
            context,
            "SELECT requesters FROM runner_machines WHERE id=? AND workspace_id=?",
            (item["id"], workspace_id),
        ).fetchone()
        views, total = requester_views(
            conn, workspace_id, context, row["requesters"] if row else "[]"
        )
        item.update(requesters=views, requesters_total=total)


def requester_candidates(conn, workspace_id, context):
    """Active trusted workspace agents an owner may name as requesters (what the write accepts).

    Returns the first CANDIDATE_LIMIT and whether more exist."""
    if not has_table(conn, context, "runner_connections"):
        return [], False
    rows = select(
        conn,
        context,
        "SELECT c.id,c.name,c.policy,c.expires_at FROM connections c "
        "JOIN memberships m ON m.workspace_id=c.workspace_id AND m.user_id=c.user_id "
        "JOIN users u ON u.id=c.user_id "
        "LEFT JOIN runner_connections r ON r.connection_id=c.id "
        "WHERE c.workspace_id=? AND c.auth='pat' AND c.revoked_at IS NULL "
        "AND u.disabled_at IS NULL AND r.connection_id IS NULL ORDER BY c.name,c.id",
        (workspace_id,),
    ).fetchall()
    result = []
    for row in rows:
        context.remaining()
        expires = control_db._parse(row["expires_at"])
        if (expires is None or expires > context.now) and (
            Policy.from_json(row["policy"]).trust == "trusted"
        ):
            result.append({"id": identifier(row["id"]), "name": display(row["name"])})
            if len(result) > CANDIDATE_LIMIT:
                break
    return result[:CANDIDATE_LIMIT], len(result) > CANDIDATE_LIMIT


def machines(conn, workspace_id, context, *, active_only=False):
    if not has_table(conn, context, "runner_machines"):
        return []
    rows = select(
        conn,
        context,
        "SELECT m.id,m.label,m.enabled,m.revoked_at,m.binding_state,m.max_in_flight,"
        + "m.max_starts_per_hour,m.grant_version,m.created_at,mc.last_used_at AS machine_last_used_at,"
        "wc.last_used_at AS worker_last_used_at FROM runner_machines m "
        "LEFT JOIN connections mc ON mc.id=m.machine_connection_id AND mc.workspace_id=m.workspace_id "
        "LEFT JOIN connections wc ON wc.id=m.worker_connection_id AND wc.workspace_id=m.workspace_id "
        "WHERE m.workspace_id=? "
        + ("AND m.enabled=1 AND m.revoked_at IS NULL " if active_only else "")
        + "ORDER BY m.created_at,m.id",
        (workspace_id,),
    ).fetchall()
    result = []
    for row in rows:
        context.remaining()
        result.append(
            {
                "id": identifier(row["id"], r"mach_[a-f0-9]{16}"),
                "label": display(row["label"]),
                "enabled": bool(row["enabled"]),
                "revoked_at": timestamp(row["revoked_at"]),
                "binding_state": row["binding_state"]
                if row["binding_state"] in ("unbound", "owner_attested")
                else None,
                "verified": False,
                "machine_last_used_at": timestamp(row["machine_last_used_at"]),
                "worker_last_used_at": timestamp(row["worker_last_used_at"]),
                "activity": status(row["machine_last_used_at"], context.now),
                "worker_activity": status(row["worker_last_used_at"], context.now),
                "max_in_flight": row["max_in_flight"],
                "max_starts_per_hour": row["max_starts_per_hour"],
                "grant_version": row["grant_version"],
                "created_at": timestamp(row["created_at"]),
            }
        )
    return result


def worker_actors(conn, workspace_id, context):
    if not has_table(conn, context, "runner_machines"):
        return frozenset()
    return frozenset(
        "conn:" + row[0]
        for row in select(
            conn,
            context,
            "SELECT c.name FROM runner_machines m JOIN connections c ON c.id=m.worker_connection_id "
            "WHERE m.workspace_id=? AND c.workspace_id=?",
            (workspace_id, workspace_id),
        )
    )


def list_page(items, workspace_id, section, kind, after, key):
    """Counts describe the whole list; cursors bind the list, section and workspace."""
    count = len(items)
    if after and after[0] == kind:
        items = [row for row in items if list(key(row)) > after[1]]
    more = len(items) > PAGE_SIZE
    items = items[:PAGE_SIZE]
    cursor = None
    if more:
        cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    [workspace_id, section, kind, list(key(items[-1]))], separators=(",", ":")
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return {"items": items, "count": count, "has_more": more, "next_cursor": cursor}


def runtime(gateway, workspace_id, context, after):
    result = {}
    for source in ("orchestration", "orchestrators", "machines"):
        try:
            context.remaining()
            with read_control(gateway.db, context) as conn:
                if source == "orchestration":
                    value = {"enabled": orchestration_setting(conn, workspace_id, context)}
                elif source == "orchestrators":
                    with read_store(gateway, workspace_id, "boards.sqlite3", context) as bc:
                        items = orchestrators(conn, bc, workspace_id, context)
                    value = list_page(
                        items,
                        workspace_id,
                        "runtime",
                        source,
                        after,
                        lambda r: (r["slug"], r["id"]),
                    )
                else:
                    items = machines(conn, workspace_id, context)
                    value = list_page(
                        items,
                        workspace_id,
                        "runtime",
                        source,
                        after,
                        lambda r: (r["created_at"], r["id"]),
                    )
                    attach_requesters(conn, workspace_id, context, value["items"])
                    candidates, more = requester_candidates(conn, workspace_id, context)
                    value.update(requester_candidates=candidates, requester_candidates_more=more)
            context.remaining()
            result[source] = {"state": "ready", **value}
        except Exception as exc:
            log_failure(LOG, "Admin runtime source unavailable", exc)
            result[source] = {"state": "error", "error": f"{source.title()} unavailable; try again"}
    return result


def project_limits(gateway, workspace_id, context, after):
    from commonplace.orch import store
    from commonplace.projects import limits, orchestration

    with read_control(gateway.db, context) as conn:
        on = orchestration_setting(conn, workspace_id, context)
        workers = worker_actors(conn, workspace_id, context) if on else frozenset()
        grants = [
            {k: row[k] for k in ("id", "max_in_flight", "max_starts_per_hour", "created_at")}
            for row in machines(conn, workspace_id, context, active_only=True)
        ]
    projects = []
    with (
        read_store(gateway, workspace_id, "boards.sqlite3", context) as bc,
        read_store(gateway, workspace_id, "runner.sqlite3", context) as rc,
    ):
        for board in board_rows(bc, context):
            context.remaining()
            configured = limits.get(bc, board["id"])["max_agents"]
            accepted = None
            if on:
                if has_table(rc, context, "orch_configs"):
                    version = store.accepted_in(rc, board["slug"])
                    if version is not None:
                        config = version.config()
                        if config.runner is not None:
                            accepted = config.max_agents
                used = orchestration.usage(
                    bc,
                    rc,
                    board_id=board["id"],
                    board_slug=board["slug"],
                    workers=workers,
                    now=control_db._iso(context.now),
                    allow_missing_runner=True,
                )
                cap, count = used.cap, used.used
            else:
                cap, count = configured, len(limits.working(bc, board["id"]))
            projects.append(
                {
                    "id": board["id"],
                    "slug": slug(board["slug"]),
                    "name": display(board["name"]),
                    "configured_max_agents": configured,
                    "accepted_config_max_agents": accepted,
                    "effective_max_agents": cap,
                    "used": count,
                    "counting_mode": "orchestration_slots" if on else "distinct_doing_agents",
                }
            )
            context.remaining()
    pp = list_page(
        projects, workspace_id, "limits", "projects", after, lambda r: (r["slug"], r["id"])
    )
    mp = list_page(
        grants, workspace_id, "limits", "machines", after, lambda r: (r["created_at"], r["id"])
    )
    return {
        "orchestration_enabled": on,
        "workspace_max_agents": None,
        "workspace_limit_source": "not_configured",
        "projects": pp.pop("items"),
        "machines": [
            {k: r[k] for k in ("id", "max_in_flight", "max_starts_per_hour")}
            for r in mp.pop("items")
        ],
        "pagination": {"projects": pp, "machines": mp},
    }


def stuck(gateway, workspace_id, context, after):
    from commonplace.boards.display import _waiting_on_person, stale_before
    from commonplace.boards.service import _stuck
    from commonplace.boards.store import active_blockers

    with read_control(gateway.db, context) as conn:
        enforced = orchestration_setting(conn, workspace_id, context)
    cards = []
    counts = dict.fromkeys(("expired_lease", "blocked", "waiting_on_person", "stale_agent"), 0)
    with (
        read_store(gateway, workspace_id, "boards.sqlite3", context) as bc,
        read_store(gateway, workspace_id, "runner.sqlite3", context) as rc,
    ):
        expired = {}
        if has_table(rc, context, "runner_jobs"):
            for row in select(
                rc,
                context,
                "SELECT id,task_number,board_slug,state,lease_until FROM runner_jobs "
                "WHERE state IN ('leased','starting','uncertain') AND lease_until IS NOT NULL ORDER BY id",
            ):
                context.remaining()
                if control_db._parse(row["lease_until"]) <= context.now:
                    expired.setdefault((row["board_slug"], row["task_number"]), []).append(
                        {
                            "job_id": identifier(row["id"], r"job_[a-f0-9]{16}"),
                            "state": row["state"],
                            "lease_until": timestamp(row["lease_until"]),
                        }
                    )
        heartbeat = (
            "t.heartbeat_at"
            if bc is not None and "heartbeat_at" in columns(bc, context, "tasks")
            else "NULL AS heartbeat_at"
        )
        rows = (
            select(
                bc,
                context,
                f"SELECT t.id,t.title,t.status,t.assignee,t.claim_until,{heartbeat},t.updated_at,"
                "t.ask,t.ask_to,t.return_to,b.slug AS board FROM tasks t JOIN boards b ON b.id=t.board_id "
                "WHERE b.archived=0 AND t.status!='done' ORDER BY t.id",
            ).fetchall()
            if bc is not None
            else []
        )
        cutoff = stale_before(context.now)
        for row in rows:
            context.remaining()
            facts = []
            expired_jobs = expired.get((row["board"], row["id"]), [])
            claim = control_db._parse(row["claim_until"])
            if (
                row["status"] == "doing" and claim is not None and claim <= context.now
            ) or expired_jobs:
                facts.append("expired_lease")
            blockers = active_blockers(bc, row["id"])
            if blockers:
                facts.append("blocked")
            if _waiting_on_person(dict(row)):
                facts.append("waiting_on_person")
            if _stuck(row, cutoff):
                facts.append("stale_agent")
            if not facts:
                continue
            for fact in facts:
                counts[fact] += 1
            cards.append(
                {
                    "number": row["id"],
                    "board": slug(row["board"]),
                    "title": display(row["title"]),
                    "status": row["status"],
                    "assignee": display(row["assignee"]) if row["assignee"] else None,
                    "claim_until": timestamp(row["claim_until"]),
                    "heartbeat_at": timestamp(row["heartbeat_at"]),
                    "updated_at": timestamp(row["updated_at"]),
                    "reasons": facts,
                    "active_blockers": [
                        {"number": b["number"], "title": display(b["title"])} for b in blockers
                    ],
                    "expired_jobs": expired_jobs,
                    "open_url": f"#/card/{row['id']}",
                }
            )
    paged = list_page(cards, workspace_id, "stuck", "cards", after, lambda r: (r["number"],))
    return {
        "cards": paged.pop("items"),
        **paged,
        "reason_counts": counts,
        "blockers_enforced": enforced,
    }


def runtime_cursor_key(raw, workspace_id, section):
    if not raw or len(raw) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        raise ValueError()
    try:
        value = json.loads(
            base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeError):
        raise ValueError() from None
    kinds = {
        "runtime": ("orchestrators", "machines"),
        "limits": ("projects", "machines"),
        "stuck": ("cards",),
    }
    if not isinstance(value, list) or len(value) != 4 or value[:2] != [workspace_id, section]:
        raise ValueError()
    kind, key = value[2:]
    if not isinstance(kind, str) or kind not in kinds[section] or not isinstance(key, list):
        raise ValueError()
    if kind == "cards":
        valid = len(key) == 1 and type(key[0]) is int and 0 < key[0] < 2**63
    elif kind == "machines":
        valid = (
            len(key) == 2
            and timestamp(key[0])
            and isinstance(key[1], str)
            and re.fullmatch(r"mach_[a-f0-9]{16}", key[1])
        )
    else:
        valid = (
            len(key) == 2
            and isinstance(key[0], str)
            and slug(key[0])
            and type(key[1]) is int
            and 0 < key[1] < 2**63
        )
    if not valid:
        raise ValueError()
    return kind, key


INSTANCE_DENIAL = "Instance administrator access required"


def instance_authorized(gateway, principal, context):
    """Deny cheaply before the exact server user flag read, without app locks."""
    if principal is None or not principal.person or principal.role != "owner":
        return False
    with read_control(gateway.db, context) as conn:
        row = select(
            conn, context, "SELECT is_admin FROM users WHERE id=?", (principal.user_id,)
        ).fetchone()
    return row is not None and row["is_admin"] == 1


def deploy(context):
    from commonplace import __version__ as commonplace_version
    from mempalace.version import __version__ as mempalace_version

    context.remaining()
    try:
        commit = (
            resources.files("commonplace.web")
            .joinpath("_build_commit.txt")
            .read_text(encoding="ascii")
            .strip()
        )
    except (OSError, UnicodeError):
        commit = ""
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", commit):
        commit = None
    context.remaining()
    return {
        "commonplace_version": commonplace_version,
        "mempalace_version": mempalace_version,
        "commit": commit,
        "commit_source": "build" if commit else "unknown",
    }


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
            or parts[1] not in ("users", "connections", "runtime", "limits", "stuck")
        ):
            send_json(h, 404, {"error": "not found"})
            return
        workspace_id, section = parts
        try:
            pairs = parse_qsl(urlsplit(h.path).query, keep_blank_values=True, strict_parsing=True)
            if any(key != "cursor" for key, _ in pairs) or len(pairs) > 1:
                raise ValueError()
            decode = cursor_key if section in ("users", "connections") else runtime_cursor_key
            after = decode(pairs[0][1], workspace_id, section) if pairs else None
        except (ValueError, TypeError):
            send_json(h, 400, {"error": "Invalid admin query"})
            return
        try:
            context = ReadContext(gateway.admin_clock)
            if section in ("users", "connections"):
                with read_control(gateway.db, context) as conn:
                    projector = users if section == "users" else connections
                    result = projector(conn, workspace_id, context, after)
            else:
                projector = {"runtime": runtime, "limits": project_limits, "stuck": stuck}[section]
                result = projector(gateway, workspace_id, context, after)
            if section != "runtime":
                context.remaining()
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

    def instance(h):
        principal = require_principal(gateway, h)
        if principal is None:
            return
        parts = path_parts(h, "/api/admin/instance/")
        if len(parts) != 1 or parts[0] not in ("deploy", "backups"):
            send_json(h, 404, {"error": "not found"})
            return
        section = parts[0]
        try:
            context = ReadContext(gateway.admin_clock)
            if not instance_authorized(gateway, principal, context):
                send_json(h, 403, {"error": INSTANCE_DENIAL})
                return
            if urlsplit(h.path).query:
                send_json(h, 400, {"error": "Invalid admin query"})
                return
            if section == "deploy":
                result = deploy(context)
            else:
                from commonplace.backup_status import read_receipts

                result = read_receipts(context)
            send_json(h, 200, result)
        except Exception as exc:
            log_failure(LOG, "Admin instance source unavailable", exc)
            send_json(h, 503, {"error": f"{section.title()} unavailable; try again"})

    gateway.add_prefix_route("GET", "/api/admin/instance/", instance)
    gateway.add_prefix_route("GET", "/api/admin/workspaces/", inventory)
