"""One authorization and move-rule implementation for MCP and HTTP."""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from commonplace import events, privacy
from commonplace.callstack import LIMITS
from commonplace.control_db import ControlError
from commonplace.extensions import ToolError
from commonplace.http_util import log_failure

from .digest import digest
from .display import stale_before
from .store import (
    STATUSES,
    BoardStore,
    append_event,
    card,
    expire_claims,
    now,
    owns_card,
    require_visible,
    task_number,
    task_row,
)

logger = logging.getLogger(__name__)


def open_store(workspace_dir, *, legacy_origin=None, db=None, workspace_id=None) -> BoardStore:
    store = BoardStore(workspace_dir, legacy_origin=legacy_origin)
    if db is not None and (db.rule_stores(workspace_id) or {}).get("boards_store_id") is None:
        from commonplace.rules.checks import meta

        db.bind_rule_stores(workspace_id, None, meta(store.path, "store_id"))
    return store


class VersionClash(ToolError):
    def __init__(self, current):
        super().__init__("no: card changed")
        self.current = current


class ParentError(ToolError):
    """An explicit parent is invalid or outside this actor's work."""


def validate_text(value, name, *, required=False, limit=100_000):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ToolError(f"no: {name} must be {'nonempty ' if required else ''}text")
    return value


CALLSTACK_KEYS = {"base", "head", "stats", "roots", "files", "truncated", "more"}
CALLSTACK_NODE = {"name", "file", "line", "change", "params", "children", "more", "seen"}


def callstack_json(value):
    """The PR call tree an agent reports, checked and stored as compact JSON (None clears)."""
    if value is None:
        return None
    nodes = 0

    def check(ok):
        if not ok:
            raise ToolError("no: invalid callstack")

    def text(item, key, limit, required=False):
        if key in item or required:
            check(isinstance(item.get(key), str) and 0 < len(item[key]) <= limit)

    def count(item, key):
        if key in item:
            check(type(item[key]) is int and 0 <= item[key] <= 1_000_000)

    def node(item, depth):
        nonlocal nodes
        nodes += 1
        check(isinstance(item, dict) and not set(item) - CALLSTACK_NODE)
        check(
            depth < LIMITS["depth"]
            and nodes <= LIMITS["nodes"]
            and item.get("change") in ("new", "changed", "unchanged")
        )
        text(item, "name", LIMITS["name"], required=True)
        text(item, "file", LIMITS["file"])
        text(item, "params", LIMITS["params"])
        count(item, "line")
        count(item, "more")
        check(isinstance(item.get("seen", False), bool))
        check(isinstance(item.get("children", []), list))
        for child in item.get("children", []):
            node(child, depth + 1)

    check(isinstance(value, dict) and not set(value) - CALLSTACK_KEYS)
    text(value, "base", LIMITS["base"])
    text(value, "head", LIMITS["head"])
    count(value, "more")
    check(isinstance(value.get("truncated", False), bool))
    stats = value.get("stats", {})
    check(isinstance(stats, dict) and set(stats) <= {"new", "changed", "files"})
    for key in stats:
        count(stats, key)
    check(isinstance(value.get("roots", []), list) and isinstance(value.get("files", []), list))
    for root in value.get("roots", []):
        node(root, 0)
    check(len(value.get("files", [])) <= LIMITS["files"])
    for item in value.get("files", []):
        check(isinstance(item, dict) and set(item) == {"file", "change"})
        text(item, "file", LIMITS["file"], required=True)
        check(item["change"] in ("new", "changed", "deleted"))
    stored = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(stored.encode()) > LIMITS["bytes"]:
        raise ToolError("no: callstack is too large")
    return stored


def choices(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 6:
        raise ToolError("no: options must contain up to six choices")
    return [validate_text(v, "option", required=True, limit=120) for v in value]


def review_request(question, options, context):
    help_text = (
        "no: review needs a real question (at least 24 characters), context in body/status_line "
        "or context (at least 20 characters), and at least 2 distinct options. "
        "Stay in doing while waiting on a gate or ask your orchestrator."
    )
    try:
        question = validate_text(question, "question", limit=5000)
        context = validate_text(context, "context")
        options = choices(options)
    except ToolError:
        raise ToolError(help_text) from None
    if (
        len(question.strip()) < 24
        or len(context.strip()) < 20
        or len(options) < 2
        or len({v.strip().casefold() for v in options}) != len(options)
    ):
        raise ToolError(help_text)
    return question, options


def review_context(row, updates=None):
    return "\n\n".join((updates or {}).get(key, row[key]) or "" for key in ("status_line", "body"))


def flags_for(*values):
    content = "\n".join(v for v in values if isinstance(v, str))
    patterns = {
        "ignore_previous": r"ignore\s+(?:all\s+)?previous",
        "tool_syntax": r"<tool_call|\btools/call\b|<function=",
        "curl_pipe": r"\b(?:curl|wget)\b[^\n]*\|\s*(?:ba)?sh\b",
        "long_base64": r"[A-Za-z0-9+/]{200,}={0,2}",
        "hidden_unicode": r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]",
        "image_query": r"https?://[^\s]+\.(?:png|jpg|jpeg|gif|svg|webp)\?",
    }
    return sorted(
        {name for name, pattern in patterns.items() if re.search(pattern, content, re.I)}
        | set(privacy.scan(content))
    )


def normalize_repo(value):
    value = validate_text(value, "repo", required=True, limit=1000).strip()
    if value.startswith("git@"):
        value = value[4:].replace(":", "/", 1)
    elif "://" in value:
        parsed = urlparse(value)
        value = (parsed.hostname or "") + parsed.path
    value = value.rstrip("/").removesuffix(".git").lower()
    if len(value.split("/")) == 2 and "." not in value.split("/", 1)[0]:
        value = "github.com/" + value
    return value


def _restricted(principal):
    return principal.policy.trust == "untrusted" or principal.policy.write_wing in (
        "inbox",
        "wing_inbox",
    )


def _trusted_agent(principal):
    return (
        not principal.person and principal.policy.trust == "trusted" and not _restricted(principal)
    )


def _can_coordinate(principal, wing):
    return (
        not _restricted(principal)
        and principal.policy.has_scope("memory:read")
        and principal.policy.has_scope("memory:write")
        and principal.policy.tool_visible("space_read")
        and principal.policy.tool_visible("space_post")
        and principal.policy.can_read_wing(wing)
        and principal.policy.can_write_wing(wing)
    )


def _waiting_on_person(row):
    return (
        bool(row["ask"])
        or any((row[key] or "").startswith("user:") for key in ("assignee", "ask_to"))
        or (row["status"] == "backlog" and (row["return_to"] or "").startswith("user:"))
    )


def _owner(principal, *, trusted_read=False):
    if principal.role == "owner" or (
        trusted_read and _trusted_agent(principal) and principal.policy.has_scope("memory:read")
    ):
        return
    raise ToolError("no: only a workspace owner can do that")


def needs_me(store, principal) -> dict:
    """The user's visible decision queue. One card can belong to several buckets."""
    _owner(principal, trusted_read=True)
    result = {
        key: [] for key in ("asks", "done_review", "backlog_from_agents", "ready_to_merge", "stale")
    }
    cutoff = stale_before()
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT t.*,b.slug AS board,b.wing AS board_wing FROM tasks t "
            "JOIN boards b ON b.id=t.board_id ORDER BY t.created_at,t.id"
        ).fetchall()
        for row in rows:
            if not principal.policy.can_read_wing(row["board_wing"]):
                continue
            item = card(conn, row, principal)
            addressed = row["ask_to"] in (None, f"user:{principal.user_id}")
            if row["status"] == "review" and addressed:
                result[
                    "done_review"
                    if row["review_kind"] == "completion" or row["ask"] == "Done?"
                    else "asks"
                ].append(item)
            if (
                row["status"] == "backlog"
                and row["created_by"].startswith("conn:")
                and row["proposal_status"] != "rejected"
            ):
                result["backlog_from_agents"].append(item)
            if row["pr_url"] and row["checks"] == "passing" and row["status"] != "done":
                result["ready_to_merge"].append(item)
            if row["status"] == "doing" and row["assignee"].startswith("conn:"):
                if (row["heartbeat_at"] or row["updated_at"]) < cutoff:
                    result["stale"].append(item)
    result["count"] = len({c["number"] for bucket in result.values() for c in bucket})
    return result


class BoardService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.principal = ctx.principal
        self.store = open_store(
            ctx.workspace_dir(),
            legacy_origin=self._legacy_restricted_origin,
            db=ctx.db,
            workspace_id=self.principal.workspace_id,
        )
        self.pending = []

    def _legacy_restricted_origin(self, row):
        if row["stamp_trust"] not in ("trusted", "standard"):
            return True
        try:
            connection = self.ctx.db.connection(
                row["stamp_connection"], self.principal.workspace_id
            )
            if (
                connection["name"] != row["stamp_connection"]
                or connection["user_id"] != row["stamp_user"]
                or connection["created_at"] > row["created_at"]
            ):
                return True
            creator = self.ctx.db.authenticate_connection(connection["id"])
        except (ControlError, ValueError, TypeError):
            return True
        return creator is None or _restricted(creator)

    @contextmanager
    def transaction(self):
        self.pending = []
        conflict = None
        with self.store.transaction() as conn:
            try:
                yield conn
            except VersionClash as exc:
                # System expiries precede the version check; publish that current card.
                conflict = exc
        for kind, subject, data, actor in self.pending:
            try:
                events.emit(
                    self.ctx.db,
                    self.principal.workspace_id,
                    kind,
                    subject=subject,
                    actor=actor,
                    data=data,
                )
            except Exception as exc:
                log_failure(logger, "committed board event emission failed", exc)
        if conflict is not None:
            raise conflict

    def _emit(self, conn, number, kind, *, actor=None, **extra):
        row = task_row(conn, number)
        actor = actor or self.principal.actor
        data = {
            "board": row["board"],
            "number": number,
            "parent": row["parent_number"],
            "title": row["title"],
            "status": row["status"],
            "assignee": row["assignee"],
            "ask_to": row["ask_to"],
            "actor": actor,
            **extra,
        }
        self.pending.append((kind, f"#{number}", data, actor))

    def _log(self, conn, number, kind, **fields):
        append_event(
            conn,
            number,
            kind,
            self.principal.actor,
            trust=self.principal.policy.trust,
            user_id=self.principal.user_id,
            **fields,
        )

    def _own_card(self, row):
        return owns_card(row, self.principal.actor)

    def _top_level_taker(self):
        p = self.principal
        return not p.person and p.parent_connection_id is None

    def _creator_connection(self, row):
        try:
            actor = row["created_by"]
            if not actor.startswith("conn:") or row["stamp_connection"] != actor[5:]:
                return None
            connection = self.ctx.db.connection(actor[5:], self.principal.workspace_id)
            if (
                connection["name"] != row["stamp_connection"]
                or connection["user_id"] != row["stamp_user"]
                or connection["created_at"] > row["created_at"]
            ):
                return None
            return connection
        except (ControlError, ValueError, TypeError):
            return None

    def _top_level_origin(self, row):
        try:
            if row["agent_parent"] is not None or row["status"] not in ("todo", "doing", "review"):
                return False
            connection = self._creator_connection(row)
            if connection is None or connection["parent_id"] is not None:
                return False
            creator = self.ctx.db.authenticate_connection(connection["id"])
            return bool(
                creator is not None
                and creator.policy.has_scope("memory:write")
                and creator.policy.can_read_wing(row["board_wing"])
            )
        except (ControlError, ValueError, TypeError):
            return False

    def _owning_top_level(self, row, q):
        try:
            if q.person or q.parent_connection_id is not None:
                return False
            creator = self._creator_connection(row)
            if creator is None or creator["id"] == q.connection_id:
                return False
            # top_level_connection does not check revocation. q is always a live
            # principal (caller or authenticated approver), so revoked roots cannot match.
            return (
                self.ctx.db.top_level_connection(creator["id"], self.principal.workspace_id)["id"]
                == q.connection_id
            )
        except (ControlError, ValueError, TypeError):
            return False

    def _coordinates(self, q, row):
        try:
            return (
                not q.person
                and row["created_by"] != q.actor
                and row["board_coordinator"] == q.actor
                and _can_coordinate(q, row["board_wing"])
            )
        except (ControlError, ValueError, TypeError):
            return False

    def _top_level_actor(self, actor):
        try:
            return actor.startswith("conn:") and (
                self.ctx.db.connection(actor[5:], self.principal.workspace_id)["parent_id"] is None
            )
        except (ControlError, ValueError, TypeError):
            return False

    def _origin_released(self, row):
        actor = row["approved_by"]
        if not actor or row["proposal_status"] == "rejected":
            return False
        try:
            if actor.startswith("user:"):
                user = self.ctx.db.user(actor[5:])
                return not user["disabled_at"] and any(
                    m["id"] == user["id"] and m["role"] == "owner"
                    for m in self.ctx.db.members(self.principal.workspace_id)
                )
            if not actor.startswith("conn:"):
                return False
            connection = self.ctx.db.connection(actor[5:], self.principal.workspace_id)
            approver = self.ctx.db.authenticate_connection(connection["id"])
            return bool(
                approver is not None
                and approver.actor == actor
                and approver.policy.has_scope("memory:write")
                and approver.policy.can_read_wing(row["board_wing"])
                and (
                    _trusted_agent(approver)
                    or self._coordinates(approver, row)
                    or self._owning_top_level(row, approver)
                )
            )
        except (ControlError, ValueError, TypeError):
            return False

    def _available_origin(self, row):
        p = self.principal
        return (
            (p.person and p.role == "owner")
            or row["restricted_origin"] == 0
            or (self._own_card(row) and row["created_by"] == p.actor)
            or (
                row["assignee"] == p.actor
                and p.parent_agent is not None
                and p.parent_agent == row["created_by"]
            )
            or self._origin_released(row)
        )

    def _claimable_origin(self, row):
        return self._available_origin(row) or (
            self._top_level_taker() and self._top_level_origin(row)
        )

    def _write(self, board, *, submission=False, row=None, claiming=False):
        p = self.principal
        if not p.policy.has_scope("memory:write") or not p.policy.can_read_wing(board["wing"]):
            raise ToolError("no: this connection cannot change this board")
        if not submission and row is not None:
            available = (
                self._claimable_origin(row)
                if claiming or self._own_card(row)
                else self._available_origin(row)
            )
            if not available:
                raise ToolError(
                    "no: restricted-origin work needs parent or workspace owner release"
                )
        # Board ownership is independent of memory write destinations and trust.
        if submission or self._own_card(row):
            return
        if claiming and self._top_level_taker() and row["status"] == "todo":
            # Work-claim authority is coordination, not memory-write trust
            # (owner decision 3 Nov 14:17).
            return
        if _restricted(p):
            if submission:
                return
            raise ToolError("no: this connection can only add cards and comments")
        if not p.policy.can_write_wing(board["wing"]):
            raise ToolError("no: this connection cannot change this board")

    def _visible_boards(self, conn):
        return [
            r
            for r in conn.execute("SELECT * FROM boards WHERE archived=0 ORDER BY id")
            if self.principal.policy.can_read_wing(r["wing"])
        ]

    def _board(self, conn, ref=None, *, create=False):
        if ref is None:
            visible = self._visible_boards(conn)
            if len(visible) == 1:
                return visible[0]
            if visible:
                raise ToolError(
                    "no: choose a board with board=<slug> when several boards are visible"
                )
            ref = "commonplace"
        validate_text(ref, "board", required=True, limit=1000)
        row = conn.execute("SELECT * FROM boards WHERE slug=?", (ref,)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT b.* FROM boards b JOIN board_repos r ON r.board_id=b.id WHERE r.repo=?",
                (normalize_repo(ref),),
            ).fetchone()
        if row is not None:
            if row["archived"] or not self.principal.policy.can_read_wing(row["wing"]):
                raise ToolError("no: board not found")
            return row
        if create:
            bootstrap = (
                ref == "commonplace" and not conn.execute("SELECT 1 FROM boards LIMIT 1").fetchone()
            )
            if bootstrap or self.principal.policy.trust == "trusted":
                return self._add_board(conn, slug=ref, bootstrap=bootstrap)
        raise ToolError("no: board not found")

    def _add_board(self, conn, *, slug, name=None, wing=None, repo=None, bootstrap=False):
        p = self.principal
        if not bootstrap and not p.person and p.policy.trust != "trusted":
            raise ToolError("no: only an owner or trusted connection can create a board")
        slug = validate_text(slug, "slug", required=True, limit=100)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
            raise ToolError("no: slug must use lowercase letters, numbers, underscores or hyphens")
        existing = conn.execute("SELECT * FROM boards WHERE slug=?", (slug,)).fetchone()
        if existing is not None and not p.policy.can_read_wing(existing["wing"]):
            raise ToolError("no: board not found")
        wing = validate_text(
            wing if wing is not None else "shared", "theme", required=True, limit=200
        )
        if (
            not p.policy.has_scope("memory:write")
            or not p.policy.can_read_wing(wing)
            or (wing != "shared" and not p.policy.can_write_wing(wing))
        ):
            raise ToolError("no: this connection cannot create this board")
        if existing is not None:
            raise ToolError("no: board already exists")
        name = validate_text(name if name is not None else slug, "name", required=True, limit=1000)
        normalized_repo = normalize_repo(repo) if repo is not None else None
        if (
            normalized_repo
            and conn.execute(
                "SELECT 1 FROM board_repos WHERE repo=?", (normalized_repo,)
            ).fetchone()
        ):
            raise ToolError("no: repo is already assigned")
        cur = conn.execute(
            "INSERT INTO boards(slug,name,wing,stamp_user,stamp_connection,stamp_trust,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                slug,
                name,
                wing,
                p.user_id,
                None if p.person else p.connection,
                p.policy.trust,
                now(),
            ),
        )
        if normalized_repo:
            conn.execute(
                "INSERT INTO board_repos(repo,board_id) VALUES (?,?)",
                (normalized_repo, cur.lastrowid),
            )
        return conn.execute("SELECT * FROM boards WHERE id=?", (cur.lastrowid,)).fetchone()

    def _expire(self, conn, board):
        for row in expire_claims(conn, board["id"]):
            self._emit(
                conn,
                row["id"],
                "card.expired",
                actor="system",
                **{"from": "doing", "to": "doing", "target": row["assignee"]},
            )

    def _get(self, conn, ref):
        row = task_row(conn, ref)
        require_visible(row, self.principal)
        board = conn.execute("SELECT * FROM boards WHERE id=?", (row["board_id"],)).fetchone()
        self._expire(conn, board)
        return task_row(conn, row["id"]), board

    def _version(self, conn, row, version):
        if version is not None:
            if not isinstance(version, int) or isinstance(version, bool):
                raise ToolError("no: version must be an integer")
            if version != row["version"]:
                raise VersionClash(card(conn, row, self.principal))

    def _owner_actor(self):
        for member in self.ctx.db.members(self.principal.workspace_id):
            if member["role"] == "owner":
                return f"user:{member['id']}"
        raise ToolError("no: workspace owner not found")

    def _actor(self, value):
        value = validate_text(value, "recipient", required=True, limit=200)
        if value == "self":
            return self.principal.actor
        if value == "anyone":
            return None
        if value.casefold() == "owner":
            return self._owner_actor()
        members = self.ctx.db.members(self.principal.workspace_id)
        matches = [
            m
            for m in members
            if value.casefold()
            in (
                f"user:{m['id']}".casefold(),
                m["email"].casefold(),
                m["email"].split("@", 1)[0].casefold(),
                (self.ctx.db.user(m["id"])["name"] or "").casefold() or None,
            )
        ]
        if len(matches) == 1:
            return f"user:{matches[0]['id']}"
        if matches or value.startswith("user:") or "@" in value:
            raise ToolError("no: recipient is not a unique workspace member")
        if value.startswith("conn:"):
            value = value[5:]
        if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,120}", value):
            raise ToolError("no: invalid connection")
        return f"conn:{value}"

    def _work_actor(self, value, board):
        actor = self._actor(value)
        if actor is None or not actor.startswith("conn:"):
            return actor
        try:
            connection = self.ctx.db.connection(actor[5:], self.principal.workspace_id)
            recipient = self.ctx.db.authenticate_connection(connection["id"])
        except ControlError:
            recipient = None
        if (
            recipient is None
            or not recipient.policy.has_scope("memory:write")
            or not recipient.policy.can_read_wing(board["wing"])
        ):
            raise ToolError("no: recipient must be an active connection able to work on this board")
        return recipient.actor

    def _until(self, board):
        return (datetime.now(timezone.utc) + timedelta(minutes=board["claim_minutes"])).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def _set(self, conn, row, updates):
        if (
            self.principal.person
            and self.principal.role == "owner"
            and row["restricted_origin"] == 1
            and updates.get("status") in ("todo", "doing")
        ):
            updates["approved_by"] = self.principal.actor
        updates.update(
            version=row["version"] + 1, updated_at=now(), updated_by=self.principal.actor
        )
        if "status" in updates:
            updates["claim_from"] = (
                row["status"]
                if updates["status"] == "doing"
                and row["status"] in ("backlog", "todo")
                and not self.principal.person
                else None
            )
        if (
            row["status"] == "doing"
            and row["assignee"] == self.principal.actor
            and "claim_until" not in updates
            and not self.principal.person
        ):
            updates["claim_until"] = self._until(row)
        if "claim_until" in updates:
            updates["claim_expired_at"] = None
            if updates["claim_until"] is not None:
                updates["heartbeat_at"] = now()
        fields = ",".join(f"{key}=?" for key in updates)
        conn.execute(f"UPDATE tasks SET {fields} WHERE id=?", (*updates.values(), row["id"]))

    def _holder(self, row, *, editing=False, reopening=False, annotating=False, completing=False):
        if self.principal.person:
            return
        if row["proposal_status"] in ("pending", "rejected") and not self._own_card(row):
            raise ToolError("no: proposal needs its parent or the workspace owner")
        if row["status"] == "done" and not reopening:
            if annotating and _trusted_agent(self.principal) and not _waiting_on_person(row):
                return
            raise ToolError("no: completed cards are frozen for connections")
        if (
            row["status"] == "doing"
            and not self.principal.person
            and row["assignee"] != self.principal.actor
        ):
            raise ToolError(
                f"no: #{row['id']} is held by {row['assignee']} until {row['claim_until']}"
            )
        if (
            row["status"] == "todo"
            and row["assignee"] not in (None, self.principal.actor)
            and not self.principal.person
        ):
            raise ToolError("no: card is reserved for another connection")
        if completing and row["status"] in ("todo", "doing", "backlog") and not self._own_card(row):
            raise ToolError("no: take this card before completing it")
        if editing and not (
            (row["status"] == "doing" and row["assignee"] == self.principal.actor)
            or self._review_editor(row)
            or (
                row["status"] in ("todo", "backlog")
                and row["created_by"] == self.principal.actor
                and row["assignee"] is None
            )
        ):
            raise ToolError("no: take this card before editing it")

    def board_list(self, *, top_level=False):
        with self.transaction() as conn:
            result = []
            for board in self._visible_boards(conn):
                self._expire(conn, board)
                counts = dict.fromkeys(STATUSES, 0)
                counts.update(
                    {
                        r["status"]: r["n"]
                        for r in conn.execute(
                            "SELECT status,count(*) n FROM tasks WHERE board_id=? "
                            + ("AND parent_number IS NULL " if top_level else "")
                            + "GROUP BY status",
                            (board["id"],),
                        )
                    }
                )
                result.append(
                    {
                        "slug": board["slug"],
                        "name": board["name"],
                        "wing": board["wing"],
                        "coordinator": board["coordinator"],
                        "can_configure": self.principal.role == "owner",
                        "counts": counts,
                    }
                )
            return result

    def board_add(self, slug, name=None, wing=None, repo=None):
        with self.transaction() as conn:
            board = self._add_board(conn, slug=slug, name=name, wing=wing, repo=repo)
            return {
                "slug": board["slug"],
                "name": board["name"],
                "wing": board["wing"],
                "coordinator": board["coordinator"],
                "counts": dict.fromkeys(STATUSES, 0),
            }

    def board_set(self, board, coordinator):
        _owner(self.principal)
        with self.transaction() as conn:
            b = self._board(conn, board)
            self._write(b)
            actor = None
            if coordinator is not None:
                actor = self._actor(coordinator)
                candidates = [
                    self.ctx.db.authenticate_connection(c["id"])
                    for c in self.ctx.db.connections(self.principal.workspace_id)
                    if c["auth"] == "pat"
                ]
                if not any(
                    p is not None and p.actor == actor and _can_coordinate(p, b["wing"])
                    for p in candidates
                ):
                    raise ToolError(
                        "no: coordinator must be an active agent with access to this board"
                    )
            conn.execute("UPDATE boards SET coordinator=? WHERE id=?", (actor, b["id"]))
            self.pending.append(
                (
                    "space_configured",
                    f"space:{b['slug']}",
                    {"board": b["slug"]},
                    self.principal.actor,
                )
            )
            result = {"board": b["slug"], "coordinator": actor}
        if actor is not None:
            from commonplace import webhooks

            if not webhooks.has_space_subscription(
                self.ctx.db, self.ctx.gateway, self.principal.workspace_id, actor, b["slug"]
            ):
                result["warning"] = (
                    "Coordinator has no active webhook matching space_message on this board. "
                    f"Register webhook_add url=… events=space_message board={b['slug']}."
                )
        return result

    @staticmethod
    def _message(row):
        return {
            "id": row["id"],
            "text": row["text"],
            "actor": f"conn:{row['stamp_connection']}"
            if row["stamp_connection"] is not None
            else f"user:{row['stamp_user']}",
            "stamp_user": row["stamp_user"],
            "stamp_connection": row["stamp_connection"],
            "stamp_trust": row["stamp_trust"],
            "flags": json.loads(row["flags"]),
            "at": row["created_at"],
        }

    def space_read(self, board, after=None, last=None, before=None):
        if not self.principal.policy.has_scope("memory:read"):
            raise ToolError("no: this connection cannot read this board")
        for name, value in (("after", after), ("before", before)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**63
            ):
                raise ToolError(f"no: {name} must be a nonnegative message cursor")
        if last is not None and (type(last) is not int or not 1 <= last <= 100):
            raise ToolError("no: last must be between 1 and 100")
        if after is not None and (last is not None or before is not None):
            raise ToolError("no: after cannot be combined with last or before")
        with self.store.connection() as conn:
            b = self._board(conn, board)
            backward = last is not None or before is not None
            boundary = before if before is not None else 2**63 - 1
            window = (
                ("<?" if before is not None else "<=?") + " ORDER BY id DESC LIMIT ?"
                if backward
                else ">? ORDER BY id LIMIT ?"
            )
            messages = [
                self._message(row)
                for row in conn.execute(
                    "SELECT * FROM space_messages WHERE board_id=? AND id" + window,
                    (b["id"], boundary if backward else after or 0, last or 100),
                )
            ]
            if backward:
                messages.reverse()
            oldest = messages[0]["id"] if messages else 0
            return {
                "board": b["slug"],
                "coordinator": b["coordinator"],
                "messages": messages,
                "cursor": messages[-1]["id"] if messages else after or 0,
                "oldest": oldest,
                "has_more": bool(
                    conn.execute(
                        "SELECT 1 FROM space_messages WHERE board_id=? AND id<? LIMIT 1",
                        (b["id"], oldest),
                    ).fetchone()
                ),
            }

    def space_post(self, board, text):
        text = validate_text(text, "text", required=True)
        p = self.principal
        with self.transaction() as conn:
            b = self._board(conn, board)
            self._write(b)
            cur = conn.execute(
                "INSERT INTO space_messages(board_id,text,stamp_user,stamp_connection,"
                "stamp_trust,flags,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    b["id"],
                    text,
                    p.user_id,
                    None if p.person else p.connection,
                    p.policy.trust,
                    json.dumps(flags_for(text)),
                    now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM space_messages WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            self.pending.append(
                (
                    "space_message" if p.person else "space_replied",
                    f"space:{b['slug']}",
                    {"board": b["slug"], "message_id": row["id"], "coordinator": b["coordinator"]},
                    p.actor,
                )
            )
            return self._message(row)

    def task_list(
        self, board=None, status=None, mine=False, *, all_statuses=False, top_level=False
    ):
        with self.transaction() as conn:
            boards = [self._board(conn, board)] if board is not None else self._visible_boards(conn)
            if status is not None and status not in STATUSES:
                raise ToolError("no: invalid status")
            result = []
            for b in boards:
                self._expire(conn, b)
                for row in conn.execute(
                    "SELECT id FROM tasks WHERE board_id=? "
                    + ("AND parent_number IS NULL " if top_level else "")
                    + "ORDER BY position,id",
                    (b["id"],),
                ):
                    task = task_row(conn, row["id"])
                    if task["status"] not in (
                        (status,)
                        if status
                        else STATUSES
                        if all_statuses
                        else ("todo", "doing", "review")
                    ) and not (mine and status is None and task["proposal_status"] == "pending"):
                        continue
                    if mine and self.principal.actor not in (
                        task["assignee"],
                        task["return_to"],
                        task["ask_to"],
                        task["proposed_to"] if task["proposal_status"] == "pending" else None,
                        task["created_by"] if task["proposal_status"] == "pending" else None,
                    ):
                        continue
                    result.append(card(conn, task, self.principal))
            return result

    def _history_link_visible(self, memory_id, visibility):
        if self.principal.policy.sees_all_wings:
            return True
        if memory_id not in visibility:
            try:
                theme, _title = self._memory_theme(memory_id)
            except ToolError:
                visibility[memory_id] = False
            else:
                visibility[memory_id] = self.principal.policy.can_read_wing(theme)
        return visibility[memory_id]

    def _history_text(self, event, visibility):
        text = event["text"]
        if (
            not text
            or event["kind"] not in ("edit", "move")
            or event["from_status"] != "review"
            or self.principal.policy.sees_all_wings
        ):
            return text
        try:
            previous = json.loads(text)
        except ValueError:
            return text
        if not isinstance(previous, dict) or not isinstance(previous.get("links"), list):
            return text
        previous["links"] = [
            memory_id
            for memory_id in previous["links"]
            if self._history_link_visible(memory_id, visibility)
        ]
        return json.dumps(previous, ensure_ascii=False)

    def task_show(self, task):
        with self.transaction() as conn:
            row, _board = self._get(conn, task)
            result = card(conn, row, self.principal)
            hidden = self.store.hidden_link_targets(conn, row["id"], self.principal)
            linked = self.store.links(conn, row["id"], self.principal)
            visibility = {memory_id: False for memory_id in hidden}
            visibility.update({link["drawer_id"]: True for link in linked})
            result["events"] = [
                {
                    "kind": r["kind"],
                    "actor": f"conn:{r['stamp_connection']}"
                    if r["stamp_connection"]
                    else r["stamp_user"]
                    if r["stamp_user"].startswith(("user:", "system"))
                    else f"user:{r['stamp_user']}",
                    "text": self._history_text(r, visibility),
                    "at": r["created_at"],
                    "choice": r["choice"],
                    "target": r["target"],
                    **({"reason": r["reason"]} if r["kind"] == "release" else {}),
                    # Events that opened an ask carry the choices offered, verbatim.
                    **(
                        {"options": json.loads(r["options"] or "[]"), "context": r["context"]}
                        if r["to_status"] == "review" and r["kind"] in ("ask", "handoff", "move")
                        else {}
                    ),
                }
                for r in conn.execute(
                    "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (row["id"],)
                )
                if not (
                    r["kind"] in ("link", "unlink")
                    and not self._history_link_visible(r["target"], visibility)
                )
            ]
            result["linked"] = linked
            result["callstack"] = json.loads(row["callstack"]) if row["callstack"] else None
            return result

    def _parent_card(self, conn, board, parent, *, delegating=False):
        p = self.principal
        root = None
        if not p.person:
            try:
                ancestor = self.ctx.db.top_level_connection(p.connection_id, p.workspace_id)
            except ControlError as exc:
                raise ParentError(f"no: invalid connection ancestry: {exc}") from None
            if ancestor["id"] != p.connection_id:
                root = f"conn:{ancestor['name']}"
            elif delegating:
                root = p.actor
        if parent is not None:
            if root is None:
                if p.person:
                    raise ParentError("no: only a sub-actor can specify a parent card")
                root = p.actor
            try:
                number = task_number(parent)
                if number >= 2**63:
                    raise ToolError("no: card not found")
                row = task_row(conn, number)
            except ToolError:
                raise ParentError("no: parent card not found on this board") from None
            if row["board_id"] != board["id"]:
                raise ParentError("no: parent card must be on this board")
        elif root is not None:
            row = conn.execute(
                "SELECT id,parent_number,board_id,created_by,assignee FROM tasks WHERE board_id=? "
                "AND status='doing' AND assignee=? ORDER BY updated_at DESC,id DESC LIMIT 1",
                (board["id"], root),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT id,parent_number,board_id,created_by,assignee FROM tasks WHERE board_id=? "
                    "AND status!='done' AND created_by=? ORDER BY created_at DESC,id DESC LIMIT 1",
                    (board["id"], root),
                ).fetchone()
        else:
            row = None
        seen = set()
        while row is not None and row["parent_number"] is not None:
            if row["id"] in seen:
                raise ParentError("no: parent card cycle")
            seen.add(row["id"])
            row = task_row(conn, row["parent_number"])
            if row["board_id"] != board["id"]:
                raise ParentError("no: parent card must be on this board")
        if row is not None and root not in (row["created_by"], row["assignee"]):
            if parent is not None:
                raise ParentError(
                    "no: parent card must be created by or assigned to the top-level actor on this board"
                )
            return None
        return row["id"] if row is not None else None

    def _new_card(
        self,
        conn,
        board,
        *,
        title,
        body="",
        status="todo",
        assignee=None,
        labels=None,
        ask=None,
        options=None,
        target=None,
        message_id=None,
        parent=None,
        claim=False,
    ):
        p = self.principal
        if message_id is not None:
            if (
                isinstance(message_id, bool)
                or not isinstance(message_id, int)
                or not 1 <= message_id < 2**63
                or not conn.execute(
                    "SELECT 1 FROM space_messages WHERE id=? AND board_id=?",
                    (message_id, board["id"]),
                ).fetchone()
            ):
                raise ToolError("no: message not found in this board")
        title = validate_text(title, "title", required=True, limit=5000)
        body = validate_text(body, "body")
        if status == "review":
            ask, options = review_request(ask, options, body)
        if labels is None:
            labels = []
        if not isinstance(labels, list) or len(labels) > 100:
            raise ToolError("no: labels must be a list")
        labels = [validate_text(label, "label", required=True, limit=200) for label in labels]
        if status not in ("todo", "backlog", "doing", "review") or (
            status == "review" and ask is None
        ):
            raise ToolError("no: new cards must start in todo, backlog or doing")
        if not isinstance(claim, bool):
            raise ToolError("no: claim must be a boolean")
        if assignee is not None:
            assignee = self._work_actor(assignee, board)
        child = None
        if assignee and assignee.startswith("conn:") and assignee != p.actor:
            try:
                candidate = self.ctx.db.connection(assignee[5:], p.workspace_id)
                if candidate["parent_id"] == p.connection_id:
                    child = self.ctx.db.authenticate_connection(candidate["id"])
            except ControlError:
                pass
        if not p.person and _restricted(p) and assignee not in (None, p.actor) and child is None:
            if p.parent_connection_id is not None:
                raise ToolError("no: assign cards only to yourself or your direct child")
            if not self._top_level_actor(assignee):
                raise ToolError(
                    "no: assign cards only to yourself, your direct child or a top-level agent"
                )
        if child is not None and (
            not child.policy.has_scope("memory:write")
            or not child.policy.can_read_wing(board["wing"])
        ):
            raise ToolError("no: child cannot work on this board")
        parent_number = self._parent_card(conn, board, parent, delegating=child is not None)
        if (
            claim
            or status == "doing"
            or (not p.person and assignee == p.actor and status == "todo")
        ):
            assignee = assignee or p.actor
            if not p.person and assignee != p.actor and child is None:
                raise ToolError("no: claim cards only for yourself or your direct child")
            status = "doing"
        if _restricted(p) and not p.person and assignee is None and status in ("todo", "backlog"):
            assignee = p.actor
        if child is not None and parent_number is None:
            parent_card = conn.execute(
                "SELECT id FROM tasks WHERE board_id=? AND parent_number IS NULL AND status!='done' "
                "AND (assignee=? OR created_by=?) ORDER BY status='doing' DESC,updated_at DESC,id DESC LIMIT 1",
                (board["id"], p.actor, p.actor),
            ).fetchone()
            parent_number = parent_card[0] if parent_card else None
        proposal = bool(p.parent_agent and not p.person)
        if proposal:
            # A child cannot escape approval by requesting another lane or an owner ask.
            if ask is not None:
                raise ToolError(
                    "no: create a proposal for your orchestrator before asking the owner"
                )
            status, assignee = "backlog", p.actor
        until = self._until(board) if status == "doing" and assignee.startswith("conn:") else None
        position = conn.execute(
            "SELECT coalesce(max(position),0)+1 FROM tasks WHERE board_id=? AND status=?",
            (board["id"], status),
        ).fetchone()[0]
        repo = conn.execute(
            "SELECT repo FROM board_repos WHERE board_id=? ORDER BY repo LIMIT 1", (board["id"],)
        ).fetchone()
        at = now()
        cur = conn.execute(
            "INSERT INTO tasks(board_id,title,body,status,position,assignee,ask,ask_options,ask_to,return_to,flags,"
            "stamp_user,stamp_connection,stamp_trust,restricted_origin,created_at,updated_at,updated_by,created_by,repo,labels,checks,message_id,parent_number) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                board["id"],
                title,
                body,
                status,
                position,
                assignee,
                ask,
                json.dumps(options or []),
                target,
                p.actor if ask else None,
                json.dumps(flags_for(title, body, ask)),
                p.user_id,
                None if p.person else p.connection,
                p.policy.trust,
                int(_restricted(p)),
                at,
                at,
                p.actor,
                p.actor,
                repo[0] if repo else None,
                json.dumps(labels),
                "",
                message_id,
                parent_number,
            ),
        )
        number = cur.lastrowid
        conn.execute(
            "UPDATE tasks SET claim_until=?,heartbeat_at=?,agent_parent=?,proposal_status=?,"
            "proposed_to=?,review_kind=? WHERE id=?",
            (
                until,
                at if until else None,
                p.parent_agent,
                "pending" if proposal else None,
                p.parent_agent if proposal else None,
                "decision" if ask is not None else None,
                number,
            ),
        )
        self._log(conn, number, "add", to_status=status)
        self._emit(conn, number, "card.created")
        if status == "review":
            self._log(
                conn,
                number,
                "ask",
                text=ask,
                target=target,
                to_status="review",
                options=options or [],
                context=body,
            )
            self._emit(conn, number, "card.review_needed", return_to=p.actor)
        return card(conn, task_row(conn, number), self.principal)

    def task_add(
        self,
        title,
        body="",
        board=None,
        status="todo",
        assignee=None,
        labels=None,
        message_id=None,
        parent=None,
        claim=False,
    ):
        with self.transaction() as conn:
            b = self._board(conn, board, create=True)
            self._write(b, submission=True)
            self._expire(conn, b)
            return self._new_card(
                conn,
                b,
                title=title,
                body=body,
                status=status,
                assignee=assignee,
                labels=labels,
                message_id=message_id,
                parent=parent,
                claim=claim,
            )

    def task_approve(self, task, approve=True, version=None):
        if not isinstance(approve, bool):
            raise ToolError("no: approve must be a boolean")
        with self.transaction() as conn:
            row, board = self._get(conn, task)
            self._write(board, submission=True)
            self._version(conn, row, version)
            p = self.principal
            owner = p.person and p.role == "owner"
            parent = False
            if not p.person and row["proposed_to"] == p.actor:
                creator = self.ctx.db.connection(
                    row["created_by"].removeprefix("conn:"), p.workspace_id
                )
                parent = creator["parent_id"] == p.connection_id
            coordinator = self._coordinates(p, row)
            root = self._owning_top_level(row, p)
            if not (owner or parent or coordinator or root):
                raise ToolError(
                    "no: only the direct parent, its top-level agent, the board coordinator or "
                    "the signed-in workspace owner may approve proposals"
                )
            if row["proposal_status"] != "pending" and not (
                owner and row["proposal_status"] == "rejected"
            ):
                raise ToolError("no: card is not a pending proposal")
            status = "todo" if approve else "backlog"
            self._set(
                conn,
                row,
                {
                    "proposal_status": "approved" if approve else "rejected",
                    "approved_by": p.actor
                    if approve
                    and (
                        owner
                        or row["restricted_origin"] == 0
                        or _trusted_agent(p)
                        or coordinator
                        or root
                    )
                    else None,
                    "status": status,
                },
            )
            self._log(
                conn,
                row["id"],
                "decide",
                choice="approve" if approve else "reject",
                from_status=row["status"],
                to_status=status,
                target=row["assignee"],
            )
            self._emit(conn, row["id"], "card.moved", **{"from": row["status"], "to": status})
            return card(conn, task_row(conn, row["id"]), p)

    def task_heartbeat(self, task):
        with self.transaction() as conn:
            row, board = self._get(conn, task)
            self._write(board, row=row)
            if row["status"] != "doing" or row["assignee"] != self.principal.actor:
                raise ToolError("no: only the holder of a doing card can heartbeat it")
            self._set(conn, row, {"heartbeat_at": now()})
            self._log(conn, row["id"], "renew")
            self._emit(conn, row["id"], "card.updated")
            return card(conn, task_row(conn, row["id"]), self.principal)

    def task_comment(self, task, text, version=None):
        with self.transaction() as conn:
            row, board = self._get(conn, task)
            self._write(board, submission=True)
            self._version(conn, row, version)
            value = validate_text(text, "comment", required=True)
            flags = sorted(set(json.loads(row["flags"] or "[]")) | set(flags_for(value)))
            self._set(conn, row, {"flags": json.dumps(flags)})
            self._log(conn, row["id"], "comment", text=value, flags=flags_for(value))
            self._emit(conn, row["id"], "card.commented")
            return card(conn, task_row(conn, row["id"]), self.principal)

    def _backlog_available(self, row):
        return (
            row["proposal_status"] not in ("pending", "rejected")
            and (
                (not self.principal.person and self._own_card(row))
                or (_trusted_agent(self.principal) and row["restricted_origin"] == 0)
            )
            and not _waiting_on_person(row)
            and row["assignee"] in (None, self.principal.actor)
        )

    def task_take_next(self, board=None, task=None):
        top = self._top_level_taker()
        narrow = _restricted(self.principal) and not top
        with self.transaction() as conn:
            if task is not None:
                row, b = self._get(conn, task)
                if board is not None and self._board(conn, board)["id"] != b["id"]:
                    raise ToolError("no: card not found")
            else:
                b = self._board(conn, board)
                self._expire(conn, b)
                row = None
            if task is None:
                candidates = conn.execute(
                    "SELECT id FROM tasks WHERE board_id=? AND status='todo' "
                    "AND (assignee IS NULL OR assignee=?) "
                    "AND (? OR coalesce(restricted_origin,1)=0 OR created_by=? OR assignee=? "
                    "OR coalesce(approved_by,'')!=''"
                    + (" OR (agent_parent IS NULL AND created_by LIKE 'conn:%')" if top else "")
                    + ") "
                    + ("AND (created_by=? OR assignee=?) " if narrow else "")
                    + (
                        ""
                        if self.principal.person
                        else "AND coalesce(ask,'')='' AND (ask_to IS NULL OR ask_to NOT LIKE 'user:%') "
                    )
                    + "ORDER BY assignee IS NULL,position,id",
                    (
                        b["id"],
                        self.principal.actor,
                        self.principal.person and self.principal.role == "owner",
                        self.principal.actor,
                        self.principal.actor,
                        *((self.principal.actor, self.principal.actor) if narrow else ()),
                    ),
                )
                for found in candidates:
                    candidate = task_row(conn, found["id"])
                    if self._claimable_origin(candidate):
                        row = candidate
                        break
                if row is None:
                    return None
            available = row["status"] == "todo" or (
                row["status"] == "backlog" and self._backlog_available(row)
            )
            if (
                not available
                or not self._claimable_origin(row)
                or row["proposal_status"] in ("pending", "rejected")
                or row["assignee"] not in (None, self.principal.actor)
                or (not self.principal.person and _waiting_on_person(row))
            ):
                raise ToolError("no: card is not available to take")
            self._write(b, row=row, claiming=True)
            self._set(
                conn,
                row,
                {
                    "status": "doing",
                    "assignee": self.principal.actor,
                    "claim_until": None if self.principal.person else self._until(b),
                },
            )
            self._log(conn, row["id"], "claim", from_status=row["status"], to_status="doing")
            self._emit(conn, row["id"], "card.claimed", **{"from": row["status"], "to": "doing"})
            result = card(conn, task_row(conn, row["id"]), self.principal)
            decision = conn.execute(
                "SELECT * FROM task_events WHERE task_id=? AND kind='decide' ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            if row["return_to"] == self.principal.actor and decision:
                actor = (
                    f"conn:{decision['stamp_connection']}"
                    if decision["stamp_connection"]
                    else f"user:{decision['stamp_user']}"
                )
                result["decision"] = {
                    "actor": actor,
                    "choice": decision["choice"],
                    "reply": decision["text"],
                }
                # Clear the delivery marker in this same versioned mutation.
                conn.execute("UPDATE tasks SET return_to=NULL WHERE id=?", (row["id"],))
                result["return_to"] = None
            return result

    def _position(self, conn, row, updates, before=None, after=None):
        if before is None and after is None:
            return
        if before is not None and after is not None:
            raise ToolError("no: use before or after")
        neighbor, _board = self._get(conn, before if before is not None else after)
        lane = updates.get("status", row["status"])
        if (
            neighbor["board_id"] != row["board_id"]
            or neighbor["status"] != lane
            or neighbor["id"] == row["id"]
        ):
            raise ToolError("no: position target must be another card in this column")
        comparison, ordering = ("<", "DESC") if before is not None else (">", "ASC")
        other = conn.execute(
            f"SELECT position FROM tasks WHERE board_id=? AND status=? AND id!=? "
            f"AND position {comparison} ? ORDER BY position {ordering} LIMIT 1",
            (row["board_id"], lane, row["id"], neighbor["position"]),
        ).fetchone()
        boundary = other[0] if other else neighbor["position"] + (-2 if before is not None else 2)
        updates["position"] = (boundary + neighbor["position"]) / 2

    def _review_editor(self, row):
        return (
            row["status"] == "review"
            and not self.principal.person
            and self.principal.actor in (row["assignee"], row["return_to"])
        )

    def _transition(self, row, status):
        if status not in STATUSES:
            raise ToolError("no: invalid status")
        old = row["status"]
        if status == old:
            return
        if row["proposal_status"] in ("pending", "rejected"):
            if (
                row["proposal_status"] == "rejected"
                and self.principal.person
                and self.principal.role == "owner"
                and status == "done"
            ):
                return
            raise ToolError(
                "no: proposal needs parent approval with task_approve before work starts"
            )
        if status in ("todo", "doing") and not self._available_origin(row):
            if (
                status == "doing"
                and old == "todo"
                and row["assignee"] == self.principal.actor
                and self._claimable_origin(row)
                and not _waiting_on_person(row)
            ):
                raise ToolError("no: claim this card with task_take_next")
            raise ToolError("no: restricted-origin work needs parent or workspace owner release")
        if (
            old == "review"
            and not self.principal.person
            and (
                not (row["ask_to"] or "").startswith("conn:")
                or any((row[key] or "").startswith("user:") for key in ("assignee", "return_to"))
            )
        ):
            raise ToolError("no: waiting on a person, they must answer via decide")
        if old == "review" and not (not self.principal.person and status == "done"):
            if self._review_editor(row) and status in ("todo", "doing"):
                return
            raise ToolError("no: use decide to move a card from review")
        if self.principal.person:
            allowed = {
                ("backlog", "todo"),
                ("todo", "doing"),
                ("doing", "todo"),
                ("doing", "review"),
                ("doing", "done"),
                ("todo", "review"),
                ("done", "todo"),
            }
            if status == "backlog" or (old, status) in allowed:
                return
        else:
            if old != "review" and _waiting_on_person(row):
                raise ToolError("no: this move requires a person or a handoff")
            if old == "backlog" and status in ("todo", "doing") and self._backlog_available(row):
                return
            if (old, status) in {
                ("todo", "doing"),
                ("todo", "review"),
                ("doing", "review"),
                ("todo", "done"),
                ("doing", "done"),
                ("review", "done"),
                ("done", "todo"),
                ("doing", "backlog"),
            }:
                return
            if old == "todo" and status == "backlog" and row["assignee"] is None:
                return
        raise ToolError("no: this move requires a person or a handoff")

    def _memory_theme(self, memory_id):
        """The memory's theme and first line, read through the caller's own
        policy: a memory the caller can't read can't be linked."""
        from commonplace.policy import canonical_wing

        result = self.ctx.memory("mempalace_get_drawer", {"drawer_id": memory_id})
        metadata = (result.get("metadata") or {}) if isinstance(result, dict) else {}
        wing = (result.get("wing") if isinstance(result, dict) else None) or metadata.get("wing")
        if not wing:
            raise ToolError("no: memory not found")
        text = result.get("content") or ""
        return canonical_wing(wing), next(iter(str(text).splitlines()), "")

    def _link(self, conn, row, memory_id, *, unlink=False):
        memory_id = validate_text(memory_id, "memory id", required=True, limit=1000)
        if unlink:
            conn.execute(
                "DELETE FROM task_links WHERE task_id=? AND drawer_id=?", (row["id"], memory_id)
            )
        else:
            theme, title = self._memory_theme(memory_id)
            conn.execute(
                "INSERT OR IGNORE INTO task_links(task_id,drawer_id,stamp_user,stamp_connection,"
                "created_at,title,theme) VALUES (?,?,?,?,?,?,?)",
                (
                    row["id"],
                    memory_id,
                    self.principal.user_id,
                    None if self.principal.person else self.principal.connection,
                    now(),
                    title,
                    theme,
                ),
            )
        self._log(conn, row["id"], "unlink" if unlink else "link", target=memory_id)

    def _pr_updates(self, row, updates, value):
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            updates["pr_number"] = value
            repo = updates.get("repo", row["repo"])
            if repo:
                updates["pr_url"] = f"https://{normalize_repo(repo)}/pull/{value}"
        elif isinstance(value, str) and value.isdigit() and int(value) > 0:
            updates["pr_number"] = int(value)
            repo = updates.get("repo", row["repo"])
            if repo:
                updates["pr_url"] = f"https://{normalize_repo(repo)}/pull/{int(value)}"
        elif isinstance(value, str) and re.fullmatch(
            r"https?://[^\s]+/(?:pull|pulls|merge_requests)/[1-9][0-9]*/?", value
        ):
            updates["pr_url"] = value
            updates["pr_number"] = int(value.rstrip("/").rsplit("/", 1)[1])
        else:
            raise ToolError("no: pr must be a URL or a positive number")

    def _record_review_withdrawal(self, conn, row, status, reason):
        if row["status"] != "review" or status not in ("todo", "doing"):
            return
        self._log(
            conn,
            row["id"],
            "release",
            text=row["ask"],
            reason=reason,
            target=row["ask_to"],
            options=json.loads(row["ask_options"] or "[]"),
            from_status="review",
            to_status=status,
            flags=flags_for(row["ask"], reason),
        )
        self._emit(
            conn,
            row["id"],
            "card.ask_withdrawn",
            target=row["ask_to"],
            return_to=row["return_to"],
            **{"from": "review", "to": status},
        )

    def _review_edit_text(self, conn, row, args, updates):
        if row["status"] != "review":
            return None
        previous = {
            key: row[key]
            for key in ("title", "body", "checks")
            if key in updates and updates[key] != row[key]
        }
        if "labels" in updates and updates["labels"] != row["labels"]:
            previous["labels"] = json.loads(row["labels"] or "[]")
        if set(args) & {"link", "unlink"}:
            previous["links"] = [
                link["drawer_id"] for link in self.store.links(conn, row["id"], self.principal)
            ]
        return json.dumps(previous, ensure_ascii=False) if previous else None

    def _open_review(self, row, args, updates, done_review):
        question, offered = review_request(
            args.get(
                "question",
                f"Is the completed work on '{row['title']}' ready to accept?"
                if done_review
                else "",
            ),
            args.get(
                "options",
                ["Accept the completed work", "Return for more work"] if done_review else None,
            ),
            args.get(
                "context",
                review_context(row, updates),
            ),
        )
        updates["review_context"] = args.get("context", review_context(row, updates))
        updates.update(
            status="review",
            ask=question,
            ask_options=json.dumps(offered),
            ask_to=self._owner_actor(),
            return_to=self.principal.actor,
            done_at=None,
            review_kind="completion" if done_review else "decision",
        )

    def task_update(self, task, *, version=None, **args):
        permitted = {
            "title",
            "body",
            "labels",
            "status",
            "before",
            "after",
            "link",
            "unlink",
            "lesson",
            "repo",
            "branch",
            "pr",
            "checks",
            "status_line",
            "callstack",
            "reason",
            "question",
            "options",
            "context",
        }
        with self.transaction() as conn:
            row, board = self._get(conn, task)
            self._write(board, row=row)
            self._version(conn, row, version)
            if set(args) - permitted or (
                "reason" in args
                and not (row["status"] == "review" and args.get("status") in ("todo", "doing"))
            ):
                raise ToolError("no: unknown update field")
            self._holder(
                row,
                completing=args.get("status") == "done",
                editing=bool(set(args) & {"title", "body", "labels"}),
                reopening=set(args) == {"status"} and args["status"] == "todo",
                annotating=bool(set(args) - {"status"})
                and set(args) - {"status"} <= {"lesson", "pr", "link"}
                and args.get("status", "done") == "done",
            )
            # The agent that asked for review may still refresh the PR's call tree.
            refresh = set(args) == {"callstack"} and row["return_to"] == self.principal.actor
            feedback = (
                self._review_editor(row)
                and set(args)
                <= {"title", "body", "checks", "labels", "link", "unlink", "status", "reason"}
                and args.get("status", "review") in ("review", "todo", "doing")
            )
            if (
                row["status"] == "review"
                and not self.principal.person
                and not refresh
                and not feedback
                and not (
                    row["return_to"] == self.principal.actor
                    and set(args) <= {"status", "lesson"}
                    and args.get("status") in ("review", "done")
                )
            ):
                raise ToolError("no: card is waiting on a person")
            if "lesson" in args and args.get("status", row["status"]) != "done":
                raise ToolError("no: a lesson requires status done")
            updates = {}
            if "labels" in args:
                labels = args["labels"]
                if not isinstance(labels, list) or len(labels) > 100:
                    raise ToolError("no: labels must be a list")
                updates["labels"] = json.dumps(
                    [validate_text(label, "label", required=True, limit=200) for label in labels]
                )
            for key in ("title", "body", "branch", "status_line"):
                if key in args:
                    updates[key] = validate_text(
                        args[key],
                        key,
                        required=key == "title",
                        limit=5000 if key == "title" else 100_000,
                    )
            if "repo" in args:
                updates["repo"] = normalize_repo(args["repo"])
            if "checks" in args:
                if args["checks"] not in ("", "pending", "passing", "failing"):
                    raise ToolError("no: checks must be pending, passing or failing")
                updates["checks"] = args["checks"]
            if "pr" in args:
                self._pr_updates(row, updates, args["pr"])
            if "status_line" in args:
                updates["heartbeat_at"] = now()
            if "callstack" in args:
                updates["callstack"] = callstack_json(args["callstack"])
            status = args.get("status", row["status"])
            self._transition(row, status)
            reason = validate_text(args.get("reason", ""), "reason") or None
            if status != row["status"]:
                updates.update(
                    status=status, claim_until=None, done_at=now() if status == "done" else None
                )
                if row["status"] == "review":
                    updates.update(
                        ask=None, ask_options=None, ask_to=None, return_to=None, review_context=None
                    )
                if status == "doing":
                    updates["assignee"] = self.principal.actor
                    updates["claim_until"] = None if self.principal.person else self._until(board)
                else:
                    updates["assignee"] = (
                        (self.principal.actor if row["status"] == "review" else row["assignee"])
                        if row["status"] in ("backlog", "review")
                        and status == "todo"
                        and not self.principal.person
                        else None
                    )
                if status == "review" or (
                    status == "done" and board["done_needs_review"] and not self.principal.person
                ):
                    done_review = status == "done"
                    self._open_review(row, args, updates, done_review)
            self._position(conn, row, updates, args.get("before"), args.get("after"))
            review_edit_text = self._review_edit_text(conn, row, args, updates)
            if "lesson" in args:
                lesson = validate_text(args["lesson"], "lesson", required=True)
                saved = self.ctx.memory(
                    "mempalace_add_drawer",
                    {
                        "content": lesson,
                        "wing": self.principal.policy.write_wing or board["wing"],
                        "room": "general",
                    },
                )
                memory_id = saved.get("drawer_id") if isinstance(saved, dict) else None
                if not memory_id:
                    raise ToolError("no: could not save lesson")
                self._link(conn, row, memory_id)
            for key in ("link", "unlink"):
                if key in args:
                    self._link(conn, row, args[key], unlink=key == "unlink")
            new_flags = flags_for(
                *(args.get(key) for key in ("title", "body", "lesson", "status_line", "reason")),
                updates.get("callstack"),
                updates.get("ask"),
                updates.get("review_context"),
            )
            if new_flags:
                updates["flags"] = json.dumps(
                    sorted(set(json.loads(row["flags"] or "[]")) | set(new_flags))
                )
            if not updates and not any(key in args for key in ("link", "unlink", "lesson")):
                return card(conn, row, self.principal)
            self._set(conn, row, updates)
            moved = updates.get("status", row["status"]) != row["status"]
            ask = (
                {
                    "text": updates["ask"],
                    "target": updates["ask_to"],
                    "options": json.loads(updates["ask_options"]),
                    "context": updates["review_context"],
                }
                if updates.get("ask") is not None
                else {"text": review_edit_text}
            )
            self._record_review_withdrawal(conn, row, status, reason)
            self._log(
                conn,
                row["id"],
                "move" if moved or updates.get("ask") is not None else "edit",
                from_status=row["status"],
                to_status=updates.get("status", row["status"]),
                **ask,
            )
            self._emit(
                conn,
                row["id"],
                "card.moved" if moved else "card.updated",
                **{"from": row["status"], "to": updates.get("status", row["status"])},
            )
            if updates.get("status") == "review":
                self._emit(conn, row["id"], "card.review_needed", return_to=self.principal.actor)
            return card(conn, task_row(conn, row["id"]), self.principal)

    def _handoff(self, conn, row, board, to, note, options=None, context=None):
        self._write(board, row=row)
        if row["status"] not in ("todo", "doing"):
            raise ToolError("no: only todo or doing cards can be handed off")
        self._holder(row)
        if row["proposal_status"] in ("pending", "rejected"):
            raise ToolError(
                "no: proposal needs parent approval with task_approve before work starts"
            )
        if not self.principal.person and _waiting_on_person(row):
            raise ToolError("no: waiting on a person, they must answer via decide")
        target = self._work_actor(to, board)
        person = target is not None and target.startswith("user:")
        if row["status"] == "todo" and not person and not self.principal.person:
            raise ToolError("no: take this card before handing it off")
        note = validate_text(note, "note", required=True)
        options = choices(options)
        if person:
            note, options = review_request(
                note, options, context if context is not None else review_context(row)
            )
        if options and not person:
            raise ToolError("no: options require a person")
        status = "review" if person else "todo"
        updates = {
            "status": status,
            "claim_until": None,
            "assignee": None if person else target,
            "ask": note if person else None,
            "ask_options": json.dumps(options),
            "ask_to": target if person else None,
            "return_to": self.principal.actor if person else None,
            "flags": json.dumps(
                sorted(set(json.loads(row["flags"] or "[]")) | set(flags_for(note, context)))
            ),
        }
        updates["review_context"] = (
            (context if context is not None else review_context(row)) if person else None
        )
        self._set(conn, row, updates)
        self._log(
            conn,
            row["id"],
            "handoff",
            text=note,
            target=target,
            from_status=row["status"],
            to_status=status,
            flags=flags_for(note, context),
            options=options if person else None,
            context=updates["review_context"],
        )
        self._emit(
            conn,
            row["id"],
            "card.handoff",
            **{
                "from": row["status"],
                "to": status,
                "return_to": updates["return_to"],
                "target": target,
            },
        )
        if person:
            self._emit(conn, row["id"], "card.review_needed", return_to=updates["return_to"])
        return card(conn, task_row(conn, row["id"]), self.principal)

    def task_handoff(self, task, to, note, options=None, version=None, context=None):
        with self.transaction() as conn:
            row, board = self._get(conn, task)
            self._version(conn, row, version)
            return self._handoff(conn, row, board, to, note, options, context)

    def ask(self, question, options=None, board=None, task=None, context=""):
        with self.transaction() as conn:
            if task is not None:
                row, b = self._get(conn, task)
                if board is not None and self._board(conn, board)["id"] != b["id"]:
                    raise ToolError("no: card not found")
                return self._handoff(
                    conn, row, b, self._owner_actor(), question, options, context or None
                )
            b = self._board(conn, board, create=True)
            self._write(b, submission=True)
            self._expire(conn, b)
            question = validate_text(question, "question", required=True, limit=5000)
            return self._new_card(
                conn,
                b,
                title=question,
                body=context,
                status="review",
                ask=question,
                options=choices(options),
                target=self._owner_actor(),
            )

    def decide(self, task, choice=None, reply=None, then="return", version=None):
        with self.transaction() as conn:
            row, board = self._get(conn, task)
            _owner(self.principal)
            if not self.principal.person:
                raise ToolError("no: only a signed-in workspace owner can answer this")
            self._write(board)
            self._version(conn, row, version)
            if row["status"] != "review":
                raise ToolError("no: card is not in review")
            if then not in ("return", "todo", "done", "backlog"):
                raise ToolError("no: invalid decision destination")
            if choice is not None:
                choice = validate_text(choice, "choice", required=True, limit=120)
                offered = json.loads(row["ask_options"] or "[]")
                if offered and choice not in offered:
                    raise ToolError("no: choose one of the offered options")
            if reply is not None:
                reply = validate_text(reply, "reply", required=True)
            if not choice and not reply:
                raise ToolError("no: choose an option or write a reply")
            status = "todo" if then == "return" else then
            assignee = row["return_to"] if then == "return" else None
            updates = {
                "status": status,
                "assignee": assignee,
                "claim_until": None,
                "ask": None,
                "ask_options": None,
                "ask_to": None,
                "review_context": None,
                "return_to": row["return_to"] if then == "return" else None,
                "done_at": now() if status == "done" else None,
                "flags": json.dumps(
                    sorted(set(json.loads(row["flags"] or "[]")) | set(flags_for(reply)))
                ),
            }
            self._set(conn, row, updates)
            self._log(
                conn,
                row["id"],
                "decide",
                text=reply,
                choice=choice,
                target=assignee,
                from_status="review",
                to_status=status,
                flags=flags_for(reply),
            )
            self._emit(
                conn,
                row["id"],
                "card.decided",
                **{
                    "from": "review",
                    "to": status,
                    "return_to": row["return_to"],
                    "ask_to": row["ask_to"],
                },
            )
            return card(conn, task_row(conn, row["id"]), self.principal)

    def needs_me(self, format="list"):
        if format not in ("list", "digest"):
            raise ToolError("no: format must be list or digest")
        groups = needs_me(self.store, self.principal)
        return digest(groups) if format == "digest" else groups

    def seed_card(
        self,
        *,
        board,
        title,
        body="",
        status="todo",
        labels=None,
        repo=None,
        branch=None,
        pr_url=None,
    ):
        """Owner's explicit import API; repeat (board, title) imports are no-ops."""
        _owner(self.principal)
        if not self.principal.person:
            raise ToolError("no: only a signed-in person can import cards")
        with self.transaction() as conn:
            b = self._board(conn, board, create=True)
            self._write(b)
            existing = conn.execute(
                "SELECT id FROM tasks WHERE board_id=? AND title=? ORDER BY id LIMIT 1",
                (b["id"], title),
            ).fetchone()
            if existing:
                return card(conn, task_row(conn, existing["id"]), self.principal)
            if repo is not None:
                normalized = normalize_repo(repo)
                mapped = conn.execute(
                    "SELECT board_id FROM board_repos WHERE repo=?", (normalized,)
                ).fetchone()
                if mapped is not None and mapped[0] != b["id"]:
                    raise ToolError("no: repo is already assigned")
                conn.execute(
                    "INSERT OR IGNORE INTO board_repos(repo,board_id) VALUES (?,?)",
                    (normalized, b["id"]),
                )
            if status not in STATUSES:
                raise ToolError("no: invalid status")
            created = self._new_card(
                conn,
                b,
                title=title,
                body=body,
                labels=labels,
                status="backlog" if status == "backlog" else "todo",
            )
            updates = {"status": status}
            if status == "doing":
                updates["assignee"] = self.principal.actor
            if status == "review":
                updates.update(ask=body or title, ask_options="[]", ask_to=self._owner_actor())
            if status == "done":
                updates["done_at"] = now()
            if repo is not None:
                updates["repo"] = normalize_repo(repo)
            if branch is not None:
                updates["branch"] = validate_text(branch, "branch")
            if pr_url is not None:
                pr_url = validate_text(pr_url, "pr_url", required=True, limit=2000)
                if not re.fullmatch(
                    r"https?://[^\s]+/(?:pull|pulls|merge_requests)/[1-9][0-9]*/?", pr_url
                ):
                    raise ToolError("no: invalid PR URL")
                updates.update(pr_url=pr_url, pr_number=int(pr_url.rstrip("/").rsplit("/", 1)[1]))
            row = task_row(conn, created["number"])
            self._set(conn, row, updates)
            if status != row["status"]:
                ask = (
                    {"text": updates["ask"], "target": updates["ask_to"], "options": []}
                    if status == "review"
                    else {}
                )
                self._log(
                    conn, row["id"], "move", from_status=row["status"], to_status=status, **ask
                )
                self._emit(conn, row["id"], "card.moved", **{"from": row["status"], "to": status})
            if status == "review":
                self._emit(conn, row["id"], "card.review_needed")
            return card(conn, task_row(conn, row["id"]), self.principal)
