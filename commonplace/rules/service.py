"""Verified identity, scope visibility and the rule lifecycle for MCP and REST."""

import hashlib
import logging
import re
from contextlib import closing, contextmanager
from pathlib import Path

from commonplace import events
from commonplace.boards.service import normalize_repo, validate_text
from commonplace.extensions import ToolError
from commonplace.http_util import log_failure
from mempalace.sqlite_crypto import connect

from . import checks, render
from .store import RuleStore, bump, now, rules_sequence, sequence

KINDS = ("permission", "limit", "instruction", "style")
SCOPES = ("global", "board", "repo")
RULE_FIELDS = (
    "id agent key text summary agent_text agent_text_by kind scope scope_ref status source pending version seq "
    "retired_by updated_at updated_by"
).split()
WAITING = "(status='proposed' OR pending=1) AND (status!='retired' OR retired_by='owner')"
logger = logging.getLogger(__name__)
_UNSET = object()
INSTRUCTIONS_TIMEOUT = 0.08


def open_store(workspace_dir):
    return RuleStore(workspace_dir)


class VersionClash(ToolError):
    def __init__(self, current):
        super().__init__("no: rule changed")
        self.current = current


def rule_json(row, *, include_display=True):
    value = {key: row[key] for key in RULE_FIELDS}
    if include_display:
        value["display_summary"] = display_summary(row["summary"], row["text"])
    return value


def sentences(value):
    """Split punctuation followed by whitespace; keep common abbreviations together."""
    parts = re.split(
        r"(?<=[.!?])\s+|(?<=[.!?][\"'”’])\s+|(?<=[。！？])\s*", " ".join(value.split())
    )
    result = []
    for part in parts:
        if not part:
            continue
        if result and re.search(r"\b(?:e\.g|i\.e|Mr|Mrs|Ms|Dr|Prof|vs)\.$", result[-1]):
            result[-1] += " " + part
        else:
            result.append(part)
    return result


def summary_text(value):
    if value is None:
        return None
    value = validate_text(value, "summary", limit=400).strip()
    if len(sentences(value)) > 2:
        raise ToolError("no: summary must be at most two sentences")
    return value or None


def display_summary(summary, text):
    if summary:
        return summary
    first = sentences(text)[0]
    return first if len(first) <= 240 else first[:239].rstrip() + "…"


def rule_text(value):
    value = validate_text(value, "text", required=True, limit=4000)
    if render.length(render.quote(value)) > render.RULE_BYTES:
        raise ToolError("no: rule text too long once escaped")
    return value


def agent_ref(agent):
    if not isinstance(agent, str) or (
        agent != "*" and (not agent.startswith("conn:") or not agent[5:].strip())
    ):
        raise ToolError("no: agent must be * or conn:<name>")
    return agent


def agent_name(agent):
    return "All agents" if agent == "*" else agent.removeprefix("conn:")


def rule_key(text):
    return "r-" + hashlib.sha1(" ".join(text.split()).encode()).hexdigest()[:10]


def fields(text, kind, scope="global", scope_ref=None, summary=_UNSET):
    text = rule_text(text)
    if kind not in KINDS or scope not in SCOPES:
        raise ToolError("no: invalid kind or scope")
    if scope == "global":
        scope_ref = None
    else:
        scope_ref = validate_text(scope_ref, "scope_ref", required=True, limit=1000)
        if scope == "repo":
            scope_ref = normalize_repo(scope_ref)
            if (
                len(scope_ref.split("/")) != 3
                or not all(scope_ref.split("/"))
                or "@" in scope_ref.split("/")[0]
            ):
                raise ToolError("no: repo must be host/owner/name")
    values = dict(text=text, kind=kind, scope=scope, scope_ref=scope_ref)
    if summary is not _UNSET:
        values["summary"] = summary_text(summary)
    return values


def row_for(conn, rule_id):
    if type(rule_id) not in (str, int) or not str(rule_id).isdigit():
        raise ToolError("no: rule not found")
    row = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        raise ToolError("no: rule not found")
    return row


class RuleService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.principal = ctx.principal
        self._store = None
        self._affected = set()
        self._board_visibility = {}
        self._moved = []

    @property
    def store(self):
        directory = self.ctx.workspace_dir()
        checks.guard(self.ctx.db, self.principal.workspace_id, directory)
        if self._store is None or Path(self._store.path).parent != Path(directory):
            self._store = open_store(directory)
            checks.guard(self.ctx.db, self.principal.workspace_id, directory)
        return self._store

    def _emit(self, kind, data):
        try:
            events.emit(
                self.ctx.db,
                self.principal.workspace_id,
                kind,
                actor=self.principal.actor,
                data=data,
            )
        except Exception as exc:
            log_failure(logger, "committed rule event emission failed", exc)

    @contextmanager
    def transaction(self):
        # Rules moved to another agent also refresh the agent they left.
        self._moved = []
        self._affected = set()
        with self.store.transaction() as conn:
            before = sequence(conn)
            yield conn
            for agent in self._affected:
                bump(conn, agent)
            changed = conn.execute(
                "SELECT agent,id,key FROM rules WHERE seq>? ORDER BY seq", (before,)
            ).fetchall()
        changed = [*changed, *self._moved]
        for agent in dict.fromkeys(row["agent"] for row in changed):
            rows = [row for row in changed if row["agent"] == agent]
            self._emit(
                "rule.changed",
                {"agent": agent, "ids": [r["id"] for r in rows], "keys": [r["key"] for r in rows]},
            )

    def _person(self, *, owner=False):
        if not self.principal.person or (owner and self.principal.role != "owner"):
            raise ToolError(
                "no: only a workspace owner can do that" if owner else "no: people only"
            )

    def _board_visible(self, slug):
        if slug not in self._board_visibility:
            self._board_visibility[slug] = self._lookup_board_visible(slug)
        return self._board_visibility[slug]

    def _lookup_board_visible(self, slug):
        # Opening BoardStore would migrate it; rules only inspect the existing store.
        path = Path(self.ctx.workspace_dir()) / "boards.sqlite3"
        if not path.is_file():
            return False
        with closing(connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT wing FROM boards WHERE slug=?", (slug,)).fetchone()
        return row is not None and self.principal.policy.can_read_wing(row[0])

    def _visible(self, row):
        return self.principal.person or (
            row["agent"] in (self.principal.actor, "*")
            and (row["scope"] != "board" or self._board_visible(row["scope_ref"]))
        )

    def _agent_view(self, rule):
        """An agent's wording on a moved rule stays between that agent and the owner."""
        actor = self.principal.actor
        if self.principal.person:
            return rule
        masked = dict(rule)
        if rule["agent_text_by"] != actor:
            masked.update(agent_text=None, agent_text_by=None, pending=0)
        if str(rule["updated_by"]).startswith("conn:") and rule["updated_by"] != actor:
            masked["updated_by"] = None
        return masked

    def _snapshot(self, conn, rule_id, *, note=None, source=None):
        row = row_for(conn, rule_id)
        p = self.principal
        conn.execute(
            "INSERT INTO rule_versions(rule_id,version,agent,text,summary,kind,scope,scope_ref,status,"
            "source,note,stamp_user,stamp_connection,stamp_trust,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["version"],
                row["agent"],
                row["text"],
                row["summary"],
                row["kind"],
                row["scope"],
                row["scope_ref"],
                row["status"],
                source or row["source"],
                note,
                p.user_id,
                None if p.person else p.actor.removeprefix("conn:"),
                p.policy.trust,
                now(),
            ),
        )
        return rule_json(row)

    def _change(self, conn, row, updates, *, note=None, source=None):
        if "text" in updates:
            rule_text(updates["text"])
        conn.execute("UPDATE meta SET value=value+1 WHERE key='seq'")
        updates = dict(
            updates,
            version=row["version"] + 1,
            seq=sequence(conn),
            updated_at=now(),
            updated_by=self.principal.actor,
        )
        conn.execute(
            "UPDATE rules SET " + ",".join(f"{key}=?" for key in updates) + " WHERE id=?",
            (*updates.values(), row["id"]),
        )
        after = row_for(conn, row["id"])
        effective_fields = ("text", "kind", "scope", "scope_ref", "agent")
        before_set = (
            tuple(row[k] for k in effective_fields)
            if row["status"] in ("accepted", "edited")
            else None
        )
        after_set = (
            tuple(after[k] for k in effective_fields)
            if after["status"] in ("accepted", "edited")
            else None
        )
        if before_set != after_set:
            if before_set is not None:
                self._affected.add(row["agent"])
            if after_set is not None:
                self._affected.add(after["agent"])
        return self._snapshot(conn, row["id"], note=note, source=source)

    def _create(self, conn, agent, key, values, *, owner=False):
        conn.execute("UPDATE meta SET value=value+1 WHERE key='seq'")
        stamp = now()
        columns = dict(
            agent=agent,
            origin_agent=(None if agent == "*" else agent) if owner else self.principal.actor,
            key=key,
            **values,
            agent_text=None if owner else values["text"],
            agent_text_by=None if owner else self.principal.actor,
            status="accepted" if owner else "proposed",
            source="owner" if owner else "agent",
            seq=sequence(conn),
            created_at=stamp,
            updated_at=stamp,
            updated_by=self.principal.actor,
        )
        cursor = conn.execute(
            "INSERT INTO rules("
            + ",".join(columns)
            + ") VALUES ("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(columns.values()),
        )
        if owner:
            self._affected.add(agent)
        return self._snapshot(conn, cursor.lastrowid)

    def rules_publish(self, rules, replace=False, agent="self"):
        if self.principal.person or not self.principal.policy.has_scope("memory:write"):
            raise ToolError("no: an agent with memory:write is required")
        if agent not in ("self", "*"):
            raise ToolError("no: agent must be self or *")
        if not isinstance(rules, list) or len(rules) > 200 or type(replace) is not bool:
            raise ToolError("no: publish requires up to 200 rules and a boolean replace")
        actor = self.principal.actor
        target = actor if agent == "self" else "*"
        prepared = {}
        for item in rules:
            if not isinstance(item, dict) or set(item) - {
                "key",
                "text",
                "summary",
                "kind",
                "scope",
                "board",
                "repo",
            }:
                raise ToolError("no: invalid rule")
            scope = item.get("scope", "global")
            if scope not in SCOPES:
                raise ToolError("no: invalid scope")
            values = fields(
                item.get("text"),
                item.get("kind"),
                scope,
                item.get(scope),
                summary=item.get("summary", _UNSET),
            )
            key = item.get("key", rule_key(values["text"]))
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", key):
                raise ToolError("no: key must match [A-Za-z0-9._-]{1,100}")
            if key in prepared:
                raise ToolError("no: duplicate rule key")
            if scope == "board" and not self._board_visible(values["scope_ref"]):
                raise ToolError("no: board not found")
            prepared[key] = values
        result = []
        with self.transaction() as conn:
            for key, values in prepared.items():
                # Keep following owner moves, without confusing self and shared keys.
                # Older snapshots have no target; their origin was always the caller.
                row = conn.execute(
                    "SELECT r.*,coalesce((SELECT CASE WHEN v.agent='*' AND v.source='owner' "
                    "THEN r.origin_agent ELSE v.agent END FROM rule_versions v WHERE v.rule_id=r.id "
                    "ORDER BY v.id LIMIT 1),origin_agent,agent) AS publish_target FROM rules r "
                    "WHERE key=? AND (agent=? OR (origin_agent=? AND publish_target=?)) "
                    "ORDER BY agent=? DESC, agent='*' DESC, id LIMIT 1",
                    (key, target, actor, target, target),
                ).fetchone()
                if row is None:
                    result.append(self._create(conn, target, key, values))
                    continue
                if not self._visible(row):
                    # The owner moved it out of this agent's sight: skip it, reveal nothing.
                    continue
                if target == "*" and row["origin_agent"] != actor:
                    raise ToolError("no: shared rule key belongs to the owner or another proposer")
                # A rule moved here from another agent is handled like an owner edit.
                owned = (
                    row["agent"] == target
                    and row["publish_target"] == target
                    and row["origin_agent"] in (None, actor)
                    and (
                        target != "*" or row["status"] == "proposed" or row["retired_by"] == "agent"
                    )
                )
                if owned and row["retired_by"] == "agent":
                    updates = dict(
                        values,
                        agent_text=values["text"],
                        agent_text_by=actor,
                        source="agent",
                        status="proposed",
                        pending=0,
                        retired_by=None,
                    )
                elif not owned or row["source"] == "owner" or row["retired_by"] == "owner":
                    updates = (
                        {
                            "agent_text": values["text"],
                            "agent_text_by": actor,
                            "pending": int(values["text"] != row["text"]),
                        }
                        if values["text"] != row["agent_text"] or row["agent_text_by"] != actor
                        else {}
                    )
                else:
                    updates = dict(
                        values,
                        agent_text=values["text"],
                        agent_text_by=actor,
                        status="proposed",
                        pending=0,
                    )
                    if all(row[k] == v for k, v in values.items()):
                        updates = {}
                    elif all(row[k] == v for k, v in values.items() if k != "summary"):
                        # Display-only changes never reopen an accepted rule.
                        updates = {"summary": values["summary"]}
                result.append(
                    self._agent_view(
                        self._change(conn, row, updates) if updates else rule_json(row)
                    )
                )
            if replace:
                rows = conn.execute(
                    "SELECT * FROM rules WHERE agent=? AND source='agent' AND status!='retired' "
                    "AND retired_by IS NULL AND coalesce(origin_agent,agent)=? "
                    "AND coalesce((SELECT CASE WHEN v.agent='*' AND v.source='owner' "
                    "THEN rules.origin_agent ELSE v.agent END FROM rule_versions v WHERE v.rule_id=rules.id "
                    "ORDER BY v.id LIMIT 1),origin_agent,agent)=? "
                    "AND (agent!='*' OR status='proposed')",
                    (target, actor, target),
                ).fetchall()
                for row in rows:
                    if row["key"] not in prepared and self._visible(row):
                        result.append(
                            self._agent_view(
                                self._change(
                                    conn,
                                    row,
                                    {"status": "retired", "retired_by": "agent"},
                                    note="dropped by agent",
                                )
                            )
                        )
            total, active = conn.execute(
                "SELECT count(*),coalesce(sum(status!='retired'),0) FROM rules WHERE agent=? "
                "AND (agent!='*' OR origin_agent=?)",
                (target, actor),
            ).fetchone()
            if active > 500 or total > 2000:
                raise ToolError("no: agent rule limit exceeded (500 non-retired, 2000 total)")
            return {"rules": result, "seq": sequence(conn)}

    def rules_get(
        self, board=_UNSET, repo=_UNSET, format="text", include_retired=_UNSET, effective=False
    ):
        if type(effective) is not bool:
            raise ToolError("no: effective must be a boolean")
        if effective:
            if self.principal.person:
                raise ToolError("no: effective=true is for agent connections")
            if any(value is not _UNSET for value in (board, repo, include_retired)):
                raise ToolError(
                    "no: effective=true always returns your full set and takes only format"
                )
            if format not in ("text", "md"):
                raise ToolError("no: invalid format or include_retired")
            try:
                if not (Path(self.ctx.workspace_dir()) / "rules.sqlite3").exists():
                    _ = self.store  # Only a first tool read creates the store.
                result = effective_rules(self.ctx, bind=True, emit=True)
                mode = checks.mode(self.ctx.gateway, self.principal)
                if mode == "unknown":
                    raise RuntimeError("check settings unavailable")
                reply, state = render.effective(
                    result["rules"], result["seq"], result["rules_seq"], format=format, mode=mode
                )
                changed = checks.record_served(
                    self.ctx, result["rules_seq"], state, prior=result["served"]
                )
                if changed and state == "too_large":
                    self._emit("rule.too_large", {"agent": self.principal.actor})
                return dict(result, reply=reply, state=state)
            except checks.StoreLost:
                return {"reply": checks.STORE_LOST}
            except Exception as exc:
                log_failure(logger, "effective rules unavailable", exc)
                return {
                    "reply": f"rules_seq unavailable: {type(exc).__name__}; keep your last copy"
                }
        board = None if board is _UNSET else board
        repo = None if repo is _UNSET else repo
        include_retired = False if include_retired is _UNSET else include_retired
        if format not in ("text", "md") or type(include_retired) is not bool:
            raise ToolError("no: invalid format or include_retired")
        if board is not None:
            validate_text(board, "board", required=True, limit=1000)
        if repo is not None:
            repo = fields("repo", "instruction", "repo", repo)["scope_ref"]
        with self.store.read() as conn:
            rows = conn.execute("SELECT * FROM rules ORDER BY id").fetchall()
            return {
                "rules": [
                    self._agent_view(rule_json(row))
                    for row in rows
                    if self._visible(row)
                    and (
                        row["scope"] == "global"
                        or (row["scope"] == "board" and row["scope_ref"] == board)
                        or (row["scope"] == "repo" and row["scope_ref"] == repo)
                    )
                    and (row["status"] != "retired" or (include_retired and format != "md"))
                ],
                "seq": sequence(conn),
            }

    def rules_changed(self, since):
        if type(since) is not int or since < 0:
            raise ToolError("no: since must be a nonnegative integer")
        with self.store.read() as conn:
            seq = sequence(conn)
            if since > seq:
                return {"rules": [], "seq": seq, "reset": True}
            rows = conn.execute(
                "SELECT agent,key,status,scope,scope_ref FROM rules WHERE seq>? ORDER BY seq",
                (since,),
            )
            return {
                "rules": [
                    {
                        "key": row["key"],
                        "status": row["status"],
                        "target": "all" if row["agent"] == "*" else "own",
                    }
                    for row in rows
                    if self._visible(row)
                ],
                "seq": seq,
            }

    def list_rules(self):
        self._person()
        with self.store.read() as conn:
            return {
                "rules": [rule_json(r) for r in conn.execute("SELECT * FROM rules ORDER BY id")],
                "suggestions": [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM rule_suggestions WHERE status='open' ORDER BY id"
                    )
                ],
                "agents": [
                    r["name"]
                    for r in self.ctx.db.connections(self.principal.workspace_id)
                    if not r["revoked_at"]
                ]
                if self.principal.role == "owner"
                else [],
                "seq": sequence(conn),
                "checks": self.ctx.db.rule_checks(self.principal.workspace_id),
                "can_edit": self.principal.role == "owner",
                "waiting": conn.execute(f"SELECT count(*) FROM rules WHERE {WAITING}").fetchone()[
                    0
                ],
                "waiting_by_agent": {
                    r["agent"]: r["waiting"]
                    for r in conn.execute(
                        f"SELECT agent,count(*) AS waiting FROM rules WHERE {WAITING} GROUP BY agent"
                    )
                },
            }

    def set_check(self, agent, mode):
        self._person(owner=True)
        agent = agent_ref(agent)
        if mode not in ("default", "off", "warn", "enforce") or (
            agent == "*" and mode == "default"
        ):
            raise ToolError("no: invalid check mode")
        target_id = self.principal.workspace_id
        if agent != "*":
            from commonplace.control_db import ControlError
            from commonplace.policy import Policy

            try:
                row = self.ctx.db.connection(agent[5:], self.principal.workspace_id)
            except ControlError:
                raise ToolError("no: connection not found") from None
            agent = "conn:" + row["name"]
            target_id = row["id"]
            if mode == "enforce" and not Policy.from_json(row["policy"]).tool_visible("rules_get"):
                raise ToolError("no: this connection cannot read rules")
        self.ctx.db.set_rule_check(
            self.principal.workspace_id, agent, mode, self.principal, target_id=target_id
        )
        self._emit("rule.check", {"agent": agent, "mode": mode})
        return {"checks": self.ctx.db.rule_checks(self.principal.workspace_id)}

    def accept_store(self):
        self._person(owner=True)
        result = checks.migrate_workspace(
            self.ctx.db, self.principal.workspace_id, self.ctx.workspace_dir(), accept=True
        )
        self._store = None
        return result

    def count(self):
        self._person()
        with self.store.read() as conn:
            return {
                "waiting": conn.execute(f"SELECT count(*) FROM rules WHERE {WAITING}").fetchone()[
                    0
                ],
                "seq": sequence(conn),
            }

    def history(self, rule_id):
        self._person()
        with self.store.connection() as conn:
            row = row_for(conn, rule_id)
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM rule_versions WHERE rule_id=? ORDER BY id", (row["id"],)
                )
            ]

    def create(self, agent, text, kind, scope="global", scope_ref=None, summary=None):
        self._person(owner=True)
        agent = agent_ref(agent)
        values = fields(text, kind, scope, scope_ref, summary)
        with self.transaction() as conn:
            key = rule_key(text)
            if conn.execute("SELECT 1 FROM rules WHERE agent=? AND key=?", (agent, key)).fetchone():
                raise ToolError("no: rule key already exists")
            return self._create(conn, agent, key, values, owner=True)

    def _versioned(self, conn, rule_id, version):
        row = row_for(conn, rule_id)
        if type(version) is not int:
            raise ToolError("no: version must be an integer")
        if row["version"] != version:
            raise VersionClash(rule_json(row))
        return row

    def edit(self, rule_id, version, **changes):
        self._person(owner=True)
        if set(changes) - {"text", "summary", "kind", "scope", "scope_ref", "note"}:
            raise ToolError("no: invalid edit")
        note = changes.pop("note", None)
        if note is not None:
            validate_text(note, "note")
        with self.transaction() as conn:
            row = self._versioned(conn, rule_id, version)
            if row["status"] == "retired":
                raise ToolError("no: restore the rule first")
            values = fields(
                **{
                    k: changes.get(k, row[k])
                    for k in ("text", "summary", "kind", "scope", "scope_ref")
                }
            )
            if all(row[k] == value for k, value in values.items()):
                raise ToolError("no: nothing to change")
            updates = dict(values, status="edited", source="owner", pending=0, retired_by=None)
            if all(row[k] == value for k, value in values.items() if k != "summary"):
                updates = {"summary": values["summary"]}
            return self._change(
                conn,
                row,
                updates,
                note=note,
            )

    def set_agent(self, rule_id, version, agent, note=None, accept=False):
        """Move a rule to another agent target; `accept` also accepts a proposal."""
        self._person(owner=True)
        agent = agent_ref(agent)
        if note is not None:
            validate_text(note, "note")
        if type(accept) is not bool:
            raise ToolError("no: accept must be a boolean")
        with self.transaction() as conn:
            row = self._versioned(conn, rule_id, version)
            if row["status"] == "retired":
                raise ToolError("no: restore the rule first")
            if accept and row["status"] != "proposed":
                raise ToolError("no: invalid action")
            if row["agent"] == agent:
                raise ToolError("no: nothing to change")
            if conn.execute(
                "SELECT 1 FROM rules WHERE agent=? AND key=?", (agent, row["key"])
            ).fetchone():
                raise ToolError(f"no: {agent_name(agent)} already has a rule with this key")
            self._moved.append({"agent": row["agent"], "id": row["id"], "key": row["key"]})
            audit = f"Applies to: {agent_name(row['agent'])} → {agent_name(agent)}"
            updates = {"agent": agent}
            if accept:
                updates["status"] = "accepted"
            if row["origin_agent"] is None and agent != "*":
                # A rule written for all agents first answers to the agent it moves to.
                updates["origin_agent"] = agent
            return self._change(
                conn,
                row,
                updates,
                note=f"{audit}\n{note}" if note and note.strip() else audit,
            )

    def action(self, rule_id, version, action):
        self._person(owner=True)
        with self.transaction() as conn:
            row = self._versioned(conn, rule_id, version)
            if action == "accept" and row["status"] == "proposed":
                updates = {"status": "accepted"}
            elif action == "retire" and row["status"] != "retired":
                updates = {"status": "retired", "retired_by": "owner"}
            elif action == "restore" and row["status"] == "retired":
                updates = {
                    "status": "edited" if row["source"] == "owner" else "accepted",
                    "retired_by": None,
                }
                if row["retired_by"] == "agent":
                    updates.update(status="accepted", source="owner")
            elif action == "accept_agent" and row["pending"] == 1 and row["agent_text"] is not None:
                updates = dict(
                    text=row["agent_text"],
                    source="agent",
                    status="accepted",
                    pending=0,
                    retired_by=None,
                )
            elif action == "keep_edit" and row["pending"] == 1:
                updates = {"pending": 0}
            else:
                raise ToolError("no: invalid action")
            return self._change(conn, row, updates)

    def suggest(self, rule_id, text, from_agent):
        self._person(owner=True)
        rule_text(text)
        validate_text(from_agent, "from_agent", required=True, limit=1000)
        with self.transaction() as conn:
            row = row_for(conn, rule_id)
            cursor = conn.execute(
                "INSERT INTO rule_suggestions(rule_id,text,from_agent,base_version,status,stamp_user,created_at) VALUES (?,?,?,?,'open',?,?)",
                (row["id"], text, from_agent, row["version"], self.principal.user_id, now()),
            )
            result = dict(
                conn.execute(
                    "SELECT * FROM rule_suggestions WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
            )
        self._emit(
            "rule.suggestion",
            {
                "rule_id": row["id"],
                "suggestion_id": result["id"],
                "action": "stored",
            },
        )
        return result

    def decide_suggestion(self, suggestion_id, action, version=None):
        self._person(owner=True)
        if action not in ("accept", "dismiss"):
            raise ToolError("no: invalid action")
        if type(suggestion_id) not in (str, int) or not str(suggestion_id).isdigit():
            raise ToolError("no: suggestion not found")
        with self.transaction() as conn:
            suggestion = conn.execute(
                "SELECT * FROM rule_suggestions WHERE id=?", (suggestion_id,)
            ).fetchone()
            if suggestion is None:
                raise ToolError("no: suggestion not found")
            if suggestion["status"] != "open":
                raise ToolError("no: suggestion already decided")
            row = row_for(conn, suggestion["rule_id"])
            if action == "accept":
                row = self._versioned(conn, row["id"], version)
                if row["version"] != suggestion["base_version"]:
                    raise VersionClash(rule_json(row))
                if row["status"] == "retired":
                    raise ToolError("no: cannot accept a suggestion on a retired rule")
            updates = (
                dict(text=suggestion["text"], status="edited", source="owner", pending=0)
                if action == "accept"
                else {}
            )
            conn.execute(
                "UPDATE rule_suggestions SET status=?,decided_at=? WHERE id=?",
                ("accepted" if action == "accept" else "dismissed", now(), suggestion_id),
            )
            result = (
                self._change(
                    conn,
                    row,
                    updates,
                    note=suggestion["text"],
                    source="suggestion" if action == "accept" else None,
                )
                if action == "accept"
                else rule_json(row)
            )
        if action == "dismiss":
            self._emit(
                "rule.suggestion",
                {
                    "rule_id": row["id"],
                    "suggestion_id": suggestion["id"],
                    "action": "dismissed",
                },
            )
        return result


def instructions_for(ctx, *, budget=render.INSTRUCTIONS_BUDGET):
    """Fail open without binding, serving, creating stores or emitting events."""
    try:
        if (
            ctx.principal.person
            or budget <= 0
            or getattr(ctx.gateway, "rules_inject_off", False)
            or not ctx.principal.policy.tool_visible("rules_get")
        ):
            return ""
        with checks.deadline(INSTRUCTIONS_TIMEOUT):
            try:
                snapshot = effective_rules(
                    ctx, bind=False, emit=False, timeout=checks.remaining(INSTRUCTIONS_TIMEOUT)
                )
            except checks.StoreLost:
                text = render.instructions([], ctx.principal.actor, store_lost=True)
                return text if render.length(text) <= budget else ""
            if not snapshot["rules"]:
                return ""
            text = render.instructions(
                snapshot["rules"],
                ctx.principal.actor,
                snapshot["rules_seq"],
                served=snapshot["served"],
                mode=checks.mode(ctx.gateway, ctx.principal),
                budget=budget,
                check_deadline=lambda: checks.remaining(INSTRUCTIONS_TIMEOUT),
            )
            return text if render.length(text) <= budget else ""
    except Exception as exc:
        log_failure(logger, "rules instruction injection failed", exc)
        return ""


def effective_rules(ctx, *, bind=False, emit=False, timeout=0.1):
    """Read an agent's complete authorized set on independent read-only snapshots.

    Only reconciliation writes. This function never constructs RuleService or
    creates a store and never records served; injection can use it directly.
    """
    if ctx.principal.person:
        raise ToolError("no: effective=true is for agent connections")
    directory = Path(ctx.workspace_dir())
    checks.reconcile(
        ctx.db, ctx.principal.workspace_id, directory, timeout=timeout, bind=bind, emit=emit
    )
    if not (directory / "rules.sqlite3").exists():
        return {"rules": [], "seq": 0, "rules_seq": None, "served": None, "empty": True}
    visible_boards, linked_repos, visible_repos = set(), set(), set()
    if (directory / "boards.sqlite3").exists():
        with checks.connection(directory / "boards.sqlite3", timeout=timeout) as conn:
            conn.execute("BEGIN")
            for board in conn.execute("SELECT slug,wing,archived FROM boards"):
                checks.remaining(timeout)
                if not board["archived"] and ctx.principal.policy.can_read_wing(board["wing"]):
                    visible_boards.add(board["slug"])
            for repo in conn.execute(
                "SELECT r.repo,b.slug FROM board_repos r JOIN boards b ON b.id=r.board_id"
            ):
                checks.remaining(timeout)
                linked_repos.add(repo["repo"])
                if repo["slug"] in visible_boards:
                    visible_repos.add(repo["repo"])
    with checks.connection(directory / "rules.sqlite3", timeout=timeout) as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT * FROM rules WHERE agent IN (?, '*') AND status IN ('accepted','edited') ORDER BY id",
            (ctx.principal.actor,),
        ).fetchall()
        selected = []
        for row in rows:
            checks.remaining(timeout)
            visible = row["scope"] == "global" or (
                row["scope"] == "board" and row["scope_ref"] in visible_boards
            )
            if row["scope"] == "repo":
                visible = row["scope_ref"] in visible_repos or (
                    ctx.principal.policy.trust == "trusted" and row["scope_ref"] not in linked_repos
                )
            if visible:
                # The enforcement view contains only the owner's effective text.
                value = rule_json(row, include_display=False)
                value.update(agent_text=None, agent_text_by=None, pending=0)
                if (
                    str(value["updated_by"]).startswith("conn:")
                    and value["updated_by"] != ctx.principal.actor
                ):
                    value["updated_by"] = None
                selected.append(value)
        served = conn.execute(
            "SELECT * FROM agent_served WHERE agent=?", (ctx.principal.actor,)
        ).fetchone()
        return {
            "rules": selected,
            "seq": sequence(conn),
            "rules_seq": rules_sequence(conn, ctx.principal.actor),
            "served": dict(served) if served is not None else None,
            "empty": False,
        }
